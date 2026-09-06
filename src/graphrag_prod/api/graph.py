"""Reader API facade over governed graph views and current industrial sources."""

from __future__ import annotations

from dataclasses import asdict
import time
from typing import Any, Callable

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.browse_models import GraphBrowseLimitExceeded, GraphViewChanged
from graphrag_prod.retrieval.models import VersionFilter

from .graph_contracts import (
    GraphBrowseRequest, GraphBrowseResponse, GraphEvidenceRequest, GraphEvidenceResponseEnvelope,
    IndustrialSourcesRequest, IndustrialSourcesResponse, IndustrialSourceChunkRequest, IndustrialSourceChunkEnvelope,
)
from .runtime import (
    ApiRuntimeError, AuthorizationError, BackendResult, ConflictError,
    DependencyTimeoutError, DependencyUnavailableError, GraphViewChangedError, UsageMetadata,
)


class Neo4jGraphOperations:
    def __init__(self, *, browser: Any, industrial_scope_resolver: Any | None = None, source_catalog: Any | None = None) -> None:
        if any(not callable(getattr(browser, name, None)) for name in ("query", "evidence")):
            raise TypeError("browser must implement query and evidence")
        if industrial_scope_resolver is not None and not callable(getattr(industrial_scope_resolver, "resolve", None)):
            raise TypeError("industrial_scope_resolver must implement resolve")
        if source_catalog is not None and any(not callable(getattr(source_catalog, name, None)) for name in ("list", "chunk")):
            raise TypeError("source_catalog must implement list and chunk")
        self.browser, self.resolver, self.sources = browser, industrial_scope_resolver, source_catalog

    @staticmethod
    def _require(principal: Principal, capability: str) -> None:
        if capability not in principal.capabilities:
            raise AuthorizationError()

    @staticmethod
    def _call(model: Any, operation: Callable[[], Any]) -> BackendResult:
        started = time.monotonic()
        try:
            value = model.model_validate(operation())
        except ApiRuntimeError:
            raise
        except PermissionError as error:
            raise AuthorizationError() from error
        except GraphViewChanged as error:
            raise GraphViewChangedError() from error
        except GraphBrowseLimitExceeded as error:
            raise ConflictError("the graph exceeds its read budget; narrow the source scope") from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult(value, UsageMetadata(total_ms=(time.monotonic() - started) * 1000))

    def query(self, principal: Principal, request: GraphBrowseRequest) -> BackendResult:
        self._require(principal, "knowledge:graph:read")

        def execute() -> Any:
            original = request.version_filter.to_domain()
            selected = original
            if request.industrial_scope is not None:
                if self.resolver is None:
                    raise DependencyUnavailableError()
                selected = self.resolver.resolve(principal, request.industrial_scope.to_domain(), version_filter=original).version_filter
                if not isinstance(selected, VersionFilter) or (original.match_none and not selected.match_none) or selected.published_at_or_before != original.published_at_or_before or len(selected.version_ids) > 100 or (not selected.match_none and not selected.version_ids) or (original.version_ids and not selected.version_ids <= original.version_ids):
                    raise DependencyUnavailableError()
            return self.browser.query(principal, request.to_domain(), version_filter=selected, view_token=request.view_token, cursor=request.cursor)

        return self._call(GraphBrowseResponse, execute)

    def evidence(self, principal: Principal, request: GraphEvidenceRequest) -> BackendResult:
        self._require(principal, "knowledge:graph:read")
        return self._call(GraphEvidenceResponseEnvelope, lambda: self.browser.evidence(principal, request.revision_ids, view_token=request.view_token))

    def source_list(self, principal: Principal, request: IndustrialSourcesRequest) -> BackendResult:
        self._require(principal, "retrieval:read")
        if self.sources is None:
            raise DependencyUnavailableError()
        return self._call(IndustrialSourcesResponse, lambda: asdict(self.sources.list(principal, family=request.family, asset_keys=request.asset_keys, limit=request.limit)))

    def source_chunk(self, principal: Principal, request: IndustrialSourceChunkRequest) -> BackendResult:
        self._require(principal, "retrieval:read")
        if self.sources is None:
            raise DependencyUnavailableError()

        def execute() -> dict[str, Any]:
            value = self.sources.chunk(principal, chunk_id=request.chunk_id)
            return {"chunk": None if value is None else asdict(value)}

        return self._call(IndustrialSourceChunkEnvelope, execute)
