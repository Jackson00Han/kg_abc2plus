"""Tests for the versioned Neo4j schema definition."""

from __future__ import annotations

import re
import unittest

from graphrag_prod.graph.schema import EXPECTED_SCHEMA, migration_statements


class SchemaMigrationTests(unittest.TestCase):
    def test_migration_is_single_source_for_expected_names(self) -> None:
        statements = migration_statements()
        names = {
            match.group(1)
            for statement in statements
            if (match := re.search(r"CREATE (?:CONSTRAINT|INDEX) (\w+)", statement))
        }
        self.assertEqual(names, {item.name for item in EXPECTED_SCHEMA})

    def test_every_statement_is_idempotent(self) -> None:
        for statement in migration_statements():
            self.assertIn("IF NOT EXISTS", statement)


if __name__ == "__main__":
    unittest.main()
