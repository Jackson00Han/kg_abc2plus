"""Additive, source-pinned structured context mapping for construction jobs.

Original Chunk outcomes and reviewed records remain immutable. This separate
manifest makes newly mapped candidates replayable and visible to auto review.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Protocol

from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.domain.ids import content_checksum

from .models import (AssertionRecord, EntityMentionRecord, RecordRevision,
                     knowledge_record_id, llm_candidate_trust)
from .review import KnowledgeReviewUnavailable, Neo4jKnowledgeReviewService
from .store import KnowledgeConflict, Neo4jKnowledgeStore, _stored_assertion

POLICY_VERSION = "context-projection:v1"
MAX_RECORDS = 6000
MAX_AUDIT_CHARS = 500000
MAX_RUNS = 16


class ContextMappingPlanner(Protocol):
    async def plan(self, payload: dict) -> dict:
        """Return a validated declarative mapping and its protected audit."""
        ...


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def projection_record_id(tenant_id, version_id, context):
    return knowledge_record_id(tenant_id, "ASSERTION", _json([
        POLICY_VERSION, version_id, context.collection_pointer, context.source_identity,
        context.property_name, context.value_pointer,
    ]))


def projection_record_ids_tx(tx, principal, job):
    """Read only manifests pinned to the already-authorized source operation."""
    rows = list(tx.run("""MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job})
        -[:HAS_CONTEXT_PROJECTION]->(r:KnowledgeContextProjectionRun {tenant_id:$tenant,job_id:$job})
        WHERE r.document_id=$document AND r.version_id=$version AND r.snapshot_id=$snapshot
          AND r.tbox_id=$tbox AND any(g IN $groups WHERE g IN r.access_groups)
        RETURN r.record_ids AS ids,r.manifest_json AS manifest,r.manifest_checksum AS checksum
        LIMIT 17""", tenant=principal.tenant_id, job=job.job_id, document=job.document_id,
        version=job.version_id, snapshot=job.snapshot_id, tbox=job.tbox_id, groups=sorted(principal.groups)))
    if len(rows)>MAX_RUNS:
        raise KnowledgeConflict("context projection run limit exceeded")
    output=set()
    for row in rows:
        raw=row["manifest"]
        if not isinstance(raw,str) or len(raw)>MAX_AUDIT_CHARS or content_checksum(raw)!=row["checksum"]:
            raise KnowledgeConflict("context projection manifest checksum differs")
        manifest=json.loads(raw)
        ids=row["ids"] or []
        if (not isinstance(ids,list) or not all(isinstance(i,str) for i in ids)
            or not set(ids)<=set(manifest.get("expected_record_ids",()))):
            raise KnowledgeConflict("context projection output differs from manifest")
        output.update(ids)
    if len(output)>MAX_RECORDS:
        raise KnowledgeConflict("context projection record limit exceeded")
    return output


class Neo4jContextProjectionService:
    def __init__(self, driver, database="neo4j", *, planner: ContextMappingPlanner | None):
        self.driver,self.database,self.planner=driver,database,planner
        self.store=Neo4jKnowledgeStore(driver,database)

    def _read(self, principal, job):
        with self.driver.session(database=self.database) as session:
            rows=list(session.run("""MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job})
                -[:HAS_CONTEXT_PROJECTION]->(r:KnowledgeContextProjectionRun {tenant_id:$tenant,job_id:$job})
                WHERE any(g IN $groups WHERE g IN r.access_groups)
                  AND r.document_id=$document AND r.version_id=$version AND r.snapshot_id=$snapshot
                  AND r.tbox_id=$tbox
                RETURN r{.*} AS run ORDER BY coalesce(r.generation,0) DESC,r.created_at DESC,r.run_id DESC
                LIMIT 17""", tenant=principal.tenant_id,job=job.job_id,document=job.document_id,
                version=job.version_id,snapshot=job.snapshot_id,tbox=job.tbox_id,groups=sorted(principal.groups)))
        if len(rows)>MAX_RUNS:
            raise KnowledgeConflict("context projection run limit exceeded")
        runs=[]
        for row in rows:
            run=dict(row["run"])
            encoded=run["manifest_json"]
            if (not isinstance(encoded,str) or len(encoded)>MAX_AUDIT_CHARS
                or content_checksum(encoded)!=run["manifest_checksum"]):
                raise KnowledgeConflict("context mapping manifest changed")
            run["_expected_ids"]=json.loads(encoded)["expected_record_ids"]
            if (not isinstance(run["_expected_ids"],list) or len(run["_expected_ids"])>MAX_RECORDS
                or not all(isinstance(i,str) and 0<len(i)<=256 for i in run["_expected_ids"])):
                raise KnowledgeConflict("context mapping manifest output bound differs")
            run["_run_count"]=len(rows)
            runs.append(run)
        # Once a mapping has planned any outputs, it owns their replay even if
        # a write failed before the progress receipt. Only empty plans may be
        # replaced by a separate, explicitly retried immutable run.
        return next((r for r in runs if r["_expected_ids"]),runs[0] if runs else None)

    def _chunks(self, principal, job, text):
        with self.driver.session(database=self.database) as session:
            rows=list(session.run("""MATCH (d:Document {tenant_id:$tenant,document_id:$document})
                -[:ACTIVE_VERSION]->(v:DocumentVersion {tenant_id:$tenant,version_id:$version})
                MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {snapshot_id:$snapshot,build_state:'PUBLISHED'})
                MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk {tenant_id:$tenant,version_id:$version})
                WHERE any(g IN $groups WHERE g IN d.access_groups)
                  AND any(g IN $groups WHERE g IN c.access_groups)
                  AND c.access_policy_id=d.access_policy_id AND c.access_policy_version=d.access_policy_version
                  AND c.access_groups=d.access_groups
                RETURN c{.*} AS chunk ORDER BY c.ordinal LIMIT 2001""", tenant=principal.tenant_id,
                document=job.document_id,version=job.version_id,snapshot=job.snapshot_id,groups=sorted(principal.groups)))
        if len(rows)!=len(job.chunks) or len(rows)>2000:
            raise KnowledgeReviewUnavailable("complete context source chunks unavailable")
        chunks=[]
        for row in rows:
            value=dict(row["chunk"])
            if (text[value["char_start"]:value["char_end"]]!=value["text"]
                or content_checksum(value["text"])!=value["checksum"]):
                raise KnowledgeConflict("context source Chunk checksum differs")
            value["access_groups"]=frozenset(value["access_groups"])
            chunks.append(SimpleNamespace(**value))
        return chunks

    def _save_plan(self, principal, job, data, manifest, created_at):
        encoded=_json(manifest)
        if len(encoded)>MAX_AUDIT_CHARS:
            raise KnowledgeConflict("context mapping audit limit exceeded")
        def work(tx):
            Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(tx,principal.tenant_id,created_at)
            count=tx.run("""MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job})
                OPTIONAL MATCH (j)-[:HAS_CONTEXT_PROJECTION]->(r:KnowledgeContextProjectionRun)
                RETURN count(r) AS count""",tenant=principal.tenant_id,job=job.job_id).single()
            if count is None or count["count"]>=MAX_RUNS:
                raise KnowledgeConflict("context projection run limit exceeded")
            generation=count["count"]+1
            identifier=_hash([POLICY_VERSION,principal.tenant_id,job.job_id,generation,content_checksum(encoded)])
            row=tx.run("""MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job})
                MERGE (r:KnowledgeContextProjectionRun {run_id:$run})
                ON CREATE SET r.tenant_id=$tenant,r.job_id=$job,r.document_id=$document,
                    r.version_id=$version,r.snapshot_id=$snapshot,r.tbox_id=$tbox,
                    r.source_checksum=$source_checksum,r.tbox_checksum=$tbox_checksum,
                    r.policy_version=$policy,r.access_groups=$groups,r.initiated_by=$actor,
                    r.generation=$generation,
                    r.created_at=$created,r.status='RUNNING',r.record_ids=[],
                    r.manifest_json=$manifest,r.manifest_checksum=$checksum
                MERGE (j)-[:HAS_CONTEXT_PROJECTION]->(r)
                RETURN r.manifest_checksum=$checksum AS compatible""", run=identifier,
                tenant=principal.tenant_id,job=job.job_id,document=job.document_id,version=job.version_id,
                snapshot=job.snapshot_id,tbox=job.tbox_id,source_checksum=content_checksum(data["text"]),
                tbox_checksum=data["tbox"].checksum,policy=POLICY_VERSION,groups=data["groups"],
                actor=principal.principal_id,created=created_at,manifest=encoded,checksum=content_checksum(encoded),generation=generation).single()
            if row is None or not row["compatible"]:
                raise KnowledgeConflict("context mapping replay conflicts with its immutable plan")
            return identifier
        with self.driver.session(database=self.database) as session:
            return session.execute_write(work)

    def _progress(self, identifier, record_ids, summary):
        with self.driver.session(database=self.database) as session:
            session.run("""MATCH (r:KnowledgeContextProjectionRun {run_id:$run})
                SET r.record_ids=$ids,r.summary_json=$summary,r.status=$status,r.updated_at=$now""",
                run=identifier,ids=sorted(record_ids),summary=_json(summary),status=summary["status"],
                now=datetime.now(timezone.utc)).consume()

    def _audit_proposal(self, principal, job, proposal):
        encoded=_json(proposal)
        if len(encoded)>MAX_AUDIT_CHARS:
            raise KnowledgeConflict("context mapping attempt exceeds audit bound")
        with self.driver.session(database=self.database) as session:
            session.run("""MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job})
                MERGE (a:KnowledgeContextMappingAttempt {attempt_id:$id})
                ON CREATE SET a.tenant_id=$tenant,a.job_id=$job,a.payload_json=$payload,
                    a.checksum=$checksum,a.access_groups=j.access_groups,a.created_at=$now,a.initiated_by=$actor
                MERGE (j)-[:HAS_CONTEXT_MAPPING_ATTEMPT]->(a)""",tenant=principal.tenant_id,
                job=job.job_id,id=_hash([principal.tenant_id,job.job_id,encoded]),payload=encoded,
                checksum=content_checksum(encoded),actor=principal.principal_id,now=datetime.now(timezone.utc)).consume()

    async def run(self, principal, job, data, *, deadline, retry=False):
        from graphrag_prod.construction.context_mapping import build_context_payload, compile_context_properties
        if not {"knowledge:construct","knowledge:review"}<=principal.capabilities:
            raise KnowledgeReviewUnavailable("context construction is not authorized")
        mapping_summary=next((c.mapping_summary for c in job.chunks if c.mapping_summary),None)
        skipped={"status":"SKIPPED","added_assertions":0,"applied":0,"uncertain":0,
                 "overridden":0,"rules":[],"issues":[]}
        if not mapping_summary:
            return skipped
        previous=self._read(principal,job)
        if previous and (previous["source_checksum"]!=content_checksum(data["text"])
                         or previous["tbox_checksum"]!=data["tbox"].checksum):
            raise KnowledgeConflict("context mapping source or ontology changed")
        if previous and retry and not previous["_expected_ids"]:
            if previous["_run_count"]>=MAX_RUNS:
                raise KnowledgeConflict("context projection run limit exceeded")
            previous=None
        if previous:
            encoded=previous["manifest_json"]
            if (content_checksum(encoded)!=previous["manifest_checksum"]
                or previous["source_checksum"]!=content_checksum(data["text"])
                or previous["tbox_checksum"]!=data["tbox"].checksum):
                raise KnowledgeConflict("context mapping source or manifest changed")
            if previous["status"]=="COMPLETED":
                return json.loads(previous["summary_json"])
            manifest=json.loads(encoded)
            proposal=manifest["proposal"]
            created=previous["created_at"].to_native() if hasattr(previous["created_at"],"to_native") else previous["created_at"]
        else:
            mentions=[r for r in data["current"] if isinstance(r,EntityMentionRecord)]
            payload=build_context_payload(data["text"],data["tbox"],mapping_summary,mentions)
            if not payload:
                return skipped
            if self.planner is None or time.monotonic()>=deadline:
                return {**skipped,"status":"UNAVAILABLE"}
            try:
                proposal=await asyncio.wait_for(self.planner.plan(payload),max(0.1,deadline-time.monotonic()))
            except Exception as error:
                self._audit_proposal(principal,job,{"status":"UNAVAILABLE","error_type":type(error).__name__})
                return {**skipped,"status":"UNAVAILABLE"}
            self._audit_proposal(principal,job,proposal)
            if proposal.get("status")!="COMPLETE":
                return {**skipped,"status":"UNAVAILABLE"}
            created=datetime.now(timezone.utc)
        chunks=self._chunks(principal,job,data["text"])
        mentions=[r for r in data["current"] if isinstance(r,EntityMentionRecord)]
        facts=[r for r in data["comparison"] if isinstance(r,AssertionRecord)]
        compilation=compile_context_properties(data["text"],data["tbox"],mapping_summary,
            mentions,facts,chunks,proposal["mapping"],deadline=deadline)
        definitions={e.name:e for e in data["tbox"].entity_types}
        records=[]
        normalizer=TBoxLiteralNormalizer()
        for spec in compilation.specs:
            mention=spec.subject_mention
            prop=next(p for p in definitions[mention.entity.entity_type].properties if p.name==spec.property_name)
            value=normalizer.normalize(prop,raw_value=spec.raw_literal,raw_unit=None,
                valid_from=None,valid_to=None,observed_at=None,source_encoding=spec.source_encoding)
            trust=llm_candidate_trust(ontology_version_id=job.tbox_id,extractor_version=POLICY_VERSION,
                prompt_version="json-context-mapping:v1",extracted_at=created)
            trust=replace(trust,origin=mention.trust.origin,authority=mention.trust.authority)
            rid=projection_record_id(principal.tenant_id,job.version_id,spec.context_property_evidence)
            records.append(AssertionRecord(revision=RecordRevision.next(rid,0),tenant_id=principal.tenant_id,
                subject=mention.entity,predicate=spec.property_name,evidence=spec.evidence,
                subject_mention_revision_id=mention.revision_id,confidence=mention.confidence,
                trust=trust,created_at=created,literal_value=spec.raw_literal,literal_semantics=value,
                context_property_evidence=spec.context_property_evidence))
        if len(records)>MAX_RECORDS:
            raise KnowledgeConflict("context candidate count exceeds bound")
        if previous is None:
            manifest={"policy_version":POLICY_VERSION,"proposal":proposal,
                "source_checksum":content_checksum(data["text"]),"tbox_checksum":data["tbox"].checksum,
                "expected_record_ids":sorted(r.record_id for r in records)}
            identifier=self._save_plan(principal,job,data,manifest,created)
        else:
            identifier=previous["run_id"]
        committed=set(previous.get("record_ids",())) if previous else set()
        expected=set(manifest["expected_record_ids"])
        if not {r.record_id for r in records}<=expected:
            raise KnowledgeConflict("context mapping output changed on replay")
        # Recover outputs written immediately before a progress receipt failed.
        with self.driver.session(database=self.database) as session:
            recovered=list(session.run("""MATCH (r:GovernedAssertionRevision {tenant_id:$tenant,version_id:$version,revision:1})
                WHERE r.record_id IN $ids AND any(g IN $groups WHERE g IN r.access_groups)
                RETURN r{.*} AS revision""",tenant=principal.tenant_id,version=job.version_id,
                ids=sorted(expected),groups=sorted(principal.groups)))
        for row in recovered:
            record=_stored_assertion(dict(row["revision"]))
            context=record.context_property_evidence
            if (context is None or context.source_checksum!=content_checksum(data["text"])
                or projection_record_id(principal.tenant_id,job.version_id,context)!=record.record_id
                or record.trust.extractor_version!=POLICY_VERSION):
                raise KnowledgeConflict("recovered context record differs from its manifest")
            committed.add(record.record_id)
        summary={"status":"RUNNING","added_assertions":len(expected),"applied":len(committed),
                 "uncertain":sum(i["entity_count"] for i in compilation.issues),
                 "overridden":compilation.overridden,"rules":list(compilation.rules),"issues":list(compilation.issues)}
        self._progress(identifier,committed,summary)
        remaining=[r for r in records if r.record_id not in committed]
        for start in range(0,len(remaining),60):
            if time.monotonic()>=deadline:
                summary["status"]="PARTIAL"
                self._progress(identifier,committed,summary)
                return summary
            packet=tuple(remaining[start:start+60])
            self.store.persist_context_candidates(principal,packet)
            committed.update(r.record_id for r in packet)
            summary["applied"]=len(committed)
            self._progress(identifier,committed,summary)
        summary["status"]="COMPLETED" if committed==expected and not compilation.issues else "PARTIAL"
        self._progress(identifier,committed,summary)
        return summary
