"""Explicit local-demo reset control, separate from production API permissions.

The HTTP admission registry and real backend worker count deliberately have
different lifetimes. A disconnected or timed-out HTTP request can leave work
running (or queued) in the production runner's thread pool. A reset cannot
begin until started workers finish, and a queued worker whose HTTP admission
has ended cannot enter the backend later and recreate old data.
"""

from __future__ import annotations

from collections.abc import Callable
import ipaddress
import json
import secrets
import threading
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from graphrag_prod.api.runtime import (
    Backend, BackendResult, ConflictError, DependencyUnavailableError,
    OperationEnvelope,
)


_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UNAVAILABLE = frozenset({"RUNNING", "FAILED"})
_MAX_ACCEPTED_RESETS = 1_024


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confirmation: Literal["RESET_ALL"]
    expected_generation: str = Field(pattern=_UUID_PATTERN, min_length=36, max_length=36)
    operation_id: str = Field(pattern=_UUID_PATTERN, min_length=36, max_length=36)


class ResetControlError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _error_response(error: ResetControlError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"error": {"code": error.code, "message": error.message}},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _single_header(scope: Scope, name: bytes) -> str | None:
    values = [
        value.decode("latin-1")
        for key, value in scope.get("headers", ()) if key.lower() == name
    ]
    return values[0] if len(values) == 1 else None


def _local_origin(scope: Scope) -> tuple[str, str, int] | None:
    host = _single_header(scope, b"host")
    scheme = scope.get("scheme", "http")
    if not host or scheme not in {"http", "https"}:
        return None
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        hostname = parsed.hostname
        if (
            not hostname or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or any(character.isspace() for character in host)
        ):
            return None
        if hostname != "localhost" and not ipaddress.ip_address(hostname).is_loopback:
            return None
        return scheme, hostname, parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None


def _authorize_control(scope: Scope, token: str, *, mutation: bool) -> None:
    origin = _local_origin(scope)
    supplied = _single_header(scope, b"x-playground-reset-token")
    allowed = bool(
        origin and supplied and supplied.isascii() and secrets.compare_digest(supplied, token)
    )
    if mutation:
        raw_origin = _single_header(scope, b"origin")
        try:
            parsed = urlsplit(raw_origin or "")
            expected = (
                parsed.scheme, parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            allowed = bool(
                allowed and raw_origin and not any(c.isspace() for c in raw_origin)
                and expected == origin
                and parsed.username is None and parsed.password is None
                and not parsed.path and not parsed.query and not parsed.fragment
            )
        except ValueError:
            allowed = False
    if not allowed:
        raise ResetControlError(
            "PLAYGROUND_RESET_FORBIDDEN", 403,
            "请从当前本地服务页面执行重置，并刷新页面获取有效控制凭据。",
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class PlaygroundResetController:
    """One process-local control for one explicitly disposable database.

    Construction does not call ``reset_callback``. The application builder
    owns disposable-database verification and supplies a prepared callback.
    Only an explicit, authenticated same-origin POST starts the daemon worker.
    """

    def __init__(
        self, reset_callback: Callable[[], None], *, control_token: str | None = None,
    ) -> None:
        if not callable(reset_callback):
            raise TypeError("reset_callback must be callable")
        if control_token is not None and (
            not isinstance(control_token, str) or len(control_token) < 32
            or not control_token.isascii()
        ):
            raise ValueError("control_token must contain at least 32 ASCII characters")
        self._callback = reset_callback
        self._token = control_token or secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        # Opaque per-process generations also fence stale tabs after a reload.
        self._generation = str(uuid4())
        self._state = "READY"
        self._reset_id: str | None = None
        self._error_code: str | None = None
        self._requests: dict[str, str] = {}
        self._workers = 0
        self._accepted: dict[str, tuple[str, dict[str, Any]]] = {}

    def _status_locked(self) -> dict[str, Any]:
        return {
            "enabled": True, "generation": self._generation,
            "state": self._state, "reset_id": self._reset_id,
            "error_code": self._error_code,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def bootstrap(self) -> dict[str, Any]:
        return {**self.status(), "control_token": self._token}

    def _require_ready_locked(self) -> None:
        if self._state in _UNAVAILABLE:
            failed = self._state == "FAILED"
            raise ResetControlError(
                "PLAYGROUND_RESET_FAILED" if failed else "PLAYGROUND_RESET_RUNNING",
                503,
                "重置未完成，请在重置面板重试。" if failed else "正在重新建立初始演示环境，请稍候。",
            )

    def admit(self, *, write: bool, generation: str | None) -> str:
        with self._lock:
            self._require_ready_locked()
            if write and generation != self._generation:
                raise ResetControlError(
                    "PLAYGROUND_RESET_STALE", 409,
                    "页面属于之前的演示环境，请刷新页面后再操作。",
                )
            request_id = uuid4().hex
            self._requests[request_id] = self._generation
            return request_id

    def release(self, request_id: str) -> None:
        with self._lock:
            self._requests.pop(request_id, None)

    def execute(self, backend: Backend, envelope: OperationEnvelope) -> BackendResult:
        with self._lock:
            if self._state in _UNAVAILABLE:
                raise DependencyUnavailableError()
            if self._requests.get(envelope.request_id) != self._generation:
                raise ConflictError()
            self._workers += 1
        try:
            return backend.execute(envelope)
        finally:
            with self._lock:
                self._workers -= 1

    def wrap_backend(self, backend: Backend) -> Backend:
        if not isinstance(backend, Backend):
            raise TypeError("backend must implement execute(envelope)")
        return _ResetAwareBackend(self, backend)

    def start(self, request: ResetRequest) -> dict[str, Any]:
        if not isinstance(request, ResetRequest):
            raise TypeError("request must be ResetRequest")
        with self._lock:
            previous = self._accepted.get(request.operation_id)
            if previous is not None:
                generation, result = previous
                if generation != request.expected_generation:
                    raise ResetControlError(
                        "PLAYGROUND_RESET_STALE", 409,
                        "该重置操作编号已经用于其他版本，请刷新页面。",
                    )
                return dict(result)
            if request.expected_generation != self._generation:
                raise ResetControlError(
                    "PLAYGROUND_RESET_STALE", 409, "环境已经变化，请刷新后重新确认。",
                )
            if self._state == "RUNNING" or self._requests or self._workers:
                raise ResetControlError(
                    "PLAYGROUND_RESET_BUSY", 409,
                    "当前仍有请求或后台任务执行，请待其完成后再重新开始。",
                )
            if len(self._accepted) >= _MAX_ACCEPTED_RESETS:
                raise ResetControlError(
                    "PLAYGROUND_RESET_LIMIT", 429,
                    "本次服务的重置次数已达上限，请重新启动本地服务。",
                )
            self._generation = str(uuid4())
            self._state = "RUNNING"
            self._reset_id = request.operation_id
            self._error_code = None
            result = self._status_locked()
            self._accepted[request.operation_id] = (
                request.expected_generation, dict(result),
            )
            worker = threading.Thread(
                target=self._reset, args=(request.operation_id,),
                name="playground-local-reset", daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self._finish_locked(request.operation_id, failed=True)
                return self._status_locked()
            return result

    def _finish_locked(self, operation_id: str, *, failed: bool) -> None:
        self._state = "FAILED" if failed else "SUCCEEDED"
        self._error_code = "PLAYGROUND_RESET_FAILED" if failed else None
        previous_generation, _ = self._accepted[operation_id]
        self._accepted[operation_id] = (previous_generation, self._status_locked())

    def _reset(self, operation_id: str) -> None:
        failed = False
        try:
            self._callback()
        except BaseException:
            # Driver/provider messages can contain credentials or source text.
            # They must never become a public status or reopen the API gate.
            failed = True
        finally:
            with self._lock:
                self._finish_locked(operation_id, failed=failed)

    def install_middleware(self, app: FastAPI) -> None:
        app.add_middleware(PlaygroundResetMiddleware, controller=self)

    def attach_routes(self, app: FastAPI) -> None:
        @app.get("/playground/reset", include_in_schema=False)
        async def get_reset(request: Request) -> JSONResponse:
            try:
                _authorize_control(request.scope, self._token, mutation=False)
                return JSONResponse(self.status(), headers={"Cache-Control": "no-store"})
            except ResetControlError as error:
                return _error_response(error)

        @app.post("/playground/reset", include_in_schema=False)
        async def post_reset(request: Request) -> JSONResponse:
            try:
                _authorize_control(request.scope, self._token, mutation=True)
                content_type = request.headers.get("content-type", "").split(";", 1)[0]
                body = await request.body()
                if content_type != "application/json" or len(body) > 2_048:
                    raise ValueError("invalid reset content")
                payload = json.loads(body, object_pairs_hook=_strict_object)
                command = ResetRequest.model_validate(payload)
                return JSONResponse(
                    self.start(command), status_code=202,
                    headers={"Cache-Control": "no-store"},
                )
            except ResetControlError as error:
                return _error_response(error)
            except (ValueError, ValidationError):
                return _error_response(ResetControlError(
                    "PLAYGROUND_RESET_INVALID_REQUEST", 422,
                    "请明确确认全部重新开始，并提交当前环境版本与唯一操作编号。",
                ))


class _ResetAwareBackend:
    def __init__(self, controller: PlaygroundResetController, backend: Backend) -> None:
        self.controller = controller
        self.backend = backend

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        return self.controller.execute(self.backend, envelope)


class PlaygroundResetMiddleware:
    """Fence all API requests plus readiness before any body/worker admission."""

    def __init__(self, app: ASGIApp, *, controller: PlaygroundResetController) -> None:
        self.app = app
        self.controller = controller

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
            path.startswith("/v1/") or path == "/health/ready"
        ):
            await self.app(scope, receive, send)
            return
        try:
            request_id = self.controller.admit(
                write=scope.get("method") not in {"GET", "HEAD", "OPTIONS"},
                generation=_single_header(scope, b"x-playground-generation"),
            )
        except ResetControlError as error:
            await _error_response(error)(scope, receive, send)
            return
        # The API's request boundary consumes this server-owned unique ID.
        # A client-provided or reused ID cannot register another worker under
        # an old request's lease, even across reset generations.
        scope = dict(scope)
        scope["headers"] = [
            (key, value) for key, value in scope.get("headers", ())
            if key.lower() != b"x-request-id"
        ] + [(b"x-request-id", request_id.encode("ascii"))]
        try:
            await self.app(scope, receive, send)
        finally:
            self.controller.release(request_id)
