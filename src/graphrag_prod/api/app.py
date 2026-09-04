"""FastAPI boundary for secure, bounded GraphRAG operations."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import inspect
import math
import re
import threading
import time
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError as FastAPIValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from graphrag_prod.observability.logging import StructuredJsonLogger
from graphrag_prod.observability.metrics import MetricsRegistry

from .auth import AuthenticatedIdentity
from .auth import AuthenticationError as JWTAuthenticationError
from .auth import JWTAuthenticator, extract_bearer_token
from .contracts import (
    AnswerRequest,
    AnswerResponse,
    DeleteRequest,
    DeleteResponse,
    ErrorResponse,
    HealthResponse,
    IngestionRequest,
    IngestionResponse,
    JobResponse,
    MAX_DOCUMENT_BYTES,
    MetricsResponse,
    ReadinessResponse,
    RetrievalRequest,
    RetrievalResponse,
)
from .knowledge_contracts import (
    AuthoritativeImportRequest,
    AuthoritativeImportResponse,
    ConstructionJobListResponse,
    ConstructionJobResponse,
    DocumentLifecycleListResponse,
    DocumentRetirementRequest,
    DocumentRetirementResponse,
    EntityResolutionApplyRequest,
    EntityResolutionApplyResponse,
    EntityResolutionResponse,
    KnowledgeConstructionRequest,
    KnowledgeConstructionResponse,
    MAX_BASE64_DOCUMENT_CHARS,
    OntologyImportRequest,
    OntologyListResponse,
    OntologyPublishRequest,
    OntologyVersionResponse,
    PublicationHistoryResponse,
    PublicationCandidatesResponse,
    PublicationRequest,
    PublicationResponse,
    PublishedGraphQualityResponse,
    ReviewBatchRequest,
    ReviewBatchResponse,
    ReviewQueueResponse,
    RecordRevisionHistoryResponse,
    RollbackRequest,
)
from .runtime import (
    ApiRuntimeError,
    AuthenticationError,
    AuthorizationError,
    Backend,
    BoundedOperationRunner,
    ErrorCode,
    OperationEnvelope,
    OperationKind,
    PrincipalRateLimiter,
    RateLimitPolicy,
    RuntimePolicy,
    classify_exception,
    required_scope,
)


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PATH_ID = r"^[^\x00-\x20\x7f]+$"
_MAX_JSON_OVERHEAD_BYTES = 64 * 1024
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class APISettings:
    service_name: str = "graphrag-prod"
    version: str = "0.1.0"
    metrics_tenant_id: str = "system"
    metrics_group: str = "system-observer"
    max_request_body_bytes: int = (
        MAX_BASE64_DOCUMENT_CHARS + _MAX_JSON_OVERHEAD_BYTES
    )
    max_concurrent_body_buffers: int = 16
    body_receive_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 5.0
    expose_openapi: bool = False

    def __post_init__(self) -> None:
        for name, maximum in (
            ("service_name", 128),
            ("version", 64),
            ("metrics_tenant_id", 256),
            ("metrics_group", 128),
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be text")
            normalized = value.strip()
            if (
                not normalized
                or len(normalized) > maximum
                or any(character in normalized for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError(f"{name} is invalid")
            object.__setattr__(self, name, normalized)
        if (
            isinstance(self.max_request_body_bytes, bool)
            or not isinstance(self.max_request_body_bytes, int)
            or self.max_request_body_bytes < MAX_DOCUMENT_BYTES
            or self.max_request_body_bytes > 8 * 1024 * 1024
        ):
            raise ValueError(
                "max_request_body_bytes must be between 5 MiB and 8 MiB"
            )
        if (
            isinstance(self.max_concurrent_body_buffers, bool)
            or not isinstance(self.max_concurrent_body_buffers, int)
            or not 1 <= self.max_concurrent_body_buffers <= 1_024
        ):
            raise ValueError("max_concurrent_body_buffers must be between 1 and 1024")
        for name in ("body_receive_timeout_seconds", "shutdown_timeout_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.1 <= float(value) <= 300.0
            ):
                raise ValueError(f"{name} must be between 0.1 and 300 seconds")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.expose_openapi, bool):
            raise TypeError("expose_openapi must be boolean")


class BoundedRequestBodyMiddleware:
    """Buffer one JSON request up to a fixed limit, including chunked bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_bytes: int,
        maximum_concurrent_bodies: int,
        receive_timeout_seconds: float,
    ) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes
        self.receive_timeout_seconds = receive_timeout_seconds
        self._capacity = threading.BoundedSemaphore(maximum_concurrent_bodies)

    @staticmethod
    def _content_lengths(scope: Scope) -> tuple[bytes, ...]:
        return tuple(
            value
            for key, value in scope.get("headers", ())
            if key.lower() == b"content-length"
        )

    @staticmethod
    async def _reject(
        scope: Scope,
        send: Send,
        status_code: int,
        *,
        code: ErrorCode = ErrorCode.INVALID_REQUEST,
        message: str = "the request is invalid",
    ) -> None:
        state = scope.setdefault("state", {})
        request_id = str(state.get("request_id") or uuid4().hex)
        trace_id = str(state.get("trace_id") or uuid4().hex)
        state["error_code"] = code.value
        body = ErrorResponse(
            code=code.value,
            message=message,
            request_id=request_id,
        ).model_dump_json().encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-request-id", request_id.encode("ascii")),
            (b"x-trace-id", trace_id.encode("ascii")),
        ]
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        if not self._capacity.acquire(blocking=False):
            await self._reject(
                scope,
                send,
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code=ErrorCode.OVERLOADED,
                message="the service is at its bounded concurrency limit",
            )
            return

        try:
            await self._buffer_and_dispatch(scope, receive, send)
        finally:
            self._capacity.release()

    async def _buffer_and_dispatch(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:

        lengths = self._content_lengths(scope)
        if len(lengths) > 1:
            await self._reject(scope, send, status.HTTP_400_BAD_REQUEST)
            return
        if lengths:
            try:
                declared = int(lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._reject(scope, send, status.HTTP_400_BAD_REQUEST)
                return
            if declared < 0:
                await self._reject(scope, send, status.HTTP_400_BAD_REQUEST)
                return
            if declared > self.maximum_bytes:
                await self._reject(scope, send, status.HTTP_413_CONTENT_TOO_LARGE)
                return

        buffered: list[Message] = []
        total = 0
        try:
            async with asyncio.timeout(self.receive_timeout_seconds):
                while True:
                    message = await receive()
                    buffered.append(message)
                    if message["type"] == "http.disconnect":
                        break
                    if message["type"] != "http.request":
                        continue
                    total += len(message.get("body", b""))
                    if total > self.maximum_bytes:
                        await self._reject(
                            scope,
                            send,
                            status.HTTP_413_CONTENT_TOO_LARGE,
                        )
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self._reject(scope, send, status.HTTP_408_REQUEST_TIMEOUT)
            return

        position = 0

        async def replay() -> Message:
            nonlocal position
            if position < len(buffered):
                message = buffered[position]
                position += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)


class EarlyAdmissionMiddleware:
    """Authenticate and rate-limit API calls before buffering their bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticator: JWTAuthenticator,
        limiter: PrincipalRateLimiter,
    ) -> None:
        self.app = app
        self.authenticator = authenticator
        self.limiter = limiter

    @staticmethod
    def _rate_limit_key(identity: AuthenticatedIdentity) -> str:
        principal = identity.principal
        return (
            f"{len(principal.tenant_id)}:{principal.tenant_id}"
            f"{len(principal.principal_id)}:{principal.principal_id}"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        try:
            values = [
                value.decode("latin-1")
                for key, value in scope.get("headers", ())
                if key.lower() == b"authorization"
            ]
            if len(values) != 1:
                raise JWTAuthenticationError("authentication failed")
            identity = self.authenticator.verify_identity(
                extract_bearer_token(values[0])
            )
        except (JWTAuthenticationError, UnicodeError) as error:
            raise AuthenticationError() from error
        state = scope.setdefault("state", {})
        state["authenticated_identity"] = identity
        state["rate_limit_decision"] = self.limiter.require(
            self._rate_limit_key(identity)
        )
        await self.app(scope, receive, send)


ShutdownCallback = Callable[[], Any | Awaitable[Any]]


def _best_effort(call: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Never let telemetry delivery change the business response."""
    try:
        call(*args, **kwargs)
    except Exception:
        pass


def _request_id(request: Request) -> str:
    values = request.headers.getlist("x-request-id")
    if len(values) == 1 and _REQUEST_ID.fullmatch(values[0]):
        return values[0]
    return uuid4().hex


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else "<unmatched>"


def _public_error_response(
    request: Request,
    error: BaseException,
    *,
    metrics: MetricsRegistry,
) -> JSONResponse:
    info = classify_exception(error)
    request.state.error_code = info.code.value
    _best_effort(metrics.record_error, info.code.value)
    request.state.error_recorded = True
    request_id = request.state.request_id
    headers: dict[str, str] = {}
    if info.status_code == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if info.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(1, math.ceil(info.retry_after_seconds)))
    return JSONResponse(
        status_code=info.status_code,
        content=ErrorResponse(
            code=info.code.value,
            message=info.public_message,
            request_id=request_id,
        ).model_dump(mode="json"),
        headers=headers,
    )


def create_app(
    *,
    authenticator: JWTAuthenticator,
    backend: Backend,
    settings: APISettings | None = None,
    runtime_policy: RuntimePolicy | None = None,
    readiness_policy: RuntimePolicy | None = None,
    rate_limit_policy: RateLimitPolicy | None = None,
    metrics: MetricsRegistry | None = None,
    logger: StructuredJsonLogger | None = None,
    shutdown_callbacks: tuple[ShutdownCallback, ...] = (),
) -> FastAPI:
    """Construct an app without reading credentials or opening resources."""

    if not isinstance(authenticator, JWTAuthenticator):
        raise TypeError("authenticator must be JWTAuthenticator")
    configuration = settings or APISettings()
    if not isinstance(configuration, APISettings):
        raise TypeError("settings must be APISettings")
    registry = metrics or MetricsRegistry()
    event_logger = logger or StructuredJsonLogger(service=configuration.service_name)
    runner = BoundedOperationRunner(backend, policy=runtime_policy)
    ready_runner = BoundedOperationRunner(
        backend,
        policy=readiness_policy
        or RuntimePolicy(
            max_workers=1,
            max_queue_size=1,
            timeout_seconds=2.0,
            max_attempts=1,
        ),
    )
    limiter = PrincipalRateLimiter(rate_limit_policy)
    in_flight = 0

    for callback in shutdown_callbacks:
        if not callable(callback):
            runner.close()
            ready_runner.close()
            limiter.close()
            raise TypeError("every shutdown callback must be callable")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Stop admission immediately.  Neo4j/provider calls have their own
        # server-side deadlines; resource callbacks then interrupt any
        # remaining I/O without allowing shutdown to wait forever.
        await runner.aclose(wait=False)
        await ready_runner.aclose(wait=False)
        limiter.close()
        for callback in shutdown_callbacks:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(callback),
                    timeout=configuration.shutdown_timeout_seconds,
                )
                if inspect.isawaitable(outcome):
                    await asyncio.wait_for(
                        outcome,
                        timeout=configuration.shutdown_timeout_seconds,
                    )
            except Exception:
                _best_effort(event_logger.error, "resource_shutdown_failed")

    app = FastAPI(
        title="Production GraphRAG API",
        version=configuration.version,
        lifespan=lifespan,
        docs_url="/docs" if configuration.expose_openapi else None,
        redoc_url="/redoc" if configuration.expose_openapi else None,
        openapi_url="/openapi.json" if configuration.expose_openapi else None,
    )
    app.state.metrics = registry
    app.state.runner = runner
    app.state.readiness_runner = ready_runner
    app.state.rate_limiter = limiter

    # Register the body limiter first.  The request-boundary decorator below
    # is inserted ahead of it and therefore remains the outer middleware,
    # observing and accounting for body-limit rejections as well.
    app.add_middleware(
        BoundedRequestBodyMiddleware,
        maximum_bytes=configuration.max_request_body_bytes,
        maximum_concurrent_bodies=configuration.max_concurrent_body_buffers,
        receive_timeout_seconds=configuration.body_receive_timeout_seconds,
    )
    app.add_middleware(
        EarlyAdmissionMiddleware,
        authenticator=authenticator,
        limiter=limiter,
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next: Any) -> Response:
        nonlocal in_flight
        request.state.request_id = _request_id(request)
        request.state.trace_id = uuid4().hex
        request.state.error_code = None
        request.state.error_recorded = False
        request.scope.setdefault("state", {})["request_id"] = request.state.request_id
        request.scope["state"]["trace_id"] = request.state.trace_id
        started = time.monotonic()
        in_flight += 1
        try:
            try:
                response = await call_next(request)
            except Exception as error:
                response = _public_error_response(request, error, metrics=registry)
            duration_ms = max(0.0, (time.monotonic() - started) * 1_000.0)
            route = _route_template(request)
            if (
                request.state.error_code is not None
                and not request.state.error_recorded
            ):
                _best_effort(registry.record_error, request.state.error_code)
                request.state.error_recorded = True
            _best_effort(
                registry.record_request,
                route,
                request.method,
                response.status_code,
                duration_ms,
            )
            log_fields = {
                "request_id": request.state.request_id,
                "trace_id": request.state.trace_id,
                "route": route,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": duration_ms,
            }
            if request.state.error_code is not None:
                log_fields["error_code"] = request.state.error_code
            if response.status_code >= 500:
                _best_effort(event_logger.error, "request_completed", **log_fields)
            elif response.status_code >= 400:
                _best_effort(event_logger.warning, "request_completed", **log_fields)
            else:
                _best_effort(event_logger.info, "request_completed", **log_fields)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Trace-ID"] = request.state.trace_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Cache-Control"] = "no-store"
            rate_decision = getattr(request.state, "rate_limit_decision", None)
            if rate_decision is not None:
                response.headers["X-RateLimit-Limit"] = str(rate_decision.limit)
                response.headers["X-RateLimit-Remaining"] = str(
                    rate_decision.remaining
                )
                response.headers["X-RateLimit-Reset"] = str(
                    max(0, math.ceil(rate_decision.reset_after_seconds))
                )
            return response
        finally:
            in_flight = max(0, in_flight - 1)

    @app.exception_handler(ApiRuntimeError)
    async def runtime_error_handler(
        request: Request, error: ApiRuntimeError
    ) -> JSONResponse:
        return _public_error_response(request, error, metrics=registry)

    @app.exception_handler(FastAPIValidationError)
    async def validation_error_handler(
        request: Request, _: FastAPIValidationError
    ) -> JSONResponse:
        from .runtime import RequestValidationError

        return _public_error_response(
            request,
            RequestValidationError(),
            metrics=registry,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        from .runtime import RequestValidationError, ResourceNotFoundError

        public_error: ApiRuntimeError
        if error.status_code == status.HTTP_404_NOT_FOUND:
            public_error = ResourceNotFoundError()
        else:
            public_error = RequestValidationError()
        return _public_error_response(request, public_error, metrics=registry)

    async def authenticated_principal(
        request: Request,
    ) -> AuthenticatedIdentity:
        identity = getattr(request.state, "authenticated_identity", None)
        if not isinstance(identity, AuthenticatedIdentity):
            raise AuthenticationError()
        return identity

    async def run_operation(
        request: Request,
        identity: AuthenticatedIdentity,
        operation: OperationKind,
        payload: Mapping[str, Any],
    ) -> Any:
        principal = identity.principal
        required = required_scope(operation)
        if required not in identity.scopes:
            raise AuthorizationError()
        result = await runner.run(
            OperationEnvelope(
                operation=operation,
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
                principal_id=principal.principal_id,
                tenant_id=principal.tenant_id,
                access_groups=principal.groups,
                scopes=identity.scopes,
                payload=payload,
            )
        )
        usage = result.usage
        for stage_name, duration_ms in usage.stages:
            try:
                registry.record_retrieval_stage(stage_name, duration_ms)
            except ValueError:
                _best_effort(event_logger.warning, "telemetry_rejected")
            except Exception:
                pass
        if usage.model_calls:
            try:
                registry.record_model_usage(
                    model_calls=usage.model_calls,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    estimated_cost_usd=usage.estimated_cost_usd,
                )
            except ValueError:
                _best_effort(event_logger.warning, "telemetry_rejected")
            except Exception:
                pass
        return result.payload

    IdentityDependency = Annotated[AuthenticatedIdentity, Depends(authenticated_principal)]
    DocumentPath = Annotated[
        str,
        Path(min_length=1, max_length=256, pattern=_PATH_ID),
    ]
    JobPath = Annotated[
        str,
        Path(min_length=1, max_length=256, pattern=_PATH_ID),
    ]
    OntologyPath = Annotated[
        str,
        Path(min_length=1, max_length=256, pattern=_PATH_ID),
    ]
    PublicationPath = Annotated[
        str,
        Path(min_length=1, max_length=256, pattern=_PATH_ID),
    ]
    KnowledgeRecordPath = Annotated[
        str,
        Path(min_length=1, max_length=256, pattern=_PATH_ID),
    ]

    @app.post(
        "/v1/documents:ingest",
        response_model=IngestionResponse,
        status_code=status.HTTP_200_OK,
    )
    async def ingest_document(
        request: Request,
        body: IngestionRequest,
        identity: IdentityDependency,
    ) -> Any:
        principal = identity.principal
        if not frozenset(body.access_groups).issubset(principal.groups):
            raise AuthorizationError()
        return await run_operation(
            request,
            identity,
            OperationKind.INGESTION,
            body.model_dump(mode="python"),
        )

    @app.delete(
        "/v1/documents/{document_id}",
        response_model=DeleteResponse,
        status_code=status.HTTP_200_OK,
    )
    async def delete_document(
        request: Request,
        document_id: DocumentPath,
        body: DeleteRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.DELETION,
            {
                "document_id": document_id,
                "request": body.model_dump(mode="python"),
            },
        )

    @app.get("/v1/jobs/{job_id}", response_model=JobResponse)
    async def get_job(
        request: Request,
        job_id: JobPath,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.JOB_STATUS,
            {"job_id": job_id},
        )

    @app.post("/v1/retrieval", response_model=RetrievalResponse)
    async def retrieve(
        request: Request,
        body: RetrievalRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.RETRIEVAL,
            body.model_dump(mode="python"),
        )

    @app.post("/v1/answers", response_model=AnswerResponse)
    async def answer(
        request: Request,
        body: AnswerRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ANSWER,
            body.model_dump(mode="python"),
        )

    @app.get("/v1/ontologies", response_model=OntologyListResponse)
    async def list_ontologies(
        request: Request,
        identity: IdentityDependency,
        key: Annotated[
            str | None,
            Query(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$"),
        ] = None,
        ontology_status: Annotated[
            str | None,
            Query(alias="status", pattern=r"^(DRAFT|PUBLISHED|RETIRED)$"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ONTOLOGY_LIST,
            {"key": key, "status": ontology_status, "limit": limit},
        )

    @app.post(
        "/v1/ontologies:import",
        response_model=OntologyVersionResponse,
        status_code=status.HTTP_200_OK,
    )
    async def import_ontology(
        request: Request,
        body: OntologyImportRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ONTOLOGY_IMPORT,
            body.model_dump(mode="python"),
        )

    @app.post(
        "/v1/ontologies/{tbox_id}:publish",
        response_model=OntologyVersionResponse,
    )
    async def publish_ontology(
        request: Request,
        tbox_id: OntologyPath,
        body: OntologyPublishRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ONTOLOGY_PUBLISH,
            {"tbox_id": tbox_id, "request": body.model_dump(mode="python")},
        )

    @app.post(
        "/v1/knowledge/authoritative:import",
        response_model=AuthoritativeImportResponse,
    )
    async def import_authoritative_knowledge(
        request: Request,
        body: AuthoritativeImportRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_IMPORT,
            body.model_dump(mode="python"),
        )

    @app.post(
        "/v1/knowledge:construct",
        response_model=KnowledgeConstructionResponse,
    )
    async def construct_knowledge(
        request: Request,
        body: KnowledgeConstructionRequest,
        identity: IdentityDependency,
    ) -> Any:
        if not frozenset(body.access_groups).issubset(identity.principal.groups):
            raise AuthorizationError()
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_CONSTRUCT,
            body.model_dump(mode="python"),
        )

    @app.get(
        "/v1/knowledge/construction-jobs/{job_id}",
        response_model=ConstructionJobResponse,
    )
    async def knowledge_construction_job(
        request: Request,
        job_id: JobPath,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOB,
            {"job_id": job_id},
        )

    @app.get(
        "/v1/knowledge/construction-jobs",
        response_model=ConstructionJobListResponse,
    )
    async def knowledge_construction_jobs(
        request: Request,
        identity: IdentityDependency,
        statuses: Annotated[
            list[Literal["RUNNING", "RETRY_WAIT", "COMPLETED"]] | None,
            Query(alias="status", min_length=1, max_length=3),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS,
            {"statuses": tuple(statuses or ()), "limit": limit},
        )

    @app.get(
        "/v1/knowledge/review-queue",
        response_model=ReviewQueueResponse,
    )
    async def knowledge_review_queue(
        request: Request,
        identity: IdentityDependency,
        statuses: Annotated[
            list[str] | None,
            Query(alias="status", min_length=1, max_length=2),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_REVIEW_QUEUE,
            {
                "statuses": tuple(statuses or ("CANDIDATE", "QUARANTINED")),
                "limit": limit,
            },
        )

    @app.get(
        "/v1/knowledge/records/{record_id}/revisions",
        response_model=RecordRevisionHistoryResponse,
    )
    async def knowledge_record_revisions(
        request: Request,
        record_id: KnowledgeRecordPath,
        identity: IdentityDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_REVISION_HISTORY,
            {"record_id": record_id, "request": {"limit": limit}},
        )

    @app.get(
        "/v1/knowledge/entity-resolution/{record_id}",
        response_model=EntityResolutionResponse,
    )
    async def entity_resolution_suggestions(
        request: Request,
        record_id: KnowledgeRecordPath,
        identity: IdentityDependency,
        expected_revision: Annotated[int, Query(ge=1, le=2_147_483_647)],
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ENTITY_RESOLUTION_SUGGEST,
            {
                "record_id": record_id,
                "expected_revision": expected_revision,
            },
        )

    @app.post(
        "/v1/knowledge/entity-resolution:apply",
        response_model=EntityResolutionApplyResponse,
    )
    async def apply_entity_resolution(
        request: Request,
        body: EntityResolutionApplyRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.ENTITY_RESOLUTION_APPLY,
            body.model_dump(mode="python"),
        )

    @app.post(
        "/v1/knowledge/reviews:batch",
        response_model=ReviewBatchResponse,
    )
    async def review_knowledge_batch(
        request: Request,
        body: ReviewBatchRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_REVIEW_BATCH,
            body.model_dump(mode="python"),
        )

    @app.post(
        "/v1/knowledge/publications:publish",
        response_model=PublicationResponse,
    )
    async def publish_knowledge(
        request: Request,
        body: PublicationRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_PUBLISH,
            body.model_dump(mode="python"),
        )

    @app.get(
        "/v1/knowledge/publication-candidates",
        response_model=PublicationCandidatesResponse,
    )
    async def knowledge_publication_candidates(
        request: Request,
        identity: IdentityDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_PUBLICATION_CANDIDATES,
            {"limit": limit},
        )

    @app.post(
        "/v1/knowledge/publications/{publication_id}:rollback",
        response_model=PublicationResponse,
    )
    async def rollback_knowledge(
        request: Request,
        publication_id: PublicationPath,
        body: RollbackRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_ROLLBACK,
            {
                "publication_id": publication_id,
                "request": body.model_dump(mode="python"),
            },
        )

    @app.get(
        "/v1/knowledge/quality",
        response_model=PublishedGraphQualityResponse,
    )
    async def published_graph_quality(
        request: Request,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_QUALITY,
            {},
        )

    @app.get(
        "/v1/knowledge/documents",
        response_model=DocumentLifecycleListResponse,
    )
    async def active_knowledge_documents(
        request: Request,
        identity: IdentityDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_DOCUMENTS,
            {"limit": limit},
        )

    @app.post(
        "/v1/knowledge/documents/{document_id}:retire",
        response_model=DocumentRetirementResponse,
    )
    async def retire_knowledge_document(
        request: Request,
        document_id: DocumentPath,
        body: DocumentRetirementRequest,
        identity: IdentityDependency,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_DOCUMENT_RETIRE,
            {
                "document_id": document_id,
                "request": body.model_dump(mode="python"),
            },
        )

    @app.get(
        "/v1/knowledge/publications",
        response_model=PublicationHistoryResponse,
    )
    async def knowledge_publication_history(
        request: Request,
        identity: IdentityDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> Any:
        return await run_operation(
            request,
            identity,
            OperationKind.KNOWLEDGE_HISTORY,
            {"limit": limit},
        )

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(
            service=configuration.service_name,
            version=configuration.version,
        )

    @app.get(
        "/health/ready",
        response_model=ReadinessResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
    )
    async def readiness(request: Request) -> Any:
        result = await ready_runner.run(
            OperationEnvelope(
                operation=OperationKind.READINESS,
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
                principal_id="service-health-probe",
                tenant_id="system",
                access_groups=frozenset({"system-health"}),
                scopes=frozenset({"health:probe"}),
                payload={},
            )
        )
        body = ReadinessResponse.model_validate(result.payload, from_attributes=True)
        if body.status == "not_ready":
            request.state.error_code = ErrorCode.DEPENDENCY_UNAVAILABLE.value
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=body.model_dump(mode="json"),
            )
        return body

    @app.get("/v1/metrics", response_model=MetricsResponse)
    async def operational_metrics(
        identity: IdentityDependency,
    ) -> MetricsResponse:
        principal = identity.principal
        if "metrics:read" not in identity.scopes:
            raise AuthorizationError()
        if (
            principal.tenant_id != configuration.metrics_tenant_id
            or configuration.metrics_group not in principal.groups
        ):
            raise AuthorizationError()
        snapshot = registry.snapshot()
        snapshot["generated_at"] = datetime.now(UTC)
        snapshot["in_flight"] = in_flight
        return MetricsResponse.model_validate(snapshot)

    return app


__all__ = [
    "APISettings",
    "BoundedRequestBodyMiddleware",
    "EarlyAdmissionMiddleware",
    "create_app",
]
