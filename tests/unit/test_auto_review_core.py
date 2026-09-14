"""Independent source-proof and identity boundaries for automatic pre-review."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest

from graphrag_prod.construction.structured import StructuredMappingExtractor
from graphrag_prod.construction.workflow import _to_abox_batch
from graphrag_prod.domain.ids import relationship_property_value_id
from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.models import RecordRevision
from graphrag_prod.knowledge.store import KnowledgeConflict
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.knowledge.auto_review import (
    Neo4jAutoReviewService, StructuredEvidence, _fact_conflicts, _identity_values,
)
from graphrag_prod.ontology.models import Cardinality
from tests.unit.test_construction_extraction import _profile, _tbox
from tests.unit.test_structured_mapping import base, chunk_for, fixture, parsed
from tests.unit.test_knowledge_store import _assertion_properties, _mention_properties


def source_records(*, renamed=False, identity=False):
    data, plan = fixture(3)
    for row in data["machines"]:
        row["ownership_basis"] = "来源登记"
    plan["collections"][1]["relations"][0]["properties"] = [
        {"field": "/ownership_basis", "property": "basis"}]
    if renamed:
        data = {"payload": {"arbitrary_owners": data["organizations"], "arbitrary_objects": data["machines"]}}
        plan["collections"][0]["path"] = "/payload/arbitrary_owners"
        plan["collections"][1]["path"] = "/payload/arbitrary_objects"
        plan["collections"][1]["relations"][0]["target_collection"] = "/payload/arbitrary_owners"
    tbox = _tbox()
    if identity:
        asset = next(item for item in tbox.entity_types if item.name == "Asset")
        props = tuple(replace(p, required=True, cardinality=Cardinality.ONE)
                      if p.name == "serialNumber" else p for p in asset.properties)
        asset = replace(asset, properties=props, identity_properties=("serialNumber",))
        tbox = replace(tbox, entity_types=tuple(asset if e.name == "Asset" else e for e in tbox.entity_types))
    document = parsed(data)
    model, _ = base(plan, tbox)
    extractor = StructuredMappingExtractor(model, document)
    extractor.prepare_document(read=lambda _: None, persist=lambda *_: None, before_model_call=lambda: None)
    mentions, facts = [], []
    for seed in document.chunks:
        chunk = chunk_for(seed)
        result = extractor.extract_audited(artifact_id="audit", input_hash="input", chunk=chunk, profile=_profile())
        batch = _to_abox_batch(result, chunk=chunk, extracted_at=datetime(2026, 9, 13, tzinfo=timezone.utc))
        if batch:
            mentions.extend(batch.mentions)
            facts.extend(batch.assertions)
    proof = StructuredEvidence(document.normalized_text, mentions[0].evidence.document_id,
                               tbox.tbox_id, extractor.summary(), mentions)
    return document, tbox, extractor.summary(), mentions, facts, proof


class AutomaticReviewSourceProofTests(unittest.TestCase):
    def test_generic_source_records_and_every_exact_property_and_join_have_proof(self):
        for renamed in (False, True):
            with self.subTest(renamed=renamed):
                _, _, _, mentions, facts, proof = source_records(renamed=renamed)
                revisions = {m.revision_id: m for m in mentions}
                self.assertEqual(len({proof.identity(m).source_id for m in mentions}), 4)
                self.assertEqual(len(facts), 6)
                self.assertTrue(all(proof.fact_mapping(f, revisions) for f in facts))
                relationships = [f for f in facts if f.object_entity]
                self.assertEqual(len(relationships), 3)
                self.assertTrue(all(f.relationship_properties[0].name == "basis" for f in relationships))
                literals = [f.literal_semantics for f in facts if f.object_entity is None]
                self.assertTrue(all(v.source_encoding == "JSON_STRING" for v in literals))
                self.assertEqual({v.canonical_value for v in literals}, {f"原始\\编号-{i}" for i in range(3)})

    def test_property_proof_rejects_wrong_predicate_wrong_range_and_changed_source(self):
        document, tbox, summary, mentions, facts, proof = source_records()
        revisions = {m.revision_id: m for m in mentions}
        fact = next(f for f in facts if f.object_entity is None)
        self.assertIsNone(proof.fact_mapping(replace(fact, predicate="pressure"), revisions))
        shifted = replace(fact.evidence, char_start=fact.evidence.char_start + 1, char_end=fact.evidence.char_end + 1)
        self.assertIsNone(proof.fact_mapping(replace(fact, evidence=shifted), revisions))
        changed_text = document.normalized_text.replace("serial", "xxxxxx")
        changed = StructuredEvidence(changed_text, mentions[0].evidence.document_id, tbox.tbox_id, summary, mentions)
        self.assertIsNone(changed.fact_mapping(fact, revisions))

    def test_relation_direction_target_and_property_ranges_are_independently_checked(self):
        _, _, _, mentions, facts, proof = source_records()
        revisions = {m.revision_id: m for m in mentions}
        rels = [f for f in facts if f.object_entity]
        fact = rels[0]
        wrong_direction = replace(fact, subject=fact.object_entity, object_entity=fact.subject,
                                  subject_mention_revision_id=fact.object_mention_revision_id,
                                  object_mention_revision_id=fact.subject_mention_revision_id)
        self.assertIsNone(proof.fact_mapping(wrong_direction, revisions))
        wrong_target = replace(fact, object_entity=rels[1].object_entity,
                               object_mention_revision_id=rels[1].object_mention_revision_id)
        self.assertIsNone(proof.fact_mapping(wrong_target, revisions))
        self.assertIsNone(proof.fact_mapping(replace(fact, relationship_properties=()), revisions))
        prop = fact.relationship_properties[0]
        shifted_id = relationship_property_value_id(prop.tenant_id, prop.relationship_type, prop.name,
            prop.literal_semantics.identity_reference, prop.evidence_chunk_id,
            prop.evidence_char_start + 1, prop.evidence_char_end - 1,
            prop.extractor_version, prop.schema_version)
        shifted = replace(prop, property_value_id=shifted_id, evidence_char_start=prop.evidence_char_start + 1,
                          evidence_char_end=prop.evidence_char_end - 1, evidence_text=prop.evidence_text[1:-1])
        self.assertIsNone(proof.fact_mapping(replace(fact, relationship_properties=(shifted,)), revisions))

    def test_source_identity_is_document_scoped_and_unrelated_identity_cannot_be_restored(self):
        document, tbox, summary, mentions, _, _ = source_records()
        wrong = StructuredEvidence(document.normalized_text, "different-document", tbox.tbox_id, summary, mentions)
        self.assertTrue(all(wrong.identity(m) is None for m in mentions))
        wrong.restore(mentions)
        self.assertTrue(all(wrong.identity(m) is None for m in mentions))
        missing = StructuredEvidence(document.normalized_text, mentions[0].evidence.document_id, tbox.tbox_id, None, mentions)
        self.assertTrue(all(missing.identity(m) is None for m in mentions))

    def test_source_summary_must_agree_with_actual_record_counts_and_unique_ids(self):
        document, tbox, summary, mentions, _, _ = source_records()
        invalid = deepcopy(summary)
        invalid["collections"][0]["record_count"] += 1
        with self.assertRaisesRegex(ValueError, "count differs"):
            StructuredEvidence(document.normalized_text, mentions[0].evidence.document_id, tbox.tbox_id, invalid, mentions)
        duplicate = document.normalized_text.replace('"asset-1"', '"asset-0"')
        with self.assertRaisesRegex(ValueError, "not unique"):
            StructuredEvidence(duplicate, mentions[0].evidence.document_id, tbox.tbox_id, summary, mentions)

    def test_identity_requires_declared_complete_nonconflicting_properties(self):
        _, plain_tbox, _, plain_mentions, plain_facts, _ = source_records()
        self.assertEqual(_identity_values(plain_mentions, plain_facts, plain_tbox), {})
        _, tbox, _, mentions, facts, _ = source_records(identity=True)
        values = _identity_values(mentions, facts, tbox)
        self.assertEqual(len(values), 3)
        serials = [f for f in facts if f.predicate == "serialNumber"]
        first = serials[0]
        missing = _identity_values(mentions, [f for f in facts if f is not first], tbox)
        self.assertNotIn(first.subject.entity_id, missing)
        contradictory = replace(serials[1], subject=first.subject,
                                subject_mention_revision_id=first.subject_mention_revision_id)
        conflict = _identity_values(mentions, [*facts, contradictory], tbox)
        self.assertNotIn(first.subject.entity_id, conflict)
        self.assertEqual(_identity_values(mentions, [*facts, first], tbox), values)

    def test_same_fact_support_is_not_a_conflict_but_different_values_are(self):
        _, _, _, _, facts, _ = source_records()
        self.assertEqual(_fact_conflicts([*facts, *facts]), set())
        serials = [f for f in facts if f.predicate == "serialNumber"]
        first = serials[0]
        different = replace(serials[1], subject=first.subject,
                            subject_mention_revision_id=first.subject_mention_revision_id)
        self.assertEqual(_fact_conflicts([*facts, different]), {(first.subject.entity_id, "serialNumber", None)})
        rel = next(f for f in facts if f.object_entity)
        removed_metadata = replace(rel, relationship_properties=())
        self.assertIn((rel.subject.entity_id, rel.predicate, rel.object_entity.entity_id),
                      _fact_conflicts([rel, removed_metadata]))

    def test_public_receipt_never_exposes_private_lease_or_internal_fields(self):
        result = Neo4jAutoReviewService._public({"job_id": "job", "items": [],
                                               "_lease_token": "internal-secret"})
        self.assertNotIn("_lease_token", result)

    def test_commit_rechecks_pinned_identity_evidence_and_newly_confirmed_matches(self):
        _, tbox, _, mentions, facts, _ = source_records(identity=True)
        first = next(f for f in facts if f.predicate == "serialNumber")
        members = [m for m in mentions if m.entity.entity_id == first.subject.entity_id]
        group = {"members": members, "target": first.subject,
                 "signature": _identity_values(mentions, facts, tbox)[first.subject.entity_id],
                 "identity_fact_revisions": {first.record_id: first.revision_id}}
        principal = Principal("reviewer", tbox.tenant_id, frozenset({"engineers"}),
                              frozenset({"knowledge:review", "knowledge:construct"}))

        class Rows(list):
            def single(self):
                return self[0] if self else None

        class ComparisonTx:
            def __init__(self, identity_mentions, identity_facts):
                self.replies = iter([
                    [{"tbox": {"definition_json": json.dumps(tbox.to_mapping()),
                       "status": tbox.status.value, "tbox_id": tbox.tbox_id,
                       "checksum": tbox.checksum, "tenant_id": tbox.tenant_id,
                       "key": tbox.key, "version": tbox.version}}],
                    [{"revision": _mention_properties(m)} for m in identity_mentions],
                    [{"revision": _assertion_properties(f)} for f in identity_facts],
                ])

            def run(self, query, **parameters):
                return Rows(next(self.replies))

        serials = [f for f in facts if f.predicate == "serialNumber"]
        Neo4jAutoReviewService._check_identity_comparison_tx(ComparisonTx(mentions, serials), principal, [group])
        with self.assertRaisesRegex(KnowledgeConflict, "evidence changed"):
            Neo4jAutoReviewService._check_identity_comparison_tx(
                ComparisonTx(mentions, [f for f in serials if f is not first]), principal, [group])

        other = next(m for m in mentions if m.entity.entity_type == "Asset" and m.entity.entity_id != first.subject.entity_id)
        confirmed = replace(other, revision=RecordRevision.next("external-mention", 0),
            trust=replace(other.trust, status=GovernanceStatus.APPROVED,
                          reviewed_by="operator", reviewed_at=other.created_at))
        matching_fact = replace(first, revision=RecordRevision.next("external-identity-fact", 0),
                                subject=confirmed.entity, subject_mention_revision_id=confirmed.revision_id)
        # Unreviewed target evidence cannot establish an automatic strong match.
        Neo4jAutoReviewService._check_identity_comparison_tx(
            ComparisonTx([*mentions, confirmed], [*serials, matching_fact]), principal, [group])
        matching_fact = replace(matching_fact, trust=replace(matching_fact.trust,
            status=GovernanceStatus.APPROVED, reviewed_by="operator", reviewed_at=matching_fact.created_at))
        with self.assertRaisesRegex(KnowledgeConflict, "confirmed identity appeared"):
            Neo4jAutoReviewService._check_identity_comparison_tx(
                ComparisonTx([*mentions, confirmed], [first, matching_fact]), principal, [group])


if __name__ == "__main__":
    unittest.main()

class AutomaticReviewBudgetTests(unittest.TestCase):
    def test_large_model_payload_splits_by_char_budget_and_keeps_partial_failure(self):
        import asyncio
        import time
        from graphrag_prod.knowledge.auto_review_model import AutoReviewModelDecision, AutoReviewModelResult
        from graphrag_prod.knowledge.auto_review import _json
        calls=[]
        class Reviewer:
            async def review(self, kind, payload):
                calls.append(payload)
                self.assertion = len(_json(payload)) <= 80000
                rid=payload["records"][0]["record_id"]
                return AutoReviewModelResult((AutoReviewModelDecision(rid,"APPROVE",evidence_ids=(rid,),reason="explicit"),),
                    {"attempts":[{}]},"UNAVAILABLE" if rid=="bad" else "COMPLETE")
        service=object.__new__(Neo4jAutoReviewService)
        service.reviewer=Reviewer()
        service._save=lambda *_:None
        service._audit=lambda *_:None
        state={"model_calls":0}
        payload={"records":[{"record_id":r,"evidence_ids":[r]} for r in ("good","bad")],
                 "evidence":[{"evidence_id":r,"text":"x"*50000} for r in ("good","bad")]}
        result=asyncio.run(service._model(state,"facts",payload,time.monotonic()+30))
        self.assertEqual(len(calls),2)
        self.assertTrue(all(len(_json(p))<80000 for p in calls))
        self.assertEqual(state["model_calls"],2)
        self.assertEqual(result.decisions[0].action,"APPROVE")
        self.assertEqual(result.decisions[1].action,"UNCERTAIN")
        self.assertTrue(result.decisions[1].reason.startswith("AUTO_REVIEW_MODEL_UNAVAILABLE"))

    def test_positive_relationship_cardinality_conflicts_wait_but_duplicate_support_does_not(self):
        _,tbox,_,_,facts,_=source_records()
        rule=next(r for r in tbox.relationship_types if r.name=="OWNS")
        tbox=replace(tbox,relationship_types=tuple(replace(r,source_cardinality=Cardinality.ZERO_OR_ONE)
                    if r.name==rule.name else r for r in tbox.relationship_types))
        relations=[f for f in facts if f.object_entity]
        conflicts=_fact_conflicts(relations,tbox)
        self.assertEqual(len(conflicts),3)
        self.assertEqual(_fact_conflicts([relations[0],relations[0]],tbox),set())

class AutomaticReviewReadinessTests(unittest.TestCase):
    def test_authorized_topology_groups_missing_project_context_without_inheriting_it(self):
        from pathlib import Path
        from tests.unit.test_ontology_source import tbox
        from graphrag_prod.knowledge.auto_review import required_property_issues
        text=(Path(__file__).resolve().parents[2]/"busway_files/topology.source.json").read_text()
        data=json.loads(text)
        entities=[]
        pairs=set()
        for collection,kind,key in (("assets",None,"asset_ref"),("ports","Port","port_ref"),
                                    ("measurement_points","MeasurementPoint","point_ref")):
            for row in data[collection]:
                identifier=row[key]
                entities.append({"entity_id":identifier,"entity_type":kind or row["entity_type"],"record_id":"mention:"+identifier})
                pairs.update((identifier,name) for name,value in row.items() if value is not None)
        # Repeated mentions do not multiply missing-field work items.
        entities.extend({**r,"record_id":"z-duplicate:"+r["record_id"]} for r in tuple(entities))
        issues=required_property_issues(tbox(),entities,pairs,text)
        self.assertEqual(len(issues),1)
        self.assertEqual(issues[0]["property_name"],"project_id")
        self.assertEqual(issues[0]["entity_count"],24)
        self.assertEqual(issues[0]["source_paths"],["/metadata/project_id","/metadata/source_location/project_id"])
        self.assertEqual(len(issues[0]["record_ids"]),24)
        self.assertTrue(all(r=="mention:"+e for e,r in zip(issues[0]["entity_ids"],issues[0]["record_ids"])))
        self.assertNotIn("value",issues[0])
        supplied=pairs|{(identifier,"project_id") for identifier in issues[0]["entity_ids"]}
        self.assertEqual(required_property_issues(tbox(),entities,supplied,text),[])

    def test_root_context_hint_does_not_treat_record_fields_as_document_defaults(self):
        from graphrag_prod.knowledge.auto_review import required_property_issues
        _,tbox,_,mentions,_,_=source_records(identity=True)
        asset=next(m for m in mentions if m.entity.entity_type=="Asset")
        entity={"entity_id":asset.entity.entity_id,"entity_type":"Asset","record_id":asset.record_id}
        issues=required_property_issues(tbox,[entity],set(),'{"rows":[{"serialNumber":"private-local"}]}')
        serial=next(i for i in issues if i["property_name"]=="serialNumber")
        self.assertEqual(serial["source_paths"],[])
        self.assertEqual(serial["entity_count"],1)

    def test_public_issue_bounds_preserve_counts_and_aligned_representatives(self):
        issue={"code":"PROPERTY_REQUIRED","property_name":"p","entity_ids":[f"e{i}" for i in range(201)],
               "record_ids":[f"r{i}" for i in range(201)],"entity_count":201,"reason":"missing","source_paths":[]}
        public=Neo4jAutoReviewService._public({"items":[],"issues":[issue]})
        self.assertTrue(public["truncated"])
        self.assertEqual(public["issues"][0]["entity_count"],201)
        self.assertEqual(public["issues"][0]["entity_ids"][-1],"e199")
        self.assertEqual(public["issues"][0]["record_ids"][-1],"r199")


class IndividualFactReviewTests(unittest.IsolatedAsyncioTestCase):
    """Regression: different objects' values are not one contradictory fact."""

    async def run_facts(self, *, mapping_action="VALID", uncertain_id=None,
                        unavailable=False, commit_failure=None):
        import time
        from graphrag_prod.knowledge.auto_review_model import AutoReviewModelDecision, AutoReviewModelResult
        _, tbox, _, mentions, facts, proof = source_records()
        approved_mentions = [replace(m, trust=replace(m.trust, status=GovernanceStatus.APPROVED,
            reviewed_by="operator", reviewed_at=m.created_at)) for m in mentions]
        data = {"tbox": tbox, "original": mentions, "current": [*approved_mentions, *facts],
                "comparison": [*approved_mentions, *facts]}
        calls, committed, items = [], [], {}
        service = object.__new__(Neo4jAutoReviewService)
        service._save = lambda *_: None
        service._audit = lambda *_: None
        def commit(_service, _state, records):
            if commit_failure and any(f.record_id == facts[0].record_id for f in records):
                raise KnowledgeConflict("candidate changed during model call")
            committed.extend(records)
        service._commit = commit
        async def model(state, kind, payload, deadline):
            calls.append((kind, payload))
            if unavailable:
                return None
            decisions = []
            for r in payload["records"]:
                action = mapping_action if kind == "mapping" else (
                    "UNCERTAIN" if r["record_id"] == uncertain_id else "APPROVE")
                reason = "组样本不同，不能作为单一事实批准" if kind == "mapping" else "本条证据需要核对"
                decisions.append(AutoReviewModelDecision(r["record_id"], action,
                    evidence_ids=tuple(r["evidence_ids"]), reason=reason))
            return AutoReviewModelResult(tuple(decisions), {}, "COMPLETE")
        service._model = model
        principal = Principal("reviewer", tbox.tenant_id, frozenset({"engineers"}),
                              frozenset({"knowledge:review", "knowledge:construct"}))
        await service._facts(principal, principal, {}, data, proof, items, time.monotonic()+30)
        return facts, calls, committed, items

    async def test_mapping_validates_semantics_but_each_fact_keeps_own_result(self):
        facts, calls, committed, items = await self.run_facts()
        self.assertEqual({f.record_id for f in committed}, {f.record_id for f in facts})
        self.assertEqual({kind for kind, _ in calls}, {"mapping"})
        for _, payload in calls:
            self.assertEqual(payload["rules"]["review_unit"], "MAPPING_RULE")
            for record in payload["records"]:
                self.assertTrue(record["record_id"].startswith("mapping:"))
                self.assertNotIn(record["record_id"], items)
        for fact in facts:
            self.assertEqual(items[fact.record_id]["decision"], "AUTO_APPROVED")
            self.assertEqual(items[fact.record_id]["input_revision"], fact.revision.revision)
            self.assertEqual(items[fact.record_id]["evidence_ids"], [fact.evidence.chunk_id])

    async def test_mapping_uncertain_falls_back_to_each_fact_without_copying_group_reason(self):
        _, _, _, _, original_facts, _ = source_records()
        uncertain = original_facts[0].record_id
        facts, calls, committed, items = await self.run_facts(mapping_action="UNCERTAIN", uncertain_id=uncertain)
        individual = [r for kind, payload in calls if kind == "facts" for r in payload["records"]]
        self.assertEqual({r["record_id"] for r in individual}, {f.record_id for f in facts})
        self.assertTrue(all("samples" not in r and r["evidence_ids"] == [r["record_id"]] for r in individual))
        self.assertEqual(len(committed), len(facts)-1)
        self.assertEqual(items[uncertain]["decision"], "NEEDS_HUMAN")
        self.assertEqual(items[uncertain]["reason"], "本条证据需要核对")
        self.assertFalse(any("组样本" in item["reason"] for item in items.values()))

    async def test_mapping_service_failure_is_incomplete_not_human_uncertainty(self):
        facts, calls, committed, items = await self.run_facts(unavailable=True)
        self.assertEqual(committed, [])
        self.assertEqual({kind for kind, _ in calls}, {"mapping"})
        self.assertEqual(len(items), len(facts))
        self.assertEqual({i["decision"] for i in items.values()}, {"INCOMPLETE"})

    async def test_stale_commit_does_not_block_unrelated_facts(self):
        facts, _, committed, items = await self.run_facts(commit_failure=True)
        self.assertEqual({f.record_id for f in committed}, {f.record_id for f in facts[1:]})
        self.assertEqual(items[facts[0].record_id]["decision"], "INCOMPLETE")
        self.assertEqual(items[facts[0].record_id]["reason_code"], "COMMIT_REVALIDATION_REQUIRED")
