"""Automatic identity/fact review against a disposable, real Neo4j database."""
import asyncio
from dataclasses import replace
import json
import unittest

from graphrag_prod.construction.structured import StructuredDocumentParser, select_structured_extractor
from graphrag_prod.knowledge.auto_review import Neo4jAutoReviewService
from graphrag_prod.knowledge.auto_review_model import AutoReviewModelDecision, AutoReviewModelResult
from graphrag_prod.knowledge.review import KnowledgeReviewUnavailable
from graphrag_prod.construction.workflow import ConstructionAuthorizationError
from tests.integration import test_construction_workflow_neo4j as existing
from tests.unit.test_structured_mapping import base, fixture


class Reviewer:
    def __init__(self, uncertain_id=None):
        self.calls=[]
        self.uncertain_id=uncertain_id

    async def review(self, kind, payload):
        self.calls.append((kind,payload))
        output=[]
        for r in payload["records"]:
            uncertain = self.uncertain_id is not None and r.get("source_identity",{}).get("id") == self.uncertain_id
            target = next(iter(r.get("candidate_target_ids",[])),None)
            action = "UNCERTAIN" if uncertain else "VALID" if kind=="mapping" else "APPROVE" if kind=="facts" else "MATCH" if target else "NEW"
            output.append(AutoReviewModelDecision(r["record_id"],action,
                target_id=target if action=="MATCH" else None,
                new_group_id=r.get("group_id") if action=="NEW" else None,
                evidence_ids=tuple(r["evidence_ids"]),reason="Deterministic test reviewer checks explicit evidence."))
        return AutoReviewModelResult(tuple(output),{"attempts":[{"fixture":True}],"input":payload},"COMPLETE")


class AutoReviewNeo4jTests(unittest.TestCase):
    setUpClass=classmethod(existing.Neo4jConstructionWorkflowIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(existing.Neo4jConstructionWorkflowIntegrationTests.tearDownClass.__func__)
    setUp=existing.Neo4jConstructionWorkflowIntegrationTests.setUp
    tearDown=existing.Neo4jConstructionWorkflowIntegrationTests.tearDown

    def construct(self, *, name="one", count=4):
        data,plan=fixture(count)
        plan["collections"][1]["properties"]=[]
        plan["collections"][1]["ignored_fields"]={"/serial":"outside ontology"}
        model,_=base(plan,self.tbox)
        self.workflow.extractor_factory=lambda _:model
        self.workflow.document_extractor_selector=select_structured_extractor
        self.workflow.parser=StructuredDocumentParser(json_max_chars=800)
        return self.workflow.run(self.principal,json.dumps(data,ensure_ascii=False,indent=2).encode(),
            replace(self.metadata,mime_type="application/json",canonical_uri="urn:auto-review:"+name,operation_key=name))

    def errors(self):
        rows,_,_=self.driver.execute_query("MATCH (a:KnowledgeAutoReviewDecision) WHERE a.kind IN ['RUN_FAILURE','COMMIT_REJECTED'] RETURN a.payload_json AS payload",database_=self.database)
        return [r["payload"] for r in rows]

    def test_complete_groups_facts_replay_audit_and_access(self):
        job=self.construct(count=20)
        model=Reviewer()
        service=Neo4jAutoReviewService(self.driver,self.database,reviewer=model)
        result=asyncio.run(service.run(self.principal,job_id=job.job_id))
        self.assertEqual(result["status"],"COMPLETED",self.errors())
        self.assertEqual(result["counts"]["approved_groups"],21,self.errors())
        self.assertEqual(result["counts"]["approved_assertions"],20,self.errors())
        self.assertEqual(result["counts"]["manual_groups"],0)
        self.assertEqual(sum(len(p["records"]) for k,p in model.calls if k=="identity"),21)
        self.assertTrue(any(r["mention_count"]>1 for k,p in model.calls if k=="identity" for r in p["records"]))
        rows,_,_=self.driver.execute_query("""MATCH (h:KnowledgeRecordHead)-[:CURRENT_REVISION]->(r)
            RETURN r.governance_status AS status,r.reviewed_by AS reviewed_by,r.authority_level AS authority,
                   r.entity_id AS entity_id,r.record_id AS record_id""",database_=self.database)
        self.assertTrue(all(r["status"]=="APPROVED" for r in rows))
        self.assertTrue(all(r["reviewed_by"].startswith("service:auto-review:") for r in rows))
        self.assertTrue(all(r["authority"]=="SECONDARY" for r in rows))
        self.assertEqual(len({r["entity_id"] for r in rows if r["entity_id"]}),21)
        calls=len(model.calls)
        replay=asyncio.run(service.run(self.principal,job_id=job.job_id))
        self.assertEqual(replay,result)
        self.assertEqual(len(model.calls),calls)
        self.assertFalse(any(k.startswith("_") for k in service.get(self.principal,job.job_id)))
        for other in [replace(self.principal,tenant_id="other"),replace(self.principal,groups=frozenset({"private"}))]:
            with self.assertRaises((KnowledgeReviewUnavailable,ConstructionAuthorizationError)):
                service.get(other,job.job_id)
        with self.driver.session(database=self.database) as session:
            count=session.run("MATCH (p:KnowledgePublication) RETURN count(p) AS n").single()["n"]
        self.assertEqual(count,0)

    def test_uncertain_identity_only_blocks_related_fact_and_cross_source_names_stay_unmerged(self):
        job=self.construct()
        model=Reviewer("asset-0")
        service=Neo4jAutoReviewService(self.driver,self.database,reviewer=model)
        result=asyncio.run(service.run(self.principal,job_id=job.job_id))
        self.assertEqual(result["status"],"COMPLETED",self.errors())
        self.assertEqual(result["counts"]["approved_groups"],4,self.errors())
        self.assertEqual(result["counts"]["manual_groups"],1)
        self.assertEqual(result["counts"]["approved_assertions"],3,self.errors())
        self.assertEqual(result["counts"]["blocked_assertions"],1)
        second=self.construct(name="two")
        next_result=asyncio.run(service.run(self.principal,job_id=second.job_id))
        self.assertEqual(next_result["counts"]["approved_groups"],0)
        self.assertEqual(next_result["counts"]["manual_groups"],5)
        self.assertTrue(all(i["reason_code"]=="AMBIGUOUS_IDENTITY" for i in next_result["items"] if i["record_kind"]=="ENTITY_MENTION"))

    def test_provider_unavailable_retains_candidates_and_retry_works(self):
        job=self.construct(count=2)
        service=Neo4jAutoReviewService(self.driver,self.database,reviewer=None)
        first=asyncio.run(service.run(self.principal,job_id=job.job_id))
        self.assertEqual(first["status"],"PARTIAL")
        self.assertEqual(first["counts"]["approved_mentions"],0)
        service.reviewer=Reviewer()
        second=asyncio.run(service.run(self.principal,job_id=job.job_id,retry=True))
        self.assertEqual(second["status"],"COMPLETED",self.errors())
        self.assertEqual(second["counts"]["approved_groups"],3,self.errors())
        self.assertEqual(second["counts"]["approved_assertions"],2,self.errors())

    def test_complete_scoped_identity_links_multiple_documents_and_separates_other_project(self):
        from graphrag_prod.ontology.models import (Cardinality, EntityTypeDefinition,
            PropertyDataType, PropertyDefinition, TBoxStatus)
        from graphrag_prod.ontology.store import Neo4jTBoxStore
        required=lambda name:PropertyDefinition(name,PropertyDataType.STRING,True,Cardinality.ONE)
        original=self.tbox
        draft=replace(original,version=2,status=TBoxStatus.DRAFT,entity_types=(
            EntityTypeDefinition("Company",("company-id","llm-candidate"),
                properties=(required("companyCode"),),identity_properties=("companyCode",)),
            EntityTypeDefinition("Asset",("asset-id","llm-candidate"),
                properties=(required("projectCode"),required("serialNumber")),
                identity_properties=("projectCode","serialNumber")),))
        store=Neo4jTBoxStore(self.driver,self.database)
        store.import_version(draft)
        self.tbox=store.publish(self.tenant_id,draft.tbox_id,expected_active_tbox_id=original.tbox_id)
        reviewer=Reviewer()
        service=Neo4jAutoReviewService(self.driver,self.database,reviewer=reviewer)

        def upload(label,project,count):
            data,plan=fixture(count)
            data["organizations"][0]["registry_code"]="COMPANY-GLOBAL-1"
            plan["collections"][0]["properties"]=[{"field":"/registry_code","property":"companyCode"}]
            plan["collections"][1]["properties"]=[{"field":"/serial","property":"serialNumber"},
                                                   {"field":"/project","property":"projectCode"}]
            for i,row in enumerate(data["machines"]):
                row["serial"]=f"SERIAL-{i}"
                row["project"]=project
            model,_=base(plan,self.tbox)
            self.workflow.extractor_factory=lambda _:model
            self.workflow.document_extractor_selector=select_structured_extractor
            self.workflow.parser=StructuredDocumentParser(json_max_chars=1200)
            job=self.workflow.run(self.principal,json.dumps(data,ensure_ascii=False,indent=2).encode(),
                replace(self.metadata,mime_type="application/json",canonical_uri="urn:positive-identity:"+label,
                        operation_key="positive-"+label))
            result=asyncio.run(service.run(self.principal,job_id=job.job_id))
            self.assertEqual(result["status"],"COMPLETED",self.errors())
            self.assertEqual(result["counts"]["approved_groups"],count+1,self.errors())
            self.assertEqual(result["counts"]["approved_assertions"],1+3*count,self.errors())
            self.assertEqual(result["issues"],[],self.errors())
            self.assertEqual(service.get(self.principal,job.job_id)["issues"],[])
            ids=[rid for c in job.chunks for rid in c.mention_record_ids]
            rows,_,_=self.driver.execute_query("""MATCH (:KnowledgeRecordHead {record_kind:'ENTITY_MENTION'})
                -[:CURRENT_REVISION]->(m:GovernedEntityMentionRevision) WHERE m.record_id IN $ids
                RETURN m.entity_id AS entity_id,m.entity_type AS entity_type,m.canonical_name AS name,
                       m.record_id AS record_id,m.governance_status AS status""",ids=ids,database_=self.database)
            self.assertTrue(all(r["status"]=="APPROVED" for r in rows))
            assertions=[rid for c in job.chunks for rid in c.assertion_record_ids]
            frows,_,_=self.driver.execute_query("""MATCH (:KnowledgeRecordHead {record_kind:'ASSERTION'})
                -[:CURRENT_REVISION]->(a:GovernedAssertionRevision) WHERE a.record_id IN $ids
                MATCH (s:GovernedEntityMentionRevision {revision_id:a.subject_mention_revision_id})
                OPTIONAL MATCH (o:GovernedEntityMentionRevision {revision_id:a.object_mention_revision_id})
                RETURN a.subject_entity_id=s.entity_id AS subject_matches,
                       a.object_kind='literal' OR a.object_entity_id=o.entity_id AS object_matches,
                       s.governance_status AS subject_status,a.governance_status AS fact_status,
                       a.predicate AS predicate,a.subject_entity_id AS subject_id,a.object_entity_id AS object_id""",
                ids=assertions,database_=self.database)
            self.assertEqual(len(frows),1+3*count)
            self.assertTrue(all(r["subject_matches"] and r["object_matches"] for r in frows))
            self.assertTrue(all(r["subject_status"]==r["fact_status"]=="APPROVED" for r in frows))
            return rows,frows,result

        first,_,_=upload("first","PROJECT-A",2)
        calls_before=len(reviewer.calls)
        second,second_facts,_=upload("second","PROJECT-A",2)
        self.assertEqual({r["entity_id"] for r in first},{r["entity_id"] for r in second})
        second_identity=[r for kind,payload in reviewer.calls[calls_before:] if kind=="identity" for r in payload["records"]]
        self.assertEqual(len(second_identity),3)
        self.assertTrue(all(len(r["candidate_target_ids"])==1 for r in second_identity))
        company=next(r["entity_id"] for r in first if r["entity_type"]=="Company")
        assets={r["entity_id"] for r in first if r["entity_type"]=="Asset"}
        self.assertTrue(all(r["subject_id"]==company and r["object_id"] in assets
                            for r in second_facts if r["predicate"]=="OWNS"))
        third,_,_=upload("other-project","PROJECT-B",1)
        self.assertEqual({r["entity_id"] for r in third if r["entity_type"]=="Company"},{company})
        third_assets={r["entity_id"] for r in third if r["entity_type"]=="Asset"}
        self.assertFalse(third_assets & assets)  # Same local name/serial, different declared project scope.
        rows,_,_=self.driver.execute_query("""MATCH (:KnowledgeRecordHead {record_kind:'ENTITY_MENTION'})
            -[:CURRENT_REVISION]->(m:GovernedEntityMentionRevision)
            RETURN count(DISTINCT m.entity_id) AS entities,count(m) AS mentions""",database_=self.database)
        self.assertEqual(rows[0]["entities"],4)
        self.assertGreater(rows[0]["mentions"],4)

    def test_uncertain_mapping_reviews_each_relation_and_keeps_only_one_manual(self):
        job = self.construct(count=3)
        class IndividualReviewer(Reviewer):
            async def review(self, kind, payload):
                result = await super().review(kind, payload)
                if kind == "mapping":
                    return replace(result, decisions=tuple(replace(d, action="UNCERTAIN",
                        reason="组内对象不同，需单条复核") for d in result.decisions))
                if kind == "facts":
                    return replace(result, decisions=tuple(replace(d, action="UNCERTAIN",
                        reason="仅本条关系含义不明") if i == 0 else d
                        for i, d in enumerate(result.decisions)))
                return result
        model = IndividualReviewer()
        service = Neo4jAutoReviewService(self.driver, self.database, reviewer=model)
        result = asyncio.run(service.run(self.principal, job_id=job.job_id))
        self.assertEqual(result["status"], "COMPLETED", self.errors())
        self.assertEqual(result["counts"]["approved_assertions"], 2, self.errors())
        self.assertEqual(result["counts"]["manual_assertions"], 1)
        items = [i for i in result["items"] if i["record_kind"] == "ASSERTION"]
        uncertain = next(i for i in items if i["decision"] == "NEEDS_HUMAN")
        self.assertEqual(uncertain["reason"], "仅本条关系含义不明")
        self.assertTrue(all("组内对象" not in i["reason"] for i in items))
        self.assertEqual(sum(len(p["records"]) for k, p in model.calls if k == "facts"), 3)
        # Retry changes only the unresolved record; confirmed evidence is retained.
        service.reviewer = Reviewer()
        retried = asyncio.run(service.run(self.principal, job_id=job.job_id, retry=True))
        self.assertEqual(retried["counts"]["approved_assertions"], 3, self.errors())
        self.assertEqual(retried["counts"]["manual_assertions"], 0)
        rows, _, _ = self.driver.execute_query("""MATCH (:KnowledgeRecordHead {record_kind:'ASSERTION'})
            -[:CURRENT_REVISION]->(a:GovernedAssertionRevision)
            RETURN a.record_id AS id, a.revision AS revision""", database_=self.database)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["revision"] for r in rows}, {2})

    def test_human_identity_confirmation_resumes_dependent_fact_without_overwriting_human(self):
        from datetime import datetime, timezone
        from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService, ReviewRequest, ReviewRecordKind
        from graphrag_prod.knowledge.trust import GovernanceStatus
        job = self.construct(count=2)
        class AmbiguousReviewer(Reviewer):
            async def review(self, kind, payload):
                result = await super().review(kind, payload)
                if kind == "identity":
                    unresolved = {r["record_id"] for r in payload["records"]
                                  if r.get("source_identity", {}).get("id") in {"asset-0", "asset-1"}}
                    return replace(result, decisions=tuple(replace(d, action="UNCERTAIN", target_id=None,
                        new_group_id=None) if d.record_id in unresolved else d for d in result.decisions))
                return result
        service = Neo4jAutoReviewService(self.driver, self.database, reviewer=AmbiguousReviewer())
        first = asyncio.run(service.run(self.principal, job_id=job.job_id))
        self.assertEqual(first["counts"]["blocked_assertions"], 2)
        pending = next(i for i in first["items"] if i["record_kind"] == "ENTITY_MENTION"
                       and i["decision"] == "NEEDS_HUMAN")
        reviews = Neo4jKnowledgeReviewService(self.driver, self.database)
        reviews.review_batch(self.principal, (ReviewRequest(
            ReviewRecordKind.ENTITY_MENTION, pending["record_id"], pending["input_revision"],
            GovernanceStatus.APPROVED, datetime.now(timezone.utc), "人工核对来源对象及范围，独立建档。",
            identity_action="INDEPENDENT"),))
        service.reviewer = Reviewer()
        with self.assertRaises(KnowledgeReviewUnavailable):
            asyncio.run(service.run(self.principal, job_id=job.job_id, retry=True,
                                   resume_identity_record_ids=("other-job-record",)))
        resumed = asyncio.run(service.run(self.principal, job_id=job.job_id, retry=True,
                                         resume_identity_record_ids=(pending["record_id"],)))
        self.assertEqual(resumed["status"], "COMPLETED", self.errors())
        self.assertEqual(resumed["counts"]["blocked_assertions"], 1)
        self.assertEqual(resumed["counts"]["manual_groups"], 1)
        self.assertFalse(any(kind == "identity" for kind, _ in service.reviewer.calls))
        self.assertEqual(resumed["counts"]["approved_assertions"], 1, self.errors())
        self.assertNotIn(pending["record_id"], {i["record_id"] for i in resumed["items"]})
        rows, _, _ = self.driver.execute_query("""MATCH (:KnowledgeRecordHead {record_id:$id})
            -[:CURRENT_REVISION]->(m:GovernedEntityMentionRevision)
            RETURN m.reviewed_by AS reviewer""", id=pending["record_id"], database_=self.database)
        self.assertEqual(rows[0]["reviewer"], self.principal.principal_id)
