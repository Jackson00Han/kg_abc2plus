"""Tests for the versioned Neo4j schema definition."""

from __future__ import annotations

import re
import unittest

from graphrag_prod.graph.schema import (
    EXPECTED_SCHEMA,
    MIGRATION_PATH,
    migration_paths,
    migration_statements,
)


class SchemaMigrationTests(unittest.TestCase):
    def test_migration_is_single_source_for_expected_names(self) -> None:
        statements = migration_statements()
        names: set[str] = set()
        for statement in statements:
            if match := re.search(
                r"CREATE (?:FULLTEXT )?(?:CONSTRAINT|INDEX) (\w+)", statement
            ):
                names.add(match.group(1))
            elif match := re.search(r"DROP (?:CONSTRAINT|INDEX) (\w+)", statement):
                names.discard(match.group(1))
        self.assertEqual(names, {item.name for item in EXPECTED_SCHEMA})

    def test_every_statement_is_idempotent(self) -> None:
        for statement in migration_statements():
            if statement.startswith("DROP "):
                self.assertIn("IF EXISTS", statement)
            elif statement.startswith("MATCH "):
                self.assertIn("MERGE ", statement)
            else:
                self.assertIn("IF NOT EXISTS", statement)

    def test_migrations_are_loaded_in_filename_order(self) -> None:
        paths = migration_paths()
        self.assertEqual(paths, tuple(sorted(paths, key=lambda item: item.name)))
        self.assertEqual(
            [path.name for path in paths],
            [
                "001_provenance_schema.cypher",
                "002_incremental_ingestion_schema.cypher",
                "003_graph_governance_schema.cypher",
                "004_retrieval_schema.cypher",
                "005_retrieval_partition_schema.cypher",
                "006_property_graph_tbox_schema.cypher",
                "007_governed_abox_schema.cypher",
                "008_knowledge_review_publication_schema.cypher",
                "009_knowledge_construction_schema.cypher",
                "010_knowledge_publication_tbox_binding.cypher",
                "011_relationship_property_value_schema.cypher",
                "012_published_quality_history_schema.cypher",
            ],
        )
        statements = migration_statements()
        self.assertIn("DROP CONSTRAINT chunk_ordinal_unique IF EXISTS", statements)
        self.assertFalse(
            any(
                statement.startswith("CREATE CONSTRAINT chunk_ordinal_unique ")
                for statement in statements
            )
        )

    def test_original_migration_path_remains_usable(self) -> None:
        legacy_statements = migration_statements(MIGRATION_PATH)
        self.assertTrue(legacy_statements)
        self.assertFalse(
            any(statement.startswith("DROP ") for statement in legacy_statements)
        )
        self.assertTrue(
            any(
                statement.startswith("CREATE CONSTRAINT chunk_ordinal_unique ")
                for statement in legacy_statements
            )
        )
        self.assertLess(len(legacy_statements), len(migration_statements()))

    def test_publication_tbox_backfill_requires_every_revision_to_prove_binding(
        self,
    ) -> None:
        statement = next(
            value
            for value in migration_statements()
            if "proven_revision_count" in value
        )
        self.assertIn("revision_count > 0", statement)
        self.assertIn("proven_revision_count = revision_count", statement)
        self.assertIn(
            "revision.tenant_id = publication.tenant_id",
            statement,
        )
        self.assertIn("revision.ontology_version_id IS NOT NULL", statement)


if __name__ == "__main__":
    unittest.main()
