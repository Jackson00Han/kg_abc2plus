"""Real bounded Cypher membership matrix, using only rolled-back synthetic nodes."""

from __future__ import annotations

import ipaddress
import os
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from neo4j import GraphDatabase

from graphrag_prod.knowledge.publication_guard import MAX_PUBLICATION_MANIFEST_RECORDS, publication_members_guard


class PublicationMemberGuardNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("only an explicitly disposable test database is allowed")
        uri = os.environ["TEST_NEO4J_URI"]
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError("only a loopback test database is allowed")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = GraphDatabase.driver(uri, auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]))

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def test_exact_membership_limits_and_legacy_source_only_compatibility(self):
        def member(identifier, **changes):
            return {"revision_id": identifier, "tenant_id": "guard-matrix", "ontology_version_id": "tbox",
                    "governance_status": "PUBLISHED", **changes}

        cases = [
            ("source-only", [], [], True),
            ("valid", ["a", "b"], [member("a"), member("b")], True),
            ("missing", ["a", "b"], [member("a")], False),
            ("extra", ["a"], [member("a"), member("b")], False),
            ("same-count-replacement", ["a", "b"], [member("a"), member("c")], False),
            ("duplicate-binding", ["a", "b"], [member("a"), member("a")], False),
            ("duplicate-manifest", ["a", "a"], [member("a"), member("b")], False),
            ("cross-tenant", ["a"], [member("a", tenant_id="other")], False),
            ("other-ontology", ["a"], [member("a", ontology_version_id="other")], False),
            ("candidate-revision", ["a"], [member("a", governance_status="CANDIDATE")], False),
            ("wrong-node-label", ["a"], [member("a", wrong_label=True)], False),
            ("missing-manifest", None, [], False),
            ("full-manifest-limit", [str(x) for x in range(MAX_PUBLICATION_MANIFEST_RECORDS)],
             [member(str(x)) for x in range(MAX_PUBLICATION_MANIFEST_RECORDS)], True),
            ("over-manifest-limit", [str(x) for x in range(MAX_PUBLICATION_MANIFEST_RECORDS + 1)],
             [member(str(x)) for x in range(MAX_PUBLICATION_MANIFEST_RECORDS + 1)], False),
        ]
        for name, manifest, members, expected in cases:
            with self.subTest(case=name), self.driver.session(database=self.database) as session:
                with session.begin_transaction(timeout=15.0) as tx:
                    fixture = uuid4().hex
                    tx.run("""
                        CREATE (p:KnowledgePublication {
                            publication_id:$fixture, tenant_id:$tenant_id,
                            ontology_version_id:'tbox', published_revision_ids:$manifest,
                            manifest_version:3, status:'ACTIVE'
                        })
                        WITH p UNWIND $members AS data
                        MERGE (member:PublicationGuardFixture {fixture_id:$fixture, member_id:data.revision_id})
                        SET member += data
                        FOREACH (_ IN CASE WHEN coalesce(data.wrong_label,false) THEN [] ELSE [1] END |
                            SET member:GovernedAssertionRevision)
                        CREATE (p)-[:PUBLISHES_KNOWLEDGE_REVISION]->(member)
                    """, fixture=fixture, tenant_id="guard-matrix", manifest=manifest, members=members).consume()
                    row = tx.run(
                        "MATCH (publication:KnowledgePublication {publication_id:$fixture}) RETURN "
                        + publication_members_guard("publication") + " AS complete",
                        fixture=fixture, tenant_id="guard-matrix",
                    ).single()
                    self.assertIs(row["complete"], expected)
                    tx.rollback()


if __name__ == "__main__":
    unittest.main()
