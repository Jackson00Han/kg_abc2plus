"""Real-Neo4j graph governance, reporting, and quarantine tests."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.graph import (
    AuthoritativeIdentifier,
    IssueSeverity,
    Neo4jGraphQualityService,
    Neo4jProvenanceStore,
    ResolutionCandidate,
    ResolutionOutcome,
    apply_schema,
    verify_schema,
)
from graphrag_prod.ingestion import IngestionPlan, Neo4jIngestionService
from graphrag_prod.ingestion.models import default_artifact_input_hash
from tests.fixtures.ingestion import (
    FIXED_TIME,
    FixedClock,
    make_bundles,
    make_governance_policy,
    make_plan,
    make_principal,
    make_profile,
)


class Neo4jGraphQualityIntegrationTests(unittest.TestCase):
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
            auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count", database_=cls.database
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        self.clock = FixedClock()
        self.ingestion = Neo4jIngestionService(
            self.driver, self.database, worker_id="stage4-worker", clock=self.clock
        )
        self.quality = Neo4jGraphQualityService(self.driver, self.database)
        self.provenance = Neo4jProvenanceStore(self.driver, self.database)
        self.plan = make_plan(tenant_id="tenant-stage4")
        self.ingestion.ingest(self.plan)

    def tearDown(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)

    def test_clean_active_graph_has_reproducible_report_and_review_sample(self) -> None:
        report = self.quality.audit(
            self.plan.tenant_id,
            self.plan.governance_policy,
            generated_at=FIXED_TIME,
            sample_seed="review-seed",
            sample_size=4,
        )
        replay = self.quality.audit(
            self.plan.tenant_id,
            self.plan.governance_policy,
            generated_at=FIXED_TIME,
            sample_seed="review-seed",
            sample_size=4,
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.run_id, replay.run_id)
        self.assertEqual(report.review_sample, replay.review_sample)
        self.assertEqual(dict(report.counts)["active_entities"], 1)
        self.assertEqual(dict(report.counts)["accepted_assertions"], 3)
        self.assertEqual(len(report.review_sample), 4)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (run:GraphQualityRun {run_id: $run_id})-[:USES_POLICY]->
                  (policy:GraphGovernancePolicy)
            RETURN run.report_hash AS report_hash, policy.policy_id AS policy_id
            """,
            run_id=report.run_id,
            database_=self.database,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["policy_id"], self.plan.governance_policy.policy_id)

    def test_resolution_decision_persists_exact_rule_and_mention_evidence(self) -> None:
        mention_ids = tuple(bundle.mentions[0].mention_id for bundle in self.plan.bundles[:2])
        identifier = AuthoritativeIdentifier("ticker", "AAPL")
        left = ResolutionCandidate(
            "raw-apple-inc", self.plan.tenant_id, "Company", "Apple Inc.",
            ("Apple",), (identifier,), (mention_ids[0],),
        )
        right = ResolutionCandidate(
            "raw-apple", self.plan.tenant_id, "Company", "Apple",
            (), (identifier,), (mention_ids[1],),
        )
        decision = self.quality.resolve_and_record(
            left,
            right,
            policy=self.plan.governance_policy,
            decided_at=FIXED_TIME,
        )
        replay = self.quality.resolve_and_record(
            right,
            left,
            policy=self.plan.governance_policy,
            decided_at=FIXED_TIME + timedelta(hours=1),
        )

        self.assertEqual(decision.outcome, ResolutionOutcome.MERGE)
        self.assertEqual(decision.decision_id, replay.decision_id)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (decision:EntityResolutionDecision {
                raw_decision_id: $raw_decision_id
            })-[:EVIDENCE_MENTION]->(mention:EntityMention)
            RETURN decision.rule_id AS rule_id,
                   decision.outcome AS outcome,
                   collect(mention.mention_id) AS evidence
            """,
            raw_decision_id=decision.decision_id,
            database_=self.database,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rule_id"], "exact-authoritative-identifier:v1")
        self.assertEqual(records[0]["outcome"], "MERGE")
        self.assertEqual(set(records[0]["evidence"]), set(mention_ids))

    def test_quarantine_is_audited_and_removes_assertion_from_evidence_reads(self) -> None:
        assertion = self.plan.bundles[0].all_assertions[0]
        principal = make_principal(self.plan.tenant_id)
        self.assertEqual(len(self.provenance.get_assertion_evidence(principal, assertion.assertion_id)), 1)

        decision_id = self.quality.adjudicate(
            tenant_id=self.plan.tenant_id,
            target_kind="Assertion",
            target_id=assertion.assertion_id,
            action="QUARANTINE",
            reviewer_id="reviewer-1",
            rationale="adjudicated semantic support is insufficient",
            decided_at=FIXED_TIME + timedelta(hours=1),
            policy=self.plan.governance_policy,
        )
        replay_id = self.quality.adjudicate(
            tenant_id=self.plan.tenant_id,
            target_kind="Assertion",
            target_id=assertion.assertion_id,
            action="QUARANTINE",
            reviewer_id="reviewer-1",
            rationale="adjudicated semantic support is insufficient",
            decided_at=FIXED_TIME + timedelta(hours=1),
            policy=self.plan.governance_policy,
        )

        self.assertEqual(decision_id, replay_id)
        self.assertEqual(self.provenance.get_assertion_evidence(principal, assertion.assertion_id), ())
        report = self.quality.audit(
            self.plan.tenant_id,
            self.plan.governance_policy,
            generated_at=FIXED_TIME + timedelta(hours=2),
        )
        self.assertEqual(dict(report.counts)["quarantined_assertions"], 1)

    def test_ingestion_gate_persists_low_confidence_quarantine_reason(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        raw_bundles = make_bundles(tenant_id="tenant-stage4-low-confidence")
        changed = tuple(
            dataclasses.replace(
                bundle,
                assertion=(
                    None
                    if bundle.assertion is None
                    else dataclasses.replace(bundle.assertion, confidence=0.5)
                ),
                additional_assertions=tuple(
                    dataclasses.replace(assertion, confidence=0.5)
                    for assertion in bundle.additional_assertions
                ),
            )
            for bundle in raw_bundles
        )
        plan = IngestionPlan.build(
            operation_key="low-confidence-governed",
            profile=make_profile(),
            governance_policy=make_governance_policy(),
            bundles=changed,
            expected_active_snapshot_id=None,
            source_generation=0,
            artifact_input_hashes={
                bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
                for bundle in changed
            },
            created_at=FIXED_TIME,
        )
        self.assertEqual(len(plan.governance_findings), 3)
        self.assertTrue(all(not item.all_assertions[0].accepted for item in plan.bundles))

        self.ingestion.ingest(plan)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:HAS_GOVERNANCE_FINDING]->(finding:GraphGovernanceFinding)
            RETURN collect(finding.code) AS codes
            """,
            snapshot_id=plan.snapshot.snapshot_id,
            database_=self.database,
        )
        self.assertEqual(records[0]["codes"], ["ASSERTION_BELOW_THRESHOLD"] * 3)
        report = self.quality.audit(
            plan.tenant_id,
            plan.governance_policy,
            generated_at=FIXED_TIME,
        )
        self.assertTrue(report.passed)
        self.assertEqual(dict(report.counts)["quarantined_assertions"], 3)
        first_assertion = plan.bundles[0].all_assertions[0]
        principal = make_principal(plan.tenant_id)
        self.assertEqual(
            self.provenance.get_assertion_evidence(
                principal, first_assertion.assertion_id
            ),
            (),
        )
        self.quality.adjudicate(
            tenant_id=plan.tenant_id,
            target_kind="Assertion",
            target_id=first_assertion.assertion_id,
            action="ACCEPT",
            reviewer_id="reviewer-2",
            rationale="manual evidence review confirms the literal claim",
            decided_at=FIXED_TIME + timedelta(hours=1),
            policy=plan.governance_policy,
        )
        self.assertEqual(
            len(
                self.provenance.get_assertion_evidence(
                    principal, first_assertion.assertion_id
                )
            ),
            1,
        )

    def test_audit_detects_unsupported_claim_orphan_duplicate_and_hub(self) -> None:
        subject = self.plan.bundles[0].entities[0]
        assertion = self.plan.bundles[0].all_assertions[0]
        self.driver.execute_query(
            """
            MATCH (snapshot:KnowledgeSnapshot)-[:INCLUDES_ASSERTION]->
                  (assertion:Assertion {assertion_id: $assertion_id})
                  -[:SUBJECT]->(subject:Entity)
            MATCH (assertion)-[evidence:EVIDENCED_BY]->(chunk:Chunk)
            WITH snapshot, assertion, subject, evidence, chunk
            UNWIND range(1, 23) AS ordinal
            CREATE (extra:Assertion {
                assertion_id: 'test-hub-extra-' + toString(ordinal),
                tenant_id: assertion.tenant_id,
                subject_entity_id: assertion.subject_entity_id,
                object_entity_id: assertion.object_entity_id,
                predicate: assertion.predicate,
                object_kind: assertion.object_kind,
                literal_value: assertion.literal_value,
                evidence_chunk_id: assertion.evidence_chunk_id,
                evidence_char_start: assertion.evidence_char_start,
                evidence_char_end: assertion.evidence_char_end,
                extractor_version: assertion.extractor_version,
                schema_version: assertion.schema_version,
                confidence: assertion.confidence,
                accepted: assertion.accepted,
                governance_status: assertion.governance_status
            })
            CREATE (extra)-[:SUBJECT]->(subject)
            CREATE (extra)-[:EVIDENCED_BY]->(chunk)
            CREATE (snapshot)-[:INCLUDES_ASSERTION {
                confidence: 1.0, accepted: true
            }]->(extra)
            WITH DISTINCT assertion, evidence
            DELETE evidence
            CREATE (:Entity {
                entity_id: 'orphan-stage4', tenant_id: $tenant_id,
                entity_type: 'Company', canonical_key: 'ticker:ORPH',
                canonical_name: 'Orphan Corp', aliases: []
            })
            """,
            assertion_id=assertion.assertion_id,
            tenant_id=self.plan.tenant_id,
            database_=self.database,
        )
        report = self.quality.audit(
            self.plan.tenant_id,
            self.plan.governance_policy,
            generated_at=FIXED_TIME,
        )
        codes = {(issue.code, issue.object_id) for issue in report.issues}
        self.assertIn(("UNSUPPORTED_ASSERTION", assertion.assertion_id), codes)
        self.assertIn(("ORPHAN_ENTITY", "orphan-stage4"), codes)
        self.assertIn(("ANOMALOUS_HUB", subject.entity_id), codes)
        self.assertFalse(report.passed)
        self.assertTrue(any(issue.severity is IssueSeverity.ERROR for issue in report.issues))

    def test_invalid_active_pattern_and_normalized_duplicate_are_reported(self) -> None:
        assertion = self.plan.bundles[0].all_assertions[0]
        entity = self.plan.bundles[0].entities[0]
        self.driver.execute_query(
            """
            MATCH (assertion:Assertion {assertion_id: $assertion_id})
            SET assertion.predicate = 'UNDECLARED'
            WITH assertion
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            CREATE (duplicate:Entity {
                entity_id: 'duplicate-stage4', tenant_id: $tenant_id,
                entity_type: 'Company', canonical_key: 'ticker:DUPL',
                canonical_name: $canonical_name, aliases: []
            })
            CREATE (snapshot)-[:INCLUDES_ENTITY {
                canonical_name: $canonical_name, aliases: []
            }]->(duplicate)
            """,
            assertion_id=assertion.assertion_id,
            snapshot_id=self.plan.snapshot.snapshot_id,
            tenant_id=self.plan.tenant_id,
            canonical_name=entity.canonical_name,
            database_=self.database,
        )
        report = self.quality.audit(
            self.plan.tenant_id,
            self.plan.governance_policy,
            generated_at=FIXED_TIME,
        )
        codes = {issue.code for issue in report.issues}
        self.assertIn("INVALID_RELATIONSHIP_PATTERN", codes)
        self.assertIn("POTENTIAL_DUPLICATE_NAME", codes)
        self.assertIn("ENTITY_WITHOUT_ACTIVE_MENTION", codes)

    def test_accept_refuses_a_target_with_unresolved_source_support_error(self) -> None:
        assertion = self.plan.bundles[0].all_assertions[0]
        self.driver.execute_query(
            "MATCH (a:Assertion {assertion_id: $id})-[r:EVIDENCED_BY]->() DELETE r",
            id=assertion.assertion_id,
            database_=self.database,
        )
        with self.assertRaisesRegex(ValueError, "unresolved quality errors"):
            self.quality.adjudicate(
                tenant_id=self.plan.tenant_id,
                target_kind="Assertion",
                target_id=assertion.assertion_id,
                action="ACCEPT",
                reviewer_id="reviewer-1",
                rationale="attempted acceptance",
                decided_at=FIXED_TIME,
                policy=self.plan.governance_policy,
            )


if __name__ == "__main__":
    unittest.main()
