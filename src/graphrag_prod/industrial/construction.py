"""Server-owned industrial upload policy; uploads never acquire reference authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from graphrag_prod.domain import Principal
from .corpus import FAMILY_TO_CONTRACT
from .provenance import Neo4jIndustrialProvenanceStore, canonical_json
from .retrieval import IndustrialScope, Neo4jIndustrialScopeResolver

if TYPE_CHECKING:
    from graphrag_prod.construction.parser import BoundedDocumentParser

INDUSTRIAL_TENANT = "industrial-schneider-demo"
INDUSTRIAL_TBOX_KEY = "industrial-electric-v1"
UPLOAD_POLICY = "industrial-user-upload:v1"
INDUSTRIAL_RERANK_PROFILE = "listwise-evidence-v1"
INDUSTRIAL_UPLOAD_CHUNK_CHARS = 900


def industrial_upload_parser() -> BoundedDocumentParser:
    from graphrag_prod.construction.parser import BoundedDocumentParser, ChunkingConfig
    return BoundedDocumentParser(
        chunking=ChunkingConfig(max_chars=INDUSTRIAL_UPLOAD_CHUNK_CHARS)
    )


@dataclass(frozen=True, slots=True)
class IndustrialUploadContext:
    family: str
    asset_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.family not in FAMILY_TO_CONTRACT:
            raise ValueError("industrial upload family is unsupported")
        scope = IndustrialScope(family=self.family, asset_keys=self.asset_keys)
        object.__setattr__(self, "asset_keys", scope.asset_keys)

    def identity(self, principal: Principal) -> dict[str, Any]:
        return {"family": self.family, "asset_keys": list(self.asset_keys), "source_kind": "USER_UPLOAD", "policy_version": UPLOAD_POLICY,
                "principal_id": principal.principal_id}


class IndustrialUploadRejected(ValueError):
    pass


class IndustrialUploadUnavailable(PermissionError):
    pass


class IndustrialUploadBudgetExceeded(ValueError):
    """Exact rendered input exceeds the locked provider's byte limits."""

    def __init__(self) -> None:
        super().__init__(
            "industrial upload text or title exceeds the contextual reranker UTF-8 "
            "byte budget; shorten the title or split the file"
        )


class Neo4jIndustrialUploadPolicy:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.resolver = Neo4jIndustrialScopeResolver(driver, database)
        self.provenance = Neo4jIndustrialProvenanceStore(driver, database)

    def preflight(self, principal: Principal, metadata: Any) -> None:
        context = metadata.industrial_context
        if principal.tenant_id != INDUSTRIAL_TENANT:
            if context is not None:
                raise IndustrialUploadRejected("industrial context requires industrial tenant")
            return
        if not isinstance(context, IndustrialUploadContext) or metadata.tbox_key != INDUSTRIAL_TBOX_KEY:
            raise IndustrialUploadRejected("industrial uploads require the composed industrial ontology and context")
        if not metadata.access_groups <= frozenset({"public", "engineering", "maintenance"}):
            raise IndustrialUploadRejected("industrial upload groups are unsupported")
        uri = urlsplit(metadata.canonical_uri)
        if (uri.scheme != "industrial-upload" or uri.netloc != INDUSTRIAL_TENANT
                or not uri.path.strip("/") or uri.query or uri.fragment):
            raise IndustrialUploadRejected("industrial upload URI must use the reserved upload namespace")
        # Each intended reader group must be able to see the selected asset identity.
        # This avoids making a protected asset discoverable through a public upload facet.
        for group in sorted(metadata.access_groups):
            audience = Principal(principal.principal_id, principal.tenant_id, frozenset({group}), principal.capabilities)
            result = self.resolver.resolve(audience, IndustrialScope(
                family=context.family, asset_keys=context.asset_keys,
                include_references=False, source_kinds=("SYNTHETIC_FIELD_RECORD",),
            ))
            if context.asset_keys and result.version_filter.match_none:
                raise IndustrialUploadUnavailable("selected industrial scope is unavailable")

    def validate_parsed(self, metadata: Any, parsed: Any) -> None:
        """Use the actual locked renderer and transport budgets without a model call."""
        from graphrag_prod.domain.ids import content_checksum
        from graphrag_prod.retrieval.rerank_provider import rerank_cache_identity
        from graphrag_prod.retrieval.reranking import RerankCandidate, RerankingError
        try:
            candidates = tuple(RerankCandidate(
                chunk_id=f"upload-preflight-{seed.ordinal}", text=seed.text,
                checksum=content_checksum(seed.text), source_title=metadata.title, source_section=seed.section,
            ) for seed in parsed.chunks)
        except ValueError as error:
            from graphrag_prod.construction.parser import DocumentParseError
            raise DocumentParseError("industrial source contains an invalid evidence chunk") from error
        try:
            from .retrieval import build_rerank_scope_context
            query = "Industrial upload compatibility check"
            context = build_rerank_scope_context(metadata.industrial_context.asset_keys)
            if context:
                query += "\n" + context
            rerank_cache_identity(query, candidates, profile=INDUSTRIAL_RERANK_PROFILE)
        except RerankingError as error:
            raise IndustrialUploadBudgetExceeded() from error

    def persist(self, principal: Principal, metadata: Any, parsed: Any, job: Any) -> None:
        context = metadata.industrial_context
        if context is None:
            return
        # Revalidate after source publication, before extraction or successful completion.
        self.preflight(principal, metadata)
        payload = {
            "format_version": 1, "source_kind": "USER_UPLOAD",
            "source_key": "upload:" + job.document_id,
            "tenant_id": principal.tenant_id, "document_id": job.document_id,
            "version_id": job.version_id, "normalized_checksum": parsed.normalized_checksum,
            "original_checksum": parsed.original_checksum,
            "access_policy_id": job.access_policy_id, "access_policy_version": job.access_policy_version,
            "access_groups": sorted(job.access_groups), "family": context.family,
            "contract_family": FAMILY_TO_CONTRACT[context.family], "asset_keys": list(context.asset_keys),
            "source_origin": "USER_PROVIDED_NOT_VERIFIED", "policy_version": UPLOAD_POLICY,
            "construction_job_id": job.job_id, "construction_request_fingerprint": job.request_fingerprint,
            "construction_context": context.identity(principal),
            "source_locations": [asdict(item) for item in parsed.source_locations],
            "sections": [],
        }
        self.provenance.persist_user_upload(principal, payload, snapshot_id=job.snapshot_id,
                                            context_json=canonical_json(context.identity(principal)))
