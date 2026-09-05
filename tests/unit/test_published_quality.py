"""Unit tests for the bounded active-publication quality audit."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from typing import Any, Self

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    assertion_id,
    mention_id,
    relationship_property_value_id,
)
from graphrag_prod.domain.models import (
    RelationshipPropertyValue,
    TypedLiteralValue,
    canonical_relationship_object_reference,
)
from graphrag_prod.graph.published_quality import (
    _ENTITIES_QUERY,
    _REVISIONS_QUERY,
    _STATE_QUERY,
    Neo4jPublishedGraphQualityService,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityLimits,
    PublishedGraphQualityUnavailable,
)
from graphrag_prod.ontology.models import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)

TENANT = "tenant-industrial"
MANIFEST_HASH = "a" * 64


def _principal(*, capable: bool = True) -> Principal:
    return Principal(
        principal_id="quality-reviewer",
        tenant_id=TENANT,
        groups=frozenset({"plant-readers", "public"}),
        capabilities=(frozenset({"knowledge:quality"}) if capable else frozenset()),
    )


def _tbox() -> TBoxVersion:
    return TBoxVersion(
        tenant_id=TENANT,
        key="industrial-core",
        version=1,
        status=TBoxStatus.PUBLISHED,
        entity_types=(
            EntityTypeDefinition("Company", ("company",)),
            EntityTypeDefinition("Facility", ("facility",)),
        ),
        relationship_types=(
            RelationshipTypeDefinition(
                "OWNS",
                source_types=("Company",),
                target_types=("Facility",),
            ),
        ),
    )


def _state(
    revision_ids: tuple[str, ...],
    *,
    tbox_value: TBoxVersion | None = None,
) -> dict[str, Any]:
    tbox = tbox_value or _tbox()
    return {
        "publication": {
            "publication_id": "publication-7",
            "tenant_id": TENANT,
            "generation": 7,
            "manifest_hash": MANIFEST_HASH,
            "ontology_version_id": tbox.tbox_id,
            "status": "ACTIVE",
        },
        "active_link_count": 1,
        "manifest_revision_ids": list(revision_ids),
        "manifest_revision_count": len(revision_ids),
        "tbox": {
            "tbox_id": tbox.tbox_id,
            "tenant_id": tbox.tenant_id,
            "key": tbox.key,
            "version": tbox.version,
            "status": tbox.status.value,
            "checksum": tbox.checksum,
            "definition_json": json.dumps(
                tbox.with_status(TBoxStatus.DRAFT).to_mapping(),
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
        "exact_tbox_link_count": 1,
        "wrong_tbox_link_count": 0,
        "corpus_revision": 19,
        "acl_complete": True,
    }


def _base_revision(
    revision_id: str,
    *,
    chunk_id: str,
    record_kind: str,
) -> dict[str, Any]:
    tbox = _tbox()
    row: dict[str, Any] = {
        "revision": {
            "revision_id": revision_id,
            "record_id": f"record-{revision_id}",
            "revision": 1,
            "tenant_id": TENANT,
            "ontology_version_id": tbox.tbox_id,
            "governance_status": "PUBLISHED",
            "origin": "EXPERT_IMPORT",
            "authority_level": "AUTHORITATIVE",
            "document_id": "document-1",
            "version_id": "version-1",
            "chunk_id": chunk_id,
            "access_policy_id": "policy-1",
            "access_policy_version": 1,
            "access_groups": ["plant-readers"],
            "evidence_char_start": 0,
            "evidence_char_end": 10,
            "confidence": 1.0,
            "extractor_version": "expert-extractor:v1",
        },
        "labels": [
            "GovernedEntityMentionRevision"
            if record_kind == "ENTITY_MENTION"
            else "GovernedAssertionRevision"
        ],
        "publication_record_kinds": [record_kind],
        "head_count": 1,
        "current_pointer_count": 1,
        "matching_current_count": 1,
        "head_tenant_id": TENANT,
        "head_record_kind": record_kind,
        "head_current_revision": 1,
        "literal_source_tokens_valid": True,
        "evidence_link_count": 1,
        "evidence_chunk_count": 1,
        "evidence_chunk_id": chunk_id,
        "evidence_document_count": 1,
        "active_snapshot_count": 1,
        "valid_evidence_path_count": 1,
        "entity_link_count": 0,
        "linked_entity_id": None,
        "linked_entity_type": None,
        "linked_entity_tenant_id": None,
        "subject_link_count": 0,
        "linked_subject_id": None,
        "linked_subject_type": None,
        "linked_subject_tenant_id": None,
        "object_link_count": 0,
        "linked_object_id": None,
        "linked_object_type": None,
        "linked_object_tenant_id": None,
        "support_link_count": 0,
        "matching_subject_mention_count": 0,
        "matching_object_mention_count": 0,
        "navigation_mention_count": 1,
        "active_mention_membership_count": 1,
        "navigation_mention_id": None,
        "valid_navigation_mention_count": 1,
        "navigation_assertion_count": 1,
        "active_assertion_membership_count": 1,
        "navigation_assertion_id": None,
        "valid_navigation_assertion_count": 1,
        "materialized_property_link_count": 0,
        "materialized_property_count": 0,
        "valid_materialized_property_count": 0,
        "materialized_property_values": [],
        "publication_membership_count": 1,
    }
    return row


def _mention(
    revision_id: str,
    *,
    chunk_id: str,
    entity_id: str,
    entity_type: str,
    canonical_key: str,
) -> dict[str, Any]:
    row = _base_revision(revision_id, chunk_id=chunk_id, record_kind="ENTITY_MENTION")
    surface = canonical_key.partition(":")[2]
    row["revision"].update(
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_key=canonical_key,
        surface=surface,
        evidence_char_end=len(surface),
    )
    row.update(
        entity_link_count=1,
        linked_entity_id=entity_id,
        linked_entity_type=entity_type,
        linked_entity_tenant_id=TENANT,
        navigation_mention_id=mention_id(
            chunk_id,
            entity_type,
            0,
            len(surface),
            surface,
            "expert-extractor:v1",
        ),
    )
    return row


def _set_assertion_navigation_id(row: dict[str, Any]) -> None:
    revision = row["revision"]
    relationship_properties = tuple(
        RelationshipPropertyValue.from_mapping(item)
        for item in json.loads(
            revision.get("relationship_properties_json", "[]")
        )
    )
    if revision["object_kind"] == "entity":
        reference = canonical_relationship_object_reference(
            revision["object_entity_id"],
            relationship_properties,
        )
    else:
        literal = TypedLiteralValue.from_flat_properties(revision)
        reference = (
            literal.identity_reference
            if literal is not None
            else revision["literal_value"]
        )
    row["navigation_assertion_id"] = assertion_id(
        revision["tenant_id"],
        revision["subject_entity_id"],
        revision["predicate"],
        revision["object_kind"],
        reference,
        revision["chunk_id"],
        revision["evidence_char_start"],
        revision["evidence_char_end"],
        revision["extractor_version"],
        revision["ontology_version_id"],
    )


def _assertion() -> dict[str, Any]:
    row = _base_revision(
        "assertion-owns", chunk_id="chunk-rel", record_kind="ASSERTION"
    )
    row["revision"].update(
        subject_entity_id="entity-company",
        subject_entity_type="Company",
        subject_canonical_key="company:northstar",
        predicate="OWNS",
        object_kind="entity",
        object_entity_id="entity-facility",
        object_entity_type="Facility",
        object_canonical_key="facility:plant-7",
        subject_mention_revision_id="mention-company",
        object_mention_revision_id="mention-facility",
    )
    row.update(
        subject_link_count=1,
        linked_subject_id="entity-company",
        linked_subject_type="Company",
        linked_subject_tenant_id=TENANT,
        object_link_count=1,
        linked_object_id="entity-facility",
        linked_object_type="Facility",
        linked_object_tenant_id=TENANT,
        support_link_count=2,
        matching_subject_mention_count=1,
        matching_object_mention_count=1,
    )
    _set_assertion_navigation_id(row)
    return row


def _revisions() -> list[dict[str, Any]]:
    return [
        _mention(
            "mention-company",
            chunk_id="chunk-company",
            entity_id="entity-company",
            entity_type="Company",
            canonical_key="company:northstar",
        ),
        _mention(
            "mention-facility",
            chunk_id="chunk-facility",
            entity_id="entity-facility",
            entity_type="Facility",
            canonical_key="facility:plant-7",
        ),
        _assertion(),
    ]


def _entities() -> list[dict[str, Any]]:
    return [
        {
            "entity": {
                "entity_id": "entity-company",
                "tenant_id": TENANT,
                "entity_type": "Company",
                "canonical_key": "company:northstar",
            },
            "published_mention_count": 1,
            "published_degree": 1,
            "active_membership_count": 1,
            "sample_chunk_id": "chunk-company",
        },
        {
            "entity": {
                "entity_id": "entity-facility",
                "tenant_id": TENANT,
                "entity_type": "Facility",
                "canonical_key": "facility:plant-7",
            },
            "published_mention_count": 1,
            "published_degree": 1,
            "active_membership_count": 1,
            "sample_chunk_id": "chunk-facility",
        },
    ]


class _Rows:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self.values = values

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.values)


class _Step:
    def __init__(
        self,
        marker: str,
        rows: list[dict[str, Any]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.marker = marker
        self.rows = rows or []
        self.error = error


class _Transaction:
    def __init__(self, steps: list[_Step]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **parameters: Any) -> _Rows:
        if not isinstance(query, str):
            raise TypeError("managed transaction queries must be strings")
        if not self.steps:
            raise AssertionError("unexpected transaction query")
        step = self.steps.pop(0)
        if step.marker not in query:
            raise AssertionError(f"expected query marker {step.marker!r}")
        self.calls.append((query, parameters))
        if step.error is not None:
            raise step.error
        return _Rows(step.rows)


class _Session:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_read(self, work: Any, principal: Principal) -> Any:
        self.driver.execute_read_calls += 1
        self.driver.work_metadata = dict(work.metadata)
        self.driver.work_timeout = work.timeout
        if self.driver.execute_error is not None:
            raise self.driver.execute_error
        return work(self.driver.transaction, principal)


class _Driver:
    def __init__(
        self,
        steps: list[_Step],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self.transaction = _Transaction(steps)
        self.execute_error = execute_error
        self.databases: list[str] = []
        self.execute_read_calls = 0
        self.work_metadata: dict[str, str] | None = None
        self.work_timeout: float | None = None

    def session(self, *, database: str) -> _Session:
        self.databases.append(database)
        return _Session(self)


def _driver(
    *,
    state: dict[str, Any] | None = None,
    revisions: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
) -> _Driver:
    selected_revisions = _revisions() if revisions is None else revisions
    selected_entities = _entities() if entities is None else entities
    selected_state = (
        _state(tuple(row["revision"]["revision_id"] for row in selected_revisions))
        if state is None
        else state
    )
    return _Driver(
        [
            _Step("published-quality:state", [selected_state]),
            _Step("published-quality:revisions", selected_revisions),
            _Step("published-quality:entities", selected_entities),
        ]
    )


class PublishedGraphQualityTests(unittest.TestCase):
    def test_clean_publication_passes_in_one_bounded_read_transaction(self) -> None:
        driver = _driver()
        service = Neo4jPublishedGraphQualityService(
            driver,
            database=" governed ",
            limits=PublishedGraphQualityLimits(transaction_timeout_seconds=4.5),
        )

        report = service.audit(_principal())

        self.assertTrue(report.passed)
        self.assertEqual(report.total_issue_count, 0)
        self.assertEqual(report.publication_id, "publication-7")
        self.assertEqual(report.publication_generation, 7)
        self.assertEqual(report.manifest_hash, MANIFEST_HASH)
        self.assertEqual(report.ontology_version_id, _tbox().tbox_id)
        self.assertEqual(report.tbox_checksum, _tbox().checksum)
        self.assertEqual(report.corpus_revision, 19)
        self.assertEqual(len(report.graph_digest), 64)
        self.assertEqual(
            dict(report.counts),
            {
                "assertions": 1,
                "canonical_entities": 2,
                "entity_mentions": 2,
                "literal_assertions": 0,
                "relationship_assertions": 1,
                "revisions": 3,
            },
        )
        self.assertEqual(driver.databases, ["governed"])
        self.assertEqual(driver.execute_read_calls, 1)
        self.assertEqual(
            driver.work_metadata,
            {"component": "graphrag-published-quality", "operation": "audit"},
        )
        self.assertEqual(driver.work_timeout, 4.5)
        self.assertEqual(len(driver.transaction.calls), 3)
        self.assertEqual(driver.transaction.steps, [])
        state_params = driver.transaction.calls[0][1]
        self.assertEqual(state_params["tenant_id"], TENANT)
        self.assertEqual(state_params["groups"], ["plant-readers", "public"])
        for _, parameters in driver.transaction.calls[1:]:
            self.assertEqual(parameters["publication_id"], "publication-7")
            self.assertEqual(parameters["manifest_hash"], MANIFEST_HASH)
            self.assertEqual(parameters["publication_generation"], 7)
            self.assertEqual(parameters["ontology_version_id"], _tbox().tbox_id)

    def test_missing_capability_fails_before_database_access(self) -> None:
        driver = _driver()
        service = Neo4jPublishedGraphQualityService(driver)

        with self.assertRaises(PublishedGraphQualityAuthorizationError):
            service.audit(_principal(capable=False))

        self.assertEqual(driver.databases, [])
        self.assertEqual(driver.transaction.calls, [])

    def test_partial_acl_fails_closed_before_counts_or_graph_rows(self) -> None:
        state = _state(("mention-company",))
        state["acl_complete"] = False
        driver = _Driver([_Step("published-quality:state", [state])])

        with self.assertRaises(PublishedGraphQualityAuthorizationError) as raised:
            Neo4jPublishedGraphQualityService(driver).audit(_principal())

        self.assertNotIn("revision", str(raised.exception).casefold())
        self.assertEqual(len(driver.transaction.calls), 1)
        self.assertEqual(driver.transaction.steps, [])

    def test_no_or_multiple_active_publication_is_a_clear_conflict(self) -> None:
        for rows in ([], [_state(("one",)), _state(("two",))]):
            with self.subTest(row_count=len(rows)):
                driver = _Driver([_Step("published-quality:state", rows)])
                with self.assertRaises(PublishedGraphQualityConflict):
                    Neo4jPublishedGraphQualityService(driver).audit(_principal())

    def test_revision_and_entity_caps_use_max_plus_one_and_fail_closed(self) -> None:
        revisions = _revisions()
        state = _state(("one", "two"))
        revision_driver = _Driver(
            [
                _Step("published-quality:state", [state]),
                _Step("published-quality:revisions", revisions),
            ]
        )
        limits = PublishedGraphQualityLimits(max_revisions=2, max_entities=2)
        with self.assertRaises(PublishedGraphQualityLimitExceeded):
            Neo4jPublishedGraphQualityService(revision_driver, limits=limits).audit(
                _principal()
            )
        self.assertEqual(revision_driver.transaction.calls[1][1]["revision_limit"], 3)
        self.assertEqual(len(revision_driver.transaction.calls), 2)

        entity_driver = _driver()
        entity_limits = PublishedGraphQualityLimits(max_revisions=3, max_entities=1)
        with self.assertRaises(PublishedGraphQualityLimitExceeded):
            Neo4jPublishedGraphQualityService(
                entity_driver, limits=entity_limits
            ).audit(_principal())
        self.assertEqual(entity_driver.transaction.calls[2][1]["entity_limit"], 2)

    def test_order_does_not_change_stable_report_or_run_id(self) -> None:
        first = Neo4jPublishedGraphQualityService(_driver()).audit(_principal())
        second_driver = _driver(
            revisions=list(reversed(_revisions())),
            entities=list(reversed(_entities())),
        )
        second = Neo4jPublishedGraphQualityService(second_driver).audit(_principal())

        self.assertEqual(first, second)
        self.assertEqual(first.run_id, second.run_id)

    def test_corruption_codes_are_reported_without_source_text(self) -> None:
        clean = Neo4jPublishedGraphQualityService(_driver()).audit(_principal())
        revisions = _revisions()
        revisions[-1]["revision"]["authority_level"] = "SECONDARY"
        revisions[-1]["valid_evidence_path_count"] = 0
        revisions[-1]["revision"]["predicate"] = "SECRET-PREDICATE"
        revisions[-1]["revision"]["literal_raw_value"] = "do-not-leak-source"
        entities = _entities()
        entities[0]["published_mention_count"] = 0
        entities[1]["published_degree"] = 1_000
        report = Neo4jPublishedGraphQualityService(
            _driver(revisions=revisions, entities=entities)
        ).audit(_principal())

        codes = {issue.code for issue in report.issues}
        self.assertTrue(
            {
                "ORIGIN_AUTHORITY_INVALID",
                "EVIDENCE_ACTIVE_SNAPSHOT_INVALID",
                "RELATIONSHIP_PATTERN_INVALID",
                "ORPHAN_ENTITY",
                "ANOMALOUS_HUB",
            }
            <= codes
        )
        serialized = report.to_json() + repr(report)
        self.assertNotEqual(report.run_id, clean.run_id)
        self.assertNotIn("do-not-leak-source", serialized)
        self.assertNotIn("SECRET-PREDICATE", serialized)

    def test_active_navigation_projection_tampering_is_reported(self) -> None:
        revisions = _revisions()
        revisions[0]["valid_navigation_mention_count"] = 0
        revisions[-1]["valid_navigation_assertion_count"] = 0

        report = Neo4jPublishedGraphQualityService(
            _driver(revisions=revisions)
        ).audit(_principal())

        self.assertTrue(
            {
                "ACTIVE_MENTION_PROJECTION_INVALID",
                "ACTIVE_ASSERTION_PROJECTION_INVALID",
            }
            <= {issue.code for issue in report.issues}
        )

    def test_relationship_endpoint_cardinality_uses_complete_publication(self) -> None:
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="industrial-endpoints",
            version=1,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition("Company", ("company",)),
                EntityTypeDefinition("Facility", ("facility",)),
            ),
            relationship_types=(
                RelationshipTypeDefinition(
                    "OWNS",
                    source_types=("Company",),
                    target_types=("Facility",),
                    source_cardinality=Cardinality.ONE,
                ),
            ),
        )
        revisions = _revisions()
        for row in revisions:
            row["revision"]["ontology_version_id"] = tbox.tbox_id
            if "GovernedAssertionRevision" in row["labels"]:
                _set_assertion_navigation_id(row)
        clean_state = _state(
            tuple(row["revision"]["revision_id"] for row in revisions),
            tbox_value=tbox,
        )
        clean = Neo4jPublishedGraphQualityService(
            _driver(state=clean_state, revisions=revisions)
        ).audit(_principal())
        self.assertTrue(clean.passed, clean.to_json())

        missing = revisions[:2]
        missing_state = _state(
            tuple(row["revision"]["revision_id"] for row in missing),
            tbox_value=tbox,
        )
        missing_report = Neo4jPublishedGraphQualityService(
            _driver(state=missing_state, revisions=missing)
        ).audit(_principal())
        self.assertIn(
            "RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID",
            {issue.code for issue in missing_report.issues},
        )

        second_mention = _mention(
            "mention-facility-2",
            chunk_id="chunk-facility-2",
            entity_id="entity-facility-2",
            entity_type="Facility",
            canonical_key="facility:plant-8",
        )
        second_mention["revision"]["ontology_version_id"] = tbox.tbox_id
        second_assertion = _assertion()
        second_assertion["revision"].update(
            revision_id="assertion-owns-2",
            record_id="record-assertion-owns-2",
            object_entity_id="entity-facility-2",
            object_canonical_key="facility:plant-8",
            object_mention_revision_id="mention-facility-2",
            ontology_version_id=tbox.tbox_id,
        )
        second_assertion.update(
            linked_object_id="entity-facility-2",
            navigation_assertion_id=None,
        )
        _set_assertion_navigation_id(second_assertion)
        overflow = [*revisions, second_mention, second_assertion]
        overflow_entities = [
            *_entities(),
            {
                "entity": {
                    "entity_id": "entity-facility-2",
                    "tenant_id": TENANT,
                    "entity_type": "Facility",
                    "canonical_key": "facility:plant-8",
                },
                "published_mention_count": 1,
                "published_degree": 1,
                "active_membership_count": 1,
                "sample_chunk_id": "chunk-facility-2",
            },
        ]
        overflow_state = _state(
            tuple(row["revision"]["revision_id"] for row in overflow),
            tbox_value=tbox,
        )
        overflow_report = Neo4jPublishedGraphQualityService(
            _driver(
                state=overflow_state,
                revisions=overflow,
                entities=overflow_entities,
            )
        ).audit(_principal())
        self.assertIn(
            "RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID",
            {issue.code for issue in overflow_report.issues},
        )

    def test_typed_literal_and_relationship_property_contracts_pass(self) -> None:
        relationship_property = PropertyDefinition(
            name="basis",
            datatype=PropertyDataType.STRING,
            required=True,
            cardinality=Cardinality.ONE,
        )
        entity_property = PropertyDefinition(
            name="rating",
            datatype=PropertyDataType.STRING,
            required=True,
            cardinality=Cardinality.ONE,
        )
        tbox = TBoxVersion(
            tenant_id=TENANT,
            key="industrial-properties",
            version=1,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Company", ("company",), properties=(entity_property,)
                ),
                EntityTypeDefinition("Facility", ("facility",)),
            ),
            relationship_types=(
                RelationshipTypeDefinition(
                    "OWNS",
                    source_types=("Company",),
                    target_types=("Facility",),
                    properties=(relationship_property,),
                ),
            ),
        )
        revisions = _revisions()
        relationship = revisions[-1]
        property_literal = TypedLiteralValue(
            datatype="STRING",
            typed_value="owns",
            raw_value="owns",
            canonical_value="owns",
        )
        property_value = RelationshipPropertyValue(
            property_value_id=relationship_property_value_id(
                TENANT,
                "OWNS",
                "basis",
                property_literal.identity_reference,
                "chunk-rel",
                1,
                5,
                "expert-reviewed-v1",
                tbox.tbox_id,
            ),
            tenant_id=TENANT,
            relationship_type="OWNS",
            name="basis",
            literal_semantics=property_literal,
            evidence_chunk_id="chunk-rel",
            evidence_char_start=1,
            evidence_char_end=5,
            evidence_text="owns",
            extractor_version="expert-reviewed-v1",
            schema_version=tbox.tbox_id,
        )
        relationship["revision"].update(
            relationship_properties_format_version=1,
            relationship_properties_json=json.dumps(
                [property_value.to_mapping()], separators=(",", ":"), sort_keys=True
            ),
        )
        relationship["materialized_property_count"] = 1
        relationship["materialized_property_link_count"] = 1
        relationship["valid_materialized_property_count"] = 1
        relationship["materialized_property_values"] = [
            {
                "property_value_id": property_value.property_value_id,
                "tenant_id": property_value.tenant_id,
                "relationship_type": property_value.relationship_type,
                "name": property_value.name,
                **property_value.literal_semantics.to_flat_properties(),
                "evidence_chunk_id": property_value.evidence_chunk_id,
                "evidence_char_start": property_value.evidence_char_start,
                "evidence_char_end": property_value.evidence_char_end,
                "extractor_version": property_value.extractor_version,
                "schema_version": property_value.schema_version,
                "confidence": property_value.confidence,
                "document_id": "document-1",
                "version_id": "version-1",
                "access_policy_id": "policy-1",
                "access_policy_version": 1,
                "access_groups": ["plant-readers"],
            }
        ]

        literal = TypedLiteralValue(
            datatype="STRING",
            typed_value="A",
            raw_value="A",
            canonical_value="A",
        )
        literal_row = _base_revision(
            "assertion-rating", chunk_id="chunk-company", record_kind="ASSERTION"
        )
        literal_row["revision"].update(
            subject_entity_id="entity-company",
            subject_entity_type="Company",
            subject_canonical_key="company:northstar",
            predicate="rating",
            object_kind="literal",
            subject_mention_revision_id="mention-company",
            literal_value="A",
            **literal.to_flat_properties(),
        )
        literal_row.update(
            subject_link_count=1,
            linked_subject_id="entity-company",
            linked_subject_type="Company",
            linked_subject_tenant_id=TENANT,
            support_link_count=1,
            matching_subject_mention_count=1,
        )
        revisions.append(literal_row)
        for row in revisions:
            row["revision"]["ontology_version_id"] = tbox.tbox_id
            if "GovernedAssertionRevision" in row["labels"]:
                _set_assertion_navigation_id(row)
        revision_ids = tuple(row["revision"]["revision_id"] for row in revisions)
        state = _state(revision_ids, tbox_value=tbox)

        report = Neo4jPublishedGraphQualityService(
            _driver(state=state, revisions=revisions)
        ).audit(_principal())

        self.assertTrue(report.passed, report.to_json())
        self.assertEqual(report.total_issue_count, 0)

        broken = deepcopy(revisions)
        broken[-2]["revision"]["relationship_properties_json"] = "[]"
        broken[-2]["materialized_property_count"] = 0
        broken[-2]["materialized_property_link_count"] = 0
        broken[-2]["valid_materialized_property_count"] = 0
        broken[-2]["materialized_property_values"] = []
        _set_assertion_navigation_id(broken[-2])
        broken_report = Neo4jPublishedGraphQualityService(
            _driver(state=state, revisions=broken)
        ).audit(_principal())
        self.assertIn(
            "RELATIONSHIP_PROPERTY_SCHEMA_INVALID",
            {issue.code for issue in broken_report.issues},
        )

        materialized_tamper = deepcopy(revisions)
        materialized_tamper[-2]["materialized_property_values"][0][
            "confidence"
        ] = 0.5
        materialized_report = Neo4jPublishedGraphQualityService(
            _driver(state=state, revisions=materialized_tamper)
        ).audit(_principal())
        self.assertIn(
            "RELATIONSHIP_PROPERTY_MATERIALIZATION_INVALID",
            {issue.code for issue in materialized_report.issues},
        )

    def test_backend_exception_is_sanitized(self) -> None:
        driver = _Driver([], execute_error=RuntimeError("backend-sensitive-marker"))

        with self.assertRaises(PublishedGraphQualityUnavailable) as raised:
            Neo4jPublishedGraphQualityService(driver).audit(_principal())

        self.assertNotIn("sensitive-marker", str(raised.exception))
        self.assertNotIn("sensitive-marker", repr(raised.exception))

    def test_timeout_is_preserved_for_the_api_deadline_handler(self) -> None:
        timeout = TimeoutError("quality audit deadline exceeded")
        driver = _Driver([], execute_error=timeout)

        with self.assertRaises(TimeoutError) as raised:
            Neo4jPublishedGraphQualityService(driver).audit(_principal())

        self.assertIs(raised.exception, timeout)
        self.assertEqual(driver.execute_read_calls, 1)

    def test_query_contract_is_read_only_acl_bound_and_never_projects_text(
        self,
    ) -> None:
        combined = f"{_STATE_QUERY}\n{_REVISIONS_QUERY}\n{_ENTITIES_QUERY}"
        for token in (
            "KnowledgePublicationState",
            "ACTIVE_KNOWLEDGE_PUBLICATION",
            "USES_TBOX_VERSION",
            "PUBLISHES_KNOWLEDGE_REVISION",
            "USES_KNOWLEDGE_SNAPSHOT",
            "CURRENT_REVISION",
            "ACTIVE_SNAPSHOT",
            "ACTIVE_VERSION",
            "HAS_RELATIONSHIP_PROPERTY",
            "RelationshipPropertyValue",
            "substring(",
            "$tenant_id",
            "$groups",
        ):
            self.assertIn(token, combined)
        lowered = combined.casefold()
        for write_token in (" create ", " merge ", " set ", " delete ", " remove "):
            self.assertNotIn(write_token, lowered)
        self.assertNotIn("revision {.*}", combined)
        self.assertNotIn("chunk {.*}", combined)
        self.assertNotIn("chunk.text AS", combined)
        self.assertNotIn("evidence_text AS", combined)
        self.assertLess(
            _REVISIONS_QUERY.index("ORDER BY revision.revision_id"),
            _REVISIONS_QUERY.index("LIMIT $revision_limit"),
        )
        self.assertLess(
            _ENTITIES_QUERY.index("ORDER BY entity.entity_id"),
            _ENTITIES_QUERY.index("LIMIT $entity_limit"),
        )

    def test_invalid_limits_fail_before_io(self) -> None:
        for value in (True, 0, -1):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                PublishedGraphQualityLimits(max_revisions=value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PublishedGraphQualityLimits(transaction_timeout_seconds=float("inf"))

    def test_manifest_or_tbox_identity_corruption_fails_closed(self) -> None:
        for mutation in ("manifest", "tbox", "active-link"):
            with self.subTest(mutation=mutation):
                state = deepcopy(_state(("mention-company",)))
                if mutation == "manifest":
                    state["publication"]["manifest_hash"] = "not-a-digest"
                else:
                    if mutation == "tbox":
                        state["tbox"]["tenant_id"] = "tenant-other"
                    else:
                        state["active_link_count"] = 2
                driver = _Driver([_Step("published-quality:state", [state])])
                with self.assertRaises(PublishedGraphQualityConflict):
                    Neo4jPublishedGraphQualityService(driver).audit(_principal())


if __name__ == "__main__":
    unittest.main()
