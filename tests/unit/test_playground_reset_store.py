"""Fail-closed public-fixture caching before a local Playground reset."""

from __future__ import annotations

import copy
from dataclasses import asdict, replace
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from scripts import playground_reset_store, run_playground
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


class PlaygroundResetStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_dev_corpus_fixture()
        cls.bundles = tuple(bundle for plan in cls.fixture.plans for bundle in plan.bundles)

    def setUp(self):
        self.driver = Mock()
        profile = self.fixture.build.manifest["embedding_profile"]
        self.embedder = Mock(**{key: profile[key] for key in (
            "provider", "model", "revision", "dimensions", "normalization", "embedding_space_id",
        )})
        self.rows = []
        for bundle in self.bundles:
            embedding = bundle.embedding
            self.rows.append({
                "expected_chunk_id": bundle.chunk.chunk_id,
                "text": bundle.chunk.text,
                "checksum": bundle.chunk.checksum,
                "embedding": {**asdict(embedding), "vector_checksum": embedding.vector_checksum},
            })

    def _capture(self, *, rows=None, fixture=None):
        self.driver.execute_query.return_value = (self.rows if rows is None else rows, None, None)
        return playground_reset_store.capture_reset_embeddings(
            self.driver, "reset-test-database", fixture or self.fixture, self.embedder,
        )

    def test_capture_is_read_only_exact_and_cannot_call_a_provider(self):
        cached = self._capture(rows=list(reversed(self.rows)))
        self.assertEqual(len(cached.chunks), 120)
        self.assertEqual({chunk.tenant_id for chunk in cached.chunks}, {"tenant-alpha", "tenant-beta"})
        for key in ("provider", "model", "revision", "dimensions", "normalization", "embedding_space_id"):
            self.assertEqual(getattr(cached, key), getattr(self.embedder, key))
        texts = [self.bundles[-1].chunk.text, self.bundles[0].chunk.text, self.bundles[-1].chunk.text]
        self.assertEqual(cached.embed_documents(texts), [
            self.bundles[-1].embedding.vector, self.bundles[0].embedding.vector,
            self.bundles[-1].embedding.vector,
        ])
        with self.assertRaisesRegex(ValueError, "fixture"):
            cached.embed_documents([texts[0], "uploaded private business content"])
        with self.assertRaises(TypeError):
            cached.vectors[texts[0]] = (1.0,)
        self.rows[-1]["embedding"]["vector"] = [0.0] * cached.dimensions
        self.assertEqual(cached.embed_documents([texts[0]]), [self.bundles[-1].embedding.vector])
        self.embedder.embed_documents.assert_not_called()
        self.embedder.client.embeddings.create.assert_not_called()
        self.driver.session.assert_not_called()
        self.driver.execute_query.assert_called_once()
        call = self.driver.execute_query.call_args
        self.assertEqual(call.kwargs["database_"], "reset-test-database")
        self.assertEqual(call.kwargs["limit"], 121)
        self.assertEqual(call.kwargs["embedding_space_id"], cached.embedding_space_id)
        self.assertEqual(call.kwargs["chunks"], [
            {"chunk_id": bundle.chunk.chunk_id, "tenant_id": bundle.chunk.tenant_id}
            for bundle in self.bundles
        ])
        self.assertGreater(call.args[0].timeout, 0)
        self.assertNotRegex(call.args[0].text, r"\b(?:CREATE|SET|DELETE|REMOVE|MERGE)\b")

    def test_missing_extra_duplicate_and_foreign_chunk_coverage_are_rejected(self):
        duplicate = copy.deepcopy(self.rows)
        duplicate[-1] = copy.deepcopy(duplicate[0])
        foreign = copy.deepcopy(self.rows)
        foreign[-1]["expected_chunk_id"] = "uploaded-private-chunk"
        variants = (self.rows[:-1], self.rows + [self.rows[0]], duplicate, foreign)
        for rows in variants:
            with self.subTest(row_count=len(rows), last_id=rows[-1]["expected_chunk_id"]):
                with self.assertRaises(ValueError):
                    self._capture(rows=rows)
        self.embedder.embed_documents.assert_not_called()

    def test_source_profile_identity_and_corrupt_vectors_cannot_be_cached(self):
        mutations = (
            ("row", "text", "modified source"), ("row", "checksum", "0" * 64),
            ("embedding", "tenant_id", "tenant-beta"),
            ("embedding", "chunk_id", self.bundles[1].chunk.chunk_id),
            ("embedding", "embedding_id", "unrelated-embedding"),
            ("embedding", "embedding_space_id", "same-dimension-other-space"),
            ("embedding", "provider", "other-provider"), ("embedding", "model", "other-model"),
            ("embedding", "revision", "other-revision"),
            ("embedding", "normalization", "provider-default"),
            ("embedding", "dimensions", 64), ("embedding", "dimensions", 128.0),
            ("embedding", "dimensions", True), ("embedding", "vector_checksum", "0" * 64),
            ("embedding", "vector", []), ("embedding", "vector", [1.0]),
            ("embedding", "vector", [0.0] * 128),
            ("embedding", "vector", [True] * 128),
            ("embedding", "vector", [float("nan")] * 128),
            ("embedding", "vector", [float("inf")] * 128),
            ("embedding", "vector", [1e100] * 128),
        )
        self.assertEqual(self.bundles[0].chunk.tenant_id, "tenant-alpha")
        for target, field, value in mutations:
            with self.subTest(target=target, field=field, value_kind=type(value).__name__):
                rows = copy.deepcopy(self.rows)
                mapping = rows[0] if target == "row" else rows[0]["embedding"]
                mapping[field] = value
                with self.assertRaises(ValueError):
                    self._capture(rows=rows)
        rows = copy.deepcopy(self.rows)
        rows[0]["embedding"] = None
        with self.assertRaises(ValueError):
            self._capture(rows=rows)
        self.embedder.embed_documents.assert_not_called()

    def test_capture_requires_the_complete_locked_fixture_before_reading(self):
        truncated = replace(self.fixture, plans=self.fixture.plans[:-1])
        repeated = replace(self.fixture, plans=self.fixture.plans + self.fixture.plans[:1])
        for fixture in (truncated, repeated):
            with self.subTest(plan_count=len(fixture.plans)):
                self.driver.reset_mock()
                with self.assertRaises(ValueError):
                    self._capture(fixture=fixture)
                self.driver.execute_query.assert_not_called()

    def test_reset_revalidates_complete_cache_before_any_destructive_query(self):
        for corrupt in ("wrong_type", "partial", "changed_source", "bad_vector"):
            with self.subTest(corrupt=corrupt):
                cached = self._capture()
                if corrupt == "wrong_type":
                    cached = self.embedder
                elif corrupt == "partial":
                    # Deliberately simulate corrupted process memory; reset must
                    # recheck the locked fixture before deleting any stored data.
                    chunks = cached.chunks[:-1]
                    object.__setattr__(cached, "chunks", chunks)
                    object.__setattr__(cached, "vectors", MappingProxyType({
                        chunk.text: cached.vectors[chunk.text] for chunk in chunks
                    }))
                elif corrupt == "changed_source":
                    changed = copy.copy(cached.chunks[0])
                    object.__setattr__(changed, "text", "untrusted replacement")
                    object.__setattr__(cached, "chunks", (changed, *cached.chunks[1:]))
                else:
                    values = dict(cached.vectors)
                    values[cached.chunks[0].text] = (0.0,) * cached.dimensions
                    object.__setattr__(cached, "vectors", MappingProxyType(values))
                self.driver.reset_mock()
                with (
                    patch.object(run_playground, "_load_corpus") as load,
                    patch.object(run_playground, "_reuse_corpus") as reuse,
                ):
                    with self.assertRaises(ValueError):
                        playground_reset_store.reset_playground_corpus(self.driver, "reset-test-database", cached)
                    self.driver.execute_query.assert_not_called()
                    load.assert_not_called()
                    reuse.assert_not_called()

    def test_reset_reloads_cached_vectors_then_verifies_and_propagates_failures(self):
        cached = self._capture()
        for fail_stage in (None, "delete", "load", "verify"):
            with self.subTest(fail_stage=fail_stage):
                calls = []

                def stage(name):
                    def invoke(*args, **kwargs):
                        calls.append(name)
                        if fail_stage == name:
                            raise RuntimeError("deterministic reset failure")
                        return self.fixture
                    return invoke

                self.driver.reset_mock()
                self.driver.execute_query.side_effect = stage("delete")
                with (
                    patch.object(run_playground, "_load_corpus", side_effect=stage("load")) as load,
                    patch.object(run_playground, "_reuse_corpus", side_effect=stage("verify")) as reuse,
                ):
                    if fail_stage is None:
                        playground_reset_store.reset_playground_corpus(self.driver, "reset-test-database", cached)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "deterministic reset failure"):
                            playground_reset_store.reset_playground_corpus(self.driver, "reset-test-database", cached)
                    expected = ["delete", "load", "verify"]
                    if fail_stage is not None:
                        expected = expected[:expected.index(fail_stage) + 1]
                    self.assertEqual(calls, expected)
                    query = self.driver.execute_query.call_args.args[0]
                    self.assertIn("DETACH DELETE", query.text)
                    self.assertNotIn("DROP", query.text)
                    self.assertGreater(query.timeout, 0)
                    if "load" in calls:
                        load.assert_called_once_with(self.driver, "reset-test-database", cached)
                    if "verify" in calls:
                        reuse.assert_called_once_with(self.driver, "reset-test-database", cached)
        self.embedder.embed_documents.assert_not_called()
        self.embedder.client.embeddings.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
