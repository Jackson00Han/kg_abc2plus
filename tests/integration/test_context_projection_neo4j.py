"""Real, disposable Neo4j checks for additive context construction and review."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from graphrag_prod.construction.context_mapping import VERSION
from graphrag_prod.construction.structured import StructuredDocumentParser, select_structured_extractor
from graphrag_prod.knowledge.auto_review import Neo4jAutoReviewService
from graphrag_prod.knowledge.context_projection import Neo4jContextProjectionService
from graphrag_prod.knowledge.review import KnowledgeReviewUnavailable, ReviewRecordKind, ReviewRequest
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.construction.workflow import ConstructionAuthorizationError
from graphrag_prod.ontology.models import Cardinality, EntityTypeDefinition, PropertyDataType, PropertyDefinition, TBoxStatus
from graphrag_prod.ontology.store import Neo4jTBoxStore
from tests.integration import test_auto_review_neo4j as auto_fixture
from tests.integration import test_construction_workflow_neo4j as construction_fixture
from tests.unit.test_structured_mapping import base, fixture


class Planner:
    def __init__(self):
        self.calls=0

    async def plan(self,payload):
        self.calls+=1
        return {"status":"COMPLETE","mapping":{"version":VERSION,"rules":[{
            "collection":"/machines","property":"projectCode","value_path":"/metadata/project",
            "scope_path":"","entity_types":["Asset"],"binding":{"mode":"IDENTITY_TEMPLATE",
                "template_path":"/metadata/identity","context_variable":"project",
                "identity_field":"/key","record_variables":{"serial":"/serial"}},
            "reason":"The original identity template explicitly scopes this record to its project."}]},
            "audit":{"attempts":[{"fixture":True,"payload":payload}]}}


class ContextProjectionNeo4jTests(unittest.TestCase):
    setUpClass=classmethod(construction_fixture.Neo4jConstructionWorkflowIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(construction_fixture.Neo4jConstructionWorkflowIntegrationTests.tearDownClass.__func__)
    tearDown=construction_fixture.Neo4jConstructionWorkflowIntegrationTests.tearDown

    def setUp(self):
        construction_fixture.Neo4jConstructionWorkflowIntegrationTests.setUp(self)
        required=lambda name:PropertyDefinition(name,PropertyDataType.STRING,True,Cardinality.ONE)
        draft=replace(self.tbox,version=2,status=TBoxStatus.DRAFT,entity_types=(
            EntityTypeDefinition("Company",("company-id","llm-candidate")),
            EntityTypeDefinition("Asset",("asset-id","llm-candidate"),
                properties=(required("projectCode"),required("serialNumber")),
                identity_properties=("projectCode","serialNumber")),))
        tboxes=Neo4jTBoxStore(self.driver,self.database)
        tboxes.import_version(draft)
        self.tbox=tboxes.publish(self.tenant_id,draft.tbox_id,expected_active_tbox_id=self.tbox.tbox_id)
        self.planner=Planner()
        self.projection=Neo4jContextProjectionService(self.driver,self.database,planner=self.planner)
        self.reviewer=auto_fixture.Reviewer()
        self.auto=Neo4jAutoReviewService(self.driver,self.database,reviewer=self.reviewer,context_projection=self.projection)

    def construct(self,name="first",project="P1",count=3,bad=False):
        data,plan=fixture(count)
        data={"metadata":{"project":project,"identity":"{project}/{serial}"},**data}
        data["organizations"][0]["key"]="org-"+name
        for i,record in enumerate(data["machines"]):
            record.update(key=f"{project}/A{i}",serial=f"A{i}",owner="org-"+name)
        if bad:
            data["machines"][-1]["key"]="OTHER/A2"
        model,_=base(plan,self.tbox)
        self.workflow.extractor_factory=lambda _:model
        self.workflow.document_extractor_selector=select_structured_extractor
        self.workflow.parser=StructuredDocumentParser(json_max_chars=280)
        return self.workflow.run(self.principal,json.dumps(data,indent=2).encode(),
            replace(self.metadata,mime_type="application/json",canonical_uri="urn:context:"+name,operation_key=name))

    def query(self,query,**params):
        with self.driver.session(database=self.database) as session:
            return [dict(r) for r in session.run(query,**params)]

    def errors(self):
        return [r["payload"] for r in self.query("MATCH (a:KnowledgeAutoReviewDecision) WHERE a.kind IN ['RUN_FAILURE','COMMIT_REJECTED'] RETURN a.payload_json AS payload")]

    def run_review(self,job,**kwargs):
        result=asyncio.run(self.auto.run(self.principal,job_id=job.job_id,**kwargs))
        self.assertEqual(result["status"],"COMPLETED",self.errors())
        return result

    def test_additive_repair_preserves_old_heads_evidence_audit_and_manual_decisions(self):
        job=self.construct()
        old=Neo4jAutoReviewService(self.driver,self.database,reviewer=self.reviewer)
        before=asyncio.run(old.run(self.principal,job_id=job.job_id))
        self.assertEqual(before["counts"]["approved_assertions"],6,self.errors())
        heads=self.query("MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r) RETURN h.record_id AS id,r.revision_id AS revision")
        outcomes=self.query("MATCH (o:KnowledgeConstructionChunkOutcome) RETURN o.outcome_id AS id,o.result_json AS result")
        result=self.run_review(job,retry=True)
        self.assertEqual(result["context_mapping"]["added_assertions"],3,self.errors())
        self.assertEqual(result["counts"]["approved_assertions"],9,self.errors())
        self.assertEqual(result["issues"],[])
        after={r["id"]:r["revision"] for r in self.query("MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r) RETURN h.record_id AS id,r.revision_id AS revision")}
        self.assertTrue(all(after[r["id"]]==r["revision"] for r in heads))
        self.assertEqual(outcomes,self.query("MATCH (o:KnowledgeConstructionChunkOutcome) RETURN o.outcome_id AS id,o.result_json AS result"))
        contexts=self.query("""MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r:GovernedAssertionRevision)
            WHERE r.context_property_evidence_json IS NOT NULL
            RETURN r.record_id AS id,r.revision AS revision,r.context_property_evidence_json AS context,
                r.chunk_id AS chunk,r.governance_status AS status,r.authority_level AS authority""")
        self.assertEqual(len(contexts),3)
        self.assertTrue(all(r["status"]=="APPROVED" and r["authority"]=="SECONDARY" for r in contexts))
        self.assertTrue(any(json.loads(r["context"])["value_evidence"]["chunk_id"]!=r["chunk"] for r in contexts))
        self.assertEqual(self.query("MATCH (p:KnowledgePublication) RETURN count(p) AS n")[0]["n"],0)
        calls=self.planner.calls
        self.run_review(job,retry=True)
        self.assertEqual(self.planner.calls,calls)
        self.assertEqual(len(self.query("MATCH (h:KnowledgeRecordHead) RETURN h.record_id AS id")),len(heads)+3)
        selected=contexts[0]
        self.auto.review.review_batch(self.principal,(ReviewRequest(ReviewRecordKind.ASSERTION,
            selected["id"],selected["revision"],GovernanceStatus.QUARANTINED,datetime.now(timezone.utc),
            "Human verification of source scope remains pending."),))
        manual=self.run_review(job,retry=True)
        self.assertEqual(manual["counts"]["manual_assertions"],1)
        self.assertEqual(self.query("""MATCH (:KnowledgeRecordHead {record_id:$id})-[:CURRENT_REVISION]->(r)
            RETURN r.governance_status AS status,r.reviewed_by AS actor""",id=selected["id"])[0],
            {"status":"QUARANTINED","actor":self.principal.principal_id})
        self.assertEqual(self.planner.calls,calls)
        for other in (replace(self.principal,tenant_id="foreign"),replace(self.principal,groups=frozenset({"private"}))):
            with self.assertRaises((KnowledgeReviewUnavailable,ConstructionAuthorizationError)):
                asyncio.run(self.auto.run(other,job_id=job.job_id,retry=True))

    def test_context_identity_precedes_cross_document_matching_and_wrong_scope_is_human(self):
        first=self.construct()
        result=self.run_review(first)
        self.assertEqual(result["counts"]["approved_assertions"],9,self.errors())
        second=self.construct("second")
        result2=self.run_review(second)
        self.assertEqual(result2["counts"]["approved_groups"],4,self.errors())
        self.assertEqual(result2["counts"]["approved_assertions"],9,self.errors())
        def asset_ids(job):
            return {r["entity"] for r in self.query("""MATCH (:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r:GovernedEntityMentionRevision)
                WHERE r.version_id=$version AND r.entity_type='Asset' RETURN r.entity_id AS entity""",version=job.version_id)}
        self.assertEqual(asset_ids(first),asset_ids(second))
        third=self.construct("third",project="P2")
        self.run_review(third)
        self.assertFalse(asset_ids(first)&asset_ids(third))
        bad=self.construct("bad",bad=True)
        bad_result=self.run_review(bad)
        self.assertEqual(bad_result["context_mapping"]["uncertain"],1)
        self.assertTrue(any(i["code"]=="CONTEXT_IDENTITY_CONFLICT" for i in bad_result["issues"]))

    def test_saved_mapping_survives_write_failure_without_another_model_call(self):
        job=self.construct()
        original=self.projection.store.persist_context_candidates
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError("fixture interruption after candidates were durable but before progress receipt")
        self.projection.store.persist_context_candidates=fail
        first=asyncio.run(self.auto.run(self.principal,job_id=job.job_id))
        self.assertEqual(first["status"],"PARTIAL")
        self.assertEqual(self.planner.calls,1)
        self.assertEqual(self.query("MATCH (r:GovernedAssertionRevision) WHERE r.context_property_evidence_json IS NOT NULL RETURN count(r) AS n")[0]["n"],3)
        self.assertEqual(self.query("MATCH (r:KnowledgeContextProjectionRun) RETURN r.record_ids AS ids")[0]["ids"],[])
        self.projection.store.persist_context_candidates=original
        resumed=self.run_review(job,retry=True)
        self.assertEqual(resumed["context_mapping"]["applied"],3,self.errors())
        self.assertEqual(resumed["counts"]["approved_assertions"],9,self.errors())
        self.assertEqual(self.planner.calls,1)

    def test_explicit_retry_replans_empty_manifest_and_retains_both_audits(self):
        class InitiallyEmptyPlanner(Planner):
            async def plan(self,payload):
                result=await super().plan(payload)
                if self.calls==1:
                    result["mapping"]["rules"]=[]
                return result
        planner=InitiallyEmptyPlanner()
        self.projection.planner=planner
        job=self.construct()
        first=self.run_review(job)
        self.assertEqual(first["context_mapping"]["added_assertions"],0)
        old=self.query("MATCH (r:KnowledgeContextProjectionRun) RETURN r.run_id AS id,r.manifest_json AS manifest,r.manifest_checksum AS checksum")
        heads=self.query("MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r) RETURN h.record_id AS id,r.revision_id AS revision")
        self.run_review(job)
        self.assertEqual(planner.calls,1)
        second=self.run_review(job,retry=True)
        self.assertEqual(planner.calls,2)
        self.assertEqual(second["context_mapping"]["added_assertions"],3,self.errors())
        self.assertEqual(second["counts"]["approved_assertions"],9,self.errors())
        self.assertEqual(second["issues"],[])
        audits=self.query("MATCH (r:KnowledgeContextProjectionRun) RETURN r.run_id AS id,r.manifest_json AS manifest,r.manifest_checksum AS checksum")
        self.assertEqual(len(audits),2)
        self.assertIn(old[0],audits)
        after={r["id"]:r["revision"] for r in self.query("MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r) RETURN h.record_id AS id,r.revision_id AS revision")}
        self.assertTrue(all(after[r["id"]]==r["revision"] for r in heads))
        self.run_review(job,retry=True)
        self.assertEqual(planner.calls,2)
        self.assertEqual(len(self.query("MATCH (r:KnowledgeContextProjectionRun) RETURN r.run_id AS id")),2)
