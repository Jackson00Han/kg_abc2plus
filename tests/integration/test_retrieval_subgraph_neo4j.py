"""Disposable-Neo4j checks for governed evidence-subgraph projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    chunk_id,
    content_checksum,
    document_id,
    entity_id,
    version_id,
)
from graphrag_prod.domain.models import Chunk, Document, DocumentVersion
from graphrag_prod.graph.provenance import Neo4jProvenanceStore, ProvenanceBundle
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
    llm_candidate_trust,
)
from graphrag_prod.knowledge.review import (
    KNOWLEDGE_PUBLISH_CAPABILITY,
    KNOWLEDGE_REVIEW_CAPABILITY,
    AssertionEdit,
    MentionEdit,
    Neo4jKnowledgePublicationService,
    Neo4jKnowledgeReviewService,
    ReviewRecordKind,
    ReviewRequest,
)
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.trust import GovernanceStatus, TrustMetadata
from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    Neo4jTBoxStore,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from graphrag_prod.retrieval import (
    EvidenceSubgraphLimits,
    Neo4jEvidenceSubgraphProjector,
    SubgraphTrustPolicy,
)


CREATED_AT = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
REVIEWED_AT = CREATED_AT + timedelta(hours=1)
PUBLISHED_AT = REVIEWED_AT + timedelta(hours=1)
SPLITTER = "subgraph-integration:v1"
PUBLIC_GROUP = "plant-public"
RESTRICTED_GROUP = "plant-restricted"


def _source_bundles(
    tenant_id: str,
) -> tuple[tuple[ProvenanceBundle, ...], Chunk, Chunk]:
    chunk_specs = (
        (
            "public",
            "Acme and Pump-7 overview.",
            frozenset({PUBLIC_GROUP}),
            "Overview",
        ),
        (
            "restricted",
            "Acme owns Pump-7. Pump-7 pressure is 12 bar.",
            frozenset({RESTRICTED_GROUP}),
            "Asset facts",
        ),
    )
    chunks: list[Chunk] = []
    bundles: list[ProvenanceBundle] = []
    for scope, text, groups, section in chunk_specs:
        canonical_uri = (
            f"https://example.com/{tenant_id}/plant-register/{scope}"
        )
        document_identifier = document_id(tenant_id, canonical_uri)
        checksum = content_checksum(text)
        version_identifier = version_id(
            document_identifier,
            checksum,
            checksum,
        )
        policy_id = f"{tenant_id}:plant-knowledge:{scope}"
        document = Document(
            document_id=document_identifier,
            tenant_id=tenant_id,
            canonical_uri=canonical_uri,
            title=f"Plant knowledge register: {scope}",
            source_name="subgraph-integration",
            access_policy_id=policy_id,
            access_policy_version=1,
            access_groups=groups,
            created_at=CREATED_AT,
        )
        version = DocumentVersion(
            version_id=version_identifier,
            document_id=document_identifier,
            tenant_id=tenant_id,
            checksum=checksum,
            original_checksum=checksum,
            normalized_text=text,
            version_number=1,
            mime_type="text/plain",
            language="en",
            published_at=CREATED_AT,
            ingested_at=CREATED_AT,
        )
        chunk = Chunk(
            chunk_id=chunk_id(
                version_identifier,
                SPLITTER,
                0,
                0,
                len(text),
                checksum,
            ),
            version_id=version_identifier,
            document_id=document_identifier,
            tenant_id=tenant_id,
            access_policy_id=policy_id,
            access_policy_version=1,
            access_groups=groups,
            ordinal=0,
            text=text,
            checksum=checksum,
            char_start=0,
            char_end=len(text),
            page_number=1,
            section=section,
            splitter_version=SPLITTER,
        )
        chunks.append(chunk)
        bundles.append(
            ProvenanceBundle(
                document=document,
                version=version,
                chunk=chunk,
                embedding=None,
                entities=(),
                mentions=(),
                assertion=None,
            )
        )
    return tuple(bundles), chunks[0], chunks[1]


def _tbox(tenant_id: str) -> TBoxVersion:
    return TBoxVersion(
        tenant_id=tenant_id,
        key="industrial-assets",
        version=1,
        status=TBoxStatus.DRAFT,
        entity_types=(
            EntityTypeDefinition("Company", ("company-id",)),
            EntityTypeDefinition(
                "Asset",
                ("asset-id",),
                properties=(
                    PropertyDefinition(
                        "PRESSURE",
                        PropertyDataType.STRING,
                        False,
                        Cardinality.ZERO_OR_ONE,
                    ),
                ),
            ),
        ),
        relationship_types=(
            RelationshipTypeDefinition(
                "OWNS",
                ("Company",),
                ("Asset",),
            ),
        ),
    )


def _identity(
    tenant_id: str,
    entity_type: str,
    canonical_key: str,
    canonical_name: str,
) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id(tenant_id, entity_type, canonical_key),
        tenant_id=tenant_id,
        entity_type=entity_type,
        canonical_key=canonical_key,
        canonical_name=canonical_name,
    )


def _evidence(
    chunk: Chunk,
    start: int,
    end: int,
) -> EvidenceReference:
    relative_start = start - chunk.char_start
    relative_end = end - chunk.char_start
    return EvidenceReference(
        tenant_id=chunk.tenant_id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        chunk_id=chunk.chunk_id,
        char_start=start,
        char_end=end,
        quoted_text=chunk.text[relative_start:relative_end],
        access_policy_id=chunk.access_policy_id,
        access_policy_version=chunk.access_policy_version,
        access_groups=chunk.access_groups,
    )


def _mention(
    *,
    tenant_id: str,
    source_key: str,
    entity: EntityIdentity,
    evidence: EvidenceReference,
    trust: TrustMetadata,
    confidence: float = 1.0,
) -> EntityMentionRecord:
    return EntityMentionRecord(
        revision=RecordRevision.next(
            knowledge_record_id(
                tenant_id,
                "ENTITY_MENTION",
                source_key,
            ),
            0,
        ),
        tenant_id=tenant_id,
        entity=entity,
        evidence=evidence,
        confidence=confidence,
        trust=trust,
        created_at=CREATED_AT,
    )


class Neo4jEvidenceSubgraphIntegrationTests(unittest.TestCase):
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
        )
        cls.driver.verify_connectivity()
        rows, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if rows[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")
        cls._build_governed_fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=cls.database,
        )
        cls.driver.close()

    @classmethod
    def _build_governed_fixture(cls) -> None:
        cls.alpha = "tenant-subgraph-alpha"
        cls.beta = "tenant-subgraph-beta"
        cls.alpha_expert = Principal(
            "expert:alpha",
            cls.alpha,
            frozenset({PUBLIC_GROUP, RESTRICTED_GROUP}),
            frozenset(
                {
                    KNOWLEDGE_REVIEW_CAPABILITY,
                    KNOWLEDGE_PUBLISH_CAPABILITY,
                }
            ),
        )
        cls.alpha_public_reader = Principal(
            "reader:alpha",
            cls.alpha,
            frozenset({PUBLIC_GROUP}),
        )
        cls.beta_expert = Principal(
            "expert:beta",
            cls.beta,
            frozenset({PUBLIC_GROUP, RESTRICTED_GROUP}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        tbox_store = Neo4jTBoxStore(cls.driver, cls.database)
        provenance = Neo4jProvenanceStore(cls.driver, cls.database)

        source_by_tenant: dict[str, tuple[Chunk, Chunk]] = {}
        tbox_ids: dict[str, str] = {}
        for tenant_id in (cls.alpha, cls.beta):
            tbox = _tbox(tenant_id)
            tbox_store.import_version(tbox)
            tbox_store.publish(
                tenant_id,
                tbox.tbox_id,
                expected_active_tbox_id=None,
            )
            tbox_ids[tenant_id] = tbox.tbox_id
            bundles, public_chunk, restricted_chunk = _source_bundles(tenant_id)
            for bundle in bundles:
                provenance.write_bundle(bundle)
                cls.driver.execute_query(
                    """
                    MATCH (document:Document {
                        tenant_id: $tenant_id,
                        document_id: $document_id
                    })-[:ACTIVE_VERSION]->(version:DocumentVersion {
                        tenant_id: $tenant_id,
                        version_id: $version_id
                    })-[:HAS_CHUNK]->(chunk:Chunk {
                        tenant_id: $tenant_id,
                        chunk_id: $chunk_id
                    })
                    CREATE (snapshot:KnowledgeSnapshot {
                        snapshot_id: $snapshot_id,
                        tenant_id: $tenant_id,
                        document_id: $document_id,
                        version_id: $version_id,
                        profile_id: 'subgraph-integration:v1',
                        build_state: 'PUBLISHED',
                        created_at: $created_at
                    })
                    CREATE (snapshot)-[:OF_VERSION]->(version)
                    CREATE (snapshot)-[:INCLUDES_CHUNK]->(chunk)
                    CREATE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
                    """,
                    tenant_id=tenant_id,
                    document_id=bundle.document.document_id,
                    version_id=bundle.version.version_id,
                    chunk_id=bundle.chunk.chunk_id,
                    snapshot_id=(
                        f"{tenant_id}:subgraph-snapshot:"
                        f"{bundle.chunk.chunk_id}:v1"
                    ),
                    created_at=CREATED_AT,
                    database_=cls.database,
                )
            source_by_tenant[tenant_id] = (public_chunk, restricted_chunk)

        cls.alpha_public_chunk, cls.alpha_restricted_chunk = source_by_tenant[
            cls.alpha
        ]
        cls.beta_public_chunk, _ = source_by_tenant[cls.beta]
        alpha_tbox_id = tbox_ids[cls.alpha]
        beta_tbox_id = tbox_ids[cls.beta]

        store = Neo4jKnowledgeStore(cls.driver, cls.database)
        alpha_publication = Neo4jKnowledgePublicationService(
            cls.driver,
            cls.database,
        )

        company = _identity(cls.alpha, "Company", "company-id:ACME", "Acme")
        pump = _identity(cls.alpha, "Asset", "asset-id:P-7", "Pump-7")
        public_company = _mention(
            tenant_id=cls.alpha,
            source_key="alpha:public:acme",
            entity=company,
            evidence=_evidence(cls.alpha_public_chunk, 0, 4),
            trust=authoritative_import_trust(
                ontology_version_id=alpha_tbox_id,
                imported_by=cls.alpha_expert.principal_id,
                imported_at=CREATED_AT,
            ),
        )
        public_pump_start = cls.alpha_public_chunk.text.index("Pump-7")
        public_pump = _mention(
            tenant_id=cls.alpha,
            source_key="alpha:public:pump",
            entity=pump,
            evidence=_evidence(
                cls.alpha_public_chunk,
                public_pump_start,
                public_pump_start + len("Pump-7"),
            ),
            trust=authoritative_import_trust(
                ontology_version_id=alpha_tbox_id,
                imported_by=cls.alpha_expert.principal_id,
                imported_at=CREATED_AT,
            ),
        )
        public_batch = ABoxRecordBatch(
            cls.alpha,
            (public_company, public_pump),
        )
        store.import_authoritative(public_batch)
        first = alpha_publication.publish(
            cls.alpha_expert,
            tuple(record.revision_id for record in public_batch.mentions),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )

        restricted_start = cls.alpha_restricted_chunk.char_start
        first_pump_start = restricted_start + cls.alpha_restricted_chunk.text.index(
            "Pump-7"
        )
        restricted_company = _mention(
            tenant_id=cls.alpha,
            source_key="alpha:restricted:acme",
            entity=company,
            evidence=_evidence(
                cls.alpha_restricted_chunk,
                restricted_start,
                restricted_start + 4,
            ),
            trust=authoritative_import_trust(
                ontology_version_id=alpha_tbox_id,
                imported_by=cls.alpha_expert.principal_id,
                imported_at=CREATED_AT,
            ),
        )
        restricted_pump = _mention(
            tenant_id=cls.alpha,
            source_key="alpha:restricted:pump",
            entity=pump,
            evidence=_evidence(
                cls.alpha_restricted_chunk,
                first_pump_start,
                first_pump_start + len("Pump-7"),
            ),
            trust=authoritative_import_trust(
                ontology_version_id=alpha_tbox_id,
                imported_by=cls.alpha_expert.principal_id,
                imported_at=CREATED_AT,
            ),
        )
        owns_end = restricted_start + len("Acme owns Pump-7.")
        owns = AssertionRecord(
            revision=RecordRevision.next(
                knowledge_record_id(cls.alpha, "ASSERTION", "alpha:owns"),
                0,
            ),
            tenant_id=cls.alpha,
            subject=company,
            predicate="OWNS",
            evidence=_evidence(
                cls.alpha_restricted_chunk,
                restricted_start,
                owns_end,
            ),
            subject_mention_revision_id=restricted_company.revision_id,
            object_entity=pump,
            object_mention_revision_id=restricted_pump.revision_id,
            confidence=1.0,
            trust=authoritative_import_trust(
                ontology_version_id=alpha_tbox_id,
                imported_by=cls.alpha_expert.principal_id,
                imported_at=CREATED_AT,
            ),
            created_at=CREATED_AT,
        )
        relationship_batch = ABoxRecordBatch(
            cls.alpha,
            (restricted_company, restricted_pump),
            (owns,),
        )
        store.import_authoritative(relationship_batch)
        second = alpha_publication.publish(
            cls.alpha_expert,
            tuple(
                record.revision_id
                for record in (
                    *relationship_batch.mentions,
                    *relationship_batch.assertions,
                )
            ),
            expected_active_publication_id=first.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
        )

        second_pump_start = restricted_start + cls.alpha_restricted_chunk.text.index(
            "Pump-7",
            cls.alpha_restricted_chunk.text.index("Pump-7") + 1,
        )
        candidate_pump = _identity(
            cls.alpha,
            "Asset",
            f"llm-candidate:{pump.entity_id}",
            "Pump-7",
        )
        candidate_trust = llm_candidate_trust(
            ontology_version_id=alpha_tbox_id,
            extractor_version="qwen-industrial:v1",
            prompt_version="asset-tbox:v1",
            extracted_at=CREATED_AT,
        )
        candidate_mention = _mention(
            tenant_id=cls.alpha,
            source_key="alpha:llm:pump-pressure",
            entity=candidate_pump,
            evidence=_evidence(
                cls.alpha_restricted_chunk,
                second_pump_start,
                second_pump_start + len("Pump-7"),
            ),
            trust=candidate_trust,
            confidence=0.91,
        )
        pressure_end = cls.alpha_restricted_chunk.char_end
        candidate_pressure = AssertionRecord(
            revision=RecordRevision.next(
                knowledge_record_id(cls.alpha, "ASSERTION", "alpha:llm:pressure"),
                0,
            ),
            tenant_id=cls.alpha,
            subject=candidate_pump,
            predicate="PRESSURE",
            evidence=_evidence(
                cls.alpha_restricted_chunk,
                second_pump_start,
                pressure_end,
            ),
            subject_mention_revision_id=candidate_mention.revision_id,
            literal_value="12 bar",
            confidence=0.88,
            trust=candidate_trust,
            created_at=CREATED_AT,
        )
        candidate_batch = ABoxRecordBatch(
            cls.alpha,
            (candidate_mention,),
            (candidate_pressure,),
        )
        store.persist_llm_candidates(candidate_batch)
        review = Neo4jKnowledgeReviewService(cls.driver, cls.database)
        review_result = review.review_batch(
            cls.alpha_expert,
            (
                ReviewRequest(
                    ReviewRecordKind.ENTITY_MENTION,
                    candidate_mention.record_id,
                    1,
                    GovernanceStatus.APPROVED,
                    REVIEWED_AT,
                    "Pump identity and exact span verified.",
                    MentionEdit(pump, candidate_mention.confidence),
                ),
                ReviewRequest(
                    ReviewRecordKind.ASSERTION,
                    candidate_pressure.record_id,
                    1,
                    GovernanceStatus.APPROVED,
                    REVIEWED_AT,
                    "Pressure fact verified against the exact source span.",
                    AssertionEdit(
                        pump,
                        "PRESSURE",
                        candidate_mention.revision_id,
                        candidate_pressure.confidence,
                        literal_value="12 bar",
                    ),
                ),
            ),
        )
        cls.alpha_active_publication = alpha_publication.publish(
            cls.alpha_expert,
            tuple(outcome.revision_id for outcome in review_result.outcomes),
            expected_active_publication_id=second.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=2),
        )
        cls.alpha_tbox_id = alpha_tbox_id
        cls.alpha_company_id = company.entity_id
        cls.alpha_pump_id = pump.entity_id

        beta_company = _identity(cls.beta, "Company", "company-id:ACME", "Acme")
        beta_public_company = _mention(
            tenant_id=cls.beta,
            source_key="beta:public:acme",
            entity=beta_company,
            evidence=_evidence(cls.beta_public_chunk, 0, 4),
            trust=authoritative_import_trust(
                ontology_version_id=beta_tbox_id,
                imported_by=cls.beta_expert.principal_id,
                imported_at=CREATED_AT,
            ),
        )
        store.import_authoritative(ABoxRecordBatch(cls.beta, (beta_public_company,)))
        beta_publication = Neo4jKnowledgePublicationService(cls.driver, cls.database)
        beta_publication.publish(
            cls.beta_expert,
            (beta_public_company.revision_id,),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )

    def test_cross_chunk_one_hop_and_trust_filter_use_exact_evidence(self) -> None:
        projector = Neo4jEvidenceSubgraphProjector(self.driver, self.database)
        result = projector.project(
            self.alpha_expert,
            (self.alpha_public_chunk.chunk_id,),
        )

        self.assertEqual(
            {node.entity.entity_id for node in result.entities},
            {self.alpha_company_id, self.alpha_pump_id},
        )
        self.assertEqual(len(result.relationship_assertions), 1)
        self.assertEqual(len(result.literal_assertions), 1)
        self.assertEqual(len(result.paths), 2)
        self.assertEqual(
            set(result.matched_chunk_ids),
            {
                self.alpha_public_chunk.chunk_id,
                self.alpha_restricted_chunk.chunk_id,
            },
        )
        for assertion in result.assertions:
            self.assertEqual(
                assertion.evidence.citation.chunk_id,
                self.alpha_restricted_chunk.chunk_id,
            )
            relative_start = (
                assertion.evidence.char_start
                - assertion.evidence.citation.char_start
            )
            relative_end = (
                assertion.evidence.char_end
                - assertion.evidence.citation.char_start
            )
            self.assertEqual(
                assertion.evidence.citation.chunk_text[
                    relative_start:relative_end
                ],
                assertion.evidence.quoted_text,
            )
        self.assertEqual(
            result.publication_ids,
            (self.alpha_active_publication.publication_id,),
        )

        authoritative = projector.project(
            self.alpha_expert,
            (self.alpha_public_chunk.chunk_id,),
            trust_policy=SubgraphTrustPolicy.AUTHORITATIVE_ONLY,
        )
        self.assertEqual(len(authoritative.relationship_assertions), 1)
        self.assertEqual(authoritative.literal_assertions, ())
        self.assertTrue(
            all(
                evidence.provenance.authority.value == "AUTHORITATIVE"
                for node in authoritative.entities
                for evidence in node.evidence
            )
        )

    def test_tenant_acl_and_active_lifecycle_boundaries_fail_closed(self) -> None:
        projector = Neo4jEvidenceSubgraphProjector(self.driver, self.database)
        public = projector.project(
            self.alpha_public_reader,
            (self.alpha_public_chunk.chunk_id,),
        )
        self.assertEqual(len(public.entities), 2)
        self.assertEqual(public.assertions, ())
        self.assertEqual(
            public.matched_chunk_ids,
            (self.alpha_public_chunk.chunk_id,),
        )

        cross_tenant = projector.project(
            self.beta_expert,
            (self.alpha_public_chunk.chunk_id,),
        )
        self.assertEqual(cross_tenant.entities, ())
        self.assertEqual(cross_tenant.assertions, ())
        missing = projector.project(
            self.alpha_expert,
            ("same-tenant-chunk-that-does-not-exist",),
        )
        self.assertEqual(missing.entities, ())
        self.assertEqual(missing.assertions, ())
        self.assertEqual(missing.publication_ids, ())
        beta_own = projector.project(
            self.beta_expert,
            (self.beta_public_chunk.chunk_id,),
        )
        self.assertEqual(len(beta_own.entities), 1)

        checks = (
            (
                "snapshot",
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })-[edge:ACTIVE_SNAPSHOT]->(snapshot)
                DELETE edge
                RETURN snapshot.snapshot_id AS value
                """,
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                }), (snapshot:KnowledgeSnapshot {snapshot_id: $value})
                CREATE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
                """,
            ),
            (
                "version",
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })-[edge:ACTIVE_VERSION]->(version)
                DELETE edge
                RETURN version.version_id AS value
                """,
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                }), (version:DocumentVersion {version_id: $value})
                CREATE (document)-[:ACTIVE_VERSION]->(version)
                """,
            ),
            (
                "tbox",
                """
                MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                      -[edge:ACTIVE_TBOX_VERSION]->(tbox)
                DELETE edge
                RETURN tbox.tbox_id AS value
                """,
                """
                MATCH (catalog:TBoxCatalog {tenant_id: $tenant_id}),
                      (tbox:TBoxVersion {tbox_id: $value})
                CREATE (catalog)-[:ACTIVE_TBOX_VERSION]->(tbox)
                """,
            ),
            (
                "publication",
                """
                MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                      -[edge:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
                DELETE edge
                RETURN publication.publication_id AS value
                """,
                """
                MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id}),
                      (publication:KnowledgePublication {publication_id: $value})
                CREATE (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
                """,
            ),
        )
        for label, remove_query, restore_query in checks:
            with self.subTest(boundary=label):
                rows, _, _ = self.driver.execute_query(
                    remove_query,
                    tenant_id=self.alpha,
                    document_id=self.alpha_public_chunk.document_id,
                    database_=self.database,
                )
                self.assertEqual(len(rows), 1)
                try:
                    hidden = projector.project(
                        self.alpha_expert,
                        (self.alpha_public_chunk.chunk_id,),
                    )
                    self.assertEqual(hidden.entities, ())
                    self.assertEqual(hidden.assertions, ())
                    self.assertEqual(hidden.publication_ids, ())
                finally:
                    self.driver.execute_query(
                        restore_query,
                        tenant_id=self.alpha,
                        document_id=self.alpha_public_chunk.document_id,
                        value=rows[0]["value"],
                        database_=self.database,
                    )

    def test_projection_hard_limits_are_enforced_after_graph_expansion(self) -> None:
        limits = EvidenceSubgraphLimits(
            max_selected_chunks=1,
            max_entities=1,
            max_assertions=1,
            max_paths=1,
            max_mentions_per_entity=1,
            max_chunk_chars=100,
            max_total_evidence_chars=100,
        )
        result = Neo4jEvidenceSubgraphProjector(
            self.driver,
            self.database,
        ).project(
            self.alpha_expert,
            (self.alpha_public_chunk.chunk_id,),
            limits=limits,
        )

        self.assertLessEqual(len(result.entities), 1)
        self.assertLessEqual(len(result.assertions), 1)
        self.assertLessEqual(len(result.paths), 1)
        self.assertTrue(
            all(len(node.evidence) <= 1 for node in result.entities)
        )


if __name__ == "__main__":
    unittest.main()
