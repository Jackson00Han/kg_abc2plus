"""Separate mutation budgets from complete retained publication manifests."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from graphrag_prod.api.knowledge import _inventory_item_payload
from graphrag_prod.api.knowledge_contracts import (
    ActivePublicationInventoryResponse,
    PublicationPreviewResponse, PublicationRequest, PublicationResponse,
)
from graphrag_prod.knowledge import review
from graphrag_prod.knowledge.models import RecordRevision
from graphrag_prod.knowledge.publication_guard import MAX_PUBLICATION_CHANGE_RECORDS, MAX_PUBLICATION_MANIFEST_RECORDS
from tests.fixtures.knowledge import make_knowledge_batch
from tests.unit.test_knowledge_review import _NoSessionDriver, _principal
from tests.unit.test_api_knowledge import _inventory


NOW = datetime(2026, 9, 13, tzinfo=UTC)
Service = review.Neo4jKnowledgePublicationService


def _records(count):
    base = make_knowledge_batch(authoritative=True).mentions[0]
    return tuple(replace(base, revision=RecordRevision.next(f"capacity-record-{i}", 0)) for i in range(count))


class PublicationCapacityTests(unittest.TestCase):
    def _preview(self, carried, added):
        """Run real manifest assembly; replace only graph IO and evidence readers.

        The integration suite separately proves actual parser/ingestion/evidence
        validation. Spies here ensure complete retained records reach validators,
        membership linking, materialization and the actual preview serializer.
        """
        tx = Mock()
        tx.run.return_value.single.side_effect = [
            {"publication": None}, {"publication_id": "next-publication"},
        ]
        current_id = "previous-publication" if carried else None
        entries = tuple((record, "snapshot") for record in carried)
        added_by_id = {record.revision_id: (record, "snapshot") for record in added}
        with ExitStack() as stack:
            def method(name, **kwargs):
                return stack.enter_context(patch.object(Service, name, **kwargs))
            stack.enter_context(patch.object(review.Neo4jKnowledgeReviewService, "_lock_tenant_corpus_tx"))
            method("_lock_publication_state_tx", return_value={
                "active_publication_id": current_id,
                "publication_generation": 1 if carried else 0,
                "activation_generation": 1 if carried else 0,
            })
            method("_load_completed_publication_tx", return_value={
                "ontology_version_id": "tbox-capacity",
                "published_revision_ids": tuple(record.revision_id for record in carried),
            })
            method("_load_manifest_record_entries_tx", return_value=entries)
            method("_load_current_publishable_tx", side_effect=lambda tx, principal, rid: added_by_id[rid])
            cardinality = method("_validate_property_cardinality_tx", return_value="tbox-capacity")
            hierarchy = method("_validate_hierarchies_tx")
            linked = method("_link_manifest_tx")
            materialized = method("_materialize_records_tx")
            method("_deactivate_materialization_tx")
            method("_activate_publication_tx")
            for name in ("load_sources_tx", "load_publication_sources_tx", "source_summaries_tx"):
                stack.enter_context(patch.object(review, name, return_value=[]))
            stack.enter_context(patch.object(review, "require_embedding_coverage_tx", return_value="embedding-space"))
            with self.assertRaises(review._PublicationPreviewReady) as result:
                Service._publish_tx(tx, _principal(), tuple(record.revision_id for record in added),
                    "next-publication", current_id, NOW, (), (), preview_only=True)
            total = len(carried) + len(added)
            self.assertEqual(len(cardinality.call_args.args[2]), total)
            self.assertEqual(len(hierarchy.call_args.args[3]), total)
            self.assertEqual(len(linked.call_args.args[3]), total)
            self.assertEqual(len(materialized.call_args.args[3]), total)
            preview = result.exception.payload
            PublicationPreviewResponse.model_validate(preview)
            self.assertEqual(len(preview["records_after"]), total)
            self.assertEqual(len(preview["source_revision_ids"]), len(added))
            return preview

    def test_two_legal_changes_retain_300_plus_279_records(self):
        records = _records(579)
        first = self._preview((), records[:300])
        second = self._preview(records[:300], records[300:])
        first_ids = {item["revision"]["revision_id"] for item in first["records_after"]}
        second_ids = {item["revision"]["revision_id"] for item in second["records_after"]}
        self.assertEqual(len(first_ids), 300)
        self.assertEqual(len(second_ids), 579)
        self.assertTrue(first_ids < second_ids)

    def test_cumulative_2001_rejected_even_with_one_new_record(self):
        records = _records(MAX_PUBLICATION_MANIFEST_RECORDS + 1)
        with self.assertRaisesRegex(review.KnowledgePublicationConflict, "bounded manifest limit"):
            self._preview(records[:-1], records[-1:])

    def test_single_501_and_aggregate_501_rejected_before_database(self):
        service = Service(_NoSessionDriver())
        for incoming, removed in ((501, 0), (300, 201)):
            with self.subTest(incoming=incoming, removed=removed):
                added = tuple(f"revision-{i}" for i in range(incoming))
                deleted = tuple(f"record-{i}" for i in range(removed))
                with self.assertRaises(ValidationError):
                    PublicationRequest(approved_revision_ids=added, remove_record_ids=deleted)
                with self.assertRaises(ValueError):
                    service.publish(_principal(), added, remove_record_ids=deleted,
                        expected_active_publication_id=None, published_at=NOW)
        self.assertEqual(len(PublicationRequest(approved_revision_ids=tuple(f"r-{i}" for i in range(500))).approved_revision_ids), 500)

    def test_api_retained_manifest_accepts_579_and_2000_but_rejects_2001(self):
        fields = dict(publication_id="publication", ontology_version_id="ontology", generation=2,
            manifest_hash="a" * 64, source_revision_ids=[f"new-{i}" for i in range(279)],
            removed_record_ids=[], replaced_record_ids=[], status="ACTIVE", created_by="expert",
            created_at=NOW, activated_at=NOW)
        for count in (579, MAX_PUBLICATION_MANIFEST_RECORDS):
            self.assertEqual(len(PublicationResponse(**fields,
                published_revision_ids=[f"r-{i}" for i in range(count)]).published_revision_ids), count)
        with self.assertRaises(ValidationError):
            PublicationResponse(**fields, published_revision_ids=[f"r-{i}" for i in range(MAX_PUBLICATION_MANIFEST_RECORDS + 1)])

    def test_dev_mini_profile_matches_distinct_shared_budgets(self):
        path = Path(__file__).resolve().parents[2] / "contracts/profiles/dev-mini-construction.v2.json"
        profile = json.loads(path.read_text())
        self.assertEqual(profile["max_publication_change_records"], MAX_PUBLICATION_CHANGE_RECORDS)
        self.assertEqual(profile["max_publication_manifest_records"], MAX_PUBLICATION_MANIFEST_RECORDS)
        self.assertEqual(MAX_PUBLICATION_CHANGE_RECORDS, 500)
        self.assertEqual(MAX_PUBLICATION_MANIFEST_RECORDS, 2000)

    def test_inventory_counts_full_manifest_while_items_remain_paged(self):
        inventory = _inventory()
        payload = inventory.to_dict()
        payload.pop("tenant_id")
        payload["items"] = [_inventory_item_payload(item) for item in inventory.items]
        for count in (579, MAX_PUBLICATION_MANIFEST_RECORDS):
            response = ActivePublicationInventoryResponse.model_validate({
                **payload, "total_record_count": count, "matching_record_count": count,
                "truncated": True,
            })
            self.assertEqual(response.total_record_count, count)
            self.assertEqual(response.matching_record_count, count)
            self.assertEqual(len(response.items), 1)
        for field in ("total_record_count", "matching_record_count"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ActivePublicationInventoryResponse.model_validate({
                    **payload, "total_record_count": MAX_PUBLICATION_MANIFEST_RECORDS,
                    "matching_record_count": MAX_PUBLICATION_MANIFEST_RECORDS,
                    "truncated": True, field: MAX_PUBLICATION_MANIFEST_RECORDS + 1,
                })
        items = [{**payload["items"][0], "record_id": f"record-{i:04d}",
                  "revision_id": f"revision-{i:04d}"} for i in range(501)]
        page = {**payload, "total_record_count": 579, "matching_record_count": 579,
                "truncated": True}
        self.assertEqual(len(ActivePublicationInventoryResponse.model_validate({
            **page, "items": items[:500],
        }).items), 500)
        with self.assertRaises(ValidationError) as error:
            ActivePublicationInventoryResponse.model_validate({**page, "items": items})
        self.assertTrue(any(item["type"] == "too_long" for item in error.exception.errors()))
