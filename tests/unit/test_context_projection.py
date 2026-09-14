"""Stable output identity and fail-closed extension manifest boundaries."""
import json
import asyncio
from types import SimpleNamespace
from contextlib import nullcontext
import unittest

from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.knowledge.context_projection import Neo4jContextProjectionService, projection_record_id, projection_record_ids_tx
from graphrag_prod.knowledge.store import KnowledgeConflict
from graphrag_prod.knowledge.auto_review import Neo4jAutoReviewService, _item


class ContextProjectionTests(unittest.TestCase):
    def context(self,**changes):
        return SimpleNamespace(**{**dict(collection_pointer="/machines",source_identity="P1/A0",
            property_name="projectCode",value_pointer="/metadata/project"),**changes})

    def test_stable_property_identity_is_source_and_scope_bound(self):
        original=projection_record_id("tenant","version",self.context())
        self.assertEqual(original,projection_record_id("tenant","version",self.context()))
        for tenant,version,context in (("other","version",self.context()),("tenant","other",self.context()),
            ("tenant","version",self.context(source_identity="P2/A0")),
            ("tenant","version",self.context(collection_pointer="/other")),
            ("tenant","version",self.context(value_pointer="/scope/project"))):
            self.assertNotEqual(original,projection_record_id(tenant,version,context))

    def load(self,rows):
        tx=SimpleNamespace(run=lambda *args,**kwargs:rows)
        principal=SimpleNamespace(tenant_id="tenant",groups=frozenset({"public"}))
        job=SimpleNamespace(job_id="job",document_id="doc",version_id="version",snapshot_id="snapshot",tbox_id="ontology")
        return projection_record_ids_tx(tx,principal,job)

    def test_output_must_be_listed_by_an_intact_manifest(self):
        encoded=json.dumps({"expected_record_ids":["fact-1"]})
        row={"ids":["fact-1"],"manifest":encoded,"checksum":content_checksum(encoded)}
        self.assertEqual(self.load([row]),{"fact-1"})
        with self.assertRaises(KnowledgeConflict):
            self.load([{**row,"ids":["foreign-fact"]}])
        with self.assertRaises(KnowledgeConflict):
            self.load([{**row,"manifest":encoded+" "}])

    def test_partial_output_is_visible_and_run_count_is_bounded(self):
        encoded=json.dumps({"expected_record_ids":["fact-1","fact-2"]})
        row={"ids":["fact-1"],"manifest":encoded,"checksum":content_checksum(encoded)}
        self.assertEqual(self.load([row]),{"fact-1"})
        with self.assertRaises(KnowledgeConflict):
            self.load([row]*17)

    def test_new_context_decisions_remain_visible_before_old_approvals(self):
        old=[{"record_id":f"old-{i}","decision":"AUTO_APPROVED","reason_code":"PREVIOUS_AUTO_APPROVAL"} for i in range(310)]
        new=[{"record_id":f"context-{i}","decision":"AUTO_APPROVED","reason_code":"SOURCE_FACT_VERIFIED"} for i in range(24)]
        uncertain={"record_id":"manual","decision":"NEEDS_HUMAN","reason_code":"MODEL_UNCERTAIN"}
        public=Neo4jAutoReviewService._public({"items":[*old,*new,uncertain],"issues":[]})
        self.assertTrue(public["truncated"])
        self.assertEqual(public["items"][0],uncertain)
        self.assertEqual(public["items"][1:25],new)
        self.assertEqual(len(public["items"]),200)

    def test_review_receipt_names_both_exact_source_chunks(self):
        record=SimpleNamespace(record_id="context-fact",revision=SimpleNamespace(revision=1),
            evidence=SimpleNamespace(chunk_id="target-chunk"),
            context_property_evidence=SimpleNamespace(value_evidence=SimpleNamespace(chunk_id="value-chunk")))
        item=_item(record,"AUTO_APPROVED","SOURCE_FACT_VERIFIED","Source checked.")
        self.assertEqual(item["evidence_ids"],["target-chunk","value-chunk"])
        self.assertEqual(item["input_revision"],1)
        record.context_property_evidence.value_evidence.chunk_id="target-chunk"
        self.assertEqual(_item(record,"AUTO_APPROVED","SOURCE_FACT_VERIFIED","Source checked.")["evidence_ids"],["target-chunk"])

    def test_retry_drops_approvals_whose_records_were_manually_rejected(self):
        service=object.__new__(Neo4jAutoReviewService)
        service.context_projection=None
        data={"current":[],"comparison":[],"original":[],"groups":["public"],
            "text":"{}","tbox":SimpleNamespace(entity_types=[])}
        service._job=lambda *args:SimpleNamespace(job_id="job",status="COMPLETED",chunks=(),document_id="doc",tbox_id="tbox")
        service._load=lambda *args:data
        previous=[{"record_id":"rejected-record","record_kind":"ASSERTION","decision":"AUTO_APPROVED", "reason_code":"PREVIOUS_AUTO_APPROVAL"}]
        service._start=lambda principal,job,state,retry:({**state,"items":previous},True)
        service._save=lambda *args:None
        service._audit=lambda *args:None
        async def phase(*args):
            return None
        service._identities=phase
        service._facts=phase
        from graphrag_prod.domain.access import Principal
        principal=Principal("reviewer","tenant",frozenset({"public"}),frozenset({"knowledge:review","knowledge:construct"}))
        result=asyncio.run(service.run(principal,job_id="job",retry=True))
        self.assertEqual(result["items"],[])
        self.assertEqual(result["counts"]["approved_assertions"],0)
        self.assertEqual(previous[0]["decision"],"AUTO_APPROVED")  # Original audit is unchanged.

    def test_nonempty_manifest_wins_over_a_later_empty_manifest(self):
        def row(identifier,ids):
            manifest=json.dumps({"expected_record_ids":ids})
            return {"run":{"run_id":identifier,"manifest_json":manifest,"manifest_checksum":content_checksum(manifest)}}
        rows=[row("new-empty",[]),row("existing-output",["fact-1"])]
        driver=SimpleNamespace(session=lambda **kw:nullcontext(SimpleNamespace(run=lambda *args,**kw:rows)))
        service=Neo4jContextProjectionService(driver,planner=None)
        job=SimpleNamespace(job_id="job",document_id="doc",version_id="version",snapshot_id="snapshot",tbox_id="tbox")
        principal=SimpleNamespace(tenant_id="tenant",groups=frozenset({"public"}))
        selected=service._read(principal,job)
        self.assertEqual(selected["run_id"],"existing-output")
        self.assertEqual(selected["_run_count"],2)

    def test_empty_plan_replanning_is_bounded_before_another_model_call(self):
        service=object.__new__(Neo4jContextProjectionService)
        service._read=lambda *args:{"_expected_ids":[],"_run_count":16,
            "source_checksum":content_checksum("{}"),"tbox_checksum":"tbox-checksum"}
        principal=SimpleNamespace(capabilities=frozenset({"knowledge:construct","knowledge:review"}))
        job=SimpleNamespace(chunks=(SimpleNamespace(mapping_summary={"collections":[]}),))
        data={"text":"{}","tbox":SimpleNamespace(checksum="tbox-checksum")}
        with self.assertRaisesRegex(KnowledgeConflict,"run limit"):
            asyncio.run(service.run(principal,job,data,deadline=1e20,retry=True))
