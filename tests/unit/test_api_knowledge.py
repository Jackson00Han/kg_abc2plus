"""Contracts and adapters for governed knowledge HTTP operations."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from graphrag_prod.api.backend import GraphRAGApplicationBackend
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import (
    AuthoritativeImportRequest,
    KnowledgeConstructionRequest,
    OntologyImportRequest,
    OntologyListRequest,
    OntologyListResponse,
    OntologyPublishRequest,
    PublicationHistoryRequest,
    PublicationRequest,
    ReviewBatchRequest,
    ReviewQueueRequest,
    RollbackRequest,
)
from graphrag_prod.api.runtime import (
    AuthorizationError,
    BackendResult,
    ConflictError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    OperationEnvelope,
    OperationKind,
    RequestValidationError,
    ResourceNotFoundError,
    required_scope,
)
from graphrag_prod.construction import DocumentParseError
from graphrag_prod.domain import Principal
from graphrag_prod.knowledge import KnowledgeWriteResult
from graphrag_prod.knowledge.review import (
    KnowledgePublicationView,
    ReviewBatchResult,
    ReviewOutcome,
)
from graphrag_prod.ontology import EntityTypeDefinition, TBoxStatus, TBoxVersion


NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


def _construct_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operation_key": "construction-000001",
        "canonical_uri": "https://example.test/asset.txt",
        "title": "Asset report",
        "source_name": "controlled upload",
        "mime_type": "text/plain",
        "language": "en",
        "tbox_key": "industrial-assets",
        "content_base64": base64.b64encode(b"Acme owns Pump-7.").decode(),
    }
    value.update(changes)
    return value


def _authoritative_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ontology_version_id": "tbox-1",
        "mentions": [
            {
                "source_key": "expert-pump-7",
                "entity": {
                    "entity_type": "Asset",
                    "canonical_key": "asset-id:P-7",
                    "canonical_name": "Pump-7",
                },
                "evidence": {
                    "document_id": "document-1",
                    "version_id": "version-1",
                    "chunk_id": "chunk-1",
                    "char_start": 10,
                    "char_end": 16,
                    "quoted_text": "Pump-7",
                },
            }
        ],
        "assertions": [],
    }
    value.update(changes)
    return value


class KnowledgeContractTests(unittest.TestCase):
    def test_construct_uses_strict_canonical_base64_and_supported_mime(self) -> None:
        for mime_type in (
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
        ):
            with self.subTest(mime_type=mime_type):
                request = KnowledgeConstructionRequest.model_validate(
                    _construct_payload(mime_type=mime_type)
                )
                self.assertEqual(request.decoded_content(), b"Acme owns Pump-7.")
        for changes in (
            {"content_base64": "not base64"},
            {"content_base64": "eA"},
            {"content_base64": base64.urlsafe_b64encode(b"\xff").decode()},
            {"mime_type": "application/pdf"},
            {"content_base64": base64.b64encode(b"").decode()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    KnowledgeConstructionRequest.model_validate(
                        _construct_payload(**changes)
                    )

    def test_clients_cannot_supply_identity_acl_or_capabilities(self) -> None:
        targets = (
            (KnowledgeConstructionRequest, _construct_payload()),
            (AuthoritativeImportRequest, _authoritative_payload()),
            (
                OntologyImportRequest,
                {
                    "key": "industrial-assets",
                    "version": 1,
                    "entity_types": [
                        {
                            "name": "Asset",
                            "canonical_key_namespaces": ["asset-id"],
                        }
                    ],
                },
            ),
        )
        for model, payload in targets:
            for forbidden in (
                "tenant_id",
                "principal_id",
                "capabilities",
                "access_groups",
            ):
                with self.subTest(model=model.__name__, forbidden=forbidden):
                    with self.assertRaises(ValidationError):
                        model.model_validate({**payload, forbidden: "forged"})

    def test_operation_scopes_are_independent_and_writes_never_retry(self) -> None:
        expected = {
            OperationKind.ONTOLOGY_LIST: ("ontology:read", False),
            OperationKind.ONTOLOGY_IMPORT: ("ontology:write", True),
            OperationKind.ONTOLOGY_PUBLISH: ("ontology:publish", True),
            OperationKind.KNOWLEDGE_IMPORT: ("knowledge:import", True),
            OperationKind.KNOWLEDGE_CONSTRUCT: ("knowledge:construct", True),
            OperationKind.KNOWLEDGE_REVIEW_QUEUE: ("knowledge:review", False),
            OperationKind.KNOWLEDGE_REVIEW_BATCH: ("knowledge:review", True),
            OperationKind.KNOWLEDGE_PUBLISH: ("knowledge:publish", True),
            OperationKind.KNOWLEDGE_ROLLBACK: ("knowledge:publish", True),
            OperationKind.KNOWLEDGE_HISTORY: ("knowledge:publish", False),
        }
        for operation, (scope, write) in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(required_scope(operation), scope)
                self.assertEqual(operation.is_write, write)
                self.assertEqual(operation.is_retry_safe, not write)


class _Documents:
    ingest = delete = get_job = lambda *args, **kwargs: None


class _Queries:
    retrieve = answer = lambda *args, **kwargs: None


class _Readiness:
    check = lambda *args, **kwargs: None


class _Knowledge:
    def __init__(self) -> None:
        self.principal: Principal | None = None

    def ontology_list(self, principal: Principal, _request: object) -> BackendResult:
        self.principal = principal
        return BackendResult(OntologyListResponse(items=()))

    ontology_import = ontology_publish = authoritative_import = lambda *args: None
    construct = review_queue = review_batch = lambda *args: None
    publish = rollback = history = lambda *args: None


class KnowledgeBackendTests(unittest.TestCase):
    def test_legacy_backend_constructor_remains_valid_and_defaults_closed(self) -> None:
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
        )
        with self.assertRaises(ResourceNotFoundError):
            backend.execute(
                OperationEnvelope(
                    operation=OperationKind.ONTOLOGY_LIST,
                    request_id="request-1",
                    trace_id="trace-1",
                    principal_id="expert-1",
                    tenant_id="tenant-alpha",
                    access_groups=frozenset({"engineers"}),
                    scopes=frozenset({"ontology:read"}),
                    payload={},
                )
            )

    def test_backend_passes_verified_scopes_as_domain_capabilities(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        result = backend.execute(
            OperationEnvelope(
                operation=OperationKind.ONTOLOGY_LIST,
                request_id="request-1",
                trace_id="trace-1",
                principal_id="expert-1",
                tenant_id="tenant-alpha",
                access_groups=frozenset({"engineers"}),
                scopes=frozenset({"ontology:read", "knowledge:review"}),
                payload={"limit": 10},
            )
        )
        self.assertEqual(result.payload.items, ())
        assert knowledge.principal is not None
        self.assertEqual(
            knowledge.principal.capabilities,
            frozenset({"ontology:read", "knowledge:review"}),
        )

    def test_wrong_scope_stops_before_knowledge_adapter(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        with self.assertRaises(AuthorizationError):
            backend.execute(
                OperationEnvelope(
                    operation=OperationKind.ONTOLOGY_LIST,
                    request_id="request-1",
                    trace_id="trace-1",
                    principal_id="expert-1",
                    tenant_id="tenant-alpha",
                    access_groups=frozenset({"engineers"}),
                    scopes=frozenset({"ontology:write"}),
                    payload={},
                )
            )
        self.assertIsNone(knowledge.principal)


class _RowsSession:
    def __init__(self, owner: "_Driver") -> None:
        self.owner = owner

    def __enter__(self) -> "_RowsSession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> list[dict[str, object]]:
        self.owner.calls.append((query, parameters))
        if self.owner.failure is not None:
            raise self.owner.failure
        if self.owner.allowed and self.owner.active:
            return [
                {
                    "access_policy_id": "policy-engineering",
                    "access_policy_version": 3,
                    "access_groups": ["engineers"],
                }
            ]
        return []


class _Driver:
    def __init__(
        self,
        *,
        allowed: bool = True,
        active: bool = True,
        failure: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.active = active
        self.failure = failure
        self.calls: list[tuple[str, dict[str, object]]] = []

    def session(self, **_kwargs: object) -> _RowsSession:
        return _RowsSession(self)


class _Construction:
    def __init__(self) -> None:
        self.call = None

    def run(self, *_args: object) -> object:
        self.call = _args
        return SimpleNamespace(
            job_id="job-1",
            document_id="document-1",
            version_id="version-1",
            snapshot_id="snapshot-1",
            tbox_id="tbox-1",
            chunks=(
                SimpleNamespace(
                    chunk_id="chunk-1",
                    artifact_id="artifact-1",
                    status="CANDIDATE",
                    finding_codes=(),
                    mention_record_ids=("mention-1",),
                    assertion_record_ids=(),
                    replayed=False,
                ),
            ),
        )


class _MalformedConstruction:
    def run(self, *_args: object) -> object:
        return SimpleNamespace(
            job_id="",
            document_id="document-1",
            version_id="version-1",
            snapshot_id="snapshot-1",
            tbox_id="tbox-1",
            chunks=(),
        )


class _FailingConstruction:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def run(self, *_args: object) -> object:
        raise self.failure


class _KnowledgeStore:
    def __init__(self) -> None:
        self.batch = None

    def import_authoritative(self, batch: object) -> KnowledgeWriteResult:
        self.batch = batch
        return KnowledgeWriteResult(
            tenant_id="tenant-alpha",
            ontology_version_id="tbox-1",
            mention_count=1,
            assertion_count=0,
            revision_ids=(batch.mentions[0].revision_id,),  # type: ignore[attr-defined]
        )


class _TBoxes:
    def __init__(self) -> None:
        self.value = TBoxVersion(
            tenant_id="tenant-alpha",
            key="industrial-assets",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(EntityTypeDefinition("Asset", ("asset-id",)),),
            relationship_types=(),
        )
        self.imported = None
        self.publish_call = None

    def list(self, tenant_id: str, **_kwargs: object) -> tuple[TBoxVersion, ...]:
        return (self.value,) if tenant_id == "tenant-alpha" else ()

    def import_version(
        self, value: TBoxVersion, *, expected_checksum: str | None
    ) -> TBoxVersion:
        self.imported = (value, expected_checksum)
        return value

    def get(self, tenant_id: str, tbox_id: str) -> TBoxVersion:
        if tenant_id != "tenant-alpha" or tbox_id != self.value.tbox_id:
            raise KeyError("absent")
        return self.value

    def publish(
        self,
        tenant_id: str,
        tbox_id: str,
        *,
        expected_active_tbox_id: str | None,
    ) -> TBoxVersion:
        self.publish_call = (tenant_id, tbox_id, expected_active_tbox_id)
        return self.value.with_status(TBoxStatus.PUBLISHED)


class _Reviews:
    def __init__(self) -> None:
        self.queue_call = None
        self.batch_call = None

    def review_queue(self, principal: Principal, **kwargs: object) -> tuple[object, ...]:
        self.queue_call = (principal, kwargs)
        return ()

    def review_batch(
        self, principal: Principal, requests: tuple[object, ...]
    ) -> ReviewBatchResult:
        self.batch_call = (principal, requests)
        item = requests[0]
        return ReviewBatchResult(
            tenant_id=principal.tenant_id,
            outcomes=(
                ReviewOutcome(
                    record_kind=item.record_kind,
                    record_id=item.record_id,
                    previous_revision_id="revision-1",
                    revision_id="revision-2",
                    revision=2,
                    status=item.decision,
                ),
            ),
        )


class _Publications:
    def __init__(self) -> None:
        self.value = KnowledgePublicationView(
            publication_id="publication-1",
            tenant_id="tenant-alpha",
            generation=1,
            manifest_hash="b" * 64,
            source_revision_ids=("revision-2",),
            published_revision_ids=("published-revision-1",),
            removed_record_ids=(),
            replaced_record_ids=(),
            status="ACTIVE",
            created_by="expert-1",
            created_at=NOW,
            activated_at=NOW,
        )
        self.calls: list[tuple[str, object]] = []

    def publish(self, principal: Principal, ids: tuple[str, ...], **kwargs: object) -> object:
        self.calls.append(("publish", (principal, ids, kwargs)))
        return self.value

    def get(self, principal: Principal, publication_id: str) -> object | None:
        self.calls.append(("get", (principal, publication_id)))
        return self.value if publication_id == self.value.publication_id else None

    def rollback(self, principal: Principal, publication_id: str, **kwargs: object) -> object:
        self.calls.append(("rollback", (principal, publication_id, kwargs)))
        return self.value

    def history(self, principal: Principal, *, limit: int) -> tuple[object, ...]:
        self.calls.append(("history", (principal, limit)))
        return (self.value,)


class KnowledgeAdapterTests(unittest.TestCase):
    def _adapter(
        self,
        driver: _Driver,
        store: _KnowledgeStore,
        *,
        construction: object | None = None,
        tboxes: object | None = None,
        reviews: object | None = None,
        publications: object | None = None,
    ) -> Neo4jKnowledgeOperations:
        return Neo4jKnowledgeOperations(
            driver=driver,
            construction=construction or _Construction(),
            tboxes=tboxes or SimpleNamespace(),
            knowledge=store,
            reviews=reviews or SimpleNamespace(),
            publications=publications or SimpleNamespace(),
            clock=lambda: NOW,
        )

    def test_authoritative_import_hydrates_acl_from_authorized_chunk(self) -> None:
        driver = _Driver()
        store = _KnowledgeStore()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        result = self._adapter(driver, store).authoritative_import(
            principal,
            AuthoritativeImportRequest.model_validate(_authoritative_payload()),
        )
        self.assertEqual(result.payload.mention_count, 1)
        self.assertIsNotNone(store.batch)
        mention = store.batch.mentions[0]  # type: ignore[union-attr]
        self.assertEqual(mention.tenant_id, "tenant-alpha")
        self.assertEqual(mention.evidence.access_groups, frozenset({"engineers"}))
        self.assertEqual(mention.trust.reviewed_by, "expert-1")
        self.assertEqual(driver.calls[0][1]["tenant_id"], "tenant-alpha")
        self.assertEqual(driver.calls[0][1]["groups"], ["engineers"])

    def test_missing_and_cross_tenant_evidence_have_one_public_error(self) -> None:
        store = _KnowledgeStore()
        request = AuthoritativeImportRequest.model_validate(_authoritative_payload())
        errors: list[tuple[type[Exception], str]] = []
        for tenant in ("tenant-alpha", "tenant-other"):
            principal = Principal(
                "expert-1",
                tenant,
                frozenset({"engineers"}),
                frozenset({"knowledge:import"}),
            )
            with self.assertRaises(ResourceNotFoundError) as caught:
                self._adapter(_Driver(allowed=False), store).authoritative_import(
                    principal, request
                )
            errors.append((type(caught.exception), caught.exception.public_message))
        self.assertEqual(errors[0], errors[1])

    def test_authoritative_import_rejects_historical_chunk_evidence(self) -> None:
        driver = _Driver(active=False)
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        with self.assertRaises(ResourceNotFoundError):
            self._adapter(driver, _KnowledgeStore()).authoritative_import(
                principal,
                AuthoritativeImportRequest.model_validate(_authoritative_payload()),
            )
        query = driver.calls[0][0]
        self.assertIn("ACTIVE_VERSION", query)
        self.assertIn("ACTIVE_SNAPSHOT", query)
        self.assertIn("INCLUDES_CHUNK", query)
        self.assertIn("OF_VERSION", query)
        self.assertIn("build_state: 'PUBLISHED'", query)

    def test_evidence_dependency_failures_are_redacted_and_classified(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        request = AuthoritativeImportRequest.model_validate(_authoritative_payload())
        for failure, expected in (
            (TimeoutError("driver timed out with secret detail"), DependencyTimeoutError),
            (RuntimeError("driver failed with secret detail"), DependencyUnavailableError),
        ):
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected) as caught:
                    self._adapter(
                        _Driver(failure=failure), _KnowledgeStore()
                    ).authoritative_import(principal, request)
                self.assertNotIn("secret detail", caught.exception.public_message)

    def test_tbox_adapter_injects_tenant_and_keeps_publish_cas(self) -> None:
        tboxes = _TBoxes()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"ontology:read", "ontology:write", "ontology:publish"}),
        )
        adapter = self._adapter(_Driver(), _KnowledgeStore(), tboxes=tboxes)
        listed = adapter.ontology_list(
            principal, OntologyListRequest(key="industrial-assets", limit=1)
        )
        imported = adapter.ontology_import(
            principal,
            OntologyImportRequest(
                key="industrial-assets",
                version=1,
                entity_types=(
                    {
                        "name": "Asset",
                        "canonical_key_namespaces": ("asset-id",),
                    },
                ),
            ),
        )
        published = adapter.ontology_publish(
            principal,
            tboxes.value.tbox_id,
            OntologyPublishRequest(expected_active_tbox_id=None),
        )
        self.assertEqual(len(listed.payload.items), 1)
        self.assertEqual(imported.payload.status, "DRAFT")
        assert tboxes.imported is not None
        self.assertEqual(tboxes.imported[0].tenant_id, "tenant-alpha")
        self.assertEqual(published.payload.status, "PUBLISHED")
        self.assertEqual(
            tboxes.publish_call,
            ("tenant-alpha", tboxes.value.tbox_id, None),
        )

    def test_construct_review_and_publication_adapters_preserve_capabilities(self) -> None:
        construction = _Construction()
        reviews = _Reviews()
        publications = _Publications()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset(
                {"knowledge:construct", "knowledge:review", "knowledge:publish"}
            ),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=construction,
            reviews=reviews,
            publications=publications,
        )
        constructed = adapter.construct(
            principal,
            KnowledgeConstructionRequest.model_validate(_construct_payload()),
        )
        queued = adapter.review_queue(principal, ReviewQueueRequest(limit=5))
        reviewed = adapter.review_batch(
            principal,
            ReviewBatchRequest(
                decisions=(
                    {
                        "record_kind": "ENTITY_MENTION",
                        "record_id": "mention-1",
                        "expected_revision": 1,
                        "decision": "REJECTED",
                        "notes": "Wrong identity",
                    },
                )
            ),
        )
        published = adapter.publish(
            principal,
            PublicationRequest(approved_revision_ids=("revision-2",)),
        )
        rolled_back = adapter.rollback(
            principal,
            "publication-1",
            RollbackRequest(expected_active_publication_id="publication-2"),
        )
        history = adapter.history(principal, PublicationHistoryRequest(limit=5))
        self.assertEqual(constructed.payload.job_id, "job-1")
        assert construction.call is not None
        self.assertEqual(construction.call[0], principal)
        self.assertEqual(construction.call[1], b"Acme owns Pump-7.")
        self.assertEqual(queued.payload.items, ())
        self.assertEqual(reviewed.payload.outcomes[0].status, "REJECTED")
        assert reviews.batch_call is not None
        self.assertEqual(reviews.batch_call[1][0].reviewed_at, NOW)
        self.assertEqual(published.payload.publication_id, "publication-1")
        self.assertEqual(rolled_back.payload.publication_id, "publication-1")
        self.assertEqual(len(history.payload.items), 1)
        rollback = next(value for name, value in publications.calls if name == "rollback")
        self.assertEqual(rollback[0], principal)
        self.assertEqual(rollback[1], "publication-1")
        self.assertEqual(
            rollback[2]["expected_active_publication_id"],
            "publication-2",
        )

    def test_malformed_dependency_output_is_not_reported_as_client_input(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=_MalformedConstruction(),
        )
        with self.assertRaises(DependencyUnavailableError):
            adapter.construct(
                principal,
                KnowledgeConstructionRequest.model_validate(_construct_payload()),
            )

    def test_construction_failures_use_runtime_error_taxonomy(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )
        request = KnowledgeConstructionRequest.model_validate(_construct_payload())
        cases = (
            (TimeoutError("provider timeout detail"), DependencyTimeoutError),
            (RuntimeError("provider failure detail"), DependencyUnavailableError),
            (ValueError("malformed provider output"), DependencyUnavailableError),
            (DocumentParseError("invalid uploaded JSON"), RequestValidationError),
            (ConflictError(), ConflictError),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                adapter = self._adapter(
                    _Driver(),
                    _KnowledgeStore(),
                    construction=_FailingConstruction(failure),
                )
                with self.assertRaises(expected):
                    adapter.construct(principal, request)

    def test_rollback_unknown_and_cross_tenant_targets_are_indistinguishable(self) -> None:
        publications = _Publications()
        adapter = self._adapter(
            _Driver(), _KnowledgeStore(), publications=publications
        )
        errors: list[tuple[type[Exception], str]] = []
        for tenant in ("tenant-alpha", "tenant-other"):
            principal = Principal(
                "expert-1",
                tenant,
                frozenset({"engineers"}),
                frozenset({"knowledge:publish"}),
            )
            with self.assertRaises(ResourceNotFoundError) as caught:
                adapter.rollback(
                    principal,
                    "unknown-publication",
                    RollbackRequest(
                        expected_active_publication_id="active-publication"
                    ),
                )
            errors.append((type(caught.exception), caught.exception.public_message))
        self.assertEqual(errors[0], errors[1])


if __name__ == "__main__":
    unittest.main()
