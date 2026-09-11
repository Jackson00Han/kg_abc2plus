"""Cold-query, global-identity and provenance checks against disposable Neo4j."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import AuthoritativeImportRequest, PublicationRequest
from graphrag_prod.construction import (
    ConstructionConfig,
    ConstructionMetadata,
    Neo4jKnowledgeConstructionWorkflow,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion.pipeline import EmbeddingProfile, Neo4jIncrementalPipeline
from graphrag_prod.knowledge.entity_resolution import (
    EntityResolutionService,
    IdentityPropertyValue,
    Neo4jAuthoritativeEntitySource,
    ResolutionOutcome,
)
from graphrag_prod.knowledge.models import EntityIdentity
from graphrag_prod.ontology import Neo4jTBoxStore, TBoxVersion
from graphrag_prod.playground.industrial_demo import (
    build_authoritative_import,
    get_industrial_demo_kit,
)


NOW = datetime(2026, 9, 6, tzinfo=UTC)
TRANSACTION_TIMEOUT_SECONDS = 30.0


class _TimedReadSession:
    def __init__(self, session: object) -> None:
        self.session = session

    def execute_read(self, work, *args, **kwargs):
        bounded = neo4j.unit_of_work(timeout=TRANSACTION_TIMEOUT_SECONDS)(work)
        return self.session.execute_read(bounded, *args, **kwargs)


class _TimedReadDriver:
    """Use the existing live transaction cap without modifying matcher queries."""

    def __init__(self, driver: object) -> None:
        self.driver = driver

    @contextmanager
    def session(self, **kwargs):
        with self.driver.session(**kwargs) as session:
            yield _TimedReadSession(session)


class Neo4jIdentityResolutionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = ("TEST_NEO4J_URI", "TEST_NEO4J_USER", "TEST_NEO4J_PASSWORD", "TEST_NEO4J_DATABASE")
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
            max_transaction_retry_time=0,
        )
        cls.driver.verify_connectivity()
        rows, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count", database_=cls.database
        )
        if rows[0]["count"]:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        self.addCleanup(self._clear_disposable_database)
        self.kit = get_industrial_demo_kit()
        self.tenant = "tenant-identity-integration"
        self.principal = Principal(
            "expert:identity",
            self.tenant,
            frozenset({"engineers"}),
            frozenset({"knowledge:construct", "knowledge:import", "knowledge:publish"}),
        )
        draft = TBoxVersion.from_mapping({
            **self.kit["ontology"], "tenant_id": self.tenant, "status": "DRAFT"
        })
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(draft)
        self.tbox = tboxes.publish(self.tenant, draft.tbox_id, expected_active_tbox_id=None)

        def no_extractor(_tbox):
            self.fail("identity integration setup must not construct a model client")

        workflow = Neo4jKnowledgeConstructionWorkflow(
            driver=self.driver,
            database=self.database,
            pipeline=Neo4jIncrementalPipeline(self.driver, self.database, worker_id="identity-integration"),
            embedding_provider=lambda **kwargs: (0.6, 0.8),
            embedding_profile=EmbeddingProfile("offline", "identity-test", "v1", 2, "l2-unit"),
            extractor_factory=no_extractor,
            config=ConstructionConfig(
                extractor_signature="identity-integration:no-model:v1",
                prompt_signature="identity-integration:no-prompt:v1",
            ),
            clock=lambda: NOW,
        )
        source = next(item for item in self.kit["files"] if item["id"] == "authoritative_source")
        uploaded = workflow.run(
            self.principal,
            source["text"].encode("utf-8"),
            ConstructionMetadata(
                operation_key="identity-integration-source",
                canonical_uri=source["metadata"]["canonical_uri"],
                title=source["metadata"]["title"],
                source_name=source["metadata"]["source_name"],
                mime_type="text/plain",
                language="zh",
                tbox_key=self.tbox.key,
                access_groups=self.principal.groups,
                published_at=NOW,
                extraction_mode="SOURCE_ONLY",
            ),
        )
        self.document_id, self.version_id = uploaded.document_id, uploaded.version_id
        # Publication now validates the active embedding generation and complete
        # source coverage, so prepare the same offline space used by ingestion.
        from graphrag_prod.domain import ChunkEmbedding, chunk_embedding_id
        from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
        profile = EmbeddingProfile("offline", "identity-test", "v1", 2, "l2-unit")
        embedding = ChunkEmbedding(chunk_embedding_id(uploaded.chunks[0].chunk_id, profile.embedding_space_id),
            self.tenant, uploaded.chunks[0].chunk_id, profile.embedding_space_id,
            profile.provider, profile.model, profile.revision, profile.dimensions,
            profile.normalization, NOW, (0.6, 0.8))
        indexes = Neo4jEmbeddingIndexManager(self.driver, self.database)
        generation = indexes.prepare(tenant_id=self.tenant, embedding_profile=embedding, generation_version=1)
        indexes.activate(generation.generation_id, expected_active_generation_id=None)
        self.payload = build_authoritative_import(
            tbox_id=self.tbox.tbox_id,
            document_id=uploaded.document_id,
            version_id=uploaded.version_id,
            source_bytes=source["text"].encode("utf-8"),
            chunks=[{"chunk_id": uploaded.chunks[0].chunk_id, "char_start": 0,
                     "char_end": source["characters"], "text": source["text"]}],
        )
        self.operations = Neo4jKnowledgeOperations(
            allow_legacy_authoritative_import=True,
            driver=self.driver, database=self.database, construction=workflow, clock=lambda: NOW
        )
        self.publication_id = None
        self._publish_import(self.payload)
        self.source = Neo4jAuthoritativeEntitySource(_TimedReadDriver(self.driver), self.database)
        self.identity = (IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-101"),)

    def _clear_disposable_database(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)

    def _publish_import(self, payload):
        imported = self.operations.authoritative_import(
            self.principal, AuthoritativeImportRequest.model_validate(payload)
        ).payload
        publication = self.operations.publish(self.principal, PublicationRequest(
            approved_revision_ids=imported.revision_ids,
            expected_active_publication_id=self.publication_id,
        )).payload
        self.publication_id = publication.publication_id

    def _match(self, *, principal=None, identity=None):
        return self.source.find_exact_identity_properties(
            principal or self.principal,
            ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment",
            identity_properties=self.identity if identity is None else identity,
        )

    def test_cold_unique_identity_finishes_within_live_cap_and_homonym_stays_separate(self) -> None:
        # Force compilation of the actual count/fetch queries, including in a
        # full suite that might already have exercised these same query strings.
        self.driver.execute_query("CALL db.clearQueryCaches()", database_=self.database)
        started = time.monotonic()
        match = self._match()
        elapsed = time.monotonic() - started
        observation_dir = os.getenv("GRAPHRAG_EVALUATION_OUTPUT_DIR")
        if observation_dir:
            output = Path(observation_dir) / "identity-resolution-cold.json"
            output.write_text(json.dumps({
                "query_cache_cleared": True,
                "count_and_fetch_seconds": elapsed,
                "transaction_timeout_seconds": TRANSACTION_TIMEOUT_SECONDS,
                "match_count": match.match_count,
            }, sort_keys=True) + "\n", encoding="utf-8")
        self.assertLess(elapsed, TRANSACTION_TIMEOUT_SECONDS, f"cold count/fetch took {elapsed:.3f}s")
        self.assertEqual(match.match_count, 1)
        self.assertEqual(match.target.entity.canonical_key, "equipment-id:bc-p-101")
        self.assertTrue(match.target.evidence)
        for evidence in match.target.evidence:
            self.assertEqual(evidence.document_id, self.document_id)
            self.assertEqual(evidence.version_id, self.version_id)
        key_match = self.source.find_exact_canonical_key(
            self.principal, ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment", canonical_key="equipment-id:bc-p-101",
        )
        alias_match = self.source.find_exact_governed_alias(
            self.principal, ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment", candidate_values=("北辰一号循环水泵",),
        )
        self.assertEqual(key_match.target.entity.entity_id, match.target.entity.entity_id)
        self.assertEqual(alias_match.target.entity.entity_id, match.target.entity.entity_id)
        service = EntityResolutionService(self.source, active_tbox=self.tbox)
        candidate = EntityIdentity(
            entity_id=entity_id(self.tenant, "Equipment", "llm-candidate:homonym"),
            tenant_id=self.tenant, entity_type="Equipment",
            canonical_key="llm-candidate:homonym", canonical_name="循环水泵", aliases=(),
        )
        outcome = service.suggest(self.principal, candidate, identity_properties=(
            IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-202"),
        ))
        self.assertEqual(outcome[0].outcome, ResolutionOutcome.NO_MATCH)
        for identity in (
            (IdentityPropertyValue("EquipmentCode", "INTEGER", "BC-P-101"),),
            (IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-101", "kW"),),
        ):
            with self.subTest(identity=identity):
                self.assertEqual(self._match(identity=identity).match_count, 0)

    def test_identity_authority_is_hidden_from_wrong_tenant_or_access_group(self) -> None:
        for principal in (
            replace(self.principal, tenant_id="tenant-outsider"),
            replace(self.principal, groups=frozenset({"public"})),
        ):
            with self.subTest(principal=principal):
                match = self._match(principal=principal)
                self.assertEqual(match.match_count, 0)
                self.assertIsNone(match.target)
        self.assertEqual(self._match().match_count, 1)

    def test_global_uniqueness_counts_entities_instead_of_mentions(self) -> None:
        original = next(item for item in self.payload["mentions"] if item["entity"]["entity_type"] == "Equipment")
        fact = next(item for item in self.payload["assertions"] if item["predicate"] == "EquipmentCode")
        second_mention = deepcopy(original)
        second_mention["source_key"] = "identity-test:second-mention"
        # Use a distinct, exact mention span instead of trying to assign a
        # second record to an already materialized identical navigation mention.
        alias = "北辰一号循环水泵"
        second_evidence = second_mention["evidence"]
        alias_start = second_evidence["char_start"] + second_evidence["quoted_text"].index(alias)
        second_evidence.update(
            char_start=alias_start, char_end=alias_start + len(alias), quoted_text=alias
        )
        second_fact = deepcopy(fact)
        second_fact["source_key"] = "identity-test:second-fact"
        second_fact["subject_mention_source_key"] = second_mention["source_key"]
        # Additional source mentions may support the same canonical entity;
        # keep its one existing identity fact to preserve T-Box cardinality.
        self._publish_import({"ontology_version_id": self.tbox.tbox_id,
                              "mentions": [second_mention], "assertions": []})
        self.assertEqual(self._match().match_count, 1)

        duplicate_mention = deepcopy(second_mention)
        duplicate_mention["source_key"] = "identity-test:duplicate-entity"
        duplicate_mention["entity"]["canonical_key"] = "equipment-id:separate-authority-key"
        duplicate_mention["entity"]["aliases"] = []
        primary_name = original["entity"]["canonical_name"]
        primary_start = original["evidence"]["char_start"] + original["evidence"]["quoted_text"].index(primary_name)
        duplicate_mention["evidence"].update(
            char_start=primary_start,
            char_end=primary_start + len(primary_name),
            quoted_text=primary_name,
        )
        duplicate_fact = deepcopy(second_fact)
        duplicate_fact["source_key"] = "identity-test:duplicate-identity-fact"
        duplicate_fact["subject_mention_source_key"] = duplicate_mention["source_key"]
        self._publish_import({"ontology_version_id": self.tbox.tbox_id,
                              "mentions": [duplicate_mention], "assertions": [duplicate_fact]})
        match = self._match()
        self.assertEqual(match.match_count, 2)
        self.assertIsNone(match.target)
        self.assertIsNone(match.matched_target_value)

    def _new_upload_candidate(self):
        from graphrag_prod.knowledge.models import (
            ABoxRecordBatch, EntityMentionRecord, EvidenceReference, RecordRevision,
            knowledge_record_id, llm_candidate_trust,
        )
        from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
        from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService
        self.principal = replace(self.principal,
            capabilities=self.principal.capabilities | frozenset({'knowledge:review'}))
        original = next(x for x in self.kit['files'] if x['id'] == 'authoritative_source')
        report = next(x for x in self.kit['files'] if x['id'] == 'maintenance_report')
        uploaded = self.operations.construction.run(self.principal, report['text'].encode(),
            ConstructionMetadata(operation_key='same-document-new-version',
                canonical_uri=original['metadata']['canonical_uri'], title=original['metadata']['title'],
                source_name=original['metadata']['source_name'], mime_type='text/plain', language='zh',
                tbox_key=self.tbox.key, access_groups=self.principal.groups, published_at=NOW,
                extraction_mode='SOURCE_ONLY'))
        self.assertEqual(uploaded.document_id, self.document_id)
        self.assertNotEqual(uploaded.version_id, self.version_id)
        with self.driver.session(database=self.database) as session:
            row = session.run('MATCH (c:Chunk {chunk_id:$id}) RETURN c{.*} AS c',
                              id=uploaded.chunks[0].chunk_id).single()['c']
        name = '北辰一号循环水泵'
        start = row['text'].index(name) + row['char_start']
        key = 'llm-candidate:new-upload-pump'
        candidate = EntityMentionRecord(
            RecordRevision.next(knowledge_record_id(self.tenant, 'ENTITY_MENTION', 'new-upload-pump'), 0),
            self.tenant, EntityIdentity(entity_id(self.tenant, 'Equipment', key), self.tenant,
                'Equipment', key, name, ()),
            EvidenceReference(self.tenant, uploaded.document_id, uploaded.version_id, row['chunk_id'],
                start, start+len(name), name, row['access_policy_id'], row['access_policy_version'], frozenset(row['access_groups'])),
            1.0, llm_candidate_trust(ontology_version_id=self.tbox.tbox_id,
                extractor_version='pump-offline:v1', prompt_version='pump-offline:v1', extracted_at=NOW), NOW)
        Neo4jKnowledgeStore(self.driver, self.database).persist_llm_candidates(ABoxRecordBatch(self.tenant, (candidate,), ()))
        return candidate, Neo4jKnowledgeReviewService(self.driver, self.database)

    def test_new_upload_keeps_published_identity_search_evidence_and_manual_link(self):
        from graphrag_prod.api.knowledge_contracts import ReviewEvidenceRequest
        candidate, review = self._new_upload_candidate()
        match = self._match()
        self.assertEqual(match.match_count, 1)
        self.assertEqual(match.target.evidence[0].version_id, self.version_id)
        for query in ('', '循环水泵', match.target.entity.entity_id):
            choices = review.resolution_targets(self.principal, candidate, query)['items']
            self.assertEqual(len(choices), 1)
            self.assertTrue(choices[0]['selectable'])
            self.assertIn('BC-P-101', [p['value'] for p in choices[0]['identity_properties']])
        target = choices[0]
        for view in ('paragraph', 'surrounding', 'document'):
            context = review.evidence_context(self.principal, ReviewEvidenceRequest(
                record_id=target['record_id'], expected_revision=target['revision'], view=view))
            self.assertEqual(context['version_id'], self.version_id)
            self.assertIn('BC-P-101', context['text'])
        from graphrag_prod.api.knowledge_contracts import EntityResolutionApplyRequest
        from unittest.mock import patch
        exact = review.resolution_target(self.principal, candidate,
            target['record_id'], target['revision'])
        self.assertEqual(exact, target)
        # The API still performs an atomic revision-bound link when the
        # unrelated automatic matcher is unavailable.
        with patch.object(self.operations.resolution_source, 'find_exact_canonical_key',
                side_effect=AssertionError('manual confirmation must not rebuild suggestions')):
            result = self.operations.apply_resolution(self.principal, EntityResolutionApplyRequest(
                record_id=candidate.record_id, expected_revision=1,
                target_entity_id=match.target.entity.entity_id,
                notes='核对水泵测试包中 BC-P-101 的原文。', target_record_id=target['record_id'],
                target_expected_revision=target['revision'])).payload
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.outcomes[0].status, 'APPROVED')
        self.assertEqual(self._match().match_count, 1)

    def test_new_upload_does_not_expose_inaccessible_or_unpublished_old_sources(self):
        from graphrag_prod.api.knowledge_contracts import ReviewEvidenceRequest
        from graphrag_prod.knowledge.review import KnowledgeReviewUnavailable
        candidate, review = self._new_upload_candidate()
        target = review.resolution_targets(self.principal, candidate, '循环水泵')['items'][0]
        for principal in (replace(self.principal, tenant_id='outside-pump-workspace'),
                          replace(self.principal, groups=frozenset({'public'}))):
            self.assertEqual(self._match(principal=principal).match_count, 0)
            self.assertEqual(review.resolution_targets(principal, candidate, '循环水泵')['items'], [])
            self.assertIsNone(review.resolution_target(principal, candidate,
                target['record_id'], target['revision']))
            with self.assertRaises(KnowledgeReviewUnavailable):
                review.evidence_context(principal, ReviewEvidenceRequest(
                    record_id=target['record_id'], expected_revision=target['revision']))
        # Removing the snapshot from the active release must not expose historical evidence.
        self.driver.execute_query("""MATCH (p:KnowledgePublication {publication_id:$id})
            -[edge:USES_KNOWLEDGE_SNAPSHOT]->() DELETE edge""", id=self.publication_id, database_=self.database)
        self.assertEqual(self._match().match_count, 0)
        self.assertEqual(review.resolution_targets(self.principal, candidate, '循环水泵')['items'], [])
        self.assertIsNone(review.resolution_target(self.principal, candidate,
            target['record_id'], target['revision']))
        with self.assertRaises(KnowledgeReviewUnavailable):
            review.evidence_context(self.principal, ReviewEvidenceRequest(
                record_id=target['record_id'], expected_revision=target['revision']))

    def test_manual_apply_rechecks_revision_after_target_preflight(self):
        from graphrag_prod.api.knowledge_contracts import EntityResolutionApplyRequest
        from graphrag_prod.api.runtime import ConflictError
        from unittest.mock import patch
        candidate, review = self._new_upload_candidate()
        target = review.resolution_targets(self.principal, candidate, '循环水泵')['items'][0]
        request = EntityResolutionApplyRequest(record_id=candidate.record_id,
            expected_revision=1, target_entity_id=target['entity']['entity_id'],
            target_record_id=target['record_id'], target_expected_revision=target['revision'],
            notes='核对水泵测试包原文后选择已有实体。')
        original = self.operations.reviews.resolution_target

        def change_target_after_read(*args):
            selected = original(*args)
            self.driver.execute_query("""
                MATCH (head:KnowledgeRecordHead {tenant_id:$tenant, record_id:$record})
                SET head.current_revision = head.current_revision + 1
                """, tenant=self.tenant, record=target['record_id'], database_=self.database)
            return selected

        with patch.object(self.operations.reviews, 'resolution_target', side_effect=change_target_after_read):
            with self.assertRaises(ConflictError):
                self.operations.apply_resolution(self.principal, request)
        from graphrag_prod.knowledge.trust import GovernanceStatus
        stored = self.operations.knowledge.get_entity_mention(self.principal, candidate.record_id,
            statuses=(GovernanceStatus.CANDIDATE,))
        self.assertIsNotNone(stored)
        self.assertEqual(stored.revision_id, candidate.revision_id)
        self.assertEqual(stored.entity, candidate.entity)

    def test_corrupted_identity_evidence_or_inactive_source_cannot_match(self) -> None:
        rows, _, _ = self.driver.execute_query(
            "MATCH (fact:GovernedAssertionRevision {predicate: 'EquipmentCode'}) RETURN properties(fact) AS props",
            database_=self.database,
        )
        original = dict(rows[0]["props"])
        for damaged in (
            {"evidence_text": "not the source text"},
            {"subject_mention_revision_id": "unbound-mention"},
            {"access_groups": ["public"]},
            {"literal_datatype": "INTEGER"},
            {"literal_canonical_unit": "kW"},
        ):
            with self.subTest(damaged=damaged):
                self.driver.execute_query(
                    "MATCH (fact:GovernedAssertionRevision {revision_id: $id}) SET fact += $props",
                    id=original["revision_id"], props=damaged, database_=self.database,
                )
                try:
                    self.assertEqual(self._match().match_count, 0)
                finally:
                    self.driver.execute_query(
                        "MATCH (fact:GovernedAssertionRevision {revision_id: $id}) SET fact = $props",
                        id=original["revision_id"], props=original, database_=self.database,
                    )
        self.driver.execute_query(
            "MATCH (document:Document {document_id: $id})-[active:ACTIVE_VERSION]->() DELETE active",
            id=self.document_id, database_=self.database,
        )
        self.assertEqual(self._match().match_count, 0)


if __name__ == "__main__":
    unittest.main()
