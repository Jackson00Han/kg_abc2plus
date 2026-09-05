"""Disposable-Neo4j checks for immutable published-quality audit history."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.published_quality import (
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityIssue,
    PublishedGraphQualityReport,
    PublishedGraphReviewSampleItem,
)
from graphrag_prod.graph.published_quality_history import (
    Neo4jPublishedGraphQualityHistoryService,
    PublishedGraphQualityHistoryConflict,
)
from graphrag_prod.graph.quality import IssueSeverity
from graphrag_prod.graph.schema import apply_schema, verify_schema


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _Auditor:
    def __init__(self, value: PublishedGraphQualityReport | Exception) -> None:
        self.value = value
        self.calls = 0

    def audit(self, _: Principal) -> PublishedGraphQualityReport:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class PublishedQualityHistoryNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
            notifications_min_severity="OFF",
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.tenant_id = "tenant-quality-history"
        self.public = Principal(
            "expert:public",
            self.tenant_id,
            frozenset({"public"}),
            frozenset({"knowledge:quality"}),
        )
        self.board = Principal(
            "expert:board",
            self.tenant_id,
            frozenset({"board"}),
            frozenset({"knowledge:quality"}),
        )
        self.observed_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _seed_publication(
        self,
        suffix: str,
        generation: int,
        groups: tuple[str, ...],
    ) -> PublishedGraphQualityReport:
        publication_id = f"publication:{suffix}"
        tbox_id = f"tbox:{suffix}"
        tbox_checksum = _digest(tbox_id)
        manifest_hash = _digest(f"manifest:{suffix}")
        revision_id = f"revision:{suffix}"
        chunk_id = f"chunk:{suffix}"
        document_id = f"document:{suffix}"
        self.driver.execute_query(
            """
            MERGE (state:KnowledgePublicationState {tenant_id: $tenant_id})
            MERGE (corpus:TenantCorpusState {tenant_id: $tenant_id})
            SET corpus.corpus_revision = $corpus_revision
            WITH state
            OPTIONAL MATCH (state)-[old:ACTIVE_KNOWLEDGE_PUBLICATION]->
                  (previous:KnowledgePublication)
            SET previous.status = 'INACTIVE'
            DELETE old
            WITH state
            CREATE (tbox:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                checksum: $tbox_checksum,
                status: 'PUBLISHED'
            })
            CREATE (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id,
                access_groups: $groups
            })
            CREATE (chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id,
                access_groups: $groups
            })
            CREATE (revision:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id,
                document_id: $document_id,
                chunk_id: $chunk_id,
                access_groups: $groups
            })
            CREATE (revision)-[:IN_CHUNK]->(chunk)
            CREATE (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id,
                generation: $generation,
                manifest_hash: $manifest_hash,
                ontology_version_id: $tbox_id,
                published_revision_ids: [$revision_id],
                status: 'ACTIVE'
            })
            CREATE (publication)-[:USES_TBOX_VERSION]->(tbox)
            CREATE (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
            CREATE (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
            """,
            tenant_id=self.tenant_id,
            corpus_revision=generation,
            tbox_id=tbox_id,
            tbox_checksum=tbox_checksum,
            document_id=document_id,
            chunk_id=chunk_id,
            revision_id=revision_id,
            publication_id=publication_id,
            generation=generation,
            manifest_hash=manifest_hash,
            groups=list(groups),
            database_=self.database,
        )
        issue = PublishedGraphQualityIssue(
            "published-quality-issue:" + _digest(f"issue:{suffix}"),
            "PUBLICATION_MANIFEST_MISMATCH",
            IssueSeverity.ERROR,
            "KnowledgePublication",
            publication_id,
            "publication edges do not match the immutable revision manifest",
        )
        return PublishedGraphQualityReport(
            run_id="published-graph-quality:" + _digest(f"run:{suffix}"),
            ruleset_version="published-governed-graph-quality-v1",
            tenant_id=self.tenant_id,
            publication_id=publication_id,
            publication_generation=generation,
            manifest_hash=manifest_hash,
            ontology_version_id=tbox_id,
            tbox_checksum=tbox_checksum,
            corpus_revision=generation,
            graph_digest=_digest(f"graph:{suffix}"),
            counts=(("entity_mentions", 1), ("revisions", 1)),
            total_issue_count=1,
            total_error_count=1,
            issues_truncated=False,
            issues=(issue,),
            review_sample=(
                PublishedGraphReviewSampleItem(
                    "KnowledgePublication",
                    publication_id,
                    (issue.code,),
                    (chunk_id,),
                ),
            ),
        )

    def _service(
        self,
        report: PublishedGraphQualityReport | Exception,
        observed_at: datetime | None = None,
    ) -> Neo4jPublishedGraphQualityHistoryService:
        return Neo4jPublishedGraphQualityHistoryService(
            self.driver,
            self.database,
            auditor=_Auditor(report),
            clock=lambda: observed_at or self.observed_at,
        )

    def _node_count(self, label: str) -> int:
        records, _, _ = self.driver.execute_query(
            f"MATCH (node:{label}) RETURN count(node) AS count",
            database_=self.database,
        )
        return int(records[0]["count"])

    def test_failing_report_is_immutable_evidence_and_replay_is_noop(self) -> None:
        report = self._seed_publication("one", 1, ("public",))
        first = self._service(report).audit_and_record(self.public)
        later_actor = Principal(
            "expert:second",
            self.tenant_id,
            frozenset({"public"}),
            frozenset({"knowledge:quality"}),
        )
        replay = self._service(
            report,
            self.observed_at + timedelta(days=1),
        ).audit_and_record(later_actor)

        self.assertEqual(first, replay)
        self.assertFalse(first.passed)
        self.assertEqual(first.recorded_by, self.public.principal_id)
        self.assertEqual(first.recorded_at, self.observed_at)
        self.assertEqual(self._node_count("PublishedGraphQualityRun"), 1)
        self.assertEqual(self._node_count("PublishedGraphQualityIssue"), 1)
        self.assertEqual(self._node_count("PublishedGraphQualityReviewSample"), 1)
        self.assertEqual(self._node_count("PublishedGraphQualityAclRequirement"), 1)
        lock_rows, _, _ = self.driver.execute_query(
            """
            MATCH (publication_state:KnowledgePublicationState {
                tenant_id: $tenant_id
            })
            MATCH (corpus_state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN publication_state.__publication_cas_lock AS publication_lock,
                   corpus_state.__published_quality_history_cas_lock AS corpus_lock
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        self.assertIsNone(lock_rows[0]["publication_lock"])
        self.assertIsNone(lock_rows[0]["corpus_lock"])
        self.assertEqual(
            self._service(report).get_run(self.public, report.run_id),
            first,
        )
        serialized = str(first.to_dict())
        self.assertNotIn("source_text", serialized)
        self.assertNotIn("chunk_text", serialized)
        self.assertNotIn("evidence_text", serialized)

    def test_audit_failure_and_stale_active_boundary_write_nothing(self) -> None:
        service = self._service(PublishedGraphQualityConflict())
        with self.assertRaises(PublishedGraphQualityConflict):
            service.audit_and_record(self.public)
        self.assertEqual(self._node_count("PublishedGraphQualityRun"), 0)

        stale = self._seed_publication("stale", 1, ("public",))
        current = self._seed_publication("current", 2, ("public",))
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            self._service(stale).audit_and_record(self.public)
        self.assertEqual(self._node_count("PublishedGraphQualityRun"), 0)

        self.driver.execute_query(
            "MATCH (state:TenantCorpusState {tenant_id: $tenant_id}) "
            "DETACH DELETE state",
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            self._service(current).audit_and_record(self.public)
        self.assertEqual(self._node_count("PublishedGraphQualityRun"), 0)

    def test_partial_acl_cannot_record_or_read_a_history_run(self) -> None:
        report = self._seed_publication("board", 1, ("board",))
        with self.assertRaises(PublishedGraphQualityAuthorizationError):
            self._service(report).audit_and_record(self.public)
        self.assertEqual(self._node_count("PublishedGraphQualityRun"), 0)

        recorded = self._service(report).audit_and_record(self.board)
        with self.assertRaises(PublishedGraphQualityAuthorizationError):
            self._service(report).get_run(self.public, recorded.run_id)

        outsider = Principal(
            "expert:other-tenant",
            "tenant-other",
            frozenset({"board"}),
            frozenset({"knowledge:quality"}),
        )
        self.assertIsNone(self._service(report).get_run(outsider, recorded.run_id))

    def test_rejected_transport_projection_does_not_persist_history(self) -> None:
        report = self._seed_publication("projection", 1, ("public",))

        def reject_projection(_: PublishedGraphQualityReport) -> None:
            raise ValueError("report does not satisfy the response contract")

        service = Neo4jPublishedGraphQualityHistoryService(
            self.driver,
            self.database,
            auditor=_Auditor(report),
            report_validator=reject_projection,
        )
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            service.audit_and_record(self.public)
        for label in (
            "PublishedGraphQualityRun",
            "PublishedGraphQualityIssue",
            "PublishedGraphQualityReviewSample",
            "PublishedGraphQualityAclRequirement",
        ):
            self.assertEqual(self._node_count(label), 0)
        self.assertEqual(self._node_count("KnowledgePublication"), 1)

    def test_property_child_and_binding_edge_tampering_fail_closed(self) -> None:
        report = self._seed_publication("tamper", 1, ("public",))
        service = self._service(report)
        service.audit_and_record(self.public)

        for mutation in (
            "MATCH (run:PublishedGraphQualityRun {run_id: $run_id}) "
            "SET run.recorded_by = 'forged'",
            "MATCH (run:PublishedGraphQualityRun {run_id: $run_id}) "
            "SET run.unexpected = true",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[:HAS_PUBLISHED_QUALITY_ISSUE]->(issue) "
            "SET issue.detail = 'forged'",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[edge:HAS_PUBLISHED_QUALITY_ISSUE]->() "
            "SET edge.ordinal = 9",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[:HAS_PUBLISHED_QUALITY_SAMPLE]->(sample) "
            "SET sample.object_id = 'forged'",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[:REQUIRES_PUBLISHED_QUALITY_ACCESS]->(acl) "
            "SET acl.access_groups = ['board']",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[edge:AUDITS_KNOWLEDGE_PUBLICATION]->() "
            "SET edge.manifest_hash = 'forged'",
            "MATCH (:PublishedGraphQualityRun {run_id: $run_id})"
            "-[edge:USES_AUDITED_TBOX_VERSION]->() DELETE edge",
            "MATCH (run:PublishedGraphQualityRun {run_id: $run_id}), "
            "(publication:KnowledgePublication) "
            "CREATE (run)-[:UNEXPECTED_HISTORY_EDGE]->(publication)",
            "MATCH (run:PublishedGraphQualityRun {run_id: $run_id}), "
            "(publication:KnowledgePublication) "
            "CREATE (publication)-[:UNEXPECTED_HISTORY_EDGE]->(run)",
        ):
            self.driver.execute_query(
                mutation,
                run_id=report.run_id,
                database_=self.database,
            )
            with self.assertRaises(PublishedGraphQualityHistoryConflict):
                service.get_run(self.public, report.run_id)
            self.driver.execute_query(
                "MATCH (node) DETACH DELETE node",
                database_=self.database,
            )
            report = self._seed_publication("tamper", 1, ("public",))
            service = self._service(report)
            service.audit_and_record(self.public)

    def test_list_is_acl_filtered_bounded_and_stably_ordered(self) -> None:
        public_one = self._seed_publication("public-one", 1, ("public",))
        self._service(public_one, self.observed_at).audit_and_record(self.public)
        board_two = self._seed_publication("board-two", 2, ("board",))
        self._service(
            board_two,
            self.observed_at + timedelta(minutes=1),
        ).audit_and_record(self.board)
        public_three = self._seed_publication("public-three", 3, ("public",))
        self._service(
            public_three,
            self.observed_at + timedelta(minutes=2),
        ).audit_and_record(self.public)

        service = self._service(public_three)
        values = service.list_runs(self.public, limit=10)

        self.assertEqual(
            [value.run_id for value in values],
            [public_three.run_id, public_one.run_id],
        )
        self.assertEqual(
            service.list_runs(
                self.public,
                publication_id=public_one.publication_id,
                limit=1,
            )[0].run_id,
            public_one.run_id,
        )
        self.assertEqual(
            service.list_runs(self.public, publication_id="publication:missing"),
            (),
        )


if __name__ == "__main__":
    unittest.main()
