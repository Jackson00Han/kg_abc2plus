"""Unit checks for the trusted batched initial-load boundary."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
import unittest
from unittest import mock

from graphrag_prod.domain import active_retrieval_scope
from graphrag_prod.ingestion import (
    InitialLoadResult,
    Neo4jBulkInitialLoader,
)
from graphrag_prod.ingestion.bulk import (
    _build_payload,
    _merge_rows,
    _unique_rows,
)
from graphrag_prod.ingestion.service import IngestionConflict
from tests.fixtures.ingestion import FIXED_TIME, FixedClock, make_plan


ROOT = Path(__file__).parents[2]


class _Result:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def single(self) -> dict[str, object]:
        return self.value


class _BatchTx:
    def __init__(self, *, compatible: int) -> None:
        self.compatible = compatible
        self.queries: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append((query, parameters))
        rows = parameters["rows"]
        assert isinstance(rows, list)
        return _Result(
            {
                "matched": len(rows),
                "compatible": self.compatible,
            }
        )


class _Session:
    def __init__(self, result: InitialLoadResult) -> None:
        self.result = result
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: object, *args: object) -> InitialLoadResult:
        self.calls.append((work, args))
        return self.result


class _Driver:
    def __init__(self, result: InitialLoadResult) -> None:
        self.session_value = _Session(result)
        self.databases: list[str] = []

    def session(self, *, database: str) -> _Session:
        self.databases.append(database)
        return self.session_value


class _NaiveClock:
    def now(self) -> datetime:
        return datetime(2025, 1, 1)


class BulkInitialLoadUnitTests(unittest.TestCase):
    def test_payload_is_bounded_deduplicated_and_provider_complete(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-unit")
        payload = _build_payload(plan, FIXED_TIME)

        self.assertEqual(len(payload.chunks), 3)
        self.assertEqual(len(payload.embeddings), 3)
        self.assertEqual(len(payload.entities), 1)
        self.assertEqual(len(payload.entity_memberships), 1)
        self.assertEqual(len(payload.mentions), 3)
        self.assertEqual(len(payload.assertions), 3)
        self.assertEqual(len(payload.findings), 0)
        self.assertEqual(
            payload.job["immutable"]["request_fingerprint"],
            plan.request_fingerprint,
        )
        self.assertEqual(
            payload.snapshot["immutable"]["manifest_hash"],
            plan.snapshot.manifest_hash,
        )
        for row in payload.embeddings:
            properties = row["properties"]
            self.assertTrue(properties["cosine_indexable"])
            self.assertEqual(
                len(properties["vector"]),
                properties["dimensions"],
            )
            self.assertEqual(len(properties["vector_checksum"]), 64)

    def test_preflight_rejects_missing_materialized_embedding(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-no-vector")
        stripped = tuple(
            dataclasses.replace(bundle, embedding=None)
            for bundle in plan.bundles
        )
        invalid = dataclasses.replace(plan, bundles=stripped)
        with self.assertRaisesRegex(ValueError, "embedding for every Chunk"):
            _build_payload(invalid, FIXED_TIME)

    def test_preflight_enforces_atomic_transaction_row_ceilings(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-limits")
        with mock.patch(
            "graphrag_prod.ingestion.bulk.MAX_CHUNKS_PER_PLAN",
            len(plan.bundles) - 1,
        ):
            with self.assertRaisesRegex(ValueError, "Chunks per plan"):
                _build_payload(plan, FIXED_TIME)
        with mock.patch(
            "graphrag_prod.ingestion.bulk.MAX_EMBEDDINGS_PER_PLAN",
            sum(len(bundle.all_embeddings) for bundle in plan.bundles) - 1,
        ):
            with self.assertRaisesRegex(ValueError, "ChunkEmbeddings per plan"):
                _build_payload(plan, FIXED_TIME)
        with mock.patch(
            "graphrag_prod.ingestion.bulk.MAX_GRAPH_ROWS_PER_PLAN",
            1,
        ):
            with self.assertRaisesRegex(ValueError, "graph rows per plan"):
                _build_payload(plan, FIXED_TIME)

    def test_replay_payload_binds_active_retrieval_partition(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-retrieval-scope")
        document_groups = frozenset({"knowledge-readers", "audit-readers"})
        chunk_groups = (
            frozenset({"knowledge-readers"}),
            frozenset({"audit-readers"}),
            frozenset({"knowledge-readers", "audit-readers"}),
        )
        plan = dataclasses.replace(
            plan,
            bundles=tuple(
                dataclasses.replace(
                    bundle,
                    document=dataclasses.replace(
                        bundle.document,
                        access_groups=document_groups,
                    ),
                    chunk=dataclasses.replace(
                        bundle.chunk,
                        access_groups=chunk_groups[index],
                    ),
                )
                for index, bundle in enumerate(plan.bundles)
            ),
        )
        payload = _build_payload(plan, FIXED_TIME)
        expected = {bundle.chunk.chunk_id: bundle.chunk for bundle in plan.bundles}
        for row in payload.chunks:
            chunk = expected[row["identifier"]]
            self.assertEqual(
                row["replay"]["access_groups"],
                sorted(chunk.access_groups),
            )
            self.assertEqual(
                row["replay"]["retrieval_scope"],
                active_retrieval_scope(chunk.tenant_id, chunk.access_groups),
            )

    def test_batch_merge_fails_closed_on_any_incompatible_stable_id(self) -> None:
        rows = (
            {
                "identifier": "one",
                "immutable": {"chunk_id": "one"},
                "absent": [],
                "properties": {"chunk_id": "one"},
                "replay": {"chunk_id": "one"},
                "replay_absent": [],
            },
            {
                "identifier": "two",
                "immutable": {"chunk_id": "two"},
                "absent": [],
                "properties": {"chunk_id": "two"},
                "replay": {"chunk_id": "two"},
                "replay_absent": [],
            },
        )
        tx = _BatchTx(compatible=1)
        with self.assertRaisesRegex(IngestionConflict, "stable ID"):
            _merge_rows(
                tx,
                label="Chunk",
                id_property="chunk_id",
                rows=rows,
            )
        self.assertEqual(len(tx.queries), 1)
        self.assertIn("UNWIND $rows", tx.queries[0][0])

    def test_conflicting_duplicate_batch_rows_are_rejected_before_io(self) -> None:
        base = {
            "identifier": "same",
            "immutable": {"chunk_id": "same", "text": "one"},
        }
        changed = {
            "identifier": "same",
            "immutable": {"chunk_id": "same", "text": "two"},
        }
        with self.assertRaisesRegex(ValueError, "conflicting duplicate Chunk"):
            _unique_rows((base, changed), kind="Chunk")

    def test_public_ingest_submits_exactly_one_write_transaction(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-call")
        expected = InitialLoadResult(
            job_id="job",
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            outcome="CREATED",
            corpus_revision=1,
            chunk_count=len(plan.bundles),
            embedding_count=len(plan.bundles),
        )
        for configured_timeout, expected_timeout in ((None, 60.0), (17.0, 17.0)):
            with self.subTest(transaction_timeout_seconds=configured_timeout):
                driver = _Driver(expected)
                kwargs = (
                    {}
                    if configured_timeout is None
                    else {"transaction_timeout_seconds": configured_timeout}
                )
                loader = Neo4jBulkInitialLoader(
                    driver,
                    "neo4j",
                    clock=FixedClock(),
                    **kwargs,
                )
                self.assertEqual(loader.ingest(plan), expected)
                self.assertEqual(driver.databases, ["neo4j"])
                self.assertEqual(len(driver.session_value.calls), 1)
                work, args = driver.session_value.calls[0]
                self.assertEqual(getattr(work, "timeout"), expected_timeout)
                self.assertEqual(
                    getattr(work, "metadata"),
                    {
                        "component": "graphrag-bulk-initial-load",
                        "operation": "document-version",
                    },
                )
                self.assertIs(args[0], plan)

    def test_constructor_and_clock_boundaries_fail_before_database_io(self) -> None:
        with self.assertRaisesRegex(ValueError, "driver"):
            Neo4jBulkInitialLoader(None)
        for invalid_timeout in (True, 0, -1, float("inf"), 301):
            with self.subTest(transaction_timeout_seconds=invalid_timeout):
                with self.assertRaisesRegex(ValueError, "transaction_timeout_seconds"):
                    Neo4jBulkInitialLoader(
                        object(),
                        transaction_timeout_seconds=invalid_timeout,
                    )
        plan = make_plan(tenant_id="tenant-bulk-clock")
        driver = _Driver(
            InitialLoadResult(
                "job",
                plan.tenant_id,
                plan.document_id,
                plan.version_id,
                plan.snapshot.snapshot_id,
                "CREATED",
                1,
                3,
                3,
            )
        )
        loader = Neo4jBulkInitialLoader(driver, clock=_NaiveClock())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            loader.ingest(plan)
        self.assertEqual(driver.databases, [])

    def test_production_module_never_imports_dataset_builder_scripts(self) -> None:
        source = (
            ROOT / "src" / "graphrag_prod" / "ingestion" / "bulk.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("scripts.", source)

    def test_bulk_refuses_to_clear_current_retirement_marker(self) -> None:
        source = (ROOT / "src" / "graphrag_prod" / "ingestion" / "bulk.py").read_text(
            encoding="utf-8"
        )
        for property_name in (
            "document.retirement_id",
            "document.retirement_request_fingerprint",
            "document.retired_at",
            "document.retired_by_principal_id",
            "document.retired_active_snapshot_id",
            "document.retired_active_version_id",
        ):
            self.assertIn(property_name, source)
        self.assertIn(
            "bulk initial load cannot clear managed retirement audit state",
            source,
        )
        self.assertNotIn("REMOVE document.retirement_id", source)
        self.assertNotIn("DELETE tombstone", source)


if __name__ == "__main__":
    unittest.main()
