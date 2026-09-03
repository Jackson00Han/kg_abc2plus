"""Trust-aware evidence subgraph projection boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
import unittest

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum, entity_id
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.retrieval.subgraph import (
    EvidenceSubgraphLimits,
    HARD_MAX_ASSERTIONS,
    HARD_MAX_SELECTED_CHUNKS,
    Neo4jEvidenceSubgraphProjector,
    SubgraphProjectionError,
    SubgraphTrustPolicy,
)


TENANT = "tenant-industrial"
CHUNK_ID = "chunk-evidence-1"
CHUNK_TEXT = "Acme owns Pump-7. Pump-7 pressure is 12 bar."
NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


def _principal(
    *, tenant_id: str = TENANT, groups: frozenset[str] | None = None
) -> Principal:
    return Principal(
        "engineer-1",
        tenant_id,
        groups or frozenset({"asset-engineers"}),
    )


def _entity(
    entity_type: str,
    key: str,
    name: str,
    *,
    tenant_id: str = TENANT,
) -> dict[str, object]:
    return {
        "entity_id": entity_id(tenant_id, entity_type, key),
        "tenant_id": tenant_id,
        "entity_type": entity_type,
        "canonical_key": key,
        "canonical_name": name,
        "aliases": [],
    }


COMPANY = _entity("Company", "company-id:ACME", "Acme")
PUMP = _entity("Asset", "asset-id:P-7", "Pump-7")


def _citation(*, tenant_id: str = TENANT) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "chunk_id": CHUNK_ID,
        "chunk_checksum": content_checksum(CHUNK_TEXT),
        "chunk_text": CHUNK_TEXT,
        "document_id": "document-1",
        "document_title": "Plant asset register",
        "canonical_uri": "https://example.com/plant/assets",
        "source_name": "plant-register",
        "version_id": "version-1",
        "version_checksum": content_checksum("version-source"),
        "version_number": 1,
        "ordinal": 0,
        "char_start": 0,
        "char_end": len(CHUNK_TEXT),
        "page_number": 1,
        "section": "Assets",
        "published_at": NOW,
    }


def _revision(
    *,
    record_id: str,
    revision_id: str,
    start: int,
    end: int,
    origin: str,
    authority: str,
    entity: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "revision_id": revision_id,
        "tenant_id": TENANT,
        "document_id": "document-1",
        "version_id": "version-1",
        "chunk_id": CHUNK_ID,
        "evidence_char_start": start,
        "evidence_char_end": end,
        "evidence_text": CHUNK_TEXT[start:end],
        "access_policy_id": "tenant-industrial:asset-engineers",
        "access_policy_version": 1,
        "access_groups": ["asset-engineers"],
        "origin": origin,
        "authority_level": authority,
        "governance_status": "PUBLISHED",
        "ontology_version_id": "tbox-industrial-v1",
        "confidence": 0.97,
        "extractor_version": (
            None if authority == "AUTHORITATIVE" else "qwen-extractor:v1"
        ),
        "prompt_version": (
            None if authority == "AUTHORITATIVE" else "industrial-prompt:v1"
        ),
    }
    if entity is not None:
        result.update(
            {
                "entity_id": entity["entity_id"],
                "entity_type": entity["entity_type"],
            }
        )
    return result


COMPANY_MENTION = _revision(
    record_id="company-mention-record",
    revision_id="company-mention-published",
    start=0,
    end=4,
    origin="EXPERT_IMPORT",
    authority="AUTHORITATIVE",
    entity=COMPANY,
)
PUMP_MENTION = _revision(
    record_id="pump-mention-record",
    revision_id="pump-mention-published",
    start=10,
    end=16,
    origin="EXPERT_IMPORT",
    authority="AUTHORITATIVE",
    entity=PUMP,
)
PUMP_PRESSURE_MENTION = _revision(
    record_id="pump-pressure-mention-record",
    revision_id="pump-pressure-mention-published",
    start=18,
    end=24,
    origin="LLM_EXTRACTED",
    authority="SECONDARY",
    entity=PUMP,
)


def _mention_rows() -> tuple[dict[str, object], ...]:
    return (
        {
            "publication_id": "publication-active-1",
            "entity": COMPANY,
            "mention": COMPANY_MENTION,
            "citation": _citation(),
        },
        {
            "publication_id": "publication-active-1",
            "entity": PUMP,
            "mention": PUMP_MENTION,
            "citation": _citation(),
        },
        {
            "publication_id": "publication-active-1",
            "entity": PUMP,
            "mention": PUMP_PRESSURE_MENTION,
            "citation": _citation(),
        },
    )


def _assertion_rows() -> tuple[dict[str, object], ...]:
    relationship = _revision(
        record_id="owns-record",
        revision_id="owns-published",
        start=0,
        end=17,
        origin="EXPERT_IMPORT",
        authority="AUTHORITATIVE",
    )
    relationship.update(
        {
            "predicate": "OWNS",
            "subject_entity_id": COMPANY["entity_id"],
            "subject_mention_revision_id": COMPANY_MENTION["revision_id"],
            "object_kind": "entity",
            "object_entity_id": PUMP["entity_id"],
            "object_mention_revision_id": PUMP_MENTION["revision_id"],
            "literal_value": None,
        }
    )
    literal = _revision(
        record_id="pressure-record",
        revision_id="pressure-published",
        start=18,
        end=len(CHUNK_TEXT),
        origin="LLM_EXTRACTED",
        authority="SECONDARY",
    )
    literal.update(
        {
            "predicate": "PRESSURE",
            "subject_entity_id": PUMP["entity_id"],
            "subject_mention_revision_id": PUMP_PRESSURE_MENTION["revision_id"],
            "object_kind": "literal",
            "object_entity_id": None,
            "object_mention_revision_id": None,
            "literal_value": "12 bar",
        }
    )
    return (
        {
            "publication_id": "publication-active-1",
            "seed_chunk_id": CHUNK_ID,
            "seed_entity_id": COMPANY["entity_id"],
            "assertion": relationship,
            "subject": COMPANY,
            "object": PUMP,
            "subject_mention": COMPANY_MENTION,
            "object_mention": PUMP_MENTION,
            "citation": _citation(),
        },
        {
            "publication_id": "publication-active-1",
            "seed_chunk_id": CHUNK_ID,
            "seed_entity_id": PUMP["entity_id"],
            "assertion": literal,
            "subject": PUMP,
            "object": None,
            "subject_mention": PUMP_PRESSURE_MENTION,
            "object_mention": None,
            "citation": _citation(),
        },
    )


class _Result:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _Session:
    def __init__(
        self,
        *,
        assertion_rows: tuple[dict[str, object], ...] | None = None,
        mention_rows: tuple[dict[str, object], ...] | None = None,
        required_group: str = "asset-engineers",
    ) -> None:
        self.assertion_rows = (
            assertion_rows if assertion_rows is not None else _assertion_rows()
        )
        self.mention_rows = (
            mention_rows if mention_rows is not None else _mention_rows()
        )
        self.required_group = required_group
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if self.required_group not in parameters["groups"]:  # type: ignore[operator]
            return _Result(())
        authority_levels = set(parameters["authority_levels"])  # type: ignore[arg-type]
        if "governed-subgraph:assertions" in query:
            rows = tuple(
                row
                for row in self.assertion_rows
                if row["assertion"]["authority_level"]  # type: ignore[index]
                in authority_levels
                and row["subject_mention"]["authority_level"]  # type: ignore[index]
                in authority_levels
                and (
                    row["object_mention"] is None
                    or row["object_mention"][  # type: ignore[index]
                        "authority_level"
                    ]
                    in authority_levels
                )
            )
            return _Result(rows[: parameters["assertion_limit"]])  # type: ignore[index]
        if "governed-subgraph:mentions" in query:
            rows = tuple(
                row
                for row in self.mention_rows
                if row["mention"]["authority_level"]  # type: ignore[index]
                in authority_levels
            )
            return _Result(rows[: parameters["mention_limit"]])  # type: ignore[index]
        raise AssertionError("unexpected query")


class _Driver:
    def __init__(self, session: _Session) -> None:
        self.test_session = session
        self.session_calls = 0

    def session(self, *, database: str) -> _Session:
        self.session_calls += 1
        if database != "neo4j":
            raise AssertionError("unexpected database")
        return self.test_session


class EvidenceSubgraphProjectionTests(unittest.TestCase):
    def test_default_returns_published_authoritative_and_secondary_evidence(
        self,
    ) -> None:
        session = _Session()
        result = Neo4jEvidenceSubgraphProjector(_Driver(session)).project(
            _principal(),
            (CHUNK_ID,),
        )

        self.assertEqual(
            result.trust_policy,
            SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE,
        )
        self.assertEqual(len(result.entities), 2)
        self.assertEqual(len(result.relationship_assertions), 1)
        self.assertEqual(len(result.literal_assertions), 1)
        self.assertEqual(len(result.paths), 2)
        self.assertEqual(result.matched_chunk_ids, (CHUNK_ID,))
        self.assertEqual(result.publication_ids, ("publication-active-1",))
        self.assertEqual(
            {
                item.evidence.provenance.authority.value
                for item in result.assertions
            },
            {"AUTHORITATIVE", "SECONDARY"},
        )
        relationship = result.relationship_assertions[0]
        self.assertEqual(
            relationship.subject_mention_revision_id,
            "company-mention-published",
        )
        self.assertEqual(
            relationship.object_mention_revision_id,
            "pump-mention-published",
        )
        self.assertEqual(relationship.evidence.quoted_text, "Acme owns Pump-7.")

    def test_authoritative_only_is_a_query_filter_not_a_custom_score(self) -> None:
        session = _Session()
        result = Neo4jEvidenceSubgraphProjector(_Driver(session)).project(
            _principal(),
            (CHUNK_ID,),
            trust_policy=SubgraphTrustPolicy.AUTHORITATIVE_ONLY,
        )

        self.assertEqual(len(result.relationship_assertions), 1)
        self.assertEqual(result.literal_assertions, ())
        self.assertTrue(
            all(
                evidence.provenance.authority.value == "AUTHORITATIVE"
                for node in result.entities
                for evidence in node.evidence
            )
        )
        for _query, parameters in session.calls:
            self.assertEqual(parameters["authority_levels"], ["AUTHORITATIVE"])

    def test_typed_literal_semantics_are_projected_without_rescoring(self) -> None:
        semantics = TypedLiteralValue(
            datatype="DECIMAL",
            typed_value="1200",
            raw_value="12",
            raw_unit="bar",
            canonical_value="1200",
            canonical_unit="kPa",
        )
        rows = list(_assertion_rows())
        literal_row = dict(rows[1])
        literal_assertion = dict(literal_row["assertion"])  # type: ignore[arg-type]
        literal_assertion["literal_value"] = "12"
        literal_assertion.update(semantics.to_flat_properties())
        literal_row["assertion"] = literal_assertion
        rows[1] = literal_row

        result = Neo4jEvidenceSubgraphProjector(
            _Driver(_Session(assertion_rows=tuple(rows)))
        ).project(_principal(), (CHUNK_ID,))

        self.assertEqual(result.literal_assertions[0].literal_semantics, semantics)
        literal_path = next(
            item for item in result.paths if item.literal_value is not None
        )
        self.assertEqual(literal_path.literal_semantics, semantics)

    def test_partial_typed_literal_storage_fails_closed(self) -> None:
        rows = list(_assertion_rows())
        literal_row = dict(rows[1])
        literal_assertion = dict(literal_row["assertion"])  # type: ignore[arg-type]
        literal_assertion["literal_datatype"] = "DECIMAL"
        literal_row["assertion"] = literal_assertion
        rows[1] = literal_row

        with self.assertRaises(SubgraphProjectionError):
            Neo4jEvidenceSubgraphProjector(
                _Driver(_Session(assertion_rows=tuple(rows)))
            ).project(_principal(), (CHUNK_ID,))

    def test_acl_denial_returns_no_ids_or_existence_signal(self) -> None:
        result = Neo4jEvidenceSubgraphProjector(_Driver(_Session())).project(
            _principal(groups=frozenset({"unrelated"})),
            (CHUNK_ID,),
        )
        self.assertEqual(result.entities, ())
        self.assertEqual(result.assertions, ())
        self.assertEqual(result.paths, ())
        self.assertEqual(result.matched_chunk_ids, ())
        self.assertEqual(result.publication_ids, ())

    def test_cross_tenant_row_fails_closed(self) -> None:
        rows = list(_mention_rows())
        rows[0] = {**rows[0], "citation": _citation(tenant_id="tenant-other")}
        projector = Neo4jEvidenceSubgraphProjector(
            _Driver(_Session(assertion_rows=(), mention_rows=tuple(rows)))
        )
        with self.assertRaises(SubgraphProjectionError):
            projector.project(_principal(), (CHUNK_ID,))

    def test_limits_fail_before_database_access_and_bound_query_rows(self) -> None:
        driver = _Driver(_Session())
        projector = Neo4jEvidenceSubgraphProjector(driver)
        with self.assertRaises(ValueError):
            EvidenceSubgraphLimits(max_assertions=HARD_MAX_ASSERTIONS + 1)
        with self.assertRaises(ValueError):
            projector.project(
                _principal(),
                tuple(
                    f"chunk-{index}"
                    for index in range(HARD_MAX_SELECTED_CHUNKS + 1)
                ),
            )
        self.assertEqual(driver.session_calls, 0)

        limits = EvidenceSubgraphLimits(
            max_entities=2,
            max_assertions=2,
            max_paths=1,
            max_mentions_per_entity=1,
        )
        session = _Session()
        result = Neo4jEvidenceSubgraphProjector(_Driver(session)).project(
            _principal(), (CHUNK_ID,), limits=limits
        )
        self.assertLessEqual(len(result.entities), 2)
        self.assertLessEqual(len(result.assertions), 2)
        self.assertLessEqual(len(result.paths), 1)
        assertion_call = next(
            parameters
            for query, parameters in session.calls
            if "governed-subgraph:assertions" in query
        )
        self.assertEqual(assertion_call["assertion_limit"], 2)
        self.assertEqual(assertion_call["seed_entity_limit"], 2)

    def test_queries_parameterize_ids_and_bind_all_security_paths(self) -> None:
        session = _Session(assertion_rows=(), mention_rows=())
        selected_id = "chunk-user-controlled-' MATCH (n) RETURN n //"
        Neo4jEvidenceSubgraphProjector(_Driver(session)).project(
            _principal(),
            (selected_id,),
            trust_policy=SubgraphTrustPolicy.AUTHORITATIVE_ONLY,
        )

        for query, parameters in session.calls:
            self.assertNotIn(selected_id, query)
            self.assertEqual(parameters["chunk_ids"], [selected_id])
            for required in (
                "tenant_id: $tenant_id",
                "ACTIVE_KNOWLEDGE_PUBLICATION",
                "status: 'ACTIVE'",
                "governance_status: 'PUBLISHED'",
                "ACTIVE_SNAPSHOT",
                "ACTIVE_VERSION",
                "USES_KNOWLEDGE_SNAPSHOT",
                "ACTIVE_TBOX_VERSION",
                "DECLARES_ENTITY_TYPE",
                "authority_level IN $authority_levels",
                "substring(",
                "any(group IN $groups",
            ):
                self.assertIn(required, query)
        assertion_query = next(
            query
            for query, _parameters in session.calls
            if "governed-subgraph:assertions" in query
        )
        self.assertIn("DECLARES_RELATIONSHIP_TYPE", assertion_query)
        self.assertIn("DECLARES_PROPERTY", assertion_query)
        self.assertIn(
            "WHERE object_type.name = object.entity_type",
            assertion_query,
        )
        self.assertNotIn(
            "object IS NOT NULL AND object_type.name",
            assertion_query,
        )


if __name__ == "__main__":
    unittest.main()
