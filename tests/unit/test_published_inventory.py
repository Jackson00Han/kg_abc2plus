"""Unit checks for the active governed-publication A-Box inventory."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from typing import Any, Self

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import relationship_property_value_id
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.graph.published_inventory import (
    _ITEMS_QUERY,
    _MANIFEST_QUERY,
    ActivePublicationInventoryAuthorizationError,
    ActivePublicationInventoryConflict,
    ActivePublicationInventoryLimitExceeded,
    ActivePublicationInventoryUnavailable,
    Neo4jActivePublicationInventoryService,
)
from graphrag_prod.graph.published_quality import (
    PUBLISHED_QUALITY_RULESET_VERSION,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityIssue,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityReport,
)
from graphrag_prod.graph.quality import IssueSeverity


TENANT = "tenant-inventory"
PUBLICATION = "publication-11"
MANIFEST_HASH = "a" * 64
TBOX_ID = "tbox-industrial-v3"


def _principal(*, capable: bool = True) -> Principal:
    return Principal(
        "expert:inventory",
        TENANT,
        frozenset({"industrial-readers", "public"}),
        frozenset({"knowledge:quality"}) if capable else frozenset(),
    )


def _quality(*, passed: bool = True) -> PublishedGraphQualityReport:
    issue = PublishedGraphQualityIssue(
        issue_id="issue-1",
        code="CORRUPT",
        severity=IssueSeverity.ERROR,
        object_kind="KnowledgePublication",
        object_id=PUBLICATION,
        detail="published graph is inconsistent",
    )
    return PublishedGraphQualityReport(
        run_id="quality-run",
        ruleset_version=PUBLISHED_QUALITY_RULESET_VERSION,
        tenant_id=TENANT,
        publication_id=PUBLICATION,
        publication_generation=11,
        manifest_hash=MANIFEST_HASH,
        ontology_version_id=TBOX_ID,
        tbox_checksum="b" * 64,
        corpus_revision=4,
        graph_digest="c" * 64,
        counts=(("revisions", 3),),
        total_issue_count=0 if passed else 1,
        total_error_count=0 if passed else 1,
        issues_truncated=False,
        issues=() if passed else (issue,),
        review_sample=(),
    )


def _entity(entity_id: str, entity_type: str, name: str) -> dict[str, str]:
    return {
        "entity_id": entity_id,
        "tenant_id": TENANT,
        "entity_type": entity_type,
        "canonical_key": f"{entity_type.casefold()}:{name.casefold()}",
        "canonical_name": name,
    }


def _common_row(
    *,
    record_id: str,
    revision_id: str,
    kind: str,
    document_id: str = "document-1",
) -> dict[str, Any]:
    return {
        "revision": {
            "record_id": record_id,
            "revision_id": revision_id,
            "revision": 2,
            "tenant_id": TENANT,
            "governance_status": "PUBLISHED",
            "origin": "LLM_EXTRACTED",
            "authority_level": "SECONDARY",
            "confidence": 0.94,
            "ontology_version_id": TBOX_ID,
            "document_id": document_id,
            "version_id": "version-1",
            "chunk_id": "chunk-1",
            "access_policy_id": "policy-1",
            "access_policy_version": 1,
            "access_groups": ["industrial-readers", "public"],
            "evidence_char_start": 21,
            "evidence_char_end": 42,
            "extractor_version": "dashscope-extractor:v1",
            "relationship_properties_format_version": 1,
            "relationship_properties_json": "[]",
        },
        "revision_labels": [
            "GovernedEntityMentionRevision"
            if kind == "ENTITY_MENTION"
            else "GovernedAssertionRevision"
        ],
        "publication_record_kind": kind,
        "head_count": 1,
        "current_pointer_count": 1,
        "matching_current_count": 1,
        "head_tenant_id": TENANT,
        "head_record_kind": kind,
        "head_current_revision": 2,
        "evidence_link_count": 1,
        "evidence_chunk_count": 1,
        "evidence_document_count": 1,
        "valid_evidence_path_count": 1,
        "evidence_chunk_ordinal": 4,
        "mention_entity_link_count": 0,
        "mention_entity": None,
        "subject_link_count": 0,
        "subject_entity": None,
        "object_link_count": 0,
        "object_entity": None,
        "navigation_mention_count": 0,
        "mention_membership_count": 0,
        "valid_mention_projection_count": 0,
        "navigation_assertion_count": 0,
        "assertion_membership_count": 0,
        "valid_assertion_projection_count": 0,
    }


def _entity_assertion() -> dict[str, Any]:
    row = _common_row(
        record_id="record-assertion-a",
        revision_id="revision-assertion-a",
        kind="ASSERTION",
    )
    subject = _entity("entity-pump", "Equipment", "Pump A")
    target = _entity("entity-plant", "Facility", "Plant 7")
    row["revision"].update(
        subject_entity_id=subject["entity_id"],
        subject_entity_type=subject["entity_type"],
        subject_canonical_key=subject["canonical_key"],
        subject_canonical_name=subject["canonical_name"],
        predicate="INSTALLED_AT",
        object_kind="entity",
        object_entity_id=target["entity_id"],
        object_entity_type=target["entity_type"],
        object_canonical_key=target["canonical_key"],
        object_canonical_name=target["canonical_name"],
    )
    row.update(
        subject_link_count=1,
        subject_entity=subject,
        object_link_count=1,
        object_entity=target,
        navigation_assertion_count=1,
        assertion_membership_count=1,
        valid_assertion_projection_count=1,
    )
    return row


def _literal_assertion() -> dict[str, Any]:
    row = _common_row(
        record_id="record-assertion-b",
        revision_id="revision-assertion-b",
        kind="ASSERTION",
    )
    subject = _entity("entity-pump", "Equipment", "Pump A")
    row["revision"].update(
        subject_entity_id=subject["entity_id"],
        subject_entity_type=subject["entity_type"],
        subject_canonical_key=subject["canonical_key"],
        subject_canonical_name=subject["canonical_name"],
        predicate="MAX_PRESSURE",
        object_kind="literal",
        literal_value="12.5 MPa",
        literal_datatype="DECIMAL",
        literal_typed_value="12.5",
        literal_canonical_value="12.5",
        literal_canonical_unit="MPa",
        literal_valid_from="2025-01-01T00:00:00Z",
    )
    row.update(
        subject_link_count=1,
        subject_entity=subject,
        navigation_assertion_count=1,
        assertion_membership_count=1,
        valid_assertion_projection_count=1,
    )
    return row


def _mention() -> dict[str, Any]:
    row = _common_row(
        record_id="record-mention-a",
        revision_id="revision-mention-a",
        kind="ENTITY_MENTION",
    )
    entity = _entity("entity-pump", "Equipment", "Pump A")
    row["revision"].update(
        entity_id=entity["entity_id"],
        entity_type=entity["entity_type"],
        canonical_key=entity["canonical_key"],
        canonical_name=entity["canonical_name"],
    )
    row.update(
        mention_entity_link_count=1,
        mention_entity=entity,
        navigation_mention_count=1,
        mention_membership_count=1,
        valid_mention_projection_count=1,
    )
    return row


def _manifest() -> dict[str, Any]:
    ids = [
        "revision-assertion-a",
        "revision-assertion-b",
        "revision-mention-a",
    ]
    return {
        "manifest_revision_ids": ids,
        "membership_count": 3,
        "distinct_revision_count": 3,
        "membership_revision_ids": list(reversed(ids)),
        "valid_revision_count": 3,
    }


def _property_rows() -> list[dict[str, Any]]:
    return [
        {
            "revision_id": revision_id,
            "navigation_assertion_id": f"navigation-{revision_id}",
            "property_link_count": 0,
            "property_node_count": 0,
            "property_values": [],
        }
        for revision_id in ("revision-assertion-a", "revision-assertion-b")
    ]


def _entity_assertion_with_property() -> tuple[dict[str, Any], dict[str, Any]]:
    row = _entity_assertion()
    literal = TypedLiteralValue(
        datatype="STRING",
        typed_value="offers",
        raw_value="offers",
        canonical_value="offers",
    )
    value = RelationshipPropertyValue(
        property_value_id=relationship_property_value_id(
            TENANT,
            "INSTALLED_AT",
            "BASIS",
            literal.identity_reference,
            "chunk-1",
            21,
            27,
            "dashscope-extractor:v1",
            TBOX_ID,
        ),
        tenant_id=TENANT,
        relationship_type="INSTALLED_AT",
        name="BASIS",
        literal_semantics=literal,
        evidence_chunk_id="chunk-1",
        evidence_char_start=21,
        evidence_char_end=27,
        evidence_text="offers",
        extractor_version="dashscope-extractor:v1",
        schema_version=TBOX_ID,
        confidence=0.91,
    )
    row["revision"]["relationship_properties_json"] = json.dumps(
        [value.to_mapping()],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    node_properties = {
        "property_value_id": value.property_value_id,
        "tenant_id": TENANT,
        "relationship_type": "INSTALLED_AT",
        "name": "BASIS",
        "evidence_chunk_id": "chunk-1",
        "evidence_char_start": 21,
        "evidence_char_end": 27,
        "evidence_text": "offers",
        "extractor_version": "dashscope-extractor:v1",
        "schema_version": TBOX_ID,
        "confidence": 0.91,
        "document_id": "document-1",
        "version_id": "version-1",
        "access_policy_id": "policy-1",
        "access_policy_version": 1,
        "access_groups": ["industrial-readers", "public"],
        **literal.to_flat_properties(),
    }
    property_row = {
        "revision_id": "revision-assertion-a",
        "navigation_assertion_id": "navigation-revision-assertion-a",
        "property_link_count": 1,
        "property_node_count": 1,
        "property_values": [
            {
                "ordinal": 0,
                "node_properties": node_properties,
                "evidence_link_count": 1,
                "evidence_chunk_count": 1,
                "evidence_chunks": [
                    {
                        "tenant_id": TENANT,
                        "chunk_id": "chunk-1",
                        "ordinal": 4,
                        "char_start": 0,
                        "char_end": 100,
                    }
                ],
                "exact_evidence": True,
            }
        ],
    }
    return row, property_row


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _Tx:
    def __init__(
        self,
        manifest: dict[str, Any],
        items: list[dict[str, Any]],
        property_rows: list[dict[str, Any]],
    ) -> None:
        self.responses = [_Result([manifest]), _Result(items), _Result(property_rows)]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **parameters: Any) -> _Result:
        self.calls.append((query, parameters))
        if not self.responses:
            raise AssertionError("unexpected query")
        return self.responses.pop(0)


class _Session:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_read(self, work: Any, *args: Any) -> Any:
        self.driver.metadata = dict(work.metadata)
        self.driver.timeout = work.timeout
        if self.driver.error is not None:
            raise self.driver.error
        return work(self.driver.tx, *args)


class _Driver:
    def __init__(
        self,
        manifest: dict[str, Any] | None = None,
        items: list[dict[str, Any]] | None = None,
        property_rows: list[dict[str, Any]] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.tx = _Tx(
            _manifest() if manifest is None else manifest,
            [_entity_assertion(), _literal_assertion(), _mention()]
            if items is None
            else items,
            _property_rows() if property_rows is None else property_rows,
        )
        self.error = error
        self.databases: list[str] = []
        self.metadata: dict[str, str] | None = None
        self.timeout: float | None = None

    def session(self, *, database: str) -> _Session:
        self.databases.append(database)
        return _Session(self)


class _Quality:
    def __init__(
        self,
        report: PublishedGraphQualityReport | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.report = report or _quality()
        self.error = error
        self.calls: list[Principal] = []

    def audit(self, principal: Principal) -> PublishedGraphQualityReport:
        self.calls.append(principal)
        if self.error is not None:
            raise self.error
        return self.report


class PublishedInventoryTests(unittest.TestCase):
    def test_lists_stable_safe_entity_relationship_and_literal_summaries(self) -> None:
        driver = _Driver()
        quality = _Quality()
        service = Neo4jActivePublicationInventoryService(
            driver,
            " governed ",
            quality_service=quality,
            transaction_timeout_seconds=4.5,
        )

        inventory = service.list_active(_principal(), limit=2)

        self.assertEqual(inventory.tenant_id, TENANT)
        self.assertEqual(inventory.publication_id, PUBLICATION)
        self.assertEqual(inventory.total_record_count, 3)
        self.assertEqual(inventory.matching_record_count, 3)
        self.assertTrue(inventory.truncated)
        self.assertEqual(len(inventory.items), 2)
        relationship, literal = inventory.items
        self.assertEqual(relationship.ontology_key, "INSTALLED_AT")
        self.assertEqual(
            relationship.assertion.object_entity.display_name,  # type: ignore[union-attr]
            "Plant 7",
        )
        self.assertEqual(literal.ontology_key, "MAX_PRESSURE")
        self.assertEqual(
            literal.assertion.literal.canonical_unit,  # type: ignore[union-attr]
            "MPa",
        )
        serialized = repr(inventory.to_dict())
        self.assertNotIn("evidence_text", serialized)
        self.assertNotIn("quoted_text", serialized)
        self.assertEqual(driver.databases, ["governed"])
        self.assertEqual(
            driver.metadata,
            {
                "component": "graphrag-active-publication-inventory",
                "operation": "list",
            },
        )
        self.assertEqual(driver.timeout, 4.5)
        self.assertEqual(len(quality.calls), 1)
        self.assertIn("active-publication-inventory:manifest", driver.tx.calls[0][0])
        self.assertIn("active-publication-inventory:items", driver.tx.calls[1][0])
        self.assertEqual(driver.tx.calls[1][1]["row_limit"], 501)
        self.assertIn(
            "active-publication-inventory:relationship-properties",
            driver.tx.calls[2][0],
        )

    def test_document_filter_runs_only_after_complete_quality_audit(self) -> None:
        driver = _Driver()
        quality = _Quality()
        inventory = Neo4jActivePublicationInventoryService(
            driver,
            quality_service=quality,
        ).list_active(
            _principal(),
            document_id=" document-absent ",
        )

        self.assertEqual(len(quality.calls), 1)
        self.assertEqual(inventory.document_id, "document-absent")
        self.assertEqual(inventory.matching_record_count, 0)
        self.assertEqual(inventory.items, ())
        for _, parameters in driver.tx.calls:
            self.assertNotIn("document_id", parameters)

    def test_authorization_and_input_bounds_fail_before_audit_or_database(self) -> None:
        for kwargs in ({"limit": 0}, {"limit": 501}, {"limit": True}):
            with self.subTest(kwargs=kwargs):
                driver = _Driver()
                quality = _Quality()
                with self.assertRaises((TypeError, ValueError)):
                    Neo4jActivePublicationInventoryService(
                        driver,
                        quality_service=quality,
                    ).list_active(_principal(), **kwargs)
                self.assertEqual(quality.calls, [])
                self.assertEqual(driver.databases, [])

        driver = _Driver()
        quality = _Quality()
        with self.assertRaises(ActivePublicationInventoryAuthorizationError):
            Neo4jActivePublicationInventoryService(
                driver,
                quality_service=quality,
            ).list_active(_principal(capable=False))
        self.assertEqual(quality.calls, [])
        self.assertEqual(driver.databases, [])

    def test_quality_failures_map_to_safe_inventory_errors(self) -> None:
        cases = (
            (
                PublishedGraphQualityAuthorizationError(),
                ActivePublicationInventoryAuthorizationError,
            ),
            (PublishedGraphQualityConflict(), ActivePublicationInventoryConflict),
            (
                PublishedGraphQualityLimitExceeded(),
                ActivePublicationInventoryLimitExceeded,
            ),
        )
        for source, expected in cases:
            with self.subTest(source=type(source).__name__):
                driver = _Driver()
                with self.assertRaises(expected):
                    Neo4jActivePublicationInventoryService(
                        driver,
                        quality_service=_Quality(error=source),
                    ).list_active(_principal())
                self.assertEqual(driver.databases, [])

        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(),
                quality_service=_Quality(_quality(passed=False)),
            ).list_active(_principal())

    def test_manifest_head_and_materialization_corruption_fail_closed(self) -> None:
        bad_manifest = _manifest()
        bad_manifest["membership_revision_ids"] = [
            "revision-assertion-a",
            "revision-assertion-b",
            "revision-other",
        ]
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(manifest=bad_manifest),
                quality_service=_Quality(),
            ).list_active(_principal())

        # Even when a document filter would exclude a corrupted record, the
        # complete manifest is fetched and validated inside the read tx.
        hidden_bad_row = _mention()
        hidden_bad_row["revision"]["document_id"] = "document-hidden"
        hidden_bad_row["valid_mention_projection_count"] = 0
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(
                    items=[
                        _entity_assertion(),
                        _literal_assertion(),
                        hidden_bad_row,
                    ]
                ),
                quality_service=_Quality(),
            ).list_active(_principal(), document_id="document-1")

    def test_relationship_property_json_and_materialization_are_rechecked_in_tx(
        self,
    ) -> None:
        assertion, property_row = _entity_assertion_with_property()
        property_rows = [property_row, _property_rows()[1]]
        service = Neo4jActivePublicationInventoryService(
            _Driver(
                items=[assertion, _literal_assertion(), _mention()],
                property_rows=property_rows,
            ),
            quality_service=_Quality(),
        )
        clean = service.list_active(_principal())
        relationship_properties = clean.items[0].assertion.relationship_properties  # type: ignore[union-attr]
        self.assertEqual(len(relationship_properties), 1)
        self.assertEqual(relationship_properties[0].name, "BASIS")
        self.assertEqual(relationship_properties[0].evidence_chunk_ordinal, 4)
        self.assertNotIn("evidence_text", repr(clean.to_dict()))

        corruptions = (
            ("ordinal", lambda value: value.update(ordinal=9)),
            (
                "stable-id",
                lambda value: value["node_properties"].update(
                    property_value_id="forged-property-value"
                ),
            ),
            (
                "typed-field",
                lambda value: value["node_properties"].update(
                    literal_canonical_value="forged"
                ),
            ),
            ("evidence", lambda value: value.update(exact_evidence=False)),
            (
                "chunk-ordinal",
                lambda value: value["evidence_chunks"][0].update(ordinal=5),
            ),
        )
        for label, mutate in corruptions:
            with self.subTest(label=label):
                changed = copy.deepcopy(property_row)
                mutate(changed["property_values"][0])
                with self.assertRaises(ActivePublicationInventoryConflict):
                    Neo4jActivePublicationInventoryService(
                        _Driver(
                            items=[assertion, _literal_assertion(), _mention()],
                            property_rows=[changed, _property_rows()[1]],
                        ),
                        quality_service=_Quality(),
                    ).list_active(
                        _principal(),
                        document_id="document-absent",
                    )

    def test_complete_manifest_hard_cap_is_enforced_before_item_read(self) -> None:
        ids = [f"revision-{index:04d}" for index in range(501)]
        manifest = {
            "manifest_revision_ids": ids,
            "membership_count": 501,
            "distinct_revision_count": 501,
            "membership_revision_ids": list(reversed(ids)),
            "valid_revision_count": 501,
        }
        report = replace(_quality(), counts=(("revisions", 501),))
        driver = _Driver(manifest=manifest)

        with self.assertRaises(ActivePublicationInventoryLimitExceeded):
            Neo4jActivePublicationInventoryService(
                driver,
                quality_service=_Quality(report),
            ).list_active(_principal())

        self.assertEqual(len(driver.tx.calls), 1)

        bad_row = _entity_assertion()
        bad_row["valid_assertion_projection_count"] = 0
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(items=[bad_row, _literal_assertion(), _mention()]),
                quality_service=_Quality(),
            ).list_active(_principal())

    def test_changed_publication_or_backend_failure_is_sanitized(self) -> None:
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(manifest={}),
                quality_service=_Quality(),
            ).list_active(_principal())

        secret = "bolt://user:password@secret-host"
        with self.assertRaises(ActivePublicationInventoryUnavailable) as raised:
            Neo4jActivePublicationInventoryService(
                _Driver(error=RuntimeError(secret)),
                quality_service=_Quality(),
            ).list_active(_principal())
        self.assertNotIn(secret, str(raised.exception))

    def test_tenant_mismatch_and_unstable_backend_order_fail_closed(self) -> None:
        mismatched = replace(_quality(), tenant_id="tenant-other")
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(),
                quality_service=_Quality(mismatched),
            ).list_active(_principal())

        cross_tenant = _mention()
        cross_tenant["mention_entity"]["tenant_id"] = "tenant-other"
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(
                    items=[
                        _entity_assertion(),
                        _literal_assertion(),
                        cross_tenant,
                    ]
                ),
                quality_service=_Quality(),
            ).list_active(_principal())

        unordered = [_mention(), _entity_assertion(), _literal_assertion()]
        with self.assertRaises(ActivePublicationInventoryConflict):
            Neo4jActivePublicationInventoryService(
                _Driver(items=unordered),
                quality_service=_Quality(),
            ).list_active(_principal())

    def test_queries_do_not_project_source_or_evidence_text(self) -> None:
        manifest_return = _MANIFEST_QUERY.rsplit("RETURN", 1)[1]
        item_return = _ITEMS_QUERY.rsplit("RETURN", 1)[1]
        for projection in (manifest_return, item_return):
            self.assertNotIn("evidence_text", projection)
            self.assertNotIn("chunk.text", projection)
            self.assertNotIn("revision {.*}", projection)


if __name__ == "__main__":
    unittest.main()
