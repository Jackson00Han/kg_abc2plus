"""Unit checks for the governed-knowledge physical-delete boundary."""

from __future__ import annotations

import unittest

from graphrag_prod.ingestion import IngestionConflict, Neo4jIngestionService


class _Result:
    def __init__(self, record: dict[str, bool] | None) -> None:
        self._record = record

    def single(self) -> dict[str, bool] | None:
        return self._record


class _Transaction:
    def __init__(self, record: dict[str, bool] | None) -> None:
        self.record = record
        self.query = ""
        self.parameters: dict[str, object] = {}

    def run(self, query: str, **parameters: object) -> _Result:
        self.query = query
        self.parameters = parameters
        return _Result(self.record)


def _guard_record(**overrides: bool) -> dict[str, bool]:
    record = {
        "governed_revision_reference": False,
        "knowledge_publication_reference": False,
        "relationship_property_reference": False,
        "knowledge_construction_reference": False,
    }
    record.update(overrides)
    return record


class GovernedDeleteGuardTests(unittest.TestCase):
    def test_legacy_document_without_governed_references_is_allowed(self) -> None:
        tx = _Transaction(_guard_record())

        Neo4jIngestionService._assert_no_governed_delete_references_tx(
            tx,
            "tenant-alpha",
            "document-alpha",
        )

        self.assertEqual(
            tx.parameters,
            {
                "tenant_id": "tenant-alpha",
                "document_id": "document-alpha",
            },
        )
        self.assertIn("tenant_id: $tenant_id", tx.query)
        self.assertIn("document_id: $document_id", tx.query)

    def test_each_governed_dependency_blocks_with_one_non_enumerating_error(self) -> None:
        fields = tuple(_guard_record())
        for field in fields:
            with self.subTest(field=field):
                tx = _Transaction(_guard_record(**{field: True}))
                with self.assertRaises(IngestionConflict) as raised:
                    Neo4jIngestionService._assert_no_governed_delete_references_tx(
                        tx,
                        "tenant-secret",
                        "document-secret",
                    )
                self.assertEqual(
                    str(raised.exception),
                    "physical deletion is unavailable for governed knowledge; "
                    "use logical retirement",
                )
                self.assertNotIn("tenant-secret", str(raised.exception))
                self.assertNotIn("document-secret", str(raised.exception))
                self.assertNotIn(field, str(raised.exception))

    def test_missing_guard_result_fails_closed(self) -> None:
        tx = _Transaction(None)

        with self.assertRaisesRegex(IngestionConflict, "logical retirement"):
            Neo4jIngestionService._assert_no_governed_delete_references_tx(
                tx,
                "tenant-alpha",
                "document-alpha",
            )


if __name__ == "__main__":
    unittest.main()
