"""Unit tests for governed, audit-preserving document retirement."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any, Self

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import ingestion_job_id
from graphrag_prod.ingestion.retirement import (
    _LIST_ACTIVE_DOCUMENTS_QUERY,
    _LOCK_STATE_QUERY,
    _RETIRE_QUERY,
    DOCUMENT_RETIREMENT_OPERATION,
    DocumentRetirementBackendUnavailable,
    DocumentRetirementBlocked,
    DocumentRetirementConflict,
    DocumentRetirementRequest,
    DocumentRetirementUnavailable,
    Neo4jDocumentRetirementService,
)
from graphrag_prod.ingestion.service import IngestionConflict, Neo4jIngestionService

TENANT = "tenant-industrial"
DOCUMENT = "document-plant-manual"
SNAPSHOT = "snapshot-4"
VERSION = "version-4"
NOW = datetime(2026, 9, 4, 4, 30, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


def _principal(*, capable: bool = True) -> Principal:
    return Principal(
        principal_id="lifecycle-operator",
        tenant_id=TENANT,
        groups=frozenset({"plant-engineering", "plant-public"}),
        capabilities=(frozenset({"knowledge:lifecycle"}) if capable else frozenset()),
    )


def _request(
    *,
    operation_key: str = "retire-plant-manual-v4",
    snapshot_id: str = SNAPSHOT,
    generation: int = 4,
) -> DocumentRetirementRequest:
    return DocumentRetirementRequest(
        document_id=DOCUMENT,
        operation_key=operation_key,
        expected_active_snapshot_id=snapshot_id,
        source_generation=generation,
    )


def _active_state() -> dict[str, Any]:
    return {
        "corpus_revision": 12,
        "source_generation": 4,
        "lifecycle_status": None,
        "document_retirement_id": None,
        "document_retirement_request_fingerprint": None,
        "document_retired_at": None,
        "document_retired_by_principal_id": None,
        "document_retired_active_snapshot_id": None,
        "document_retired_active_version_id": None,
        "active_snapshot_pointer_count": 1,
        "active_snapshot_ids": [SNAPSHOT],
        "active_snapshot_version_ids": [VERSION],
        "active_snapshot_states": ["PUBLISHED"],
        "active_snapshot_tenants": [TENANT],
        "active_snapshot_documents": [DOCUMENT],
        "active_version_pointer_count": 1,
        "active_version_ids": [VERSION],
        "active_version_tenants": [TENANT],
        "active_version_documents": [DOCUMENT],
        "active_snapshot_version_binding_count": 1,
        "active_provenance_closed": True,
        "tombstone": None,
        "retirement_event": None,
        "active_publication_reference": False,
        "reviewable_revision_reference": False,
        "active_construction_reference": False,
        "active_ingestion_reference": False,
        "event_document_link_count": 0,
        "event_documents": [],
        "event_snapshot_link_count": 0,
        "event_snapshots": [],
        "event_version_link_count": 0,
        "event_versions": [],
        "tombstone_event_link_count": 0,
        "event_tombstones": [],
    }


def _mutation_result(retirement_id: str) -> dict[str, Any]:
    return {
        "retirement_id": retirement_id,
        "document_id": DOCUMENT,
        "retired_snapshot_id": SNAPSHOT,
        "retired_version_id": VERSION,
        "source_generation_before": 4,
        "source_generation_after": 5,
        "corpus_revision": 13,
        "retired_at": NOW,
    }


def _retirement_id(operation_key: str = "retire-plant-manual-v4") -> str:
    return ingestion_job_id(TENANT, DOCUMENT_RETIREMENT_OPERATION, operation_key)


def _replay_state() -> dict[str, Any]:
    request = _request()
    retirement_id = _retirement_id()
    # Capture the exact service-produced fingerprint from a dry first call.
    driver = _Driver([_Step("lock-state", _active_state()), _Step("retire", None)])
    with unittest.TestCase().assertRaises(DocumentRetirementConflict):
        Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
            _principal(), request
        )
    mutation_parameters = driver.transaction.calls[-1][1]
    fingerprint = mutation_parameters["request_fingerprint"]
    event = mutation_parameters["event_properties"]
    event = {
        **event,
        "corpus_revision": 13,
    }
    return {
        **_active_state(),
        "corpus_revision": 13,
        "source_generation": 5,
        "lifecycle_status": "RETIRED",
        "document_retirement_id": retirement_id,
        "document_retirement_request_fingerprint": fingerprint,
        "document_retired_at": NOW,
        "document_retired_by_principal_id": "lifecycle-operator",
        "document_retired_active_snapshot_id": SNAPSHOT,
        "document_retired_active_version_id": VERSION,
        "active_snapshot_pointer_count": 0,
        "active_snapshot_ids": [],
        "active_snapshot_version_ids": [],
        "active_snapshot_states": [],
        "active_snapshot_tenants": [],
        "active_snapshot_documents": [],
        "active_version_pointer_count": 0,
        "active_version_ids": [],
        "active_version_tenants": [],
        "active_version_documents": [],
        "active_snapshot_version_binding_count": 0,
        "tombstone": {
            "generation": 5,
            "lifecycle_status": "RETIRED",
            "retirement_id": retirement_id,
            "retirement_request_fingerprint": fingerprint,
            "retired_snapshot_id": SNAPSHOT,
            "retired_version_id": VERSION,
            "source_generation_before": 4,
            "retired_by_principal_id": "lifecycle-operator",
            "corpus_revision": 13,
            "retired_at": NOW,
        },
        "retirement_event": event,
        "event_document_link_count": 1,
        "event_documents": [
            {
                "tenant_id": TENANT,
                "document_id": DOCUMENT,
                "generation": 5,
                "lifecycle_status": "RETIRED",
                "retirement_id": retirement_id,
                "retirement_request_fingerprint": fingerprint,
                "retired_at": NOW,
                "retired_by_principal_id": "lifecycle-operator",
                "retired_active_snapshot_id": SNAPSHOT,
                "retired_active_version_id": VERSION,
            }
        ],
        "event_snapshot_link_count": 1,
        "event_snapshots": [
            {
                "tenant_id": TENANT,
                "document_id": DOCUMENT,
                "snapshot_id": SNAPSHOT,
                "version_id": VERSION,
                "build_state": "RETIRED",
                "retirement_id": retirement_id,
                "retired_at": NOW,
                "retired_by_principal_id": "lifecycle-operator",
            }
        ],
        "event_version_link_count": 1,
        "event_versions": [
            {
                "tenant_id": TENANT,
                "document_id": DOCUMENT,
                "version_id": VERSION,
                "lifecycle_status": "RETIRED",
                "retirement_id": retirement_id,
                "retired_at": NOW,
                "retired_by_principal_id": "lifecycle-operator",
            }
        ],
        "tombstone_event_link_count": 1,
        "event_tombstones": [
            {
                "tenant_id": TENANT,
                "document_id": DOCUMENT,
                "retirement_id": retirement_id,
            }
        ],
    }


class _Result:
    def __init__(self, row: dict[str, Any] | list[dict[str, Any]] | None) -> None:
        self.row = row

    def single(self) -> dict[str, Any] | None:
        if isinstance(self.row, list):
            raise TypeError("list result does not support single()")
        return self.row

    def __iter__(self):  # type: ignore[no-untyped-def]
        if self.row is None:
            return iter(())
        if isinstance(self.row, list):
            return iter(self.row)
        return iter((self.row,))


class _Step:
    def __init__(
        self,
        marker: str,
        row: dict[str, Any] | list[dict[str, Any]] | None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.marker = marker
        self.row = row
        self.error = error


class _Transaction:
    def __init__(self, steps: list[_Step]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **parameters: Any) -> _Result:
        if not isinstance(query, str):
            raise TypeError("managed transaction queries must be strings")
        if not self.steps:
            raise AssertionError("unexpected managed-transaction query")
        step = self.steps.pop(0)
        marker = f"governed-document-retirement:{step.marker}"
        if marker not in query:
            raise AssertionError(f"expected query marker {marker!r}")
        self.calls.append((query, parameters))
        if step.error is not None:
            raise step.error
        return _Result(step.row)


class _NoIODriver:
    def session(self, **_kwargs: object) -> Any:
        raise AssertionError("database I/O must not occur")


class _Session:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: Any, *args: Any) -> Any:
        self.driver.execute_write_calls += 1
        self.driver.work_metadata = dict(work.metadata)
        self.driver.work_timeout = work.timeout
        if self.driver.execute_error is not None:
            raise self.driver.execute_error
        return work(self.driver.transaction, *args)

    def execute_read(self, work: Any, *args: Any) -> Any:
        self.driver.execute_read_calls += 1
        self.driver.work_metadata = dict(work.metadata)
        self.driver.work_timeout = work.timeout
        if self.driver.execute_error is not None:
            raise self.driver.execute_error
        return work(self.driver.transaction, *args)


class _Driver:
    def __init__(
        self,
        steps: list[_Step],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self.transaction = _Transaction(steps)
        self.execute_error = execute_error
        self.databases: list[str] = []
        self.execute_write_calls = 0
        self.execute_read_calls = 0
        self.work_metadata: dict[str, str] | None = None
        self.work_timeout: float | None = None

    def session(self, *, database: str) -> _Session:
        self.databases.append(database)
        return _Session(self)


class DocumentRetirementTests(unittest.TestCase):
    def test_managed_reactivation_requires_complete_immutable_audit(self) -> None:
        Neo4jIngestionService._assert_reactivation_audit_state(
            {
                "lifecycle_status": "ACTIVE",
                "active_snapshot_id": SNAPSHOT,
                "active_version_id": VERSION,
            }
        )
        Neo4jIngestionService._assert_reactivation_audit_state(
            {
                "lifecycle_status": "RETIRED",
                "active_snapshot_id": None,
                "active_version_id": None,
                "retirement_id": "retirement-old",
                "retirement_request_fingerprint": "fingerprint-old",
                "retired_at": NOW,
                "retired_by_principal_id": "operator-old",
                "retired_active_snapshot_id": SNAPSHOT,
                "retired_active_version_id": VERSION,
                "retirement_audit_valid": True,
            }
        )
        invalid_states = (
            {
                "lifecycle_status": "ACTIVE",
                "active_snapshot_id": SNAPSHOT,
                "active_version_id": VERSION,
                "retirement_id": "unanchored-marker",
            },
            {
                "lifecycle_status": "RETIRED",
                "active_snapshot_id": None,
                "active_version_id": None,
                "retirement_id": "retirement-old",
                "retirement_audit_valid": False,
            },
        )
        for state in invalid_states:
            with self.subTest(state=state), self.assertRaises(IngestionConflict):
                Neo4jIngestionService._assert_reactivation_audit_state(state)

    def test_list_active_documents_is_tenant_scoped_bounded_and_metadata_only(
        self,
    ) -> None:
        driver = _Driver(
            [
                _Step(
                    "list-active",
                    [
                        {
                            "document_id": DOCUMENT,
                            "title": "Plant manual",
                            "source_name": "controlled-library",
                            "canonical_uri": "s3://industrial/plant-manual.pdf",
                            "source_generation": 4,
                            "active_snapshot_id": SNAPSHOT,
                            "active_version_id": VERSION,
                            "chunk_count": 31,
                            "access_policy_id": "plant-policy",
                            "access_policy_version": 3,
                            "access_groups": [
                                "plant-public",
                                "plant-engineering",
                            ],
                            "active_publication_reference": True,
                            "reviewable_revision_reference": False,
                            "active_construction_reference": True,
                            "active_ingestion_reference": False,
                        }
                    ],
                )
            ]
        )
        service = Neo4jDocumentRetirementService(driver, clock=_Clock())

        views = service.list_active_documents(_principal(), limit=17)

        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.tenant_id, TENANT)
        self.assertEqual(view.document_id, DOCUMENT)
        self.assertEqual(view.chunk_count, 31)
        self.assertEqual(
            view.access_groups,
            ("plant-engineering", "plant-public"),
        )
        self.assertEqual(
            view.blocker_codes,
            ("ACTIVE_KNOWLEDGE_PUBLICATION", "ACTIVE_CONSTRUCTION_JOB"),
        )
        self.assertTrue(view.blocked)
        self.assertEqual(driver.execute_read_calls, 1)
        self.assertEqual(driver.execute_write_calls, 0)
        query, parameters = driver.transaction.calls[0]
        self.assertIsInstance(query, str)
        self.assertEqual(parameters["tenant_id"], TENANT)
        self.assertEqual(parameters["limit"], 17)
        self.assertNotIn("chunk.text", query)
        self.assertNotIn("normalized_text", query)
        self.assertGreaterEqual(query.count("$principal_groups"), 3)

    def test_list_active_documents_capability_and_limit_fail_before_io(self) -> None:
        service = Neo4jDocumentRetirementService(_NoIODriver(), clock=_Clock())
        with self.assertRaises(DocumentRetirementUnavailable):
            service.list_active_documents(_principal(capable=False))
        for invalid in (True, 0, 101, -1, 1.5):
            with (
                self.subTest(limit=invalid),
                self.assertRaises((TypeError, ValueError)),
            ):
                service.list_active_documents(_principal(), limit=invalid)  # type: ignore[arg-type]

    def test_list_query_excludes_partial_acl_and_broken_active_provenance(self) -> None:
        for token in (
            "governed-document-retirement:list-active",
            "document_chunk.access_groups",
            "owned_chunk.access_groups",
            "snapshot_member.access_groups",
            "HAS_VERSION",
            "INCLUDES_CHUNK",
            "ACTIVE_KNOWLEDGE_PUBLICATION",
            "CURRENT_REVISION",
            "KnowledgeConstructionJob",
        ):
            self.assertIn(token, _LIST_ACTIVE_DOCUMENTS_QUERY)
        self.assertNotIn("chunk.text", _LIST_ACTIVE_DOCUMENTS_QUERY)
        self.assertNotIn("normalized_text", _LIST_ACTIVE_DOCUMENTS_QUERY)

    def test_success_is_one_bounded_transaction_and_returns_stable_audit_result(
        self,
    ) -> None:
        retirement_id = _retirement_id()
        driver = _Driver(
            [
                _Step("lock-state", _active_state()),
                _Step("retire", _mutation_result(retirement_id)),
            ]
        )
        service = Neo4jDocumentRetirementService(
            driver,
            "neo4j",
            clock=_Clock(),
            transaction_timeout_seconds=8.5,
        )

        result = service.retire(_principal(), _request())

        self.assertEqual(result.retirement_id, retirement_id)
        self.assertEqual(result.tenant_id, TENANT)
        self.assertEqual(result.document_id, DOCUMENT)
        self.assertEqual(result.retired_snapshot_id, SNAPSHOT)
        self.assertEqual(result.retired_version_id, VERSION)
        self.assertEqual(result.source_generation_before, 4)
        self.assertEqual(result.source_generation_after, 5)
        self.assertEqual(result.corpus_revision, 13)
        self.assertEqual(result.retired_at, NOW)
        self.assertEqual(result.status, "RETIRED")
        self.assertEqual(driver.databases, ["neo4j"])
        self.assertEqual(driver.execute_write_calls, 1)
        self.assertEqual(driver.work_timeout, 8.5)
        self.assertEqual(
            driver.work_metadata,
            {
                "component": "graphrag-document-retirement",
                "operation": "logical-retirement",
            },
        )
        self.assertFalse(driver.transaction.steps)

        state_parameters = driver.transaction.calls[0][1]
        self.assertEqual(state_parameters["tenant_id"], TENANT)
        self.assertEqual(
            state_parameters["principal_groups"],
            ["plant-engineering", "plant-public"],
        )
        mutation_parameters = driver.transaction.calls[1][1]
        self.assertEqual(mutation_parameters["next_generation"], 5)
        self.assertEqual(mutation_parameters["event_properties"]["status"], "SUCCEEDED")
        self.assertEqual(mutation_parameters["event_properties"]["outcome"], "RETIRED")
        self.assertEqual(
            mutation_parameters["event_properties"]["retired_by_principal_id"],
            "lifecycle-operator",
        )
        self.assertEqual(
            mutation_parameters["event_properties"]["source_generation_after"], 5
        )

    def test_exact_retry_returns_original_result_without_advancing_revision(
        self,
    ) -> None:
        state = _replay_state()
        driver = _Driver([_Step("lock-state", state)])

        result = Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
            _principal(), _request()
        )

        self.assertEqual(result.retirement_id, _retirement_id())
        self.assertEqual(result.source_generation_after, 5)
        self.assertEqual(result.corpus_revision, 13)
        self.assertEqual(result.retired_at, NOW)
        self.assertEqual(len(driver.transaction.calls), 1)

    def test_missing_capability_absent_and_partial_acl_share_public_failure(
        self,
    ) -> None:
        with self.assertRaises(DocumentRetirementUnavailable) as capability_error:
            Neo4jDocumentRetirementService(_NoIODriver(), clock=_Clock()).retire(
                _principal(capable=False), _request()
            )
        absent_driver = _Driver([_Step("lock-state", None)])
        with self.assertRaises(DocumentRetirementUnavailable) as absent_error:
            Neo4jDocumentRetirementService(absent_driver, clock=_Clock()).retire(
                _principal(), _request()
            )
        # Neo4j deliberately returns no row for cross-tenant IDs and for any
        # document whose every Chunk is not visible to this principal.
        partial_acl_driver = _Driver([_Step("lock-state", None)])
        with self.assertRaises(DocumentRetirementUnavailable) as acl_error:
            Neo4jDocumentRetirementService(partial_acl_driver, clock=_Clock()).retire(
                _principal(), _request()
            )

        messages = {
            str(capability_error.exception),
            str(absent_error.exception),
            str(acl_error.exception),
        }
        self.assertEqual(messages, {"document retirement target is unavailable"})

    def test_cas_and_active_path_corruption_fail_before_mutation(self) -> None:
        mutations = {
            "snapshot": {"active_snapshot_ids": ["snapshot-new"]},
            "generation": {"source_generation": 5},
            "duplicate-pointer": {"active_snapshot_pointer_count": 2},
            "binding": {"active_snapshot_version_binding_count": 0},
            "state": {"active_snapshot_states": ["STAGED"]},
            "tenant": {"active_version_tenants": ["tenant-other"]},
            "provenance": {"active_provenance_closed": False},
        }
        for name, changes in mutations.items():
            with self.subTest(name=name):
                state = {**_active_state(), **changes}
                driver = _Driver([_Step("lock-state", state)])
                with self.assertRaises(DocumentRetirementConflict):
                    Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                        _principal(), _request()
                    )
                self.assertEqual(len(driver.transaction.calls), 1)

    def test_each_live_governance_or_work_reference_blocks_retirement(self) -> None:
        for blocker in (
            "active_publication_reference",
            "reviewable_revision_reference",
            "active_construction_reference",
            "active_ingestion_reference",
        ):
            with self.subTest(blocker=blocker):
                state = _active_state()
                state[blocker] = True
                driver = _Driver([_Step("lock-state", state)])
                with self.assertRaises(DocumentRetirementBlocked):
                    Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                        _principal(), _request()
                    )
                self.assertEqual(len(driver.transaction.calls), 1)

    def test_each_tampered_immutable_replay_binding_fails_closed(self) -> None:
        corruptions = (
            ("event-idempotency", "retirement_event", "idempotency_key", "other"),
            (
                "event-snapshot",
                "retirement_event",
                "target_snapshot_id",
                "snapshot-tampered",
            ),
            (
                "event-version",
                "retirement_event",
                "target_version_id",
                "version-tampered",
            ),
            (
                "event-actor",
                "retirement_event",
                "retired_by_principal_id",
                "other-operator",
            ),
            ("tombstone-revision", "tombstone", "corpus_revision", 999),
        )
        for name, container, key, value in corruptions:
            with self.subTest(name=name):
                state = _replay_state()
                state[container] = {**state[container], key: value}
                driver = _Driver([_Step("lock-state", state)])

                with self.assertRaises(DocumentRetirementConflict):
                    Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                        _principal(), _request()
                    )
                self.assertEqual(len(driver.transaction.calls), 1)

    def test_mutation_recheck_failure_rolls_back_as_conflict(self) -> None:
        driver = _Driver([_Step("lock-state", _active_state()), _Step("retire", None)])
        with self.assertRaises(DocumentRetirementConflict):
            Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                _principal(), _request()
            )
        self.assertEqual(len(driver.transaction.calls), 2)

    def test_clean_reactivated_document_can_retire_with_an_older_tombstone(
        self,
    ) -> None:
        state = _active_state()
        state.update(
            {
                "lifecycle_status": "ACTIVE",
                "tombstone": {
                    "generation": 3,
                    "lifecycle_status": "RETIRED",
                    "retirement_id": "older-retirement",
                    "retirement_request_fingerprint": "older-fingerprint",
                    "retired_snapshot_id": "snapshot-2",
                    "retired_version_id": "version-2",
                    "source_generation_before": 2,
                    "corpus_revision": 8,
                    "retired_at": datetime(2026, 8, 1, tzinfo=UTC),
                },
            }
        )
        retirement_id = _retirement_id("new-retirement-cycle")
        driver = _Driver(
            [
                _Step("lock-state", state),
                _Step("retire", _mutation_result(retirement_id)),
            ]
        )

        result = Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
            _principal(), _request(operation_key="new-retirement-cycle")
        )

        self.assertEqual(result.retirement_id, retirement_id)
        self.assertEqual(len(driver.transaction.calls), 2)

    def test_active_pointer_with_current_retirement_projection_is_conflicted(
        self,
    ) -> None:
        state = {
            **_active_state(),
            "lifecycle_status": "RETIRED",
            "document_retirement_id": "older-retirement",
            "document_retirement_request_fingerprint": "older-fingerprint",
            "document_retired_at": NOW,
            "document_retired_by_principal_id": "older-operator",
            "document_retired_active_snapshot_id": SNAPSHOT,
            "document_retired_active_version_id": VERSION,
        }
        driver = _Driver([_Step("lock-state", state)])

        with self.assertRaises(DocumentRetirementConflict):
            Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                _principal(), _request(operation_key="new-retirement-cycle")
            )
        self.assertEqual(len(driver.transaction.calls), 1)

    def test_backend_details_are_sanitized(self) -> None:
        driver = _Driver(
            [], execute_error=RuntimeError("bolt://user:secret@internal-host")
        )
        with self.assertRaises(DocumentRetirementBackendUnavailable) as raised:
            Neo4jDocumentRetirementService(driver, clock=_Clock()).retire(
                _principal(), _request()
            )

        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("internal-host", repr(raised.exception))

    def test_query_contract_preserves_audit_data_and_rechecks_every_gate(self) -> None:
        combined = f"{_LOCK_STATE_QUERY}\n{_RETIRE_QUERY}"
        for token in (
            "TenantCorpusState",
            "KnowledgePublicationState",
            "ACTIVE_KNOWLEDGE_PUBLICATION",
            "PUBLISHES_KNOWLEDGE_REVISION",
            "USES_KNOWLEDGE_SNAPSHOT",
            "CURRENT_REVISION",
            "ACTIVE_SNAPSHOT",
            "ACTIVE_VERSION",
            "DocumentTombstone",
            "KnowledgeConstructionJob",
            "ACTIVE_EMBEDDING_INDEX",
            "embedding_generation.state = 'STALE'",
            "document.generation = $source_generation",
            "REMOVE chunk.retrieval_scope",
            "DELETE snapshot_pointer, version_pointer",
            "HAS_RETIREMENT_EVENT",
            "RETIRED_DOCUMENT",
            "RETIRED_SNAPSHOT",
            "RETIRED_VERSION",
            "active_provenance_closed",
            "$principal_groups",
            "$blocking_review_statuses",
        ):
            self.assertIn(token, combined)
        self.assertGreaterEqual(combined.count("access_groups"), 4)
        self.assertGreaterEqual(combined.count("PUBLISHES_KNOWLEDGE_REVISION"), 2)
        self.assertGreaterEqual(combined.count("CURRENT_REVISION"), 2)
        self.assertNotIn("DETACH DELETE", combined)
        self.assertNotIn("DELETE document", combined)
        self.assertNotIn("DELETE chunk", combined)
        self.assertNotIn("chunk.text", combined)
        self.assertNotIn("evidence_text", combined)

    def test_invalid_requests_configuration_and_clock_fail_before_io(self) -> None:
        for value in (True, -1, 1.2):
            with self.subTest(generation=value), self.assertRaises(ValueError):
                DocumentRetirementRequest(
                    DOCUMENT,
                    "operation",
                    SNAPSHOT,
                    value,  # type: ignore[arg-type]
                )
        for value in (True, 0, -1, float("inf"), 301):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                Neo4jDocumentRetirementService(
                    _NoIODriver(), transaction_timeout_seconds=value
                )

        class _NaiveClock:
            def now(self) -> datetime:
                return datetime(2026, 9, 4)  # noqa: DTZ001 - boundary fixture

        with self.assertRaises(ValueError):
            Neo4jDocumentRetirementService(_NoIODriver(), clock=_NaiveClock()).retire(
                _principal(), _request()
            )


if __name__ == "__main__":
    unittest.main()
