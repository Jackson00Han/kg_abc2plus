"""Resumable industrial reference loading through existing governed lifecycles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4
from typing import Any, Callable, Mapping

from graphrag_prod.construction.parser import ParsedDocument, SourceLocation
from graphrag_prod.construction.pdf_parser import PdfDocumentParser, PdfParserLimits
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import chunk_embedding_id, content_checksum, entity_id, pipeline_profile_id
from graphrag_prod.domain.models import ChunkEmbedding, GraphPipelineProfile
from graphrag_prod.graph.schema import apply_schema
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
from graphrag_prod.ingestion.pipeline import (
    ChunkSeed, EmbeddingProfile, ExtractionOutput, IncrementalIngestionRequest,
    Neo4jIncrementalPipeline,
)
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch, AssertionRecord, EntityIdentity, EntityMentionRecord,
    EvidenceReference, RecordRevision, authoritative_import_trust, knowledge_record_id,
    knowledge_revision_id,
)
from graphrag_prod.knowledge.review import (
    Neo4jKnowledgePublicationService, Neo4jKnowledgeReviewService, ReviewRecordKind,
)
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus, KnowledgeOrigin, TrustMetadata
from graphrag_prod.ontology.store import Neo4jTBoxStore

from .ontology import build_industrial_tbox
from .corpus import FAMILY_TO_CONTRACT
from .provenance import Neo4jIndustrialProvenanceStore, canonical_json, digest
from .sources import SourceCatalog


INDUSTRIAL_TENANT = "industrial-schneider-demo"
INDUSTRIAL_GROUPS = frozenset({"public", "engineering", "maintenance"})
LOAD_PROFILE = "industrial-governed-bootstrap:v1"
MAX_DOCUMENTS = 100
MAX_CHUNKS = 1000
MAX_CHUNKS_PER_SOURCE = 64
MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_NORMALIZED_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PUBLICATION_RECORDS = 500
MAX_BATCH_RECORDS = 100
REVIEW_NOTE = (
    "Project validation of a versioned authored synthetic reference fixture; "
    "not a Schneider Electric approval, real field observation, or operational authorization. "
    "Rule-derived records remain SECONDARY."
)


class IndustrialLoadConflict(ValueError):
    """A reproducible loader input or recorded lifecycle precondition differs."""


class _LoaderLease:
    """One live bootstrap per tenant, with crash expiry and owner fencing."""

    def __init__(self, driver: Any, database: str, tenant: str) -> None:
        self.driver, self.database, self.tenant = driver, database, tenant
        self.owner = str(uuid4())
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> _LoaderLease:
        self.driver.execute_query(
            "CREATE CONSTRAINT industrial_loader_tenant_unique IF NOT EXISTS "
            "FOR (lease:IndustrialLoaderLease) REQUIRE lease.tenant_id IS UNIQUE", database_=self.database,
        )
        def acquire(tx: Any) -> None:
            now = datetime.now(UTC)
            row = tx.run(
                "MERGE (lease:IndustrialLoaderLease {tenant_id:$tenant}) "
                "SET lease.__acquisition_lock=randomUUID() WITH lease REMOVE lease.__acquisition_lock "
                "RETURN lease.owner AS owner,lease.expires_at AS expires_at",
                tenant=self.tenant,
            ).single()
            expires = row["expires_at"]
            expires = expires.to_native() if hasattr(expires, "to_native") else expires
            if row["owner"] is not None and expires is not None and expires > now:
                raise IndustrialLoadConflict("another industrial load is active for this tenant")
            tx.run(
                "MATCH (lease:IndustrialLoaderLease {tenant_id:$tenant}) "
                "SET lease.owner=$owner,lease.expires_at=$expires",
                tenant=self.tenant, owner=self.owner, expires=now + timedelta(seconds=180),
            ).consume()
        with self.driver.session(database=self.database) as session:
            session.execute_write(acquire)
        def renew() -> None:
            while not self.stop.wait(30):
                try:
                    self.refresh()
                except Exception:
                    self.lost.set()
                    return
        self.thread = threading.Thread(target=renew, name="industrial-loader-lease", daemon=True)
        self.thread.start()
        return self

    def refresh(self) -> None:
        if self.lost.is_set():
            raise IndustrialLoadConflict("industrial loader lease was lost")
        now = datetime.now(UTC)
        rows, _, _ = self.driver.execute_query(
            "MATCH (lease:IndustrialLoaderLease {tenant_id:$tenant,owner:$owner}) "
            "WHERE lease.expires_at > $now SET lease.expires_at=$expires RETURN lease.owner AS owner",
            tenant=self.tenant, owner=self.owner, now=now, expires=now + timedelta(seconds=180), database_=self.database,
        )
        if len(rows) != 1:
            self.lost.set()
            raise IndustrialLoadConflict("industrial loader lease was lost")

    def __exit__(self, *args: Any) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.driver.execute_query(
            "MATCH (lease:IndustrialLoaderLease {tenant_id:$tenant,owner:$owner}) "
            "REMOVE lease.owner,lease.expires_at",
            tenant=self.tenant, owner=self.owner, database_=self.database,
        )


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def industrial_loader_principal(tenant_id: str = INDUSTRIAL_TENANT) -> Principal:
    return Principal(
        "project-reference-validator:v1", tenant_id, INDUSTRIAL_GROUPS,
        frozenset({"knowledge:import", "knowledge:review", "knowledge:publish"}),
    )


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IndustrialLoadConflict("industrial source cache must contain regular files")
        payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise IndustrialLoadConflict("industrial source cache file exceeds byte limit")
    return payload


def read_normalized_artifact(path: Path, catalog: SourceCatalog, original_cache: Path) -> dict[str, Any]:
    payload = _bounded_regular_bytes(path, MAX_NORMALIZED_ARTIFACT_BYTES)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise IndustrialLoadConflict("normalized artifact contains duplicate keys")
            value[key] = item
        return value

    value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    validate_normalized_artifact(value, catalog, original_cache=original_cache)
    return value


def validate_normalized_artifact(
    value: Mapping[str, Any], catalog: SourceCatalog, *, original_cache: Path | None = None
) -> ParsedDocument:
    expected = {
        "schema_version", "source", "original_checksum", "normalized_checksum", "normalized_text",
        "parser_version", "splitter_signature", "selected_pages", "is_explicit_excerpt",
        "source_locations", "chunks", "limitations", "artifact_checksum",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise IndustrialLoadConflict("normalized artifact has missing or unexpected fields")
    if value["schema_version"] != "industrial-normalized-source-v1" or value["is_explicit_excerpt"] is not True:
        raise IndustrialLoadConflict("normalized artifact must declare its explicit excerpt schema")
    body = {key: item for key, item in value.items() if key != "artifact_checksum"}
    if digest(body) != value["artifact_checksum"]:
        raise IndustrialLoadConflict("normalized artifact checksum differs")
    source_value = value["source"]
    sources = {item.source_id: item for item in catalog.sources}
    source = sources.get(source_value.get("source_id")) if isinstance(source_value, dict) else None
    if source is None or canonical_json(asdict(source)) != canonical_json(source_value):
        raise IndustrialLoadConflict("normalized artifact source differs from the pinned catalog")
    if source.extraction_status != "TEXT_READY" or source.applicability == "EXCLUDED":
        raise IndustrialLoadConflict("normalized source is not approved for text acquisition")
    if source.sha256 is None or value["original_checksum"] != source.sha256:
        raise IndustrialLoadConflict("normalized artifact original checksum differs")
    pages = value["selected_pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 24 or any(
        type(page) is not int or not 1 <= page <= (source.physical_pages or 0) for page in pages
    ):
        raise IndustrialLoadConflict("normalized artifact physical page selection is invalid")
    if not isinstance(value["normalized_text"], str) or len(value["normalized_text"].encode("utf-8")) > MAX_SOURCE_BYTES:
        raise IndustrialLoadConflict("normalized source exceeds online derivative byte limit")
    if not isinstance(value["source_locations"], list) or len(value["source_locations"]) > 50_000:
        raise IndustrialLoadConflict("normalized source map exceeds location limit")
    if not isinstance(value["chunks"], list) or not 1 <= len(value["chunks"]) <= MAX_CHUNKS_PER_SOURCE:
        raise IndustrialLoadConflict("normalized source exceeds bounded source job chunk limit")
    for chunk in value["chunks"]:
        if not isinstance(chunk, dict) or any(
            type(chunk.get(name)) is not int
            for name in ("ordinal", "char_start", "char_end", "page_number")
        ):
            raise IndustrialLoadConflict("normalized chunk indices and physical page must be integers")
        if chunk.get("section") is not None and not isinstance(chunk["section"], str):
            raise IndustrialLoadConflict("normalized chunk section must be text")
    try:
        parsed = ParsedDocument(
            mime_type="application/pdf", normalized_text=value["normalized_text"],
            original_checksum=value["original_checksum"], normalized_checksum=value["normalized_checksum"],
            splitter_signature=value["splitter_signature"],
            chunks=tuple(ChunkSeed(**item) for item in value["chunks"]),
            source_locations=tuple(SourceLocation(**{**item, "bbox": tuple(item["bbox"])}) for item in value["source_locations"]),
            parser_version=value["parser_version"], selected_pages=tuple(pages),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise IndustrialLoadConflict("normalized artifact exact ranges or source locations are invalid") from exc
    if any(len(seed.text) > 2_000 for seed in parsed.chunks):
        raise IndustrialLoadConflict("normalized PDF chunk exceeds configured character bound")
    signature = re.fullmatch(
        r"bounded-boundary:v1:max=([0-9]+):min-ratio=0\.6\|pdfplumber-source-map:v1:pages=([0-9,]+)",
        parsed.splitter_signature,
    )
    if signature is None or not 400 <= int(signature[1]) <= 2000 or signature[2] != ",".join(map(str, pages)):
        raise IndustrialLoadConflict("normalized artifact splitter signature is unsupported")
    if any(len(seed.text) > int(signature[1]) for seed in parsed.chunks):
        raise IndustrialLoadConflict("normalized chunk exceeds its declared splitter bound")
    if original_cache is None:
        raise IndustrialLoadConflict("official artifacts require original cache verification")
    original_path = original_cache / f"{source.source_id}.pdf"
    original_bytes = _bounded_regular_bytes(original_path, 96 * 1024 * 1024)
    if len(original_bytes) != source.byte_size:
        raise IndustrialLoadConflict("cached original PDF size differs")
    if hashlib.sha256(original_bytes).hexdigest() != source.sha256:
        raise IndustrialLoadConflict("cached original PDF checksum differs")
    # A self-consistent JSON checksum is not proof of derivation. Reparse the
    # pinned original and compare every normalized character and geometry map.
    original = PdfDocumentParser(
        limits=PdfParserLimits(max_source_bytes=96 * 1024 * 1024), selected_pages=tuple(pages),
    ).parse(original_bytes)
    if (
        original.text != parsed.normalized_text or original.parser_version != parsed.parser_version
        or original.source_locations != parsed.source_locations
    ):
        raise IndustrialLoadConflict("normalized artifact differs from independently parsed original PDF")
    return parsed


@dataclass(frozen=True, slots=True)
class PreparedIndustrialSource:
    key: str
    request: IncrementalIngestionRequest
    provenance_json: str


@dataclass(frozen=True, slots=True)
class PreparedIndustrialLoad:
    version: str
    tenant_id: str
    manifest_checksum: str
    tbox: Any
    sources: tuple[PreparedIndustrialSource, ...]
    batches: tuple[ABoxRecordBatch, ...]

    def report(self) -> dict[str, Any]:
        records = [record for batch in self.batches for record in (*batch.mentions, *batch.assertions)]
        def source_summary(item: PreparedIndustrialSource) -> dict[str, Any]:
            metadata = json.loads(item.provenance_json)
            return {
                "key": item.key, "document_id": item.request.document_id, "version_id": item.request.version_id,
                "chunks": len(item.request.chunks), "original_checksum": item.request.original_checksum,
                "source_kind": item.request.source_name, "family": metadata.get("family"),
                "asset_keys": metadata.get("asset_keys", []),
            }
        official = [item for item in self.sources if item.request.source_name == "OFFICIAL_PUBLICATION"]
        return {
            "profile": "dev-mini", "production_candidate_eligible": False,
            "version": self.version, "tenant_id": self.tenant_id,
            "manifest_checksum": self.manifest_checksum, "tbox_id": self.tbox.tbox_id,
            "documents": len(self.sources), "chunks": sum(len(item.request.chunks) for item in self.sources),
            "authored_documents": len(self.sources) - len(official), "official_excerpts": len(official),
            "official_originals": len({item.request.original_checksum for item in official}),
            "mentions": sum(len(batch.mentions) for batch in self.batches),
            "assertions": sum(len(batch.assertions) for batch in self.batches),
            "publication_records": len(records),
            "authoritative_records": sum(item.trust.authority is AuthorityLevel.AUTHORITATIVE for item in records),
            "secondary_records": sum(item.trust.authority is AuthorityLevel.SECONDARY for item in records),
            "source_versions": [source_summary(item) for item in self.sources],
        }


def prepare_industrial_load(
    corpus: Any, embedding_profile: EmbeddingProfile, *, ingested_at: datetime,
    catalog: SourceCatalog, contract: dict[str, Any],
    normalized_artifacts: tuple[dict[str, Any], ...] = (), original_cache: Path | None = None,
) -> PreparedIndustrialLoad:
    """Public preflight always validates the complete industrial acceptance model."""
    from .validation import validate_industrial_model

    validate_industrial_model(corpus, catalog, contract, build_industrial_tbox(corpus.tenant_id))
    return _prepare_industrial_load(corpus, embedding_profile, ingested_at=ingested_at,
        catalog=catalog, normalized_artifacts=normalized_artifacts, original_cache=original_cache)


def _prepare_industrial_load(
    corpus: Any, embedding_profile: EmbeddingProfile, *, ingested_at: datetime,
    normalized_artifacts: tuple[dict[str, Any], ...] = (), catalog: SourceCatalog | None = None,
    original_cache: Path | None = None,
) -> PreparedIndustrialLoad:
    if corpus.tenant_id != INDUSTRIAL_TENANT:
        raise IndustrialLoadConflict("industrial corpus must use its dedicated tenant")
    if ingested_at.tzinfo is None or ingested_at.utcoffset() is None:
        raise IndustrialLoadConflict("ingested_at must be timezone-aware")
    tbox = build_industrial_tbox(corpus.tenant_id)
    governance = tbox.compile_governance_policy()
    prepared: list[PreparedIndustrialSource] = []
    source_by_key: dict[str, PreparedIndustrialSource] = {}
    section_by_key: dict[tuple[str, str], Any] = {}

    def source(
        key: str, title: str, text: str, chunks: tuple[ChunkSeed, ...], *, groups: tuple[str, ...],
        kind: str, published_at: datetime | None, original_checksum: str,
        splitter: str, mime_type: str, language: str, extra: dict[str, Any],
    ) -> PreparedIndustrialSource:
        if key in source_by_key:
            raise IndustrialLoadConflict("duplicate industrial source key")
        if not groups or not set(groups) <= INDUSTRIAL_GROUPS:
            raise IndustrialLoadConflict("source contains unknown industrial access groups")
        if not 1 <= len(chunks) <= MAX_CHUNKS_PER_SOURCE or len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise IndustrialLoadConflict("source exceeds bounded industrial ingestion limits")
        if "".join(seed.text for seed in chunks) != text:
            raise IndustrialLoadConflict("source chunks must reproduce the complete exact text")
        signatures = ("industrial-source-normalization:v1", splitter, "source-only:v1", "none:v1", governance.policy_id, LOAD_PROFILE)
        profile = GraphPipelineProfile(pipeline_profile_id(*signatures), *signatures)
        request = IncrementalIngestionRequest(
            operation_key=f"{LOAD_PROFILE}:{corpus.version}:{key}", tenant_id=corpus.tenant_id,
            canonical_uri=f"industrial://{corpus.tenant_id}/{corpus.version}/{key}",
            title=title, source_name=kind, mime_type=mime_type, language=language,
            published_at=published_at, access_policy_id=f"industrial-acl:{corpus.tenant_id}:{'+'.join(sorted(groups))}",
            access_policy_version=1, access_groups=frozenset(groups), source_generation=0,
            expected_active_snapshot_id=None, chunks=chunks, profile=profile, governance_policy=governance,
            embedding_profile=embedding_profile, version_number=1, ingested_at=ingested_at,
            original_checksum=original_checksum,
        )
        metadata = {
            "schema_version": "industrial-source-provenance-v1", "tenant_id": corpus.tenant_id,
            "document_id": request.document_id, "version_id": request.version_id,
            "source_kind": kind, "source_key": key, "corpus_version": corpus.version,
            "original_checksum": original_checksum, "normalized_checksum": content_checksum(text),
            "access_policy_id": request.access_policy_id, "access_policy_version": 1,
            "access_groups": sorted(groups), **extra,
        }
        item = PreparedIndustrialSource(key, request, canonical_json(metadata))
        source_by_key[key] = item
        prepared.append(item)
        return item

    for document in corpus.documents:
        chunks = tuple(
            ChunkSeed(index, document.text[section.char_start:section.char_end], section.char_start,
                      section.char_end, section=section.title)
            for index, section in enumerate(document.sections)
        )
        if any(len(chunk.text) > 1200 for chunk in chunks):
            raise IndustrialLoadConflict("authored section exceeds 1200-character chunk bound")
        # Reuse the strict parsed-document gapless range and checksum invariant.
        ParsedDocument("text/markdown", document.text, content_checksum(document.text),
                       content_checksum(document.text), "industrial-authored-sections:v1", chunks)
        source(document.key, document.title, document.text, chunks,
               groups=document.access_groups, kind=document.source_kind,
               published_at=_date(document.published_at), original_checksum=content_checksum(document.text),
               splitter="industrial-authored-sections:v1", mime_type="text/markdown", language="zh",
               extra={"source_refs": [asdict(ref) for ref in document.source_refs],
                      "family": document.family, "contract_family": FAMILY_TO_CONTRACT[document.family],
                      "asset_keys": list(getattr(document, "asset_keys", ())),
                      "sections": [asdict(section) for section in document.sections],
                      "source_locations": [], "project_curated_not_company_approved": True,
                      "is_synthetic": document.source_kind == "SYNTHETIC_FIELD_RECORD"})
        for section in document.sections:
            key = (document.key, section.key)
            if key in section_by_key:
                raise IndustrialLoadConflict("duplicate industrial section key")
            section_by_key[key] = section
    for artifact in normalized_artifacts:
        if catalog is None:
            raise IndustrialLoadConflict("official artifacts require their pinned source catalog")
        parsed = validate_normalized_artifact(artifact, catalog, original_cache=original_cache)
        source_info = artifact["source"]
        from .validation import CORE_SOURCE_FAMILIES
        family = CORE_SOURCE_FAMILIES.get(source_info["source_id"])
        key = "official-" + source_info["source_id"] + "-" + digest(parsed.selected_pages)[:12]
        source(key, source_info["title"], parsed.normalized_text, parsed.chunks,
               groups=("public",), kind="OFFICIAL_PUBLICATION", published_at=_date(source_info["portal_date"]),
               original_checksum=parsed.original_checksum, splitter=parsed.splitter_signature,
               mime_type="application/pdf", language=source_info["language"][0],
               extra={"source": source_info, "source_locations": artifact["source_locations"],
                      "family": family, "contract_family": FAMILY_TO_CONTRACT.get(family),
                      "asset_keys": [], "sections": [], "is_reference_only": family is None,
                      "selected_pages": artifact["selected_pages"], "is_explicit_excerpt": True,
                      "parser_version": parsed.parser_version, "artifact_checksum": artifact["artifact_checksum"],
                      "limitations": artifact["limitations"], "is_synthetic": False})
    if not 1 <= len(prepared) <= MAX_DOCUMENTS or sum(len(item.request.chunks) for item in prepared) > MAX_CHUNKS:
        raise IndustrialLoadConflict("industrial load exceeds corpus bounds")
    entities = {item.key: item for item in corpus.entities}
    if len(entities) != len(corpus.entities):
        raise IndustrialLoadConflict("duplicate industrial entity key")
    identities = {
        key: EntityIdentity(entity_id(corpus.tenant_id, item.type_name, item.identity), corpus.tenant_id,
                            item.type_name, item.identity, item.name, tuple(item.aliases))
        for key, item in entities.items()
    }
    section_entities: dict[tuple[str, str], set[str]] = {}
    section_relations: dict[tuple[str, str], list[Any]] = {}
    for item in corpus.entities:
        section_entities.setdefault((item.document_key, item.section_key), set()).add(item.key)
    relation_keys: set[str] = set()
    for relation in corpus.relationships:
        if relation.key in relation_keys:
            raise IndustrialLoadConflict("duplicate industrial relationship key")
        relation_keys.add(relation.key)
        key = (relation.document_key, relation.section_key)
        section_relations.setdefault(key, []).append(relation)
        section_entities.setdefault(key, set()).update((relation.subject_key, relation.object_key))
    batches: list[ABoxRecordBatch] = []
    for key in sorted(section_entities):
        if key not in section_by_key:
            raise IndustrialLoadConflict("graph seed refers to an unavailable source section")
        request = source_by_key[key[0]].request
        section = section_by_key[key]
        chunk = next(item for item in request.domain_inputs()[2] if item.char_start == section.char_start)
        trust = (
            authoritative_import_trust(ontology_version_id=tbox.tbox_id,
                imported_by="project-curated-reference:v1", imported_at=ingested_at, review_notes=REVIEW_NOTE)
            if request.source_name == "CURATED_REFERENCE"
            else TrustMetadata(KnowledgeOrigin.RULE_DERIVED, AuthorityLevel.SECONDARY,
                GovernanceStatus.CANDIDATE, tbox.tbox_id, ingested_at,
                extractor_version=f"{corpus.version}:declared-exact-evidence-rule:v1")
        )
        def evidence(start: int, end: int) -> EvidenceReference:
            return EvidenceReference(corpus.tenant_id, request.document_id, request.version_id, chunk.chunk_id,
                chunk.char_start + start, chunk.char_start + end, chunk.text[start:end],
                request.access_policy_id, 1, request.access_groups)
        mentions: dict[str, EntityMentionRecord] = {}
        for entity_key in sorted(section_entities[key]):
            identity = identities[entity_key]
            start = chunk.text.find(identity.canonical_name)
            if start < 0:
                raise IndustrialLoadConflict("entity name is absent from declared exact evidence section")
            record_id = knowledge_record_id(corpus.tenant_id, "ENTITY_MENTION", f"{corpus.version}:{key[0]}:{key[1]}:{entity_key}")
            mentions[entity_key] = EntityMentionRecord(RecordRevision.next(record_id, 0), corpus.tenant_id,
                identity, evidence(start, start + len(identity.canonical_name)), 1.0, trust, ingested_at)
        assertions: list[AssertionRecord] = []
        for relation in section_relations.get(key, ()):
            assertions.append(AssertionRecord(
                RecordRevision.next(knowledge_record_id(corpus.tenant_id, "ASSERTION", f"{corpus.version}:{relation.key}"), 0),
                corpus.tenant_id, identities[relation.subject_key], relation.predicate,
                evidence(0, len(chunk.text)), mentions[relation.subject_key].revision_id, 1.0, trust, ingested_at,
                object_entity=identities[relation.object_key],
                object_mention_revision_id=mentions[relation.object_key].revision_id,
            ))
        batch = ABoxRecordBatch(corpus.tenant_id, tuple(mentions.values()), tuple(assertions))
        if len(batch.mentions) + len(batch.assertions) > MAX_BATCH_RECORDS:
            raise IndustrialLoadConflict("one source section exceeds the bounded record batch")
        batches.append(batch)
    if sum(len(batch.mentions) + len(batch.assertions) for batch in batches) > MAX_PUBLICATION_RECORDS:
        raise IndustrialLoadConflict("complete industrial publication exceeds 500 records")
    manifest = digest({"corpus": corpus.manifest_checksum,
        "artifacts": sorted(item["artifact_checksum"] for item in normalized_artifacts),
        "embedding": asdict(embedding_profile), "ontology": tbox.checksum, "loader": LOAD_PROFILE})
    return PreparedIndustrialLoad(corpus.version, corpus.tenant_id, manifest, tbox, tuple(prepared), tuple(batches))


def _require_reference_revision(
    original: EntityMentionRecord | AssertionRecord,
    current: EntityMentionRecord | AssertionRecord,
    principal: Principal,
    started: datetime,
) -> None:
    """A matching revision number alone does not prove a reference replay.

    An independent editor may approve a candidate into the same revision 2
    the bootstrap would have used. Compare all factual content and provenance
    before considering that head part of this deterministic loader operation.
    """
    ignored = {"revision", "trust", "subject_mention_revision_id", "object_mention_revision_id"}
    if type(current) is not type(original) or any(
        getattr(current, field.name) != getattr(original, field.name)
        for field in fields(original) if field.name not in ignored
    ):
        raise IndustrialLoadConflict("industrial record payload was independently revised")
    for name in ("origin", "authority", "ontology_version_id", "created_at", "extractor_version", "prompt_version"):
        if getattr(current.trust, name) != getattr(original.trust, name):
            raise IndustrialLoadConflict("industrial record provenance was independently revised")
    if current.trust.status is GovernanceStatus.CANDIDATE:
        if current.revision.revision != 1:
            raise IndustrialLoadConflict("industrial candidate was independently revised")
        return
    expected_reviewer = (
        original.trust.reviewed_by
        if original.trust.authority is AuthorityLevel.AUTHORITATIVE
        else principal.principal_id
    )
    if (current.trust.reviewed_by != expected_reviewer
            or current.trust.reviewed_at != started or current.trust.review_notes != REVIEW_NOTE):
        raise IndustrialLoadConflict("industrial review belongs to an independent reviewer")


class Neo4jIndustrialLoader:
    """One identifiable run, atomic source operations and immutable batch checkpoints."""

    def __init__(self, driver: Any, database: str = "neo4j", *,
                 checkpoint: Callable[[str, str], None] | None = None) -> None:
        self.driver = driver
        self.database = database
        self.checkpoint = checkpoint or (lambda phase, key: None)

    def load(self, corpus: Any, embedding_profile: EmbeddingProfile, embedding_provider: Any, *,
             catalog: SourceCatalog, contract: dict[str, Any],
             normalized_artifacts: tuple[dict[str, Any], ...] = (),
             original_cache: Path | None = None) -> dict[str, Any]:
        # Validate every source before the first data mutation. The lease also
        # serializes different manifests and processes, not only one CLI call.
        prepare_industrial_load(corpus, embedding_profile, ingested_at=datetime.now(UTC),
            normalized_artifacts=normalized_artifacts, catalog=catalog, contract=contract, original_cache=original_cache)
        with _LoaderLease(self.driver, self.database, corpus.tenant_id) as lease:
            def bounded_provider(**kwargs: Any) -> tuple[float, ...]:
                lease.refresh()
                result = embedding_provider(**kwargs)
                lease.refresh()
                return result
            return self._run(corpus, embedding_profile, bounded_provider,
                normalized_artifacts=normalized_artifacts, catalog=catalog,
                original_cache=original_cache, heartbeat=lease.refresh)

    def _run(self, corpus: Any, embedding_profile: EmbeddingProfile, embedding_provider: Any, *,
             normalized_artifacts: tuple[dict[str, Any], ...], catalog: SourceCatalog | None,
             original_cache: Path | None, heartbeat: Callable[[], None]) -> dict[str, Any]:
        preview = _prepare_industrial_load(corpus, embedding_profile, ingested_at=datetime.now(UTC),
            normalized_artifacts=normalized_artifacts, catalog=catalog, original_cache=original_cache)
        apply_schema(self.driver, self.database)
        for label, name in (("IndustrialCorpusLoad", "industrial_load_id_unique"),
                            ("IndustrialLoadBatch", "industrial_batch_id_unique")):
            self.driver.execute_query(f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.load_id IS UNIQUE", database_=self.database)
        load_id = digest([preview.tenant_id, preview.manifest_checksum])
        principal = industrial_loader_principal(preview.tenant_id)
        rows, _, _ = self.driver.execute_query(
            "MERGE (run:IndustrialCorpusLoad {load_id:$load_id}) "
            "ON CREATE SET run.tenant_id=$tenant,run.manifest_checksum=$manifest,run.started_at=$now,run.phase='PREPARING' "
            "RETURN run.tenant_id AS tenant,run.manifest_checksum AS manifest,run.started_at AS started_at",
            load_id=load_id, tenant=preview.tenant_id, manifest=preview.manifest_checksum, now=datetime.now(UTC), database_=self.database,
        )
        if len(rows) != 1 or rows[0]["tenant"] != preview.tenant_id or rows[0]["manifest"] != preview.manifest_checksum:
            raise IndustrialLoadConflict("industrial load checkpoint differs")
        started = rows[0]["started_at"]
        started = started.to_native() if hasattr(started, "to_native") else started
        plan = _prepare_industrial_load(corpus, embedding_profile, ingested_at=started,
            normalized_artifacts=normalized_artifacts, catalog=catalog, original_cache=original_cache)
        try:
            tboxes = Neo4jTBoxStore(self.driver, self.database)
            active_tbox = tboxes.active(plan.tenant_id, plan.tbox.key)
            if active_tbox is None:
                tboxes.import_version(plan.tbox)
                tboxes.publish(plan.tenant_id, plan.tbox.tbox_id, expected_active_tbox_id=None)
            elif active_tbox.tbox_id != plan.tbox.tbox_id or active_tbox.checksum != plan.tbox.checksum:
                raise IndustrialLoadConflict("industrial active ontology differs; explicit migration required")
            pipeline = Neo4jIncrementalPipeline(self.driver, self.database, worker_id="industrial-bootstrap", lease_seconds=60)
            provenance = Neo4jIndustrialProvenanceStore(self.driver, self.database)
            for source in plan.sources:
                heartbeat()
                self._phase(load_id, "INGESTING", source.key)
                pipeline.run(source.request, lambda **kwargs: ExtractionOutput((), (), ()), embedding_provider)
                provenance.persist(principal, json.loads(source.provenance_json))
                self.checkpoint("source", source.key)
            self._phase(load_id, "GOVERNING", "")
            store = Neo4jKnowledgeStore(self.driver, self.database)
            for batch in plan.batches:
                heartbeat()
                self._persist_batch_once(load_id, batch, store)
            review = Neo4jKnowledgeReviewService(self.driver, self.database)
            approved: list[str] = []
            for kind in (ReviewRecordKind.ENTITY_MENTION, ReviewRecordKind.ASSERTION):
                for batch in plan.batches:
                    records = batch.mentions if kind is ReviewRecordKind.ENTITY_MENTION else batch.assertions
                    for original in records:
                        heartbeat()
                        getter = store.get_entity_mention if kind is ReviewRecordKind.ENTITY_MENTION else store.get_assertion
                        current = getter(principal, original.record_id, statuses=tuple(GovernanceStatus))
                        if current is None:
                            raise IndustrialLoadConflict("persisted industrial record is unavailable")
                        _require_reference_revision(original, current, principal, started)
                        if current.trust.status is GovernanceStatus.CANDIDATE:
                            outcome = review.approve(principal, record_kind=kind, record_id=current.record_id,
                                expected_revision=current.revision.revision, reviewed_at=started, notes=REVIEW_NOTE)
                            approved.append(outcome.revision_id)
                        elif current.trust.status in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}:
                            authoritative = original.trust.authority is AuthorityLevel.AUTHORITATIVE
                            allowed_revision = 1 if authoritative else (
                                2 if current.trust.status is GovernanceStatus.APPROVED else 3
                            )
                            if current.revision.revision != allowed_revision:
                                raise IndustrialLoadConflict("industrial record was independently revised")
                            # Publication creates revision 3 from approved rule
                            # revision 2. Exact replay must reuse the original
                            # publication input IDs, not its output revisions.
                            approved.append(knowledge_revision_id(current.record_id, 1 if authoritative else 2))
                        else:
                            raise IndustrialLoadConflict("industrial record was independently revised; explicit review required")
            self.checkpoint("review", load_id)
            heartbeat()
            self._phase(load_id, "PUBLISHING", "")
            publication = Neo4jKnowledgePublicationService(self.driver, self.database).publish(
                principal, tuple(approved), expected_active_publication_id=None, published_at=started)
            self.checkpoint("publication", publication.publication_id)
            heartbeat()
            self._phase(load_id, "INDEXING", "")
            generation = self._ensure_generation(plan.tenant_id, embedding_profile, started, load_id)
            self._phase(load_id, "COMPLETE", "")
            return {**plan.report(), "load_id": load_id, "publication_id": publication.publication_id,
                    "embedding_generation_id": generation.generation_id, "phase": "COMPLETE"}
        except Exception as exc:
            # Persist a safe failure type without source text, credentials or driver details.
            self.driver.execute_query(
                "MATCH (run:IndustrialCorpusLoad {load_id:$load_id,tenant_id:$tenant}) SET run.phase='FAILED',run.error_type=$kind",
                load_id=load_id, tenant=plan.tenant_id, kind=type(exc).__name__, database_=self.database,
            )
            raise

    def _phase(self, load_id: str, phase: str, key: str) -> None:
        self.driver.execute_query(
            "MATCH (run:IndustrialCorpusLoad {load_id:$load_id,tenant_id:$tenant}) SET run.phase=$phase,run.current_source_key=$key,run.error_type=null",
            load_id=load_id, tenant=INDUSTRIAL_TENANT, phase=phase, key=key, database_=self.database,
        )

    def _persist_batch_once(self, load_id: str, batch: ABoxRecordBatch, store: Neo4jKnowledgeStore) -> None:
        authoritative = batch.mentions[0].trust.authority is AuthorityLevel.AUTHORITATIVE
        if authoritative:
            batch.require_authoritative_import()
        else:
            store.require_rule_candidates(batch)
        batch_checksum = digest(_plain(asdict(batch)))
        marker = digest([load_id, [record.record_id for record in (*batch.mentions, *batch.assertions)]])
        def write(tx: Any) -> None:
            row = tx.run(
                "MERGE (batch:IndustrialLoadBatch {load_id:$id}) "
                "ON CREATE SET batch.tenant_id=$tenant,batch.checksum=$checksum,batch.complete=false "
                "SET batch.__write_lock=randomUUID() WITH batch REMOVE batch.__write_lock "
                "RETURN batch.tenant_id AS tenant,batch.checksum AS checksum,batch.complete AS complete",
                id=marker, tenant=batch.tenant_id, checksum=batch_checksum,
            ).single()
            if row["tenant"] != batch.tenant_id or row["checksum"] != batch_checksum:
                raise IndustrialLoadConflict("immutable industrial batch checkpoint differs")
            if row["complete"]:
                return
            # Use the existing store transaction validations atomically with the
            # replay marker, after validating the exact authority/origin lane.
            store._write_batch_tx(tx, batch, authoritative)
            tx.run("MATCH (batch:IndustrialLoadBatch {load_id:$id}) SET batch.complete=true", id=marker).consume()
        with self.driver.session(database=self.database) as session:
            session.execute_write(write)
        self.checkpoint("batch", marker)

    def _ensure_generation(self, tenant: str, profile: EmbeddingProfile, now: datetime, load_id: str) -> Any:
        manager = Neo4jEmbeddingIndexManager(self.driver, self.database)
        active = manager.active_generation(tenant)
        if active is not None:
            if active.embedding_space_id != profile.embedding_space_id:
                raise IndustrialLoadConflict("active embedding vector space differs; explicit migration required")
            if not manager.coverage(active.generation_id).complete:
                raise IndustrialLoadConflict("active industrial embedding coverage is incomplete")
            return active
        def target_version(tx: Any) -> int:
            row = tx.run(
                "MATCH (run:IndustrialCorpusLoad {tenant_id:$tenant,load_id:$load_id}) "
                "SET run.__generation_lock=randomUUID() WITH run REMOVE run.__generation_lock WITH run "
                "OPTIONAL MATCH (generation:EmbeddingIndexGeneration {tenant_id:$tenant}) "
                "RETURN run.embedding_generation_version AS recorded, "
                "coalesce(max(generation.generation_version),0) AS latest",
                tenant=tenant, load_id=load_id,
            ).single()
            if row is None:
                raise IndustrialLoadConflict("industrial generation checkpoint is unavailable")
            version = int(row["recorded"]) if row["recorded"] is not None else int(row["latest"]) + 1
            tx.run(
                "MATCH (run:IndustrialCorpusLoad {tenant_id:$tenant,load_id:$load_id}) "
                "SET run.embedding_generation_version=$version",
                tenant=tenant, load_id=load_id, version=version,
            ).consume()
            return version
        with self.driver.session(database=self.database) as session:
            version = session.execute_write(target_version)
        seed = "industrial-generation-profile"
        embedding = ChunkEmbedding(chunk_embedding_id(seed, profile.embedding_space_id), tenant, seed,
            profile.embedding_space_id, profile.provider, profile.model, profile.revision, profile.dimensions,
            profile.normalization, now, (1.0,) + (0.0,) * (profile.dimensions - 1))
        target = manager.prepare(tenant_id=tenant, embedding_profile=embedding, generation_version=version)
        if not manager.coverage(target.generation_id).complete:
            raise IndustrialLoadConflict("industrial embedding coverage is incomplete")
        return manager.activate(target.generation_id, expected_active_generation_id=None)
