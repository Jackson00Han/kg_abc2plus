"""Atomic vector generation read-model tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self.record = record

    def single(self) -> dict[str, object] | None:
        return self.record


class _Session:
    def __init__(self, generation: dict[str, object]) -> None:
        self.generation = generation
        self.run_count = 0

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> _Result:
        self.run_count += 1
        if "ACTIVE_EMBEDDING_INDEX" not in query:
            raise AssertionError("active generation must use the pointer query")
        if parameters != {"tenant_id": self.generation["tenant_id"]}:
            raise AssertionError("active generation used unexpected parameters")
        return _Result({"generation": self.generation})


class _Driver:
    def __init__(self, generation: dict[str, object]) -> None:
        self.session_instance = _Session(generation)
        self.session_count = 0

    def session(self, *, database: str) -> _Session:
        self.session_count += 1
        if database != "neo4j":
            raise AssertionError("unexpected database")
        return self.session_instance


class EmbeddingIndexManagerTests(unittest.TestCase):
    def test_active_generation_reads_pointer_and_generation_in_one_query(self) -> None:
        generation = {
            "generation_id": "generation-1",
            "tenant_id": "tenant-a",
            "embedding_space_id": "space-1",
            "generation_version": 3,
            "index_name": "index_1",
            "label_name": "Label_1",
            "dimensions": 4,
            "similarity": "cosine",
            "state": "ACTIVE",
            "corpus_revision": 8,
        }
        driver = _Driver(generation)
        manager = Neo4jEmbeddingIndexManager(driver)

        with patch.object(
            manager,
            "get_generation",
            side_effect=AssertionError("must not split active read into another query"),
        ):
            active = manager.active_generation("tenant-a")

        self.assertIsNotNone(active)
        self.assertEqual(active.generation_id, generation["generation_id"])
        self.assertEqual(active.state, "ACTIVE")
        self.assertEqual(active.corpus_revision, 8)
        self.assertEqual(driver.session_count, 1)
        self.assertEqual(driver.session_instance.run_count, 1)


if __name__ == "__main__":
    unittest.main()
