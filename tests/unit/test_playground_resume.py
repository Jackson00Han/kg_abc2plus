"""Read-only restart checks for the already initialized local Playground."""

from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import run_playground
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


class PlaygroundResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_dev_corpus_fixture()

    def setUp(self):
        self.driver = Mock()
        self.embedder = run_playground._OpenAICompatibleEmbedder(
            Mock(), provider="dashscope-openai-compatible", model="text-embedding-v4",
            revision="api-v1", dimensions=64,
        )
        self.generations = [
            {
                "tenant_id": tenant, "generation_tenant_id": tenant,
                "embedding_space_id": self.embedder.embedding_space_id,
                "dimensions": 64, "similarity": "cosine",
                "index_name": f"index_{tenant}", "label_name": f"label_{tenant}",
            }
            for tenant in ("tenant-alpha", "tenant-beta")
        ]
        self.indexes = [
            {
                "name": item["index_name"], "type": "VECTOR", "entityType": "NODE",
                "state": "ONLINE", "labelsOrTypes": [item["label_name"]],
                "properties": ["vector"],
                "options": {"indexConfig": {
                    # Neo4j 5.26 SHOW INDEXES returns uppercase COSINE even
                    # when CREATE VECTOR INDEX was given lowercase cosine.
                    "vector.dimensions": 64, "vector.similarity_function": "COSINE",
                }},
            }
            for item in self.generations
        ]

    def _reuse(self, *, schema_errors=(), ready=True):
        with (
            patch.object(run_playground, "verify_schema", return_value=list(schema_errors)) as schema,
            patch.object(run_playground, "_Neo4jReadiness") as readiness,
            patch.object(run_playground, "_empty_database") as empty,
            patch.object(run_playground, "_load_corpus") as load,
            patch.object(run_playground, "apply_schema") as apply,
        ):
            readiness.return_value.check.return_value.payload = SimpleNamespace(
                status="ready" if ready else "not_ready",
            )
            try:
                return run_playground._reuse_corpus(self.driver, "neo4j", self.embedder)
            finally:
                schema.assert_called_once_with(self.driver, "neo4j")
                empty.assert_not_called()
                load.assert_not_called()
                apply.assert_not_called()
                self.driver.session.assert_not_called()
                self.embedder.client.embeddings.create.assert_not_called()

    def test_reuses_both_tenant_profiles_and_physical_indexes_without_writes(self):
        self.driver.execute_query.side_effect = [
            (self.generations, None, None), (self.indexes, None, None),
        ]
        fixture = self._reuse()
        self.assertEqual(fixture.build.manifest, self.fixture.build.manifest)
        self.assertEqual(self.driver.execute_query.call_count, 2)
        first, second = self.driver.execute_query.call_args_list
        self.assertEqual(first.kwargs["tenant_ids"], ["tenant-alpha", "tenant-beta"])
        self.assertEqual(first.kwargs["limit"], 3)
        self.assertEqual(first.args[0].timeout, 2.0)
        self.assertEqual(second.args[0].timeout, 2.0)
        self.assertIn("MATCH", first.args[0].text)
        self.assertTrue(second.args[0].text.strip().startswith("SHOW INDEXES"))

    def test_refuses_invalid_schema_or_unready_corpus_without_loading_data(self):
        for errors, ready in ((["missing constraint"], True), ([], False)):
            with self.subTest(errors=errors, ready=ready):
                with self.assertRaises(RuntimeError):
                    self._reuse(schema_errors=errors, ready=ready)
                self.driver.execute_query.assert_not_called()

    def test_rejects_changed_vector_space_tenant_or_generation_metadata(self):
        mutations = (
            {"embedding_space_id": "same-dimensions-different-model"},
            {"generation_tenant_id": "tenant-alpha"},
            {"dimensions": 128}, {"dimensions": True}, {"dimensions": 64.0},
            {"similarity": "euclidean"},
            {"index_name": "index_tenant-alpha"}, {"label_name": ""},
            {"tenant_id": "tenant-alpha"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.generations)
                rows[1].update(mutation)
                self.driver.reset_mock()
                self.driver.execute_query.side_effect = [(rows, None, None)]
                with self.assertRaisesRegex(RuntimeError, "incompatible"):
                    self._reuse()
                self.driver.execute_query.assert_called_once()
        for rows in ([], self.generations[:1], self.generations * 2):
            with self.subTest(rows=len(rows)):
                self.driver.execute_query.side_effect = [(rows, None, None)]
                with self.assertRaisesRegex(RuntimeError, "incompatible"):
                    self._reuse()

    def test_rejects_missing_or_mismatched_physical_index_configuration(self):
        mutations = (
            {"type": "RANGE"}, {"entityType": "RELATIONSHIP"},
            {"state": "POPULATING"}, {"labelsOrTypes": ["other_label"]},
            {"properties": ["other_vector"]},
            {"options": {"indexConfig": {"vector.similarity_function": "cosine"}}},
            {"options": {"indexConfig": {
                "vector.dimensions": 128, "vector.similarity_function": "cosine",
            }}},
            {"options": {"indexConfig": {
                "vector.dimensions": 64, "vector.similarity_function": "euclidean",
            }}},
            {"options": {"indexConfig": {
                "vector.dimensions": 64, "vector.similarity_function": None,
            }}},
            {"options": {"indexConfig": {
                "vector.dimensions": 64, "vector.similarity_function": ["COSINE"],
            }}},
        )
        variants = [self.indexes[:1], self.indexes * 2]
        for mutation in mutations:
            indexes = copy.deepcopy(self.indexes)
            indexes[1].update(mutation)
            variants.append(indexes)
        for indexes in variants:
            with self.subTest(indexes=indexes):
                self.driver.execute_query.side_effect = [
                    (self.generations, None, None), (indexes, None, None),
                ]
                with self.assertRaisesRegex(RuntimeError, "incompatible"):
                    self._reuse()

    def test_provider_outage_restart_preserves_validation_and_real_provider_operations(self):
        with (
            patch.object(run_playground, "_load_corpus") as load,
            patch.object(run_playground, "_reuse_corpus", return_value=self.fixture) as existing,
            patch.object(run_playground, "_warm_retrieval") as warm,
        ):
            app = run_playground.build_playground_app(
                self.driver, "neo4j", signing_key=b"unit-test-playground-signing-key-32-bytes",
                embedder=self.embedder, reuse_existing_corpus=True, skip_provider_warmup=True,
            )
            existing.assert_called_once_with(self.driver, "neo4j", self.embedder)
            load.assert_not_called()
            warm.assert_not_called()
            self.embedder.client.embeddings.create.assert_not_called()
            self.assertEqual(len(app.state.playground_gold_questions), 49)
        with self.assertRaisesRegex(ValueError, "verified existing corpus"):
            run_playground.build_playground_app(
                self.driver, "neo4j", signing_key=b"unit-test-playground-signing-key-32-bytes",
                embedder=self.embedder, skip_provider_warmup=True,
            )
        for args in (
            ["--skip-provider-warmup"],
            ["--skip-provider-warmup", "--reuse-existing-corpus", "--check"],
        ):
            with patch("sys.argv", ["run_playground", *args]), self.assertRaises(SystemExit):
                run_playground.main()

    def test_cli_and_app_require_explicit_reuse_and_preserve_default_bootstrap(self):
        self.assertFalse(run_playground._parser().parse_args([]).reuse_existing_corpus)
        self.assertTrue(run_playground._parser().parse_args([
            "--reuse-existing-corpus", "--port", "8002", "--no-open",
        ]).reuse_existing_corpus)
        for reuse in (False, True):
            with (
                self.subTest(reuse=reuse),
                patch.object(run_playground, "_load_corpus", return_value=self.fixture) as load,
                patch.object(run_playground, "_reuse_corpus", return_value=self.fixture) as existing,
                patch.object(run_playground, "_warm_retrieval") as warm,
            ):
                app = run_playground.build_playground_app(
                    self.driver, "neo4j", signing_key=b"unit-test-playground-signing-key-32-bytes",
                    embedder=self.embedder, reuse_existing_corpus=reuse,
                )
                selected, unused = (existing, load) if reuse else (load, existing)
                selected.assert_called_once_with(self.driver, "neo4j", self.embedder)
                unused.assert_not_called()
                warm.assert_called_once()
                self.assertEqual(len(app.state.playground_gold_questions), 49)
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            run_playground.build_playground_app(
                self.driver, "neo4j", signing_key=b"unit-test-playground-signing-key-32-bytes",
                embedder=self.embedder, reuse_existing_corpus="yes",
            )


if __name__ == "__main__":
    unittest.main()
