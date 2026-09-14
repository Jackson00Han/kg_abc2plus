"""Dual evidence publication and source boundaries on an owned disposable DB."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from graphrag_prod.graph.published_quality import Neo4jPublishedGraphQualityService
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from graphrag_prod.graph.browse_models import GRAPH_READ_CAPABILITY, GraphBrowseQuery
from graphrag_prod.retrieval.subgraph import Neo4jEvidenceSubgraphProjector
from graphrag_prod.api.backend import _subgraph_payload
from graphrag_prod.api.contracts import EvidenceSubgraphResponse
from graphrag_prod.api.graph_contracts import GraphEvidenceResponseEnvelope
from graphrag_prod.knowledge.review import KnowledgeReviewUnavailable, Neo4jKnowledgePublicationService
from graphrag_prod.knowledge.review_context import evidence_context_tx
from graphrag_prod.knowledge.store import KnowledgeEvidenceError, Neo4jKnowledgeStore, _stored_assertion
from graphrag_prod.knowledge.trust import GovernanceStatus
from tests.integration import test_context_projection_neo4j as fixture
from tests.integration import test_construction_workflow_neo4j as construction


class ContextContractNeo4jTests(unittest.TestCase):
    setUpClass = classmethod(construction.Neo4jConstructionWorkflowIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(construction.Neo4jConstructionWorkflowIntegrationTests.tearDownClass.__func__)
    setUp = fixture.ContextProjectionNeo4jTests.setUp
    tearDown = construction.Neo4jConstructionWorkflowIntegrationTests.tearDown
    construct = fixture.ContextProjectionNeo4jTests.construct
    query = fixture.ContextProjectionNeo4jTests.query
    errors = fixture.ContextProjectionNeo4jTests.errors
    run_review = fixture.ContextProjectionNeo4jTests.run_review
    _activate_source_index = construction.Neo4jConstructionWorkflowIntegrationTests._activate_source_index

    def contexts(self):
        return [_stored_assertion(row["revision"]) for row in self.query("""
            MATCH (:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r:GovernedAssertionRevision)
            WHERE r.context_property_evidence_json IS NOT NULL RETURN r{.*} AS revision
            ORDER BY r.context_char_start,r.record_id
        """)]

    def read_evidence(self, record, role):
        request = SimpleNamespace(record_id=record.record_id, expected_revision=record.revision.revision,
            evidence_role=role, view="paragraph", offset=0)
        with self.driver.session(database=self.database) as session:
            return session.execute_read(evidence_context_tx, self.principal, request)

    def test_context_publication_materialization_quality_and_both_source_roles(self):
        job = self.construct(count=3)
        self.run_review(job)
        contexts = self.contexts()
        self.assertEqual(len(contexts), 3)
        self.assertTrue(any(r.evidence.chunk_id != r.context_property_evidence.value_evidence.chunk_id for r in contexts))
        for record in contexts:
            primary, value = self.read_evidence(record, "PRIMARY"), self.read_evidence(record, "CONTEXT_VALUE")
            self.assertEqual(primary["quoted_text"], record.evidence.quoted_text)
            self.assertEqual(value["quoted_text"], record.literal_value)
            self.assertEqual(value["chunk_id"], record.context_property_evidence.value_evidence.chunk_id)
        self._activate_source_index(job)
        principal = replace(self.principal, capabilities=self.principal.capabilities | {"knowledge:publish", "knowledge:quality"})
        ids = tuple(row["id"] for row in self.query("""MATCH (:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r)
            WHERE r.governance_status='APPROVED' RETURN r.revision_id AS id"""))
        publication = Neo4jKnowledgePublicationService(self.driver, self.database).publish(principal, ids,
            expected_active_publication_id=None, published_at=datetime.now(timezone.utc))
        self.assertTrue(publication.published_revision_ids)
        quality = Neo4jPublishedGraphQualityService(self.driver, self.database).audit(principal)
        self.assertEqual(quality.issues, ())
        linked = self.query("""MATCH (a:Assertion)-[:CONTEXT_EVIDENCED_BY]->(c:Chunk)
            RETURN count(a) AS n""")
        self.assertEqual(linked[0]["n"], 3)
        selected_chunks = tuple(c.chunk_id for c in job.chunks)
        projector = Neo4jEvidenceSubgraphProjector(self.driver, self.database)
        graph = projector.project(principal, selected_chunks)
        projected = [a for a in graph.literal_assertions if a.predicate == "projectCode"]
        self.assertEqual(len(projected), 3)
        self.assertTrue(all(a.context_value_evidence.quoted_text == a.literal_value for a in projected))
        EvidenceSubgraphResponse.model_validate(_subgraph_payload(graph))
        principal = replace(principal, capabilities=principal.capabilities | {GRAPH_READ_CAPABILITY})
        browser = Neo4jPublishedGraphBrowser(self.driver, self.database, cursor_signing_key=b"context-test-key" * 4)
        view = browser.query(principal, GraphBrowseQuery())
        response = browser.evidence(principal, tuple(a.revision_id for a in projected), view_token=view["view_token"])
        GraphEvidenceResponseEnvelope.model_validate(response)
        self.assertTrue(all(item["context_value_evidence"]["quoted_text"] == '"P1"' for item in response["items"]))
        self.query("MATCH (c:Chunk {chunk_id:$id}) SET c.access_groups=['private']",
            id=projected[0].context_value_evidence.citation.chunk_id)
        remaining = projector.project(principal, selected_chunks)
        self.assertFalse(any(a.context_value_evidence for a in remaining.literal_assertions))

    def test_context_source_acl_change_and_retirement_hide_secondary_values(self):
        job = self.construct(count=3)
        self.run_review(job)
        record = self.contexts()[0]
        chunk_id = record.context_property_evidence.value_evidence.chunk_id
        self.query("MATCH (c:Chunk {chunk_id:$id}) SET c.access_groups=['private']", id=chunk_id)
        store = Neo4jKnowledgeStore(self.driver, self.database)
        visible = store.list_assertions(self.principal, statuses=(GovernanceStatus.APPROVED,))
        self.assertTrue(all(r.context_property_evidence is None for r in visible))
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.read_evidence(record, "CONTEXT_VALUE")
        self.query("MATCH (c:Chunk {chunk_id:$id}) SET c.access_groups=$groups", id=chunk_id,
                   groups=sorted(record.evidence.access_groups))
        self.assertEqual(self.read_evidence(record, "CONTEXT_VALUE")["quoted_text"], record.literal_value)
        self.query("MATCH (d:Document {document_id:$id}) SET d.lifecycle_status='RETIRED',d.retirement_id='test-retirement'",
                   id=job.document_id)
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.read_evidence(record, "CONTEXT_VALUE")

    def test_context_cannot_retarget_record_and_missing_secondary_link_fails_closed(self):
        job = self.construct(count=3)
        self.run_review(job)
        record = self.contexts()[0]
        other = next(r for r in self.contexts() if r.record_id != record.record_id)
        forged = replace(record, context_property_evidence=replace(record.context_property_evidence,
            record_pointer=other.context_property_evidence.record_pointer,
            source_identity=other.context_property_evidence.source_identity))
        with self.driver.session(database=self.database) as session:
            with self.assertRaises(KnowledgeEvidenceError):
                session.execute_read(Neo4jKnowledgeStore.verify_context_property_tx, forged)
        self.query("""MATCH (r:GovernedAssertionRevision {revision_id:$id})-[e:CONTEXT_EVIDENCED_BY]->()
            DELETE e""", id=record.revision_id)
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.read_evidence(record, "PRIMARY")


if __name__ == "__main__":
    unittest.main()
