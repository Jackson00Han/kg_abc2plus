"""Governed A-Box record and Neo4j persistence boundary tests."""

from __future__ import annotations

import dataclasses
import unittest

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.knowledge import (
    ABoxRecordBatch,
    AssertionRecord,
    AuthorityLevel,
    EntityIdentity,
    EntityMentionRecord,
    GovernanceStatus,
    KnowledgeConflict,
    KnowledgeEvidenceError,
    KnowledgeOrigin,
    KnowledgeSchemaError,
    Neo4jKnowledgeStore,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
    llm_candidate_trust,
    llm_quarantined_trust,
)
from tests.fixtures.knowledge import KNOWLEDGE_TIME as NOW, make_knowledge_batch


def _batch(*, authoritative: bool = True) -> ABoxRecordBatch:
    return make_knowledge_batch(authoritative=authoritative)


def _typed_literal_batch(*, canonical_value: str = "Apple") -> ABoxRecordBatch:
    batch = _batch(authoritative=True)
    source = batch.assertions[0]
    literal = TypedLiteralValue(
        datatype="STRING",
        typed_value="Apple",
        raw_value="Apple",
        canonical_value=canonical_value,
    )
    assertion = dataclasses.replace(
        source,
        predicate="DISPLAY_NAME",
        object_entity=None,
        object_mention_revision_id=None,
        literal_value="Apple",
        literal_semantics=literal,
    )
    return dataclasses.replace(batch, assertions=(assertion,))


def _rekey_batch(
    batch: ABoxRecordBatch,
    namespaces: dict[str, str],
) -> ABoxRecordBatch:
    identities: dict[str, EntityIdentity] = {}
    for mention in batch.mentions:
        source = mention.entity
        if source.entity_id in identities:
            continue
        canonical_key = f"{namespaces[source.entity_type]}:{source.entity_id}"
        identities[source.entity_id] = EntityIdentity(
            entity_id=entity_id(
                source.tenant_id,
                source.entity_type,
                canonical_key,
            ),
            tenant_id=source.tenant_id,
            entity_type=source.entity_type,
            canonical_key=canonical_key,
            canonical_name=source.canonical_name,
            aliases=source.aliases,
        )
    mentions = tuple(
        dataclasses.replace(item, entity=identities[item.entity.entity_id])
        for item in batch.mentions
    )
    assertions = tuple(
        dataclasses.replace(
            item,
            subject=identities[item.subject.entity_id],
            object_entity=(
                None
                if item.object_entity is None
                else identities[item.object_entity.entity_id]
            ),
        )
        for item in batch.assertions
    )
    return dataclasses.replace(batch, mentions=mentions, assertions=assertions)


class _Result:
    def __init__(
        self,
        *,
        one: dict[str, object] | None = None,
        rows: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.one = one
        self.rows = rows

    def single(self) -> dict[str, object] | None:
        return self.one

    def consume(self) -> None:
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _WriteTx:
    def __init__(
        self,
        batch: ABoxRecordBatch,
        *,
        stale: bool = False,
        missing_tbox: bool = False,
        historical_evidence: bool = False,
        declare_candidate_namespace: bool = True,
    ) -> None:
        self.batch = batch
        self.stale = stale
        self.missing_tbox = missing_tbox
        self.historical_evidence = historical_evidence
        self.declare_candidate_namespace = declare_candidate_namespace
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.profile_writes = 0

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append((query, parameters))
        if "SET tbox.__abox_write_lock" in query:
            return _Result(
                one=(
                    None
                    if self.missing_tbox
                    else {"tbox_id": self.batch.ontology_version_id}
                )
            )
        if "DECLARES_ENTITY_TYPE" in query:
            candidate_namespaces = (
                ["llm-candidate"] if self.declare_candidate_namespace else []
            )
            return _Result(
                rows=(
                    {
                        "name": "Company",
                        "namespaces": ["ticker", *candidate_namespaces],
                        "literal_properties": [
                            {
                                "name": "DISPLAY_NAME",
                                "datatype": "STRING",
                                "required": False,
                                "cardinality": "ZERO_OR_ONE",
                                "unit": None,
                            }
                        ],
                    },
                    {
                        "name": "Product",
                        "namespaces": ["apple-product", *candidate_namespaces],
                        "literal_properties": [],
                    },
                )
            )
        if "DECLARES_RELATIONSHIP_TYPE" in query:
            return _Result(
                rows=(
                    {
                        "name": "OFFERS",
                        "source_types": ["Company"],
                        "target_types": ["Product"],
                    },
                )
            )
        if "substring(" in query and "document_access_groups" in query:
            if self.historical_evidence:
                return _Result(one=None)
            evidence = next(
                record.evidence
                for record in (*self.batch.mentions, *self.batch.assertions)
                if record.evidence.chunk_id == parameters["chunk_id"]
                and record.evidence.char_start == parameters["evidence_char_start"]
                and record.evidence.char_end == parameters["evidence_char_end"]
            )
            return _Result(
                one={
                    "chunk_char_start": 0,
                    "chunk_char_end": 100,
                    "evidence_text": evidence.quoted_text,
                    "access_policy_id": evidence.access_policy_id,
                    "access_policy_version": evidence.access_policy_version,
                    "access_groups": sorted(evidence.access_groups),
                    "document_access_groups": sorted(evidence.access_groups),
                }
            )
        if "MERGE (head:KnowledgeRecordHead" in query:
            expected = parameters.get("record_id")
            record = next(
                item
                for item in (*self.batch.mentions, *self.batch.assertions)
                if item.record_id == expected
            )
            current = record.revision.expected_previous_revision
            if self.stale:
                current += 1
            return _Result(one={"compatible": True, "current_revision": current})
        if "MERGE (entity:Entity" in query:
            return _Result(
                one={"compatible": True, "canonical_name": None, "aliases": None}
            )
        if "SET entity.canonical_name" in query:
            self.profile_writes += 1
            return _Result()
        if "CREATE (revision:GovernedEntityMentionRevision" in query:
            return _Result(one={"revision_id": parameters["revision_id"]})
        if "CREATE (revision:GovernedAssertionRevision" in query:
            return _Result(one={"revision_id": parameters["revision_id"]})
        raise AssertionError(f"unexpected query: {query}")


class _WriteSession:
    def __init__(self, tx: _WriteTx) -> None:
        self.tx = tx

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work, *args):  # type: ignore[no-untyped-def]
        return work(self.tx, *args)


class _WriteDriver:
    def __init__(
        self,
        batch: ABoxRecordBatch,
        *,
        stale: bool = False,
        missing_tbox: bool = False,
        historical_evidence: bool = False,
        declare_candidate_namespace: bool = True,
    ) -> None:
        self.tx = _WriteTx(
            batch,
            stale=stale,
            missing_tbox=missing_tbox,
            historical_evidence=historical_evidence,
            declare_candidate_namespace=declare_candidate_namespace,
        )
        self.session_calls = 0

    def session(self, *, database: str) -> _WriteSession:
        self.session_calls += 1
        if database != "neo4j":
            raise AssertionError("unexpected database")
        return _WriteSession(self.tx)


def _mention_properties(record: EntityMentionRecord) -> dict[str, object]:
    trust = record.trust
    return {
        "revision_id": record.revision_id,
        "record_id": record.record_id,
        "revision": record.revision.revision,
        "previous_revision": record.revision.expected_previous_revision,
        "tenant_id": record.tenant_id,
        "created_at": record.created_at,
        "confidence": record.confidence,
        "document_id": record.evidence.document_id,
        "version_id": record.evidence.version_id,
        "chunk_id": record.evidence.chunk_id,
        "evidence_char_start": record.evidence.char_start,
        "evidence_char_end": record.evidence.char_end,
        "evidence_text": record.evidence.quoted_text,
        "access_policy_id": record.evidence.access_policy_id,
        "access_policy_version": record.evidence.access_policy_version,
        "access_groups": sorted(record.evidence.access_groups),
        "origin": trust.origin.value,
        "authority_level": trust.authority.value,
        "governance_status": trust.status.value,
        "ontology_version_id": trust.ontology_version_id,
        "trust_created_at": trust.created_at,
        "extractor_version": trust.extractor_version,
        "prompt_version": trust.prompt_version,
        "reviewed_by": trust.reviewed_by,
        "reviewed_at": trust.reviewed_at,
        "entity_id": record.entity.entity_id,
        "entity_type": record.entity.entity_type,
        "canonical_key": record.entity.canonical_key,
        "canonical_name": record.entity.canonical_name,
        "aliases": list(record.entity.aliases),
        "surface": record.surface,
    }


def _assertion_properties(record: AssertionRecord) -> dict[str, object]:
    subject = record.subject
    properties = {
        "revision_id": record.revision_id,
        "record_id": record.record_id,
        "revision": record.revision.revision,
        "previous_revision": record.revision.expected_previous_revision,
        "tenant_id": record.tenant_id,
        "created_at": record.created_at,
        "confidence": record.confidence,
        "document_id": record.evidence.document_id,
        "version_id": record.evidence.version_id,
        "chunk_id": record.evidence.chunk_id,
        "evidence_char_start": record.evidence.char_start,
        "evidence_char_end": record.evidence.char_end,
        "evidence_text": record.evidence.quoted_text,
        "access_policy_id": record.evidence.access_policy_id,
        "access_policy_version": record.evidence.access_policy_version,
        "access_groups": sorted(record.evidence.access_groups),
        "origin": record.trust.origin.value,
        "authority_level": record.trust.authority.value,
        "governance_status": record.trust.status.value,
        "ontology_version_id": record.trust.ontology_version_id,
        "trust_created_at": record.trust.created_at,
        "extractor_version": record.trust.extractor_version,
        "prompt_version": record.trust.prompt_version,
        "reviewed_by": record.trust.reviewed_by,
        "reviewed_at": record.trust.reviewed_at,
        "subject_entity_id": subject.entity_id,
        "subject_entity_type": subject.entity_type,
        "subject_canonical_key": subject.canonical_key,
        "subject_canonical_name": subject.canonical_name,
        "subject_aliases": list(subject.aliases),
        "predicate": record.predicate,
        "subject_mention_revision_id": record.subject_mention_revision_id,
        "object_kind": record.object_kind,
    }
    if record.object_entity is not None:
        properties.update(
            {
                "object_entity_id": record.object_entity.entity_id,
                "object_entity_type": record.object_entity.entity_type,
                "object_canonical_key": record.object_entity.canonical_key,
                "object_canonical_name": record.object_entity.canonical_name,
                "object_aliases": list(record.object_entity.aliases),
                "object_mention_revision_id": record.object_mention_revision_id,
            }
        )
    else:
        properties["literal_value"] = record.literal_value
        if record.literal_semantics is not None:
            properties.update(record.literal_semantics.to_flat_properties())
    return properties


class _ReadSession:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        return _Result(rows=({"revision": self.properties},))


class _ReadDriver:
    def __init__(self, properties: dict[str, object]) -> None:
        self.session_value = _ReadSession(properties)

    def session(self, *, database: str) -> _ReadSession:
        if database != "neo4j":
            raise AssertionError("unexpected database")
        return self.session_value


class GovernedKnowledgeModelTests(unittest.TestCase):
    def test_trust_presets_keep_authority_independent_from_review_status(self) -> None:
        expert = authoritative_import_trust(
            ontology_version_id="tbox-v1",
            imported_by="expert",
            imported_at=NOW,
        )
        llm = llm_candidate_trust(
            ontology_version_id="tbox-v1",
            extractor_version="extractor-v1",
            prompt_version="prompt-v1",
            extracted_at=NOW,
        )
        self.assertEqual(
            (expert.origin, expert.authority, expert.status),
            (
                KnowledgeOrigin.EXPERT_IMPORT,
                AuthorityLevel.AUTHORITATIVE,
                GovernanceStatus.PUBLISHED,
            ),
        )
        self.assertEqual(
            (llm.origin, llm.authority, llm.status),
            (
                KnowledgeOrigin.LLM_EXTRACTED,
                AuthorityLevel.SECONDARY,
                GovernanceStatus.CANDIDATE,
            ),
        )

    def test_quarantined_llm_trust_is_explicitly_separate_from_candidate(self) -> None:
        trust = llm_quarantined_trust(
            ontology_version_id="tbox-v1",
            extractor_version="extractor-v1",
            prompt_version="prompt-v1",
            extracted_at=NOW,
        )
        self.assertEqual(
            (trust.origin, trust.authority, trust.status),
            (
                KnowledgeOrigin.LLM_EXTRACTED,
                AuthorityLevel.SECONDARY,
                GovernanceStatus.QUARANTINED,
            ),
        )

    def test_revisions_are_deterministic_append_only_and_immutable(self) -> None:
        record_id = knowledge_record_id("tenant-a", "ENTITY_MENTION", "source:1")
        first = RecordRevision.next(record_id, 0)
        second = RecordRevision.next(record_id, 1)
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(first.revision_id, second.revision_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.revision = 2  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "expected_previous_revision"):
            RecordRevision(
                record_id=record_id,
                revision_id=first.revision_id,
                revision=1,
                expected_previous_revision=1,
            )

    def test_exact_evidence_quote_and_acl_snapshot_are_mandatory(self) -> None:
        evidence = _batch().mentions[0].evidence
        with self.assertRaisesRegex(ValueError, "length"):
            dataclasses.replace(evidence, quoted_text=evidence.quoted_text + "x")
        with self.assertRaisesRegex(ValueError, "access_groups"):
            dataclasses.replace(evidence, access_groups=frozenset())

    def test_assertion_requires_matching_endpoint_mentions_inside_evidence(self) -> None:
        batch = _batch()
        assertion = batch.assertions[0]
        invalid = dataclasses.replace(
            assertion,
            subject_mention_revision_id=batch.mentions[1].revision_id,
        )
        with self.assertRaisesRegex(ValueError, "subject"):
            ABoxRecordBatch(batch.tenant_id, batch.mentions, (invalid,))

    def test_dynamic_literal_fact_stays_an_evidence_backed_assertion(self) -> None:
        batch = _batch()
        subject = batch.mentions[0]
        literal = AssertionRecord(
            revision=RecordRevision.next(
                knowledge_record_id(batch.tenant_id, "ASSERTION", "literal:apple"),
                0,
            ),
            tenant_id=batch.tenant_id,
            subject=subject.entity,
            predicate="DISPLAY_NAME",
            evidence=subject.evidence,
            subject_mention_revision_id=subject.revision_id,
            literal_value="Apple",
            confidence=1.0,
            trust=subject.trust,
            created_at=NOW,
        )
        self.assertEqual(literal.object_kind, "literal")
        self.assertFalse(hasattr(subject.entity, "properties"))
        with self.assertRaisesRegex(ValueError, "exact evidence"):
            dataclasses.replace(literal, literal_value="unquoted dynamic value")


class Neo4jKnowledgeStoreUnitTests(unittest.TestCase):
    def test_authoritative_import_validates_source_and_publishes_profiles(self) -> None:
        batch = _batch(authoritative=True)
        driver = _WriteDriver(batch)
        result = Neo4jKnowledgeStore(driver).import_authoritative(batch)

        self.assertEqual(result.mention_count, 2)
        self.assertEqual(result.assertion_count, 1)
        self.assertEqual(result.revision_ids, tuple(
            item.revision_id for item in (*batch.mentions, *batch.assertions)
        ))
        self.assertGreater(driver.tx.profile_writes, 0)
        evidence_calls = [
            call for call in driver.tx.queries if "substring(" in call[0]
        ]
        self.assertEqual(len(evidence_calls), 3)
        for query, parameters in driver.tx.queries:
            self.assertNotIn("tenant-knowledge", query)
            self.assertNotIn("Apple", query)
            if parameters:
                self.assertIsInstance(parameters, dict)

    def test_llm_candidates_never_publish_canonical_entity_profiles(self) -> None:
        batch = _batch(authoritative=False)
        driver = _WriteDriver(batch)
        result = Neo4jKnowledgeStore(driver).persist_llm_candidates(batch)
        self.assertEqual(result.mention_count, 2)
        self.assertEqual(driver.tx.profile_writes, 0)
        self.assertFalse(
            any("MERGE (entity:Entity" in query for query, _ in driver.tx.queries)
        )

    def test_llm_candidate_namespace_must_be_declared_by_the_exact_tbox(self) -> None:
        batch = _batch(authoritative=False)
        driver = _WriteDriver(batch, declare_candidate_namespace=False)

        with self.assertRaisesRegex(KnowledgeSchemaError, "namespace"):
            Neo4jKnowledgeStore(driver).persist_llm_candidates(batch)

        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_llm_candidate_cannot_occupy_an_expert_identity_namespace(self) -> None:
        batch = _rekey_batch(
            _batch(authoritative=False),
            {"Company": "ticker", "Product": "apple-product"},
        )
        driver = _WriteDriver(batch)

        with self.assertRaisesRegex(KnowledgeSchemaError, "namespace"):
            Neo4jKnowledgeStore(driver).persist_llm_candidates(batch)

        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_authoritative_import_cannot_occupy_machine_candidate_namespace(
        self,
    ) -> None:
        batch = _rekey_batch(
            _batch(authoritative=True),
            {"Company": "llm-candidate", "Product": "llm-candidate"},
        )
        driver = _WriteDriver(batch)

        with self.assertRaisesRegex(KnowledgeSchemaError, "namespace"):
            Neo4jKnowledgeStore(driver).import_authoritative(batch)

        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_llm_quarantine_has_its_own_noncanonical_persistence_lane(self) -> None:
        candidate = _batch(authoritative=False)
        trust = llm_quarantined_trust(
            ontology_version_id=candidate.ontology_version_id,
            extractor_version="dashscope-extractor:v1",
            prompt_version="company-tbox:v1",
            extracted_at=NOW,
        )
        batch = dataclasses.replace(
            candidate,
            mentions=tuple(
                dataclasses.replace(item, trust=trust) for item in candidate.mentions
            ),
            assertions=tuple(
                dataclasses.replace(item, trust=trust) for item in candidate.assertions
            ),
        )
        driver = _WriteDriver(batch)
        result = Neo4jKnowledgeStore(driver).persist_llm_quarantined(batch)
        self.assertEqual(result.mention_count, 2)
        self.assertEqual(driver.tx.profile_writes, 0)
        self.assertFalse(
            any("MERGE (entity:Entity" in query for query, _ in driver.tx.queries)
        )

    def test_wrong_trust_lane_fails_before_database_io(self) -> None:
        llm_batch = _batch(authoritative=False)
        driver = _WriteDriver(llm_batch)
        store = Neo4jKnowledgeStore(driver)
        with self.assertRaisesRegex(ValueError, "authoritative"):
            store.import_authoritative(llm_batch)
        self.assertEqual(driver.session_calls, 0)

    def test_stale_revision_is_rejected_by_record_head_cas(self) -> None:
        batch = _batch(authoritative=True)
        driver = _WriteDriver(batch, stale=True)
        with self.assertRaisesRegex(KnowledgeConflict, "stale"):
            Neo4jKnowledgeStore(driver).import_authoritative(batch)

    def test_historical_evidence_is_rejected_inside_the_write_transaction(
        self,
    ) -> None:
        batch = _batch(authoritative=True)
        driver = _WriteDriver(batch, historical_evidence=True)
        with self.assertRaisesRegex(KnowledgeEvidenceError, "does not exist"):
            Neo4jKnowledgeStore(driver).import_authoritative(batch)

        evidence_query = next(
            query
            for query, _ in driver.tx.queries
            if "document_access_groups" in query
        )
        for required_path in (
            "ACTIVE_VERSION",
            "ACTIVE_SNAPSHOT",
            "INCLUDES_CHUNK",
            "OF_VERSION",
            "build_state: 'PUBLISHED'",
        ):
            self.assertIn(required_path, evidence_query)
        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_exact_published_tbox_is_required_before_any_record_write(self) -> None:
        batch = _batch(authoritative=True)
        driver = _WriteDriver(batch, missing_tbox=True)
        with self.assertRaisesRegex(KnowledgeSchemaError, "PUBLISHED"):
            Neo4jKnowledgeStore(driver).import_authoritative(batch)
        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_tbox_enforces_relationship_predicate_domain_and_range(self) -> None:
        batch = _batch(authoritative=True)
        invalid_assertion = dataclasses.replace(
            batch.assertions[0],
            predicate="UNKNOWN_RELATION",
        )
        invalid = dataclasses.replace(batch, assertions=(invalid_assertion,))
        driver = _WriteDriver(invalid)
        with self.assertRaisesRegex(KnowledgeSchemaError, "not declared"):
            Neo4jKnowledgeStore(driver).import_authoritative(invalid)
        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_typed_literal_is_server_normalized_and_written_as_flat_scalars(
        self,
    ) -> None:
        batch = _typed_literal_batch()
        driver = _WriteDriver(batch)
        Neo4jKnowledgeStore(driver).import_authoritative(batch)

        write = next(
            parameters
            for query, parameters in driver.tx.queries
            if "CREATE (revision:GovernedAssertionRevision" in query
        )
        properties = write["properties"]
        self.assertIsInstance(properties, dict)
        assert isinstance(properties, dict)
        self.assertEqual(properties["literal_datatype"], "STRING")
        self.assertEqual(properties["literal_typed_value"], "Apple")
        self.assertEqual(properties["literal_raw_value"], "Apple")
        self.assertEqual(properties["literal_canonical_value"], "Apple")
        self.assertTrue(
            all(
                not isinstance(value, (dict, list))
                for key, value in properties.items()
                if key.startswith("literal_")
            )
        )

    def test_new_literal_write_cannot_bypass_typed_semantics(self) -> None:
        typed = _typed_literal_batch()
        legacy_literal = dataclasses.replace(
            typed.assertions[0],
            literal_semantics=None,
        )
        batch = dataclasses.replace(typed, assertions=(legacy_literal,))
        driver = _WriteDriver(batch)

        with self.assertRaisesRegex(KnowledgeSchemaError, "typed semantics"):
            Neo4jKnowledgeStore(driver).import_authoritative(batch)

        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_forged_canonical_literal_and_single_value_conflict_fail_closed(
        self,
    ) -> None:
        forged = _typed_literal_batch(canonical_value="Forged")
        driver = _WriteDriver(forged)
        with self.assertRaisesRegex(KnowledgeSchemaError, "server normalization"):
            Neo4jKnowledgeStore(driver).import_authoritative(forged)
        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

        batch = _typed_literal_batch()
        first = batch.assertions[0]
        second_record_id = knowledge_record_id(
            batch.tenant_id,
            "ASSERTION",
            "second-display-name",
        )
        second = dataclasses.replace(
            first,
            revision=RecordRevision.next(second_record_id, 0),
        )
        conflicting = dataclasses.replace(batch, assertions=(first, second))
        driver = _WriteDriver(conflicting)
        with self.assertRaisesRegex(KnowledgeSchemaError, "single-valued"):
            Neo4jKnowledgeStore(driver).import_authoritative(conflicting)
        self.assertFalse(
            any("KnowledgeRecordHead" in query for query, _ in driver.tx.queries)
        )

    def test_reads_are_tenant_acl_and_status_scoped_with_published_default(self) -> None:
        mention = _batch(authoritative=True).mentions[0]
        driver = _ReadDriver(_mention_properties(mention))
        principal = Principal(
            "reviewer",
            mention.tenant_id,
            frozenset({"finance-readers"}),
        )
        records = Neo4jKnowledgeStore(driver).list_entity_mentions(
            principal,
            limit=7,
        )
        self.assertEqual(records, (mention,))
        query, parameters = driver.session_value.calls[0]
        self.assertEqual(parameters["tenant_id"], mention.tenant_id)
        self.assertEqual(parameters["groups"], ["finance-readers"])
        self.assertEqual(parameters["statuses"], ["PUBLISHED"])
        self.assertEqual(parameters["limit"], 7)
        self.assertGreaterEqual(query.count("any(group IN $groups"), 3)
        self.assertIn("revision.access_groups = chunk.access_groups", query)

    def test_assertion_read_round_trips_relation_and_explicit_review_status(self) -> None:
        assertion = _batch(authoritative=False).assertions[0]
        driver = _ReadDriver(_assertion_properties(assertion))
        principal = Principal(
            "reviewer",
            assertion.tenant_id,
            frozenset({"finance-readers"}),
        )
        returned = Neo4jKnowledgeStore(driver).get_assertion(
            principal,
            assertion.record_id,
            statuses=(GovernanceStatus.CANDIDATE,),
        )
        self.assertEqual(returned, assertion)
        query, parameters = driver.session_value.calls[0]
        self.assertEqual(parameters["record_id"], assertion.record_id)
        self.assertEqual(parameters["statuses"], ["CANDIDATE"])
        self.assertGreaterEqual(query.count("any(group IN $groups"), 3)

    def test_assertion_read_round_trips_optional_typed_literal_semantics(self) -> None:
        assertion = _typed_literal_batch().assertions[0]
        driver = _ReadDriver(_assertion_properties(assertion))
        principal = Principal(
            "reviewer",
            assertion.tenant_id,
            frozenset({"finance-readers"}),
        )

        returned = Neo4jKnowledgeStore(driver).get_assertion(
            principal,
            assertion.record_id,
        )

        self.assertEqual(returned, assertion)
        assert returned is not None
        self.assertEqual(returned.literal_semantics, assertion.literal_semantics)

    def test_legacy_untyped_literal_remains_readable(self) -> None:
        assertion = dataclasses.replace(
            _typed_literal_batch().assertions[0],
            literal_semantics=None,
        )
        driver = _ReadDriver(_assertion_properties(assertion))
        principal = Principal(
            "reviewer",
            assertion.tenant_id,
            frozenset({"finance-readers"}),
        )

        returned = Neo4jKnowledgeStore(driver).get_assertion(
            principal,
            assertion.record_id,
        )

        self.assertEqual(returned, assertion)
        assert returned is not None
        self.assertIsNone(returned.literal_semantics)


if __name__ == "__main__":
    unittest.main()
