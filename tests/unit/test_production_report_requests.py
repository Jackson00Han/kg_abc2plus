"""Stage 9 report-builder request identity tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from graphrag_prod.evaluation.answers import CITATION_LOCATION_FIELDS
from graphrag_prod.evaluation.gates import EVALUATION_BASELINE_VERSION
from graphrag_prod.evaluation.quality_evidence import (
    build_http_answer_commitment,
)
from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from scripts.build_production_report import (
    BACKUP_DUMP_EVIDENCE_SCHEMA_VERSION,
    _access_evidence_projection,
    _bind_backup_dump,
    _canonical_graph_state_projection,
    _request_samples,
    _validate_stage8_report,
)
from scripts.build_load_corpus import build_manifest
from scripts.run_production_load import _queries
from scripts.run_production_load import (
    _access_isolation_fault,
    _expected_load_access,
    _validate_database_access,
)


ROOT = Path(__file__).parents[2]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _row(
    *,
    case_id: str,
    dataset_id: str,
    expected_chunk_ids: list[str],
    embedding_space_id: str,
    query_vector_checksum: str,
    index: int,
    kind: str,
    client_id: str | None = None,
    answer_evidence: dict | None = None,
) -> dict:
    selected = expected_chunk_ids[0]
    return {
        "answer_evidence": answer_evidence,
        "case_id": case_id,
        "client_id": client_id or f"{kind}-client-{index % 8}",
        "completed_monotonic_ms": float(index + 2),
        "dataset_id": dataset_id,
        "domain_failure_code": None,
        "domain_status": "answered" if kind == "answer" else None,
        "embedding_space_id": embedding_space_id,
        "error_code": None,
        "expected_chunk_ids": expected_chunk_ids,
        "inactive_chunk_ids": [],
        "inactive_version_count": 0,
        "kind": kind,
        "request_id": f"{kind}-{index:04d}",
        "query_vector_checksum": query_vector_checksum,
        "retrieval_stage_ms": 0.5,
        "selected_chunk_ids": [selected],
        "selected_chunk_count": 1,
        "semantic_success": True,
        "started_monotonic_ms": float(index + 1),
        "status_code": 200,
        "trace_id": f"trace-{kind}-{index:04d}",
        "unauthorized_chunk_ids": [],
        "unauthorized_chunk_count": 0,
        "visible_chunk_ids": [selected],
    }


def _answer_commitment(gold: dict) -> dict:
    evidence = {item["chunk_id"]: item for item in gold["evidence"]}
    labels = {
        chunk_id: f"S{index}"
        for index, chunk_id in enumerate(evidence, start=1)
    }
    claims = [
        {
            "citation_ids": [labels[item] for item in claim["evidence_chunk_ids"]],
            "inference": claim["inference"],
            "material": True,
            "text_sha256": "sha256:"
            + hashlib.sha256(claim["reference_text"].encode("utf-8")).hexdigest(),
        }
        for claim in gold["claims"]
    ]
    rendered = "\n".join(
        f"{'Inference: ' if claim['inference'] else ''}{gold_claim['reference_text']} "
        + " ".join(f"[{item}]" for item in claim["citation_ids"])
        for claim, gold_claim in zip(claims, gold["claims"])
    )
    actual = {
        "answer": rendered,
        "citations": [
            {
                "citation_id": labels[chunk_id],
                **{
                    field: (
                        source[field].replace("+00:00", "Z")
                        if field == "published_at"
                        else source[field]
                    )
                    for field in CITATION_LOCATION_FIELDS
                },
            }
            for chunk_id, source in evidence.items()
        ],
        "claims": [
            {
                "citation_ids": claim["citation_ids"],
                "inference": claim["inference"],
                "material": True,
                "text": gold_claim["reference_text"],
            }
            for claim, gold_claim in zip(claims, gold["claims"])
        ],
        "conflicts": [],
        "failure_code": None,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "answered",
    }
    return build_http_answer_commitment(actual)


def _fixture() -> tuple[dict, list[dict]]:
    config = _load(ROOT / "evaluation" / "production-reference-config.v1.json")
    questions = {
        item["id"]: item
        for item in map(
            json.loads,
            (ROOT / "evaluation" / "gold-v1" / "questions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }
    gold_answers = {
        item["id"]: item
        for item in map(
            json.loads,
            (ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }
    load_manifest = _load(ROOT / "datasets" / "load-v1" / "manifest.json")
    dev_manifest = _load(ROOT / "datasets" / "dev-corpus-v1" / "manifest.json")
    answer_vector_checksums = {
        item["id"]: hashlib.sha256(
            json.dumps(
                list(item["vector"]),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        for item in map(
            json.loads,
            (ROOT / "datasets" / "dev-corpus-v1" / "vectors.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
        if item["kind"] == "query"
    }
    load_queries = load_manifest["retrieval_workload"]["queries"]
    rows = []
    for client_index in range(8):
        for client_request_index in range(64):
            query = load_queries[(client_index + client_request_index) % 64]
            index = client_index * 64 + client_request_index
            rows.append(
                _row(
                    case_id=query["case_id"],
                    client_id=f"retrieval-{client_index:02d}",
                    dataset_id="load-v1",
                    embedding_space_id=query["embedding_space_id"],
                    expected_chunk_ids=query["expected_chunk_ids"],
                    query_vector_checksum=query["query_vector_checksum"],
                    index=index,
                    kind="retrieval",
                )
            )
    rows.extend(
        _row(
            case_id=case_id,
            dataset_id="gold-v1",
            embedding_space_id=dev_manifest["embedding_profile"][
                "embedding_space_id"
            ],
            expected_chunk_ids=sorted(questions[case_id]["relevance"]),
            query_vector_checksum=answer_vector_checksums[
                questions[case_id]["vector_id"]
            ],
            index=index + 512,
            kind="answer",
            answer_evidence=_answer_commitment(gold_answers[case_id]),
        )
        for index, case_id in enumerate(config["answer"]["gold_case_ids"])
    )
    return config, rows


class ProductionReportRequestTests(unittest.TestCase):
    def test_exact_configured_gold_cases_and_load_anchors_pass(self) -> None:
        config, rows = _fixture()
        result = _request_samples(rows, config)
        self.assertEqual(len(result["answer"]), 30)
        self.assertEqual(len(result["retrieval"]), 512)
        self.assertEqual(
            {item["dataset_id"] for item in result["answer"]}, {"gold-v1"}
        )

        baseline = _load(ROOT / "evaluation" / "baselines" / "dev-mini.v1.json")
        self.assertEqual(baseline["version"], EVALUATION_BASELINE_VERSION)
        projection = baseline["deterministic_projection"]
        report = {
            "case_digests": projection["case_digests"],
            "contract_metrics": [
                {"id": metric_id, "observed": observed}
                for metric_id, observed in projection["contract_metrics"].items()
            ],
            "diagnostics": projection["diagnostics"],
            "environment": {
                "git_commit": "a" * 40,
                "git_dirty": False,
                "python": "3.12.12",
            },
            "failures": [],
            "identities": projection["identities"],
            "passed": True,
            "production_candidate_eligible": False,
            "schema_version": "evaluation-report-v1",
            "semantic_digest": baseline["semantic_digest"],
            "suite_counts": {
                name: len(test_ids)
                for name, test_ids in projection["suite_passed_test_ids"].items()
            },
        }
        with TemporaryDirectory() as directory:
            suite_dir = Path(directory)
            for name, test_ids in projection["suite_passed_test_ids"].items():
                (suite_dir / f"{name}.json").write_text(
                    json.dumps(
                        {
                            "errors": [],
                            "expected_failures": [],
                            "failures": [],
                            "passed_test_ids": test_ids,
                            "schema_version": "unittest-suite-result-v1",
                            "skipped": [],
                            "tests_run": len(test_ids),
                            "unexpected_successes": [],
                        }
                    ),
                    encoding="utf-8",
                )
            _validate_stage8_report(report, suite_dir, "a" * 40)
            forged = deepcopy(report)
            forged["contract_metrics"][0]["observed"] = -1
            with self.assertRaisesRegex(ValueError, "semantic digest does not bind"):
                _validate_stage8_report(forged, suite_dir, "a" * 40)

            wrong_commit = deepcopy(report)
            wrong_commit["environment"]["git_commit"] = "b" * 40
            with self.assertRaisesRegex(ValueError, "does not match Stage 9"):
                _validate_stage8_report(wrong_commit, suite_dir, "a" * 40)

            dirty = deepcopy(report)
            dirty["environment"]["git_dirty"] = True
            with self.assertRaisesRegex(ValueError, "clean checkout"):
                _validate_stage8_report(dirty, suite_dir, "a" * 40)

    def test_gold_identity_expected_chunks_and_selection_are_fail_closed(self) -> None:
        config, rows = _fixture()
        mutations: list[tuple[str, list[dict], str]] = []

        wrong_dataset = deepcopy(rows)
        wrong_dataset[-1]["dataset_id"] = "load-v1"
        mutations.append(("dataset", wrong_dataset, "dataset must be gold-v1"))

        wrong_expected = deepcopy(rows)
        wrong_expected[-1]["expected_chunk_ids"] = ["forged-chunk"]
        wrong_expected[-1]["selected_chunk_ids"] = ["forged-chunk"]
        wrong_expected[-1]["visible_chunk_ids"] = ["forged-chunk"]
        mutations.append(("expected", wrong_expected, "expected Chunks drifted"))

        missed_expected = deepcopy(rows)
        missed_expected[-1]["selected_chunk_ids"] = ["different-chunk"]
        missed_expected[-1]["visible_chunk_ids"] = ["different-chunk"]
        mutations.append(("selection", missed_expected, "selected no expected Chunk"))

        wrong_embedding = deepcopy(rows)
        wrong_embedding[-1]["embedding_space_id"] = "forged-space"
        mutations.append(
            ("embedding", wrong_embedding, "embedding identity drifted")
        )

        wrong_retrieval_vector = deepcopy(rows)
        wrong_retrieval_vector[0]["query_vector_checksum"] = "0" * 64
        mutations.append(
            (
                "retrieval-vector",
                wrong_retrieval_vector,
                "embedding identity drifted",
            )
        )

        wrong_rotation = deepcopy(rows)
        for field in (
            "case_id",
            "embedding_space_id",
            "expected_chunk_ids",
            "query_vector_checksum",
            "selected_chunk_ids",
            "visible_chunk_ids",
        ):
            wrong_rotation[0][field], wrong_rotation[1][field] = (
                wrong_rotation[1][field],
                wrong_rotation[0][field],
            )
        mutations.append(("round-robin", wrong_rotation, "round-robin"))

        wrong_claim_commitment = deepcopy(rows)
        wrong_claim_commitment[-1]["answer_evidence"]["claims"][0][
            "text_sha256"
        ] = "sha256:" + "0" * 64
        mutations.append(
            (
                "answer-claim-commitment",
                wrong_claim_commitment,
                "claim commitment is not gold",
            )
        )

        wrong_location_commitment = deepcopy(rows)
        wrong_location_commitment[-1]["answer_evidence"]["citations"][0][
            "location_sha256"
        ] = "sha256:" + "0" * 64
        mutations.append(
            (
                "answer-location-commitment",
                wrong_location_commitment,
                "citation location is invalid",
            )
        )

        wrong_citation = deepcopy(rows)
        wrong_citation[-1]["answer_evidence"]["citations"][0][
            "citation_id"
        ] = "S99"
        mutations.append(
            (
                "answer-citation",
                wrong_citation,
                "unknown citations|all be referenced",
            )
        )

        for name, candidate, message in mutations:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                _request_samples(candidate, config)

    def test_configured_case_set_must_be_exactly_thirty_unique_cases(self) -> None:
        config, rows = _fixture()
        invalid = deepcopy(config)
        invalid["answer"]["gold_case_ids"][-1] = invalid["answer"][
            "gold_case_ids"
        ][0]
        with self.assertRaisesRegex(ValueError, "30 gold cases"):
            _request_samples(rows, invalid)


class ProductionReportBackupDumpTests(unittest.TestCase):
    def test_dump_bytes_are_copied_and_bound_to_observation(self) -> None:
        payload = b"neo4j-stage9-dump\x00fixture"
        digest = hashlib.sha256(payload).hexdigest()
        observation = {
            "backup_sha256": digest,
            "backup_size_bytes": len(payload),
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "neo4j.dump"
            destination = root / "report" / "evidence" / "backup_dump.dump"
            source.write_bytes(payload)

            manifest = _bind_backup_dump(source, destination, observation)

            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(destination.stat().st_size, len(payload))
            self.assertEqual(
                manifest,
                {
                    "path": "evidence/backup_dump.dump",
                    "record_count": 1,
                    "schema": BACKUP_DUMP_EVIDENCE_SCHEMA_VERSION,
                    "sha256": f"sha256:{digest}",
                },
            )

            source.write_bytes(payload + b"-tampered")
            with self.assertRaisesRegex(ValueError, "does not match"):
                _bind_backup_dump(
                    source,
                    root / "reproduction" / "evidence" / "backup_dump.dump",
                    observation,
                )

            graph_state_path = root / "graph-state.json"
            graph_state = {
                "business_node_count": 123,
                "business_relationship_count": 456,
                "label_counts": {"Document": 3, "Chunk": 120},
                "schema_and_indexes_verified": True,
                "sha256": "a" * 64,
            }
            graph_state_path.write_text(json.dumps(graph_state), encoding="utf-8")
            self.assertEqual(
                _canonical_graph_state_projection(graph_state_path),
                {
                    **graph_state,
                    "label_counts": {"Chunk": 120, "Document": 3},
                    "sha256": "sha256:" + "a" * 64,
                },
            )
            graph_state["schema_and_indexes_verified"] = False
            graph_state_path.write_text(json.dumps(graph_state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema was not verified"):
                _canonical_graph_state_projection(graph_state_path)


class ProductionLoadQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = build_manifest()

    def test_queries_replay_only_the_versioned_manifest_workload(self) -> None:
        queries = _queries()
        workload = self.manifest["retrieval_workload"]
        self.assertEqual(len(queries), workload["query_count"])
        self.assertEqual(
            [item["case_id"] for item in queries],
            [item["case_id"] for item in workload["queries"]],
        )
        self.assertEqual(
            [item["expected_chunk_ids"] for item in queries],
            [item["expected_chunk_ids"] for item in workload["queries"]],
        )
        self.assertEqual(
            [item["query_vector_checksum"] for item in queries],
            [item["query_vector_checksum"] for item in workload["queries"]],
        )
        self.assertEqual(
            [item["query_text"] for item in queries],
            [item["query_text"] for item in workload["queries"]],
        )

        sets, contract, probes = _expected_load_access(self.manifest)
        self.assertEqual(
            {name: len(values) for name, values in sets.items()},
            {
                "active": 12_000,
                "all": 24_000,
                "authorized": 7_500,
                "forbidden": 4_500,
                "inactive": 12_000,
            },
        )
        _validate_database_access(
            sets["all"] | {"unrelated-dev-corpus-chunk"},
            sets["authorized"],
            sets["inactive"],
            sets,
        )
        with self.assertRaisesRegex(RuntimeError, "authorization drifted"):
            _validate_database_access(
                sets["all"],
                sets["authorized"] | {next(iter(sets["forbidden"]))},
                sets["inactive"],
                sets,
            )

        trace_stages = (
            "vector_recall",
            "bm25_recall",
            "seed_ranking",
            "graph_expansion",
            "candidate_vector_ranking",
            "final_ranking",
        )
        trace_counter = 0
        probe_bodies: list[dict] = []

        def empty_response(request: httpx.Request) -> httpx.Response:
            nonlocal trace_counter
            trace_counter += 1
            body = json.loads(request.content)
            probe_bodies.append(body)
            trace = {
                **{stage: [] for stage in trace_stages},
                "decisions": [],
                "selected_chunk_ids": [],
                "version_filter": body["version_filter"],
            }
            return httpx.Response(
                200,
                headers={
                    "x-request-id": request.headers["x-request-id"],
                    "x-trace-id": f"access-trace-{trace_counter:02d}",
                },
                json={"chunks": [], "trace": trace},
            )

        config = _load(ROOT / "evaluation" / "production-reference-config.v1.json")
        with httpx.Client(
            base_url="http://stage9.invalid",
            transport=httpx.MockTransport(empty_response),
        ) as client:
            event = _access_isolation_fault(
                client=client,
                headers={"Authorization": "Bearer redacted-test-token"},
                config=config,
                contract=contract,
                probes=probes,
            )
        self.assertTrue(event["passed"])
        self.assertEqual(len(event["access_evidence"]["probes"]), 8)
        self.assertEqual(
            [body["version_filter"] for body in probe_bodies],
            [
                {"version_ids": [probe["version_id"]]}
                for probe in probes
            ],
        )
        projected = _access_evidence_projection(
            event["access_evidence"],
            self.manifest,
            event_started_ms=event["started_ns"] / 1_000_000,
            event_completed_ms=event["finished_ns"] / 1_000_000,
        )
        self.assertEqual(projected["inventory"], contract["inventory"])

    def test_query_vector_checksum_drift_is_rejected(self) -> None:
        manifest = deepcopy(self.manifest)
        manifest["retrieval_workload"]["queries"][0][
            "query_vector_checksum"
        ] = "0" * 64
        with patch(
            "scripts.run_production_load.build_manifest",
            return_value=manifest,
        ), self.assertRaisesRegex(RuntimeError, "vector checksum drifted"):
            _queries()

        text_drift = deepcopy(self.manifest)
        text_drift["retrieval_workload"]["queries"][0][
            "query_text"
        ] = "unbound synthetic query"
        with patch(
            "scripts.run_production_load.build_manifest",
            return_value=text_drift,
        ), self.assertRaisesRegex(RuntimeError, "not bound to its source Chunk"):
            _queries()


if __name__ == "__main__":
    unittest.main()
