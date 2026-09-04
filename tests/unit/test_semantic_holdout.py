"""Checks for the independent, answer-free semantic retrieval holdout."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlparse

from graphrag_prod.evaluation.semantic_holdout import (
    QUESTION_CLASSES,
    evaluate_semantic_holdout,
    load_semantic_holdout,
    run_live_semantic_holdout,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "evaluation" / "semantic-holdout-v1" / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_artifacts() -> tuple[TemporaryDirectory[str], Path]:
    temporary = TemporaryDirectory()
    root = Path(temporary.name)
    shutil.copytree(
        ROOT / "datasets" / "dev-corpus-v1",
        root / "datasets" / "dev-corpus-v1",
    )
    shutil.copytree(
        ROOT / "evaluation" / "semantic-holdout-v1",
        root / "evaluation" / "semantic-holdout-v1",
    )
    return temporary, root


def _rewrite_questions(root: Path, mutator) -> Path:
    question_path = root / "evaluation" / "semantic-holdout-v1" / "questions.jsonl"
    rows = [
        json.loads(line)
        for line in question_path.read_text(encoding="utf-8").splitlines()
    ]
    mutator(rows)
    question_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    manifest_path = root / "evaluation" / "semantic-holdout-v1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact"]["sha256"] = _sha256(question_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


class SemanticHoldoutAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_semantic_holdout(MANIFEST, repository_root=ROOT)

    def test_checked_in_holdout_is_bound_complete_and_balanced(self) -> None:
        self.assertEqual(len(self.dataset.questions), 14)
        self.assertEqual(
            Counter(item.question_class for item in self.dataset.questions),
            Counter({question_class: 2 for question_class in QUESTION_CLASSES}),
        )
        self.assertEqual(
            {item.principal.tenant_id for item in self.dataset.questions},
            {"tenant-alpha", "tenant-beta"},
        )
        self.assertEqual(
            self.dataset.manifest["authorship"],
            "independently-authored-reviewed-holdout",
        )

    def test_questions_contain_evidence_ids_but_no_answers_or_vectors(self) -> None:
        rows = [
            json.loads(line)
            for line in (
                ROOT / "evaluation" / "semantic-holdout-v1" / "questions.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        prohibited = {
            "answer",
            "answers",
            "claims",
            "expected_answer",
            "query_embedding",
            "query_vector",
            "result",
            "vector",
            "vector_id",
        }
        self.assertTrue(all(not (set(row) & prohibited) for row in rows))
        self.assertFalse(any("$" in row["query"] or "%" in row["query"] for row in rows))
        self.assertTrue(
            all(
                item.required_evidence_chunk_ids
                for item in self.dataset.questions
                if item.answerable
            )
        )

    def test_source_evidence_exists_and_required_evidence_is_authorized(self) -> None:
        chunks = {
            row["chunk_id"]: row
            for row in (
                json.loads(line)
                for line in (
                    ROOT / "datasets" / "dev-corpus-v1" / "chunks.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
            )
        }
        for item in self.dataset.questions:
            with self.subTest(item=item.item_id):
                for chunk_id in item.required_evidence_chunk_ids:
                    chunk = chunks[chunk_id]
                    self.assertEqual(chunk["tenant_id"], item.principal.tenant_id)
                    self.assertTrue(
                        set(chunk["access_groups"]) & set(item.principal.groups)
                    )
                for chunk_id in item.forbidden_chunk_ids:
                    chunk = chunks[chunk_id]
                    authorized = (
                        chunk["tenant_id"] == item.principal.tenant_id
                        and bool(
                            set(chunk["access_groups"]) & set(item.principal.groups)
                        )
                    )
                    self.assertFalse(authorized)

    def test_duplicate_or_legacy_question_is_rejected(self) -> None:
        for mutation in ("duplicate-id", "legacy-query"):
            with self.subTest(mutation=mutation):
                temporary, root = _copy_artifacts()
                self.addCleanup(temporary.cleanup)

                def mutate(rows):
                    if mutation == "duplicate-id":
                        rows[1]["id"] = rows[0]["id"]
                    else:
                        rows[0]["query"] = "What was Northstar revenue in fiscal 2024?"

                manifest = _rewrite_questions(root, mutate)
                with self.assertRaisesRegex(ValueError, "unique|legacy"):
                    load_semantic_holdout(manifest, repository_root=root)

    def test_copied_source_prose_and_answer_values_are_rejected(self) -> None:
        queries = (
            "The amount excludes undrawn credit facilities, contracted backlog, "
            "and restricted balances described elsewhere.",
            "Locate the $2.1 billion disclosure.",
        )
        for query in queries:
            with self.subTest(query=query):
                temporary, root = _copy_artifacts()
                self.addCleanup(temporary.cleanup)
                manifest = _rewrite_questions(
                    root, lambda rows: rows[0].__setitem__("query", query)
                )
                with self.assertRaisesRegex(ValueError, "copies source|formatted"):
                    load_semantic_holdout(manifest, repository_root=root)

    def test_answer_and_query_vector_fields_are_rejected(self) -> None:
        for field, value in (("expected_answer", "secret"), ("query_vector", [1.0])):
            with self.subTest(field=field):
                temporary, root = _copy_artifacts()
                self.addCleanup(temporary.cleanup)

                def mutate(rows):
                    rows[0][field] = value

                manifest = _rewrite_questions(root, mutate)
                with self.assertRaisesRegex(ValueError, "answer/prediction"):
                    load_semantic_holdout(manifest, repository_root=root)

    def test_unknown_or_unauthorized_required_evidence_is_rejected(self) -> None:
        values = (
            ["unknown-chunk"],
            ["2be9ced2-d47d-56d2-bbca-1c5118e9c28d"],
        )
        for required in values:
            with self.subTest(required=required):
                temporary, root = _copy_artifacts()
                self.addCleanup(temporary.cleanup)
                manifest = _rewrite_questions(
                    root,
                    lambda rows: rows[0].__setitem__(
                        "required_evidence_chunk_ids", required
                    ),
                )
                with self.assertRaisesRegex(ValueError, "unknown|unauthorized"):
                    load_semantic_holdout(manifest, repository_root=root)

    def test_manifest_cannot_weaken_novelty_or_metric_gates(self) -> None:
        for section, field, value in (
            ("novelty", "maximum_legacy_sequence_similarity", 0.9),
            ("thresholds", "minimum_mrr", 0.2),
        ):
            with self.subTest(section=section):
                temporary, root = _copy_artifacts()
                self.addCleanup(temporary.cleanup)
                manifest_path = (
                    root / "evaluation" / "semantic-holdout-v1" / "manifest.json"
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[section][field] = value
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "too permissive"):
                    load_semantic_holdout(manifest_path, repository_root=root)


class SemanticHoldoutEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_semantic_holdout(MANIFEST, repository_root=ROOT)

    def _perfect_results(self) -> list[dict]:
        return [
            {
                "id": item.item_id,
                "selected_chunk_ids": list(reversed(item.required_evidence_chunk_ids)),
                "visible_chunk_ids": sorted(item.required_evidence_chunk_ids),
            }
            for item in self.dataset.questions
        ]

    def test_hand_computable_evidence_metrics_and_acl_exposure(self) -> None:
        results = self._perfect_results()
        unauthorized = next(
            item for item in self.dataset.questions if item.question_class == "unauthorized"
        )
        row = next(item for item in results if item["id"] == unauthorized.item_id)
        row["visible_chunk_ids"] = sorted(unauthorized.forbidden_chunk_ids)
        metrics = evaluate_semantic_holdout(self.dataset, results)
        self.assertEqual(metrics.item_count, 14)
        self.assertEqual(metrics.answerable_count, 10)
        self.assertEqual(metrics.unanswerable_count, 2)
        self.assertEqual(metrics.unauthorized_count, 2)
        self.assertEqual(metrics.complete_evidence_recall_at_5, 1.0)
        self.assertEqual(metrics.evidence_id_recall_at_5, 1.0)
        self.assertEqual(metrics.mrr, 1.0)
        self.assertEqual(
            metrics.forbidden_exposure_count,
            len(unauthorized.forbidden_chunk_ids),
        )

    def test_result_coverage_and_selected_order_are_strict(self) -> None:
        results = self._perfect_results()
        results.pop()
        with self.assertRaisesRegex(ValueError, "coverage"):
            evaluate_semantic_holdout(self.dataset, results)

        results = self._perfect_results()
        answerable = next(row for row in results if row["selected_chunk_ids"])
        answerable["selected_chunk_ids"] *= 2
        with self.assertRaisesRegex(ValueError, "unique"):
            evaluate_semantic_holdout(self.dataset, results)


class SemanticHoldoutLiveBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_semantic_holdout(MANIFEST, repository_root=ROOT)

    def _requester(self, provider: str, *, expose_forbidden: bool = False):
        calls: list[tuple[str, str, object, dict[str, str]]] = []
        scopes = sorted(
            {
                (item.principal.tenant_id, item.principal.groups)
                for item in self.dataset.questions
            }
        )
        personas = [
            {
                "id": f"persona-{index}",
                "tenant_id": tenant_id,
                "groups": list(groups),
                "scopes": ["retrieval:read"],
            }
            for index, (tenant_id, groups) in enumerate(scopes)
        ]
        persona_by_id = {item["id"]: item for item in personas}
        questions_by_query = {item.query: item for item in self.dataset.questions}

        def requester(method, url, *, payload, headers, timeout):
            del timeout
            path = urlparse(url).path
            calls.append((method, path, payload, dict(headers)))
            if path == "/playground/bootstrap":
                return {
                    "schema_version": "local-playground-bootstrap-v1",
                    "dataset": {
                        "id": "dev-corpus-v1",
                        "version": "1.0.1",
                        "embedding": {"provider": provider},
                    },
                    "capabilities": {"custom_semantic_retrieval": True},
                    "defaults": {"retrieval_limits": {"final_top_k": 5}},
                    "personas": personas,
                }
            if path == "/playground/session":
                self.assertEqual(method, "POST")
                persona_id = payload["persona_id"]
                self.assertIn(persona_id, persona_by_id)
                return {"access_token": f"token-{persona_id}"}
            if path == "/v1/retrieval":
                self.assertEqual(method, "POST")
                self.assertEqual(set(payload), {"limits", "query_text"})
                item = questions_by_query[payload["query_text"]]
                expected_persona = next(
                    persona
                    for persona in personas
                    if persona["tenant_id"] == item.principal.tenant_id
                    and tuple(persona["groups"]) == item.principal.groups
                )
                self.assertEqual(
                    headers,
                    {"Authorization": f"Bearer token-{expected_persona['id']}"},
                )
                selected = list(reversed(item.required_evidence_chunk_ids))
                trace = {
                    "tenant_id": item.principal.tenant_id,
                    "embedding_space_id": "dashscope:text-embedding-v4:live",
                    "selected_chunk_ids": selected,
                    "decisions": (
                        [{"chunk_id": item.forbidden_chunk_ids[0]}]
                        if expose_forbidden and item.forbidden_chunk_ids
                        else []
                    ),
                }
                for stage in (
                    "vector_recall",
                    "bm25_recall",
                    "seed_ranking",
                    "graph_expansion",
                    "candidate_vector_ranking",
                    "final_ranking",
                ):
                    trace[stage] = []
                return {
                    "trace": trace,
                    "chunks": [{"text": "TOP SECRET SOURCE TEXT"}],
                    "answer": "TOP SECRET GENERATED ANSWER",
                }
            self.fail(f"unexpected local path: {path}")

        return requester, calls

    def test_live_runner_sends_text_only_and_emits_no_source_or_answer_text(self) -> None:
        requester, calls = self._requester("dashscope-openai-compatible")
        report = run_live_semantic_holdout(self.dataset, requester=requester)
        self.assertTrue(report["passed"])
        self.assertEqual(report["metrics"]["evidence_id_recall_at_5"], 1.0)
        self.assertEqual(report["metrics"]["forbidden_exposure_count"], 0)
        retrieval_calls = [call for call in calls if call[1] == "/v1/retrieval"]
        self.assertEqual(len(retrieval_calls), len(self.dataset.questions))
        self.assertTrue(
            all(set(call[2]) == {"limits", "query_text"} for call in retrieval_calls)
        )
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("TOP SECRET", rendered)
        self.assertTrue(
            all(item.query not in rendered for item in self.dataset.questions)
        )

    def test_live_runner_rejects_fixture_embedding_provider(self) -> None:
        requester, calls = self._requester("deterministic-fixture")
        with self.assertRaisesRegex(RuntimeError, "allowed live embedding provider"):
            run_live_semantic_holdout(self.dataset, requester=requester)
        self.assertFalse(any(call[1] == "/v1/retrieval" for call in calls))

    def test_live_runner_counts_forbidden_ids_anywhere_in_trace(self) -> None:
        requester, _ = self._requester(
            "dashscope-openai-compatible", expose_forbidden=True
        )
        report = run_live_semantic_holdout(self.dataset, requester=requester)
        self.assertFalse(report["passed"])
        self.assertGreater(report["metrics"]["forbidden_exposure_count"], 0)
        self.assertIn(
            "forbidden_exposure_count above threshold", report["failures"]
        )

    def test_live_runner_accepts_only_explicit_loopback_origin(self) -> None:
        requester, _ = self._requester("dashscope-openai-compatible")
        for base_url in ("http://localhost:8000", "https://example.com:8000"):
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    run_live_semantic_holdout(
                        self.dataset,
                        base_url=base_url,
                        requester=requester,
                    )


if __name__ == "__main__":
    unittest.main()
