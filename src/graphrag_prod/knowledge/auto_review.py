"""Evidence-gated, append-only automatic pre-review of construction candidates.

A model proposes decisions; deterministic source identity, mapping, ontology,
comparison and transaction checks decide whether those proposals may be applied.
Publication stays separate. Model output is never executed as code or Cypher.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from graphrag_prod.construction.structured import VERSION as MAPPING_VERSION, collections, digest, locate_json
from graphrag_prod.construction.workflow import Neo4jConstructionAuditStore
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.facts import literal_signature
from graphrag_prod.domain.ids import content_checksum, entity_id
from graphrag_prod.ontology.store import _decode_tbox

from .models import AssertionRecord, EntityIdentity, EntityMentionRecord
from .review import (KNOWLEDGE_REVIEW_CAPABILITY, KnowledgeReviewUnavailable,
    Neo4jKnowledgeReviewService, ReviewRecordKind, ReviewRequest, _active_revision_query)
from .review_assessment import fact_signature
from .review_context import _resolution_revision_query
from .store import KnowledgeConflict, _stored_assertion, _stored_mention
from .trust import GovernanceStatus

POLICY_VERSION = "evidence-auto-review:v4"
# Reviewer upgrades must never change the stable identity namespace of objects.
IDENTITY_KEY_VERSION = "evidence-auto-review:v1"
MAX_RECORDS = 6000
MAX_COMPARISON_RECORDS = 12000
MAX_SOURCE_CHARS = 262144
MAX_GROUP_MEMBERS = 60
MODEL_BATCH_SIZE = 8
MAX_MODEL_CALLS = 40
MAX_TOTAL_MODEL_CALLS = 80
MAX_RESPONSE_ITEMS = 200
MAX_RESPONSE_ISSUES = 100
MAX_ISSUE_ENTITIES = 200
LEASE_SECONDS = 1500


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _evidence(record):
    value = record.evidence
    return {"evidence_id": record.record_id, "chunk_id": value.chunk_id,
            "document_version_id": value.version_id, "char_start": value.char_start,
            "char_end": value.char_end, "text": value.quoted_text}


def _item(record, decision, reason_code, reason, target=None):
    evidence_ids=[record.evidence.chunk_id]
    context=getattr(record,"context_property_evidence",None)
    if context is not None and context.value_evidence.chunk_id not in evidence_ids:
        evidence_ids.append(context.value_evidence.chunk_id)
    return {"record_id": record.record_id, "input_revision": record.revision.revision,
            "record_kind": "ENTITY_MENTION" if isinstance(record, EntityMentionRecord) else "ASSERTION",
            "decision": decision, "reason_code": reason_code, "reason": reason[:1000],
            "target_entity_id": target, "evidence_ids": evidence_ids}


def _fact_review_input(fact, proof, revisions):
    """One assertion and its own immutable evidence; never a mapping-group leader."""
    evidence = _evidence(fact)
    source = proof.identity(revisions[fact.subject_mention_revision_id])
    if fact.object_entity is None and source:
        evidence["exact_quote"] = evidence["text"]
        evidence["text"] = proof.text[source.node.start:source.node.end]
        evidence["context_char_start"] = source.node.start
        evidence["context_char_end"] = source.node.end
    context = fact.context_property_evidence
    if context is not None:
        value = context.value_evidence
        evidence["context_value"] = {
            "document_version_id": value.version_id, "chunk_id": value.chunk_id,
            "char_start": value.char_start, "char_end": value.char_end,
            "text": value.quoted_text, "source_path": context.value_pointer,
            "scope_path": context.scope_pointer, "binding": json.loads(context.binding_json)}
    record = {"record_id": fact.record_id, "evidence_ids": [fact.record_id],
        "subject": asdict(fact.subject), "predicate": fact.predicate,
        "object": asdict(fact.object_entity) if fact.object_entity else None,
        "literal": fact.literal_semantics.to_mapping() if fact.literal_semantics else None,
        "relationship_properties": [{"name": p.name, "literal": p.literal_semantics.to_mapping()}
                                    for p in fact.relationship_properties],
        "mapping": proof.fact_mapping(fact, revisions)}
    return record, evidence


@dataclass(frozen=True)
class SourceIdentity:
    collection: str
    source_id: str
    entity_type: str
    node: Any
    rule: dict


class StructuredEvidence:
    """Recheck source records and every mapped assertion against immutable JSON."""

    def __init__(self, text, document_id, tbox_id, summary, mentions):
        self.text, self.document_id, self.summary = text, document_id, summary
        self.identities = {}
        self.by_source = {}
        self.by_record = {}
        if not summary or len(text) > MAX_SOURCE_CHARS:
            return
        root = locate_json(text)
        groups = collections(root)
        for rule in summary.get("collections", ()):
            path = rule["collection"]
            rows = groups.get(path, ())
            if len(rows) != rule["record_count"]:
                raise ValueError("mapping collection count differs from source")
            seen = set()
            for row in rows:
                node = row.at(rule["id_field"])
                if node is None or not isinstance(node.value, str) or not node.value or node.value in seen:
                    raise ValueError("mapping source identity is not unique")
                seen.add(node.value)
                for kind in rule["entity_types"]:
                    key = "llm-candidate:" + digest({"version": MAPPING_VERSION,
                        "document": document_id, "ontology": tbox_id,
                        "collection": path, "source_id": node.value, "type": kind})
                    identity = SourceIdentity(path, node.value, kind, row, rule)
                    self.identities[key] = identity
                    self.by_source[path, node.value, kind] = identity
        for record in mentions:
            identity = self.identities.get(record.entity.canonical_key)
            # An earlier successful auto-review may retain the original source
            # identity in the record's immutable first revision; caller supplies
            # original identities separately when a cross-source link changed it.
            if identity and identity.entity_type == record.entity.entity_type:
                self.by_record[record.record_id] = identity

    def restore(self, original_mentions):
        for record in original_mentions:
            identity = self.identities.get(record.entity.canonical_key)
            if identity and identity.entity_type == record.entity.entity_type:
                self.by_record[record.record_id] = identity

    def identity(self, record):
        return self.by_record.get(record.record_id)

    def fact_mapping(self, fact, mentions_by_revision):
        if getattr(self, "document_candidates", False):
            from .document_review_evidence import document_fact_mapping
            return document_fact_mapping(self, fact, mentions_by_revision)
        subject_record = mentions_by_revision.get(fact.subject_mention_revision_id)
        subject = self.identity(subject_record) if subject_record else None
        if subject is None:
            return None
        context = fact.context_property_evidence
        if context is not None:
            from graphrag_prod.construction.context_mapping import validate_context_binding
            try:
                validate_context_binding(self.text, context, subject_type=fact.subject.entity_type,
                    predicate=fact.predicate, raw_literal=fact.literal_value)
            except (ValueError, TypeError):
                return None
            root = locate_json(self.text)
            target = root.at(context.record_pointer)
            value = root.at(context.value_pointer)
            if (context.collection_pointer != subject.collection or context.source_identity != subject.source_id
                or context.identity_pointer != subject.rule["id_field"] or target is None or value is None
                or (target.start,target.end) != (subject.node.start,subject.node.end)
                or (fact.evidence.char_start,fact.evidence.char_end) != (target.start,target.end)
                or self.text[target.start:target.end] != fact.evidence.quoted_text
                or self.text[value.start:value.end] != fact.literal_value
                or (value.start,value.end) != (context.value_evidence.char_start,context.value_evidence.char_end)
                or self.text[value.start:value.end] != context.value_evidence.quoted_text):
                return None
            return {"subject_type":subject.entity_type,"predicate":fact.predicate,"kind":"CONTEXT_PROPERTY",
                    "collection":subject.collection,"source_path":context.value_pointer,
                    "scope_path":context.scope_pointer,"mapping_checksum":context.mapping_checksum,
                    "binding":json.loads(context.binding_json)}
        # Properties must be exact complete field tokens, not model-generated
        # paraphrases. Store validation separately re-normalizes typed semantics.
        if fact.object_entity is None:
            for prop in subject.rule["properties"]:
                if prop["property"] != fact.predicate:
                    continue
                node = subject.node.at(prop["field"])
                if node and (fact.evidence.char_start, fact.evidence.char_end) in {
                    (node.start, node.end), (subject.node.start, subject.node.end)}:
                    if (self.text[node.start:node.end] == fact.literal_value
                        and self.text[fact.evidence.char_start:fact.evidence.char_end] == fact.evidence.quoted_text):
                        return {"subject_type": subject.entity_type, "predicate": fact.predicate,
                                "field": prop["field"], "kind": "PROPERTY", "collection": subject.collection}
            return None
        object_record = mentions_by_revision.get(fact.object_mention_revision_id)
        obj = self.identity(object_record) if object_record else None
        if obj is None:
            return None
        for source, target, direction in ((subject, obj, "out"), (obj, subject, "in")):
            for rel in source.rule["relations"]:
                if (rel["type"] != fact.predicate or rel["direction"] != direction
                    or rel["target_collection"] != target.collection):
                    continue
                node = source.node.at(rel["field"])
                target_node = target.node.at(rel["target_field"])
                if (node is None or target_node is None or node.value != target_node.value
                    or (fact.evidence.char_start, fact.evidence.char_end) != (source.node.start, source.node.end)
                    or self.text[source.node.start:source.node.end] != fact.evidence.quoted_text):
                    continue
                expected_properties = {}
                for prop in rel.get("properties", ()):
                    pnode = source.node.at(prop["field"])
                    if pnode is not None and pnode.value is not None:
                        expected_properties[prop["property"]] = pnode
                if set(expected_properties) != {p.name for p in fact.relationship_properties}:
                    continue
                if any((p.evidence_char_start, p.evidence_char_end) not in {
                       (expected_properties[p.name].start, expected_properties[p.name].end),
                       (source.node.start, source.node.end)}
                       or p.evidence_text != self.text[p.evidence_char_start:p.evidence_char_end]
                       or p.literal_semantics.raw_value != self.text[expected_properties[p.name].start:expected_properties[p.name].end]
                       for p in fact.relationship_properties):
                    continue
                return {"subject_type": subject.entity_type, "predicate": fact.predicate,
                        "object_type": obj.entity_type, "kind": "RELATIONSHIP", "mapping": rel,
                        "collection": source.collection}
        return None


def _identity_values(mentions, facts, tbox):
    definitions = {e.name: e for e in tbox.entity_types}
    values = defaultdict(lambda: defaultdict(set))
    for fact in facts:
        definition = definitions.get(fact.subject.entity_type)
        if definition and fact.predicate in definition.identity_properties and fact.literal_semantics:
            values[fact.subject.entity_id][fact.predicate].add(literal_signature(fact.literal_semantics))
    result = {}
    for mention in mentions:
        definition = definitions[mention.entity.entity_type]
        fields = definition.identity_properties
        selected = values[mention.entity.entity_id]
        if fields and all(len(selected[name]) == 1 for name in fields):
            result[mention.entity.entity_id] = (mention.entity.entity_type,
                tuple((name, next(iter(selected[name]))) for name in sorted(fields)))
    return result


def _fact_conflicts(facts, tbox=None):
    """Conservative comparison: differing property values/relationship metadata wait."""
    signatures = defaultdict(set)
    for fact in facts:
        key = (fact.subject.entity_id, fact.predicate,
               fact.object_entity.entity_id if fact.object_entity else None)
        signatures[key].add(fact_signature(fact))
    conflicts = {key for key, values in signatures.items() if len(values) > 1}
    if tbox is not None:
        definitions = {r.name:r for r in tbox.relationship_types}
        outgoing, incoming = defaultdict(set), defaultdict(set)
        relations = [f for f in facts if f.object_entity is not None]
        for f in relations:
            outgoing[f.predicate,f.subject.entity_id].add(f.object_entity.entity_id)
            incoming[f.predicate,f.object_entity.entity_id].add(f.subject.entity_id)
        for f in relations:
            definition = definitions[f.predicate]
            if ((definition.source_cardinality.single_valued and len(outgoing[f.predicate,f.subject.entity_id])>1)
                or (definition.target_cardinality.single_valued and len(incoming[f.predicate,f.object_entity.entity_id])>1)):
                conflicts.add((f.subject.entity_id,f.predicate,f.object_entity.entity_id))
        # Cycle detection is valid on positive visible edges; unlike missing
        # required edges it does not assume this ACL scope is the whole world.
        from graphrag_prod.ontology.hierarchy import HierarchyEdge, HierarchyValidationError, validate_hierarchy_edges
        for hierarchy in tbox.hierarchies:
            selected = [f for f in relations if f.predicate==hierarchy.relationship_type]
            try:
                validate_hierarchy_edges(tbox,(HierarchyEdge(f.subject.entity_id,f.subject.entity_type,
                    f.predicate,f.object_entity.entity_id,f.object_entity.entity_type) for f in selected))
            except HierarchyValidationError:
                conflicts.update((f.subject.entity_id,f.predicate,f.object_entity.entity_id) for f in selected)
    return conflicts


def required_property_issues(tbox, entities, property_pairs, source_text):
    """Readiness guidance only: never inherit metadata or change governance.

    A property already represented by a usable pending/approved fact belongs in
    its existing fact queue. This report covers attributes with no usable fact.
    Root-context paths are hints to review scope, not evidence of inheritance.
    """
    definitions = {e.name:e for e in tbox.entity_types}
    representatives = {}
    for row in entities:
        previous = representatives.get(row["entity_id"])
        if previous is None or row["record_id"] < previous["record_id"]:
            representatives[row["entity_id"]] = row
    missing = defaultdict(list)
    for identifier,row in sorted(representatives.items()):
        definition = definitions.get(row["entity_type"])
        if definition is None:
            continue  # The publication contract independently rejects unknown types.
        for prop in definition.properties:
            if prop.cardinality.required and (identifier,prop.name) not in property_pairs:
                missing[prop.name].append(row)
    context_paths = defaultdict(list)
    if missing and isinstance(source_text,str) and len(source_text)<=MAX_SOURCE_CHARS:
        try:
            root = locate_json(source_text)
            def visit(node,path):
                # Record arrays and embedded lists cannot establish a global
                # default. Only inspect scalar fields in root object context.
                if not isinstance(node.value,dict):
                    return
                for name,child in node.children.items():
                    location = path+"/"+name.replace("~","~0").replace("/","~1")
                    if isinstance(child.value,dict):
                        visit(child,location)
                    elif (name in missing and not isinstance(child.value,list) and child.value is not None
                          and (not isinstance(child.value,str) or child.value.strip())):
                        if len(context_paths[name])<16:
                            context_paths[name].append(location)
            visit(root,"")
        except (ValueError,TypeError,IndexError,RecursionError):
            pass  # Non-JSON documents have no JSON context path hints.
    issues = []
    for name,rows in sorted(missing.items()):
        paths = sorted(context_paths.get(name,()))
        reason = "这些已确认实体缺少本体要求的属性事实，发布前需要补齐来源映射并复核。"
        if paths:
            reason += "文档根部上下文存在同名字段，但适用范围尚未确立，系统不会自动继承或补值。"
        issues.append({"code":"PROPERTY_REQUIRED","property_name":name,
            "entity_ids":[r["entity_id"] for r in rows],"record_ids":[r["record_id"] for r in rows],
            "entity_count":len(rows),"reason":reason,"source_paths":paths})
    return issues


def _readiness_from_data(data):
    entities = [{"entity_id":r.entity.entity_id,"entity_type":r.entity.entity_type,"record_id":r.record_id}
                for r in data["current"] if isinstance(r,EntityMentionRecord)
                and r.trust.status in {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED}]
    facts = {(r.subject.entity_id,r.predicate) for r in data["comparison"]
             if isinstance(r,AssertionRecord) and r.object_entity is None and r.literal_semantics is not None
             and r.trust.status in {GovernanceStatus.CANDIDATE,GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED}}
    return required_property_issues(data["tbox"],entities,facts,data["text"])


def _combined_issues(required, context):
    result=[]
    seen=set()
    for issue in [*required,*context.get("issues",())]:
        key=(issue["code"],issue["property_name"],tuple(sorted(issue["entity_ids"])))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


class Neo4jAutoReviewService:
    def __init__(self, driver, database="neo4j", *, reviewer=None, context_projection=None):
        self.driver, self.database, self.reviewer = driver, database, reviewer
        self.review = Neo4jKnowledgeReviewService(driver, database)
        self.jobs = Neo4jConstructionAuditStore(driver, database)
        self.context_projection = context_projection

    def _job(self, principal, job_id):
        job = self.jobs.get_job(principal, job_id)
        if job is None:
            raise KnowledgeReviewUnavailable("construction job is unavailable")
        return job

    def get(self, principal, job_id):
        job = self._job(principal, job_id)
        with self.driver.session(database=self.database) as session:
            row = session.run("""MATCH (r:KnowledgeAutoReviewRun {tenant_id:$tenant,job_id:$job})
                WHERE any(g IN $groups WHERE g IN r.access_groups)
                RETURN r.summary_json AS payload ORDER BY r.updated_at DESC LIMIT 1""",
                tenant=principal.tenant_id, job=job_id, groups=sorted(principal.groups)).single()
        if row is None:
            return None
        result = json.loads(row["payload"])
        if result.get("status") != "RUNNING":
            with self.driver.session(database=self.database) as session:
                result["issues"] = session.execute_read(self._readiness_tx,principal,job)
            result["issues"] = _combined_issues(result["issues"],result.get("context_mapping",{}))
        return self._public(result)

    @staticmethod
    def _readiness_tx(tx,principal,job):
        source = tx.run("""MATCH (d:Document {tenant_id:$tenant,document_id:$document})
            -[:ACTIVE_VERSION]->(v:DocumentVersion {tenant_id:$tenant,version_id:$version})
            MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {snapshot_id:$snapshot,build_state:'PUBLISHED'})
            MATCH (:TBoxCatalog {tenant_id:$tenant})-[:ACTIVE_TBOX_VERSION]->(t:TBoxVersion {tbox_id:$tbox,status:'PUBLISHED'})
            WHERE any(g IN $groups WHERE g IN d.access_groups)
            RETURN v.normalized_text AS text,v.checksum AS checksum,t{.*} AS tbox""",
            tenant=principal.tenant_id,document=job.document_id,version=job.version_id,snapshot=job.snapshot_id,
            tbox=job.tbox_id,groups=sorted(principal.groups)).single()
        if source is None or not isinstance(source["text"],str) or content_checksum(source["text"])!=source["checksum"]:
            raise KnowledgeReviewUnavailable("readiness source or ontology is no longer available")
        tbox = _decode_tbox(source["tbox"])
        ids = sorted({rid for chunk in job.chunks for rid in chunk.mention_record_ids})
        if len(ids)>MAX_RECORDS:
            raise KnowledgeConflict("readiness record limit exceeded")
        query = _active_revision_query(ReviewRecordKind.ENTITY_MENTION,one_record=False,
            extra_where="AND revision.record_id IN $ids AND revision.version_id=$version_id",
            projection="revision.entity_id AS entity_id,revision.entity_type AS entity_type,revision.record_id AS record_id")
        rows = list(tx.run(query,tenant_id=principal.tenant_id,groups=sorted(principal.groups),
            ids=ids,version_id=job.version_id,statuses=["APPROVED","PUBLISHED"],limit=MAX_RECORDS+1))
        if len(rows)>MAX_RECORDS:
            raise KnowledgeConflict("readiness record limit exceeded")
        entities = [dict(row) for row in rows]
        entity_ids = sorted({row["entity_id"] for row in entities})
        required_names = sorted({p.name for e in tbox.entity_types for p in e.properties if p.cardinality.required})
        query = _resolution_revision_query(ReviewRecordKind.ASSERTION,one_record=False,
            extra_where="AND revision.subject_entity_id IN $entity_ids AND revision.predicate IN $predicates "
                        "AND revision.ontology_version_id=$tbox AND revision.object_kind='literal' "
                        "AND revision.literal_datatype IS NOT NULL",
            projection="revision.subject_entity_id AS entity_id,revision.predicate AS property_name")
        facts = list(tx.run(query,tenant_id=principal.tenant_id,groups=sorted(principal.groups),
            entity_ids=entity_ids,predicates=required_names,tbox=job.tbox_id,
            statuses=["CANDIDATE","APPROVED","PUBLISHED"],limit=MAX_COMPARISON_RECORDS+1)) if entity_ids and required_names else []
        if len(facts)>MAX_COMPARISON_RECORDS:
            raise KnowledgeConflict("readiness comparison limit exceeded")
        return required_property_issues(tbox,entities,{(r["entity_id"],r["property_name"]) for r in facts},source["text"])

    @staticmethod
    def _public(result):
        output = {k:v for k,v in result.items() if not k.startswith("_")}
        output["truncated"] = len(output.get("items", ())) > MAX_RESPONSE_ITEMS
        priority = {"INCOMPLETE":0,"NEEDS_HUMAN":1,"BLOCKED":2,"AUTO_APPROVED":3}
        output["items"] = sorted(output.get("items", []),key=lambda i:(priority.get(i["decision"],4),
            i.get("reason_code")=="PREVIOUS_AUTO_APPROVAL"))[:MAX_RESPONSE_ITEMS]
        issues = output.get("issues",[])
        if output.get("status") == "COMPLETED" and issues:
            output["status"] = "PARTIAL"
        output["truncated"] = output["truncated"] or len(issues)>MAX_RESPONSE_ISSUES or any(i["entity_count"]>MAX_ISSUE_ENTITIES for i in issues)
        output["issues"] = [{**i,"entity_ids":i["entity_ids"][:MAX_ISSUE_ENTITIES],
                             "record_ids":i["record_ids"][:MAX_ISSUE_ENTITIES]} for i in issues[:MAX_RESPONSE_ISSUES]]
        if output.get("context_mapping") is not None:
            context=dict(output["context_mapping"])
            if (context.get("status") == "COMPLETED" and not context.get("rules")
                    and not context.get("applied") and issues):
                context["status"] = "PARTIAL"
            cissues=context.get("issues",[])
            output["truncated"] = output["truncated"] or len(cissues)>MAX_RESPONSE_ISSUES or any(
                i["entity_count"]>MAX_ISSUE_ENTITIES for i in cissues) or len(context.get("rules",[]))>64
            context["rules"]=context.get("rules",[])[:64]
            context["issues"]=[{**i,"entity_ids":i["entity_ids"][:MAX_ISSUE_ENTITIES],
                "record_ids":i["record_ids"][:MAX_ISSUE_ENTITIES]} for i in cissues[:MAX_RESPONSE_ISSUES]]
            output["context_mapping"]=context
        return output

    def _load(self, principal, job):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(self._load_tx, principal, job)

    @staticmethod
    def _load_tx(tx, principal, job):
        row = tx.run("""MATCH (d:Document {tenant_id:$tenant,document_id:$document})
            -[:ACTIVE_VERSION]->(v:DocumentVersion {tenant_id:$tenant,version_id:$version})
            MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {snapshot_id:$snapshot,build_state:'PUBLISHED'})
            MATCH (:TBoxCatalog {tenant_id:$tenant})-[:ACTIVE_TBOX_VERSION]->(t:TBoxVersion {tbox_id:$tbox,status:'PUBLISHED'})
            WHERE any(g IN $groups WHERE g IN d.access_groups)
            RETURN v.normalized_text AS text,v.checksum AS checksum,t{.*} AS tbox,d.access_groups AS groups""",
            tenant=principal.tenant_id, document=job.document_id, version=job.version_id,
            snapshot=job.snapshot_id, tbox=job.tbox_id, groups=sorted(principal.groups)).single()
        if row is None:
            raise KnowledgeReviewUnavailable("auto-review source or ontology is no longer active")
        if not isinstance(row["text"],str) or content_checksum(row["text"]) != row["checksum"]:
            raise KnowledgeConflict("immutable source checksum differs")
        tbox = _decode_tbox(row["tbox"])
        current, comparison, original = [], [], []
        expected = {ReviewRecordKind.ENTITY_MENTION: set(x for c in job.chunks for x in c.mention_record_ids),
                    ReviewRecordKind.ASSERTION: set(x for c in job.chunks for x in c.assertion_record_ids)}
        from .context_projection import projection_record_ids_tx
        expected[ReviewRecordKind.ASSERTION].update(projection_record_ids_tx(tx,principal,job))
        if sum(map(len, expected.values())) > MAX_RECORDS:
            raise KnowledgeConflict("automatic review record limit exceeded")
        for kind, decoder in ((ReviewRecordKind.ENTITY_MENTION, _stored_mention), (ReviewRecordKind.ASSERTION, _stored_assertion)):
            params = dict(tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                statuses=["CANDIDATE", "QUARANTINED", "APPROVED", "PUBLISHED"], limit=MAX_RECORDS + 1,
                ids=sorted(expected[kind]), version_id=job.version_id)
            query = _active_revision_query(kind, one_record=False,
                extra_where="AND revision.record_id IN $ids AND revision.version_id = $version_id")
            rows = list(tx.run(query, **params))
            selected = [decoder(dict(value["revision"])) for value in rows]
            if {r.record_id for r in selected} != expected[kind]:
                # Explicitly rejected items are not review candidates. They must
                # still be authorized before excluding them from the automatic set.
                params["statuses"] += ["REJECTED", "SUPERSEDED"]
                all_rows = list(tx.run(query, **params))
                if {value["revision"]["record_id"] for value in all_rows} != expected[kind]:
                    raise KnowledgeReviewUnavailable("construction records are incomplete or unavailable")
            current.extend(selected)
            cq = _resolution_revision_query(kind, one_record=False,
                extra_where="AND revision.ontology_version_id=$tbox_id")
            comparisons = list(tx.run(cq, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                statuses=["CANDIDATE", "QUARANTINED", "APPROVED", "PUBLISHED"],
                tbox_id=job.tbox_id, limit=MAX_COMPARISON_RECORDS + 1))
            if len(comparisons) > MAX_COMPARISON_RECORDS:
                raise KnowledgeConflict("automatic review comparison limit exceeded")
            comparison.extend(decoder(dict(value["revision"])) for value in comparisons)
        # Original extraction identities are source-scoped proof, not merge
        # authority. Restrict history to the exact already-authorized job IDs.
        for value in tx.run("""MATCH (r:GovernedEntityMentionRevision {tenant_id:$tenant,revision:1})
            WHERE r.record_id IN $ids AND r.version_id=$version AND any(g IN $groups WHERE g IN r.access_groups)
            RETURN r{.*} AS revision""", tenant=principal.tenant_id, ids=sorted(expected[ReviewRecordKind.ENTITY_MENTION]),
            version=job.version_id, groups=sorted(principal.groups)):
            original.append(_stored_mention(dict(value["revision"])))
        return {"tbox": tbox, "text": row["text"], "groups": list(row["groups"]),
                "current": current, "comparison": comparison, "original": original}

    def _start(self, principal, job, state, retry):
        def work(tx):
            Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(tx, principal.tenant_id, _now())
            row = tx.run("""MATCH (r:KnowledgeAutoReviewRun {tenant_id:$tenant,job_id:$job,policy_version:$policy})
                RETURN r{.*} AS run ORDER BY r.updated_at DESC LIMIT 1""", tenant=principal.tenant_id,
                job=job.job_id, policy=POLICY_VERSION).single()
            if row:
                old = dict(row["run"])
                previous = json.loads(old["summary_json"])
                lease_until = old.get("lease_until", 0)
                if previous["status"] == "RUNNING" and lease_until > time.time():
                    return previous, False
                if previous["status"] != "RUNNING" and not retry:
                    return previous, False
                state["items"] = previous.get("items", [])
                state["run_id"] = old["run_id"]
                state["model_calls"] = previous.get("model_calls", 0)
            tx.run("""MERGE (r:KnowledgeAutoReviewRun {run_id:$run_id})
                ON CREATE SET r.tenant_id=$tenant,r.job_id=$job,r.policy_version=$policy,
                    r.created_at=$now,r.initiated_by=$initiated_by,r.access_groups=$groups
                SET r.status='RUNNING',r.updated_at=$now,r.summary_json=$summary,
                    r.lease_until=$lease,r.lease_token=$token""", run_id=state["run_id"], tenant=principal.tenant_id,
                job=job.job_id, policy=POLICY_VERSION, now=_now(), initiated_by=principal.principal_id,
                groups=state.pop("_access_groups"), summary=_json({k:v for k,v in state.items() if not k.startswith("_")}), lease=time.time()+LEASE_SECONDS,
                token=state["_lease_token"]).consume()
            return state, True
        with self.driver.session(database=self.database) as session:
            return session.execute_write(work)

    def _save(self, state):
        state["updated_at"] = _now().isoformat()
        with self.driver.session(database=self.database) as session:
            row = session.run("""MATCH (r:KnowledgeAutoReviewRun {run_id:$run,lease_token:$token})
                SET r.summary_json=$summary,r.status=$status,r.updated_at=$now,
                    r.lease_until=$lease RETURN r.run_id AS id""", run=state["run_id"], token=state["_lease_token"],
                summary=_json({k:v for k,v in state.items() if not k.startswith("_")}), status=state["status"], now=_now(),
                lease=time.time()+LEASE_SECONDS if state["status"] == "RUNNING" else 0).single()
            if row is None:
                raise KnowledgeConflict("automatic review lease changed")

    async def _model(self, state, kind, payload, deadline):
        if len(_json(payload)) > 80000 or len(payload.get("evidence", ())) > 240:
            records = payload["records"]
            if len(records) <= 1:
                return None
            from .auto_review_model import AutoReviewModelDecision, AutoReviewModelResult
            combined = []
            middle = len(records)//2
            for selected in (records[:middle],records[middle:]):
                evidence_ids = {eid for r in selected for eid in r["evidence_ids"]}
                target_ids = {eid for r in selected for eid in r.get("candidate_target_ids",())}
                targets = [t for t in payload.get("targets",()) if t["target_id"] in target_ids]
                evidence_ids.update(t["evidence"]["evidence_id"] for t in targets if t.get("evidence"))
                part = {**payload,"records":selected,"targets":targets,
                        "evidence":[e for e in payload["evidence"] if e["evidence_id"] in evidence_ids]}
                answer = await self._model(state,kind,part,deadline)
                combined.extend(answer.decisions if answer else tuple(AutoReviewModelDecision(
                    r["record_id"],"UNCERTAIN",reason="AUTO_REVIEW_MODEL_UNAVAILABLE: 审核未完成，请重试。") for r in selected))
            return AutoReviewModelResult(tuple(combined),{},"COMPLETE")
        if (self.reviewer is None or state["model_calls"] - state.get("_model_start_calls",0) + 2 > MAX_MODEL_CALLS
            or state["model_calls"] + 2 > MAX_TOTAL_MODEL_CALLS or time.monotonic() >= deadline):
            return None
        state["model_calls"] += 2  # Reserve the possible correction call.
        self._save(state)
        try:
            result = await asyncio.wait_for(self.reviewer.review(kind, payload), max(0.1, deadline-time.monotonic()))
        except Exception as exc:
            # Do not put provider exception text, source or credentials in public receipts.
            self._audit(state, "MODEL_FAILURE", {"kind": kind, "error_type": type(exc).__name__, "input_hash": _hash(payload)})
            return None
        state["model_calls"] -= max(0, 2-len(result.audit.get("attempts", ())))
        self._audit(state, "MODEL_REVIEW", {"kind": kind, "input_hash": _hash(payload), "audit": result.audit})
        return result if result.status == "COMPLETE" else None

    def _audit(self, state, kind, payload):
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._audit_tx, state, kind, payload)

    @staticmethod
    def _audit_tx(tx, state, kind, payload):
        encoded = _json(payload)
        identifier = _hash([state["run_id"], kind, encoded])
        row = tx.run("""MATCH (r:KnowledgeAutoReviewRun {run_id:$run,lease_token:$token})
            MERGE (a:KnowledgeAutoReviewDecision {decision_id:$id})
            ON CREATE SET a.run_id=$run,a.tenant_id=r.tenant_id,a.access_groups=r.access_groups,
                a.kind=$kind,a.payload_json=$payload,a.checksum=$checksum,a.created_at=$now,
                a.initiated_by=r.initiated_by,a.reviewed_by=$reviewed_by
            MERGE (r)-[:HAS_AUTO_REVIEW_DECISION]->(a)
            RETURN a.checksum AS checksum""", run=state["run_id"], token=state["_lease_token"], id=identifier,
            kind=kind, payload=encoded, checksum=_hash(payload), now=_now(), reviewed_by=state["reviewed_by"]).single()
        if row is None or row["checksum"] != _hash(payload):
            raise KnowledgeConflict("automatic review audit or lease changed")

    def _commit(self, service, state, records, target=None, selected=None, identity_groups=()):
        """Review revisions and decision audit share the same corpus-locked transaction."""
        now = _now()
        notes = _json({"review_method": "MODEL_ASSISTED", "policy_version": POLICY_VERSION,
            "run_id": state["run_id"], "initiated_by": state["initiated_by"],
            "reason": "Source identity/mapping, original evidence and comparison checks passed."})
        def work(tx):
            Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(tx, service.tenant_id, now)
            outcomes = []
            if identity_groups:
                self._check_identity_comparison_tx(tx, service, identity_groups)
            if records and isinstance(records[0], AssertionRecord):
                self._check_fact_comparison_tx(tx, service, records)
            if target is not None and any(r.entity.entity_id != target.entity_id for r in records):
                for record in records:
                    outcomes.extend(Neo4jKnowledgeReviewService._apply_entity_resolution_tx(tx, service,
                        record.record_id, record.revision.revision, target, now, notes,
                        selected.record_id if selected else None, selected.revision.revision if selected else None))
            else:
                requests = tuple(ReviewRequest(ReviewRecordKind.ENTITY_MENTION if isinstance(r, EntityMentionRecord)
                    else ReviewRecordKind.ASSERTION, r.record_id, r.revision.revision,
                    GovernanceStatus.APPROVED, now, notes) for r in records)
                outcomes.extend(Neo4jKnowledgeReviewService._review_batch_tx(tx, service, requests))
            self._audit_tx(tx, state, "APPLIED", {"records": [{"record_id": r.record_id,
                "input_revision": r.revision_id, "evidence_id": r.evidence.chunk_id} for r in records],
                "target_entity_id": target.entity_id if target else None,
                "outcomes": [asdict(o) for o in outcomes]})
            return outcomes
        with self.driver.session(database=self.database) as session:
            return session.execute_write(work)

    @staticmethod
    def _check_identity_comparison_tx(tx, principal, groups):
        first = groups[0]["members"][0]
        row = tx.run("MATCH (:TBoxCatalog {tenant_id:$tenant})-[:ACTIVE_TBOX_VERSION]->(t:TBoxVersion {tbox_id:$tbox,status:'PUBLISHED'}) RETURN t{.*} AS tbox",
            tenant=principal.tenant_id,tbox=first.trust.ontology_version_id).single()
        if row is None:
            raise KnowledgeReviewUnavailable("automatic review ontology changed")
        tbox = _decode_tbox(row["tbox"])
        types = sorted({g["members"][0].entity.entity_type for g in groups})
        predicates = sorted({p for e in tbox.entity_types if e.name in types for p in e.identity_properties})
        query = _resolution_revision_query(ReviewRecordKind.ENTITY_MENTION,one_record=False,
            extra_where="AND revision.entity_type IN $types AND revision.ontology_version_id=$tbox")
        params = dict(tenant_id=principal.tenant_id,groups=sorted(principal.groups),types=types,
                      tbox=tbox.tbox_id,statuses=["CANDIDATE","QUARANTINED","APPROVED","PUBLISHED"],limit=MAX_COMPARISON_RECORDS+1)
        rows = list(tx.run(query,**params))
        if len(rows)>MAX_COMPARISON_RECORDS:
            raise KnowledgeConflict("identity comparison limit exceeded at commit")
        mentions = [_stored_mention(dict(r["revision"])) for r in rows]
        pinned = {key:value for group in groups for key,value in group["identity_fact_revisions"].items()}
        fq = _resolution_revision_query(ReviewRecordKind.ASSERTION,one_record=False,
            extra_where="AND revision.subject_entity_type IN $types AND revision.predicate IN $predicates AND revision.ontology_version_id=$tbox")
        frows = list(tx.run(fq,**params,predicates=predicates)) if predicates else []
        if len(frows)>MAX_COMPARISON_RECORDS:
            raise KnowledgeConflict("identity evidence comparison limit exceeded at commit")
        facts = [_stored_assertion(dict(r["revision"])) for r in frows]
        if any(not any(f.record_id==rid and f.revision_id==rev for f in facts) for rid,rev in pinned.items()):
            raise KnowledgeConflict("identity evidence changed before approval")
        eligible = [f for f in facts if f.trust.status in {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED}
                    or pinned.get(f.record_id)==f.revision_id]
        signatures = _identity_values(mentions,eligible,tbox)
        for group in groups:
            members, target, signature = group["members"],group["target"],group["signature"]
            selected = group.get("selected")
            if selected is not None and not any(m.record_id==selected.record_id
                and m.revision_id==selected.revision_id and m.entity==target
                and m.trust.status in {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED} for m in mentions):
                raise KnowledgeConflict("selected confirmed identity changed before approval")
            own_ids = {m.entity.entity_id for m in members}
            for other in mentions:
                if other.entity.entity_id in own_ids or other.entity.entity_id==target.entity_id:
                    continue
                other_signature = signatures.get(other.entity.entity_id)
                if (signature and other_signature==signature and other.trust.status in
                    {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED}):
                    raise KnowledgeConflict("another confirmed identity appeared before approval")
                if (other.entity.entity_type==target.entity_type
                    and other.entity.canonical_name.casefold()==members[0].entity.canonical_name.casefold()
                    and (signature is None or other_signature is None)):
                    raise KnowledgeConflict("identity ambiguity appeared before approval")

    @staticmethod
    def _check_fact_comparison_tx(tx, principal, facts):
        row = tx.run("MATCH (:TBoxCatalog {tenant_id:$tenant})-[:ACTIVE_TBOX_VERSION]->(t:TBoxVersion {tbox_id:$tbox,status:'PUBLISHED'}) RETURN t{.*} AS tbox",
            tenant=principal.tenant_id,tbox=facts[0].trust.ontology_version_id).single()
        if row is None:
            raise KnowledgeReviewUnavailable("automatic fact review ontology changed")
        tbox = _decode_tbox(row["tbox"])
        hierarchy_predicates = [h.relationship_type for h in tbox.hierarchies]
        query = _resolution_revision_query(ReviewRecordKind.ASSERTION, one_record=False,
            extra_where="AND (revision.subject_entity_id IN $subjects OR revision.object_entity_id IN $objects OR revision.predicate IN $hierarchies) AND revision.predicate IN $predicates AND revision.ontology_version_id=$tbox")
        rows = list(tx.run(query, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
            subjects=sorted({f.subject.entity_id for f in facts}), objects=sorted({f.object_entity.entity_id for f in facts if f.object_entity}),
            hierarchies=hierarchy_predicates, predicates=sorted({f.predicate for f in facts}),
            tbox=facts[0].trust.ontology_version_id, statuses=["CANDIDATE","QUARANTINED","APPROVED","PUBLISHED"],
            limit=MAX_COMPARISON_RECORDS+1))
        if len(rows)>MAX_COMPARISON_RECORDS:
            raise KnowledgeConflict("fact comparison limit exceeded at commit")
        conflicts = _fact_conflicts([_stored_assertion(dict(row["revision"])) for row in rows],tbox)
        if any((f.subject.entity_id,f.predicate,f.object_entity.entity_id if f.object_entity else None) in conflicts for f in facts):
            raise KnowledgeConflict("fact comparison changed before automatic approval")

    async def run(self, principal, *, job_id, retry=False, deadline_seconds=1200,
                  resume_identity_record_ids=()):
        job = self._job(principal, job_id)
        service = replace(principal, principal_id="service:auto-review:"+_hash(principal.principal_id)[:16])
        state = {"job_id": job_id, "run_id": _hash([POLICY_VERSION, principal.tenant_id, job_id]),
            "status": "RUNNING", "stage": "IDENTITY", "policy_version": POLICY_VERSION,
            "initiated_by": principal.principal_id, "reviewed_by": service.principal_id,
            "counts": {k:0 for k in ("entity_groups", "approved_groups", "approved_mentions", "approved_assertions",
                        "manual_groups", "manual_assertions", "blocked_assertions", "incomplete")},
            "items": [], "issues": [], "truncated": False, "model_calls": 0, "updated_at": _now().isoformat(),
            "_lease_token": str(uuid4())}
        if KNOWLEDGE_REVIEW_CAPABILITY not in principal.capabilities or job.status != "COMPLETED":
            state.update(status="SKIPPED", stage="DONE")
            return self._public({k:v for k,v in state.items() if not k.startswith("_")})
        data = self._load(principal, job)
        resume_ids = set(resume_identity_record_ids)
        resume_entities = set()
        if resume_ids:
            confirmed = {r.record_id: r.entity.entity_id for r in data["current"]
                if isinstance(r, EntityMentionRecord) and r.trust.status in
                {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}}
            if not retry or len(resume_ids) > MAX_GROUP_MEMBERS or not resume_ids.issubset(confirmed):
                raise KnowledgeReviewUnavailable("resume identities must be confirmed records of this authorized job")
            resume_entities = {confirmed[rid] for rid in resume_ids}
        state["_access_groups"] = data["groups"]
        state, claimed = self._start(principal, job, state, retry)
        if not claimed:
            state["issues"] = _combined_issues(_readiness_from_data(data),state.get("context_mapping",{}))
            return self._public({k:v for k,v in state.items() if not k.startswith("_")})
        state["_resume_entities"] = resume_entities
        current_ids={r.record_id for r in data["current"]}
        items = {i["record_id"]:i for i in state["items"] if i["record_id"] in current_ids}
        state["_model_start_calls"] = state["model_calls"]
        deadline = time.monotonic()+min(max(float(deadline_seconds), 1), 1200)
        state["_deadline"] = deadline
        if resume_ids:
            self._audit(state, "RESUME_AFTER_IDENTITY", {"record_ids": sorted(resume_ids),
                "entity_ids": sorted(resume_entities)})
        try:
            summary = next((c.mapping_summary for c in job.chunks if c.mapping_summary), None)
            if self.context_projection is not None and not resume_entities:
                state["stage"] = "CONTEXT"
                state["context_mapping"] = {"status":"RUNNING","added_assertions":0,"applied":0,
                    "uncertain":0,"overridden":0,"rules":[],"issues":[]}
                self._save(state)
                state["context_mapping"] = await self.context_projection.run(principal,job,data,deadline=deadline,retry=retry)
                self._audit(state,"CONTEXT_MAPPING",state["context_mapping"])
                data = self._load(principal,job)
                state["stage"] = "IDENTITY"
                self._save(state)
            mentions = [r for r in data["current"] if isinstance(r, EntityMentionRecord)]
            proof = StructuredEvidence(data["text"], job.document_id, job.tbox_id, summary, mentions)
            from .document_review_evidence import install_document_candidates
            install_document_candidates(proof, mentions, data["current"], SourceIdentity)
            proof.restore(data["original"])
            await self._identities(principal, service, state, data, proof, items, deadline)
            state["stage"] = "FACTS"
            state["items"] = list(items.values())
            self._save(state)
            # Identity links advance dependent fact revisions; never approve
            # using pre-link endpoints or stale candidate versions.
            data = self._load(principal, job)
            await self._facts(principal, service, state, data, proof, items, deadline)
            context_incomplete = state.get("context_mapping",{}).get("status") in {"UNAVAILABLE","PARTIAL","RUNNING"}
            state["status"] = "PARTIAL" if context_incomplete or any(i["decision"] == "INCOMPLETE" for i in items.values()) else "COMPLETED"
        except Exception as exc:
            if state.get("context_mapping",{}).get("status")=="RUNNING":
                state["context_mapping"]["status"]="PARTIAL"
            self._audit(state, "RUN_FAILURE", {"error_type":type(exc).__name__,"detail":str(exc)[:2000]})
            for record in data["current"]:
                if record.trust.status == GovernanceStatus.CANDIDATE and items.get(record.record_id, {}).get("decision") != "AUTO_APPROVED":
                    items[record.record_id] = _item(record, "INCOMPLETE", "REVIEW_INTERRUPTED", "自动审核未完成，请重试或人工处理。")
            state["status"] = "PARTIAL"
        state["items"] = list(items.values())
        state["issues"] = _combined_issues(_readiness_from_data(data),state.get("context_mapping",{}))
        self._audit(state, "FINAL_DECISIONS", {"status":state["status"],"items":state["items"],"issues":state["issues"]})
        state["stage"] = "DONE"
        self._counts(state, data)
        self._save(state)
        return self._public({k:v for k,v in state.items() if not k.startswith("_")})

    @staticmethod
    def _counts(state, data):
        items = state["items"]
        mentions = [i for i in items if i["record_kind"] == "ENTITY_MENTION"]
        facts = [i for i in items if i["record_kind"] == "ASSERTION"]
        originals = {r.record_id:r.entity.entity_id for r in data["original"]}
        group = lambda i: originals.get(i["record_id"],i["record_id"])
        state["counts"] = {
            "entity_groups":len({group(i) for i in mentions}),
            "approved_groups":len({group(i) for i in mentions if i["decision"] == "AUTO_APPROVED"}),
            "approved_mentions":sum(i["decision"] == "AUTO_APPROVED" for i in mentions),
            "approved_assertions":sum(i["decision"] == "AUTO_APPROVED" for i in facts),
            "manual_groups":len({group(i) for i in mentions if i["decision"] == "NEEDS_HUMAN"}),
            "manual_assertions":sum(i["decision"] == "NEEDS_HUMAN" for i in facts),
            "blocked_assertions":sum(i["decision"] == "BLOCKED" for i in facts),
            "incomplete":sum(i["decision"] == "INCOMPLETE" for i in items)}

    async def _identities(self, principal, service, state, data, proof, items, deadline):
        definitions = {e.name:e for e in data["tbox"].entity_types}
        all_mentions = [r for r in data["comparison"] if isinstance(r, EntityMentionRecord)]
        all_facts = [r for r in data["comparison"] if isinstance(r, AssertionRecord)]
        revisions = {r.revision_id:r for r in [*all_mentions,*data["original"]]}
        # Only approved target identity facts, or the current document's exact
        # source-mapped identity facts, can establish a strong identity key.
        eligible = [f for f in all_facts if f.trust.status in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}
                    or (not getattr(proof, "document_candidates", False)
                        and f.evidence.document_id == proof.document_id and proof.fact_mapping(f, revisions))]
        strong = _identity_values(all_mentions, eligible, data["tbox"])
        by_strong, by_name = defaultdict(list), defaultdict(list)
        for mention in all_mentions:
            if mention.entity.entity_id in strong:
                by_strong[strong[mention.entity.entity_id]].append(mention)
            for name in (mention.entity.canonical_name, *mention.entity.aliases):
                by_name[mention.entity.entity_type, name.casefold()].append(mention)
        groups = defaultdict(list)
        for record in data["current"]:
            if not isinstance(record, EntityMentionRecord):
                continue
            if record.trust.status in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}:
                items.pop(record.record_id, None)
                # A human decision remains human, even on automatic retry.
                if (record.trust.reviewed_by or "").startswith("service:auto-review:"):
                    items[record.record_id] = _item(record, "AUTO_APPROVED", "PREVIOUS_AUTO_APPROVAL",
                        "先前自动审核已通过，保留原决定。", record.entity.entity_id)
                continue
            if state.get("_resume_entities"):
                items.setdefault(record.record_id, _item(record, "INCOMPLETE", "OUTSIDE_RESUME_SCOPE",
                    "本条身份不在此次续审范围，保留待办。"))
                continue
            items.pop(record.record_id, None)
            source = proof.identity(record)
            if record.trust.status != GovernanceStatus.CANDIDATE or source is None:
                items[record.record_id] = _item(record, "NEEDS_HUMAN", "SOURCE_IDENTITY_UNPROVEN",
                    "缺少经全量校验的结构化来源身份，或记录已被人工暂缓。")
                continue
            key = strong.get(record.entity.entity_id) or (record.entity.entity_type,
                    (record.evidence.document_id, source.collection, source.source_id))
            groups[_hash(key)].append(record)
        prepared = []
        for group_id, members in groups.items():
            first = members[0]
            source = proof.identity(first)
            signature = strong.get(first.entity.entity_id)
            possible = by_strong.get(signature, []) if signature else []
            confirmed = {m.entity.entity_id:m for m in possible if m.trust.status in
                         {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}}
            # An original deterministic source ID can safely recover a prior
            # approval of the same document's object, without global name matching.
            for candidate in all_mentions:
                if (candidate.entity.entity_id == first.entity.entity_id
                    and candidate.trust.status in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}):
                    confirmed[candidate.entity.entity_id] = candidate
            similar = [m for m in by_name[first.entity.entity_type,first.entity.canonical_name.casefold()]
                       if m.entity.entity_id != first.entity.entity_id and m.record_id not in {r.record_id for r in members}]
            unsafe = [m for m in similar if not signature or strong.get(m.entity.entity_id) is None]
            if len(members) > MAX_GROUP_MEMBERS or len(confirmed) > 1 or unsafe:
                code = "GROUP_LIMIT" if len(members) > MAX_GROUP_MEMBERS else "AMBIGUOUS_IDENTITY"
                for m in members:
                    items[m.record_id] = _item(m, "NEEDS_HUMAN", code,
                        "候选身份不唯一、跨来源身份依据不足，或对象组超过自动审核上限。")
                continue
            selected = next(iter(confirmed.values()), None)
            target = selected.entity if selected else first.entity
            allowed = definitions[first.entity.entity_type].canonical_key_namespaces
            if selected is None and (signature or target.canonical_key.partition(":")[0] not in allowed):
                namespace = next((n for n in allowed if n != "llm-candidate"), None)
                if namespace is None:
                    for m in members:
                        items[m.record_id] = _item(m, "NEEDS_HUMAN", "IDENTITY_NAMESPACE_UNAVAILABLE", "本体没有可用的正式身份命名空间。")
                    continue
                key = namespace+":auto-"+_hash([IDENTITY_KEY_VERSION, principal.tenant_id, data["tbox"].tbox_id,
                    signature if signature else [first.evidence.document_id, source.collection, source.source_id]])
                target = replace(target, canonical_key=key, entity_id=entity_id(principal.tenant_id, target.entity_type, key))
            prepared.append({"group_id":group_id, "members":members, "target":target,
                             "selected":selected, "signature":signature, "source":source,
                "identity_fact_revisions":{f.record_id:f.revision_id for f in eligible
                    if f.subject.entity_id in {m.entity.entity_id for m in members}
                    and f.predicate in definitions[first.entity.entity_type].identity_properties}})
        prepared_batches = []
        for offset in range(0, len(prepared), MODEL_BATCH_SIZE):
            batch = prepared[offset:offset+MODEL_BATCH_SIZE]
            records, evidence, targets = [], {}, {}
            for group in batch:
                leader, source = group["members"][0], group["source"]
                eid = leader.record_id
                # Full defining source record provides the type, owner and
                # identifier context even when a mention is only a reference.
                evidence[eid] = {"evidence_id":eid, "text":proof.text[source.node.start:source.node.end],
                    "document_version_id":leader.evidence.version_id,
                    "char_start":source.node.start, "char_end":source.node.end}
                contextual=[]
                for fact in eligible:
                    context=fact.context_property_evidence
                    if (context is not None and fact.record_id in group["identity_fact_revisions"]):
                        value=context.value_evidence
                        contextual.append({"predicate":fact.predicate,"source_path":context.value_pointer,
                            "scope_path":context.scope_pointer,"text":value.quoted_text,
                            "chunk_id":value.chunk_id,"document_version_id":value.version_id,
                            "char_start":value.char_start,"char_end":value.char_end,
                            "binding":json.loads(context.binding_json)})
                if contextual:
                    evidence[eid]["context_properties"]=contextual
                selected = group["selected"]
                target_ids = [selected.entity.entity_id] if selected else []
                if selected:
                    evidence[selected.record_id] = _evidence(selected)
                    targets[selected.entity.entity_id] = {"target_id":selected.entity.entity_id,
                        "entity_type":selected.entity.entity_type,"name":selected.entity.canonical_name,
                        "identity_signature":group["signature"],"evidence":_evidence(selected)}
                definition = definitions[leader.entity.entity_type]
                records.append({"record_id":eid,"group_id":group["group_id"],
                    "mention_count":len(group["members"]),"evidence_ids":[eid],
                    "proposed_name":leader.entity.canonical_name,
                    "proposed_mentions":[{"text":m.evidence.quoted_text,
                        "char_start":m.evidence.char_start,"char_end":m.evidence.char_end,
                        "document_version_id":m.evidence.version_id,"evidence_id":eid}
                        for m in group["members"]
                        if m.evidence.version_id == leader.evidence.version_id
                        and source.node.start <= m.evidence.char_start < m.evidence.char_end <= source.node.end],
                    "entity_type":leader.entity.entity_type,"entity_type_description":definition.description,
                    "source_identity":{"document_id":leader.evidence.document_id,"collection":source.collection,
                                       "field":source.rule["id_field"],"id":source.source_id},
                    "identity_signature":group["signature"],"candidate_target_ids":target_ids,
                    "rule_checks":{"source_record_unique":not getattr(proof,"document_candidates",False),
                        "candidate_identity_only":getattr(proof,"document_candidates",False),
                        "comparison_complete":True,"identity_conflict":False}})
            payload = {"records":records,"evidence":list(evidence.values()),"targets":list(targets.values()),
                "ontology":{"version_id":data["tbox"].tbox_id},
                "rules":{"source_identity_scope":"document and collection; not a global identifier",
                         "same_source_record_references_are_one_group":not getattr(proof,"document_candidates",False),
                         "candidate_group_requires_original_identity_evidence":True,
                         "new_group_id":"use the supplied group_id if the source proves this object",
                         "absence_of_target_alone_is_insufficient":True}}
            prepared_batches.append((batch,payload))
        for offset in range(0,len(prepared_batches),3):
            window = prepared_batches[offset:offset+3]
            results = await asyncio.gather(*(self._model(state,"identity",p,deadline) for _,p in window))
            for (batch,_),result in zip(window,results):
                decisions = {d.record_id:d for d in result.decisions} if result else {}
                pending_direct = []
                for group in batch:
                    leader = group["members"][0]
                    decision = decisions.get(leader.record_id)
                    accepted = decision and ((group["selected"] and decision.action == "MATCH"
                        and decision.target_id == group["target"].entity_id) or
                        (group["selected"] is None and decision.action == "NEW" and decision.new_group_id == group["group_id"]))
                    if not accepted:
                        for member in group["members"]:
                            items[member.record_id] = _item(member, "NEEDS_HUMAN" if result and not (decision and decision.reason.startswith("AUTO_REVIEW_MODEL_UNAVAILABLE")) else "INCOMPLETE",
                                "MODEL_UNCERTAIN" if result and not (decision and decision.reason.startswith("AUTO_REVIEW_MODEL_UNAVAILABLE")) else "MODEL_UNAVAILABLE",
                                decision.reason if decision else "审核模型未完成判断，请重试或人工处理。")
                        continue
                    if all(m.entity.entity_id == group["target"].entity_id for m in group["members"]):
                        pending_direct.append(group)
                    else:
                        self._apply_group(service, state, group, items)
                # Several unchanged identities can share a bounded transaction;
                # keep every object group intact instead of per-mention commits.
                packet, size = [], 0
                for group in pending_direct:
                    if packet and size+len(group["members"]) > 60:
                        self._apply_direct(service, state, packet, items)
                        packet, size = [], 0
                    packet.append(group)
                    size += len(group["members"])
                if packet:
                    self._apply_direct(service, state, packet, items)
                state["items"] = list(items.values())
                self._counts(state, data)
                self._save(state)

    def _apply_direct(self, service, state, groups, items):
        members = [m for g in groups for m in g["members"]]
        try:
            self._commit(service, state, members, identity_groups=groups)
            for member in members:
                items[member.record_id] = _item(member, "AUTO_APPROVED", "SOURCE_IDENTITY_VERIFIED",
                    "模型核对原文，程序校验来源身份及匹配范围后通过。", member.entity.entity_id)
        except (KnowledgeConflict, KnowledgeReviewUnavailable, ValueError) as exc:
            for member in members:
                items[member.record_id] = _item(member, "NEEDS_HUMAN", "COMMIT_VALIDATION_FAILED",
                    "提交时身份、证据或版本检查未通过，请复核。")
            self._audit(state, "COMMIT_REJECTED", {"records":[m.record_id for m in members],"error_type":type(exc).__name__,"detail":str(exc)[:2000]})

    def _apply_group(self, service, state, group, items):
        try:
            self._commit(service, state, group["members"], group["target"], group["selected"], (group,))
            for member in group["members"]:
                items[member.record_id] = _item(member, "AUTO_APPROVED", "IDENTITY_MATCH_VERIFIED",
                    "模型核对原文，程序校验身份键后统一绑定。", group["target"].entity_id)
        except (KnowledgeConflict, KnowledgeReviewUnavailable, ValueError) as exc:
            for member in group["members"]:
                items[member.record_id] = _item(member, "NEEDS_HUMAN", "COMMIT_VALIDATION_FAILED", "提交时身份、证据或版本检查未通过，请复核。")
            self._audit(state, "COMMIT_REJECTED", {"records":[m.record_id for m in group["members"]],"error_type":type(exc).__name__,"detail":str(exc)[:2000]})

    async def _facts(self, principal, service, state, data, proof, items, deadline):
        current_mentions = [r for r in data["comparison"] if isinstance(r, EntityMentionRecord)]
        all_facts = [r for r in data["comparison"] if isinstance(r, AssertionRecord)]
        mention_by_id = {r.record_id:r for r in current_mentions}
        revisions = {r.revision_id:r for r in [*current_mentions,*data["original"]]}
        # Links create multiple intermediate mention revisions. All revision IDs
        # referenced by the job's now-current assertions must resolve to their
        # same authorized logical mention, never an arbitrary node.
        current_facts = [r for r in data["current"] if isinstance(r, AssertionRecord)]
        required = {r.subject_mention_revision_id for r in current_facts}
        required.update(r.object_mention_revision_id for r in current_facts if r.object_mention_revision_id)
        missing = required-set(revisions)
        if missing:
            with self.driver.session(database=self.database) as session:
                for row in session.run("""MATCH (m:GovernedEntityMentionRevision {tenant_id:$tenant})
                    WHERE m.revision_id IN $ids AND m.record_id IN $records
                      AND any(g IN $groups WHERE g IN m.access_groups)
                    RETURN m{.*} AS revision""",tenant=principal.tenant_id,ids=sorted(missing),
                    records=sorted(mention_by_id),groups=sorted(principal.groups)):
                    record = _stored_mention(dict(row["revision"]))
                    revisions[record.revision_id] = record
        conflicts = _fact_conflicts(all_facts, data["tbox"])
        groups = defaultdict(list)
        mapping_by_key = {}
        individual = []
        for fact in current_facts:
            scope = state.get("_resume_entities")
            if (scope and fact.subject.entity_id not in scope
                    and (fact.object_entity is None or fact.object_entity.entity_id not in scope)
                    and fact.trust.status not in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}):
                items.setdefault(fact.record_id, _item(fact, "INCOMPLETE", "OUTSIDE_RESUME_SCOPE",
                    "本条事实不依赖本次确认的实体，保留待办。"))
                continue
            items.pop(fact.record_id, None)
            if fact.trust.status in {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED}:
                if (fact.trust.reviewed_by or "").startswith("service:auto-review:"):
                    items[fact.record_id] = _item(fact,"AUTO_APPROVED","PREVIOUS_AUTO_APPROVAL","先前自动审核已通过，保留原决定。")
                continue
            endpoints = [revisions.get(fact.subject_mention_revision_id)]
            if fact.object_entity:
                endpoints.append(revisions.get(fact.object_mention_revision_id))
            ready = all(m and mention_by_id.get(m.record_id) and mention_by_id[m.record_id].trust.status in
                        {GovernanceStatus.APPROVED,GovernanceStatus.PUBLISHED} for m in endpoints)
            if not ready:
                items[fact.record_id] = _item(fact,"BLOCKED","IDENTITY_REVIEW_REQUIRED","关联实体身份未确定，等待身份处理后再审核。")
                continue
            mapping = proof.fact_mapping(fact,revisions)
            key = (fact.subject.entity_id,fact.predicate,fact.object_entity.entity_id if fact.object_entity else None)
            if fact.trust.status != GovernanceStatus.CANDIDATE or not mapping or key in conflicts:
                reason = "FACT_CONFLICT" if key in conflicts else "SOURCE_MAPPING_UNPROVEN"
                items[fact.record_id] = _item(fact,"NEEDS_HUMAN",reason,
                    "存在不同事实值，需要核对适用时间和来源。" if key in conflicts else "原文与映射尚不能直接证明该事实，或记录已被人工暂缓。")
                continue
            if getattr(proof, "document_candidates", False):
                individual.append(fact)
                continue
            mkey = _hash([data["tbox"].checksum,proof.summary["mapping_checksum"],mapping])
            groups[mkey].append(fact)
            mapping_by_key[mkey] = mapping
        work = list(groups.items())
        prepared_batches = []
        for offset in range(0, len(work), MODEL_BATCH_SIZE):
            batch = work[offset:offset + MODEL_BATCH_SIZE]
            records, evidence = [], []
            for group_id, facts in batch:
                samples = [facts[i] for i in sorted({0, len(facts)//2, len(facts)-1})]
                sample_records = []
                for fact in samples:
                    record, ev = _fact_review_input(fact, proof, revisions)
                    record["sample_record_id"] = record.pop("record_id")
                    sample_records.append(record)
                    evidence.append(ev)
                records.append({"record_id": "mapping:" + group_id,
                    "mapping": mapping_by_key[group_id], "record_count": len(facts),
                    "evidence_ids": [f.record_id for f in samples], "samples": sample_records,
                    "all_records_checked": {"exact_source_tokens": True, "same_mapping": True,
                        "identity_endpoints_approved": True, "no_comparison_conflicts": True}})
            payload = self._fact_payload(data, records, evidence, mapping=True)
            prepared_batches.append((batch, payload))
        # A mapping proposal never becomes the reason on an individual candidate.
        # Ambiguous mappings fall back to explicit, per-assertion model review.
        pending = list(individual)
        for offset in range(0, len(prepared_batches), 3):
            window = prepared_batches[offset:offset + 3]
            results = await asyncio.gather(*(self._model(state, "mapping", p, deadline) for _, p in window))
            for (batch, _), result in zip(window, results):
                decisions = {d.record_id: d for d in result.decisions} if result else {}
                approved = []
                for group_id, facts in batch:
                    decision = decisions.get("mapping:" + group_id)
                    if self._model_unavailable(decision):
                        for fact in facts:
                            items[fact.record_id] = _item(fact, "INCOMPLETE", "MODEL_UNAVAILABLE",
                                "映射审核未完成，尚未判断本条事实；可重试。")
                    elif decision.action == "VALID":
                        approved.extend(facts)
                    else:
                        pending.extend(facts)
                self._apply_facts(service, state, approved, items,
                    "MAPPING_AND_SOURCE_VERIFIED", "映射含义已复核；本条原值、归属、证据及约束经程序校验通过。")
                state["items"] = list(items.values())
                self._counts(state, data)
                self._save(state)
        for offset in range(0, len(pending), MODEL_BATCH_SIZE):
            batch = pending[offset:offset + MODEL_BATCH_SIZE]
            inputs = [_fact_review_input(fact, proof, revisions) for fact in batch]
            payload = self._fact_payload(data, [r for r, _ in inputs], [e for _, e in inputs])
            result = await self._model(state, "facts", payload, deadline)
            decisions = {d.record_id: d for d in result.decisions} if result else {}
            approved = []
            for fact in batch:
                decision = decisions.get(fact.record_id)
                if self._model_unavailable(decision):
                    items[fact.record_id] = _item(fact, "INCOMPLETE", "MODEL_UNAVAILABLE",
                        "本条事实审核未完成，尚无有效判断；可重试。")
                elif decision.action == "APPROVE":
                    approved.append(fact)
                else:
                    items[fact.record_id] = _item(fact, "NEEDS_HUMAN", "MODEL_UNCERTAIN", decision.reason)
            self._apply_facts(service, state, approved, items,
                "SOURCE_FACT_VERIFIED", "模型逐条核对本条原文，程序校验归属、证据及约束后通过。")
            state["items"] = list(items.values())
            self._counts(state, data)
            self._save(state)

    @staticmethod
    def _model_unavailable(decision):
        return decision is None or decision.reason.startswith("AUTO_REVIEW_MODEL_UNAVAILABLE")

    @staticmethod
    def _fact_payload(data, records, evidence, *, mapping=False):
        types = {r["mapping"]["subject_type"] for r in records}
        types.update(r["mapping"].get("object_type") for r in records)
        predicates = {r["mapping"]["predicate"] for r in records}
        return {"records": records, "evidence": evidence,
            "ontology": {"version_id": data["tbox"].tbox_id,
                "entity_types": [e.to_mapping() for e in data["tbox"].entity_types if e.name in types],
                "relationship_types": [r.to_mapping() for r in data["tbox"].relationship_types if r.name in predicates]},
            "rules": {"review_unit": "MAPPING_RULE" if mapping else "INDIVIDUAL_ASSERTION",
                "program_checks_every_record": True, "different_subjects_may_have_different_values": True,
                "mapping_validity_is_not_fact_approval": mapping,
                "identity_endpoints_approved": True, "no_comparison_conflicts": True}}

    def _apply_facts(self, service, state, facts, items, code, reason):
        """Isolate stale commits without letting one unrelated candidate block a packet."""
        for start in range(0, len(facts), 60):
            packet = facts[start:start + 60]
            if time.monotonic() >= state.get("_deadline", float("inf")):
                for fact in packet:
                    items[fact.record_id] = _item(fact, "INCOMPLETE", "REVIEW_BUDGET_EXHAUSTED",
                        "本次审核时间预算已用尽，本条尚未提交；可续审。")
                continue
            try:
                self._commit(service, state, packet)
                for fact in packet:
                    items[fact.record_id] = _item(fact, "AUTO_APPROVED", code, reason)
            except (KnowledgeConflict, ValueError) as exc:
                self._audit(state, "COMMIT_REJECTED", {
                    "records": [f.record_id for f in packet], "error_type": type(exc).__name__})
                if len(packet) > 1:
                    middle = len(packet)//2
                    self._apply_facts(service, state, packet[:middle], items, code, reason)
                    self._apply_facts(service, state, packet[middle:], items, code, reason)
                else:
                    items[packet[0].record_id] = _item(packet[0], "INCOMPLETE", "COMMIT_REVALIDATION_REQUIRED",
                        "提交时记录或约束发生变化，未采用旧决定；请刷新后重试。")
            except KnowledgeReviewUnavailable:
                for fact in packet:
                    items[fact.record_id] = _item(fact, "INCOMPLETE", "COMMIT_UNAVAILABLE",
                        "提交依赖暂不可用，审核结果尚未写入；可重试。")
