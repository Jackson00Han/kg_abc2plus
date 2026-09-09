"""Lightweight checks for auditable human fact distinction."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from graphrag_prod.api.knowledge_contracts import ReviewDecisionInput
from graphrag_prod.domain.facts import FactDistinction, decode_fact_distinction
from graphrag_prod.knowledge import RecordRevision
from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService, ReviewRequest, ReviewRecordKind
from graphrag_prod.knowledge.store import _revision_properties, _entity_properties, _stored_assertion
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.knowledge.publication_preview import instance_snapshot
from graphrag_prod.knowledge.review_assessment import classify_facts, AuthoritativeFactMatch
from graphrag_prod.graph.browse_models import GraphBrowseQuery
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from graphrag_prod.retrieval.models import VersionFilter
from graphrag_prod.api.graph_contracts import GraphBrowseResponse
from tests.fixtures.knowledge import make_knowledge_batch
from tests.unit.test_knowledge_review import _principal
from tests.unit.test_knowledge_store import _typed_literal_batch
from tests.unit.test_graph_browsing import FakeGraphDriver, PIN, SCHEMA, PRINCIPAL

NOW = datetime(2025, 2, 4, tzinfo=timezone.utc)


class IndependentFactTests(unittest.TestCase):
    def test_contract_requires_reason_and_assertion_approval(self):
        values = dict(record_kind="ASSERTION", record_id="record", expected_revision=1,
            decision="APPROVED", notes="review", fact_action="INDEPENDENT", fact_reason="不同工况的独立记录")
        ReviewDecisionInput(**values)
        for update in ({"fact_reason": " "}, {"fact_reason": None}, {"decision": "REJECTED"},
                       {"record_kind": "ENTITY_MENTION"}, {"fact_action": None}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                ReviewDecisionInput(**(values | update))
        with self.assertRaises(ValueError):
            ReviewRequest(ReviewRecordKind.ASSERTION, "record", 1, GovernanceStatus.APPROVED,
                          NOW, "review", fact_action="INDEPENDENT", fact_reason=" ")

    def test_review_stores_decision_for_relation_and_property_without_changing_evidence(self):
        for record in (make_knowledge_batch().assertions[0], _typed_literal_batch().assertions[0]):
            # The fixture imports are already approved; model a pending reviewed source.
            record = replace(record, trust=replace(record.trust, status=GovernanceStatus.CANDIDATE,
                reviewed_by=None, reviewed_at=None, review_notes=None))
            request = ReviewRequest(ReviewRecordKind.ASSERTION, record.record_id, 1,
                GovernanceStatus.APPROVED, NOW, "review", fact_action="INDEPENDENT", fact_reason="另一事件的独立记录")
            with patch.object(Neo4jKnowledgeReviewService, "_approved_endpoint_tx", side_effect=lambda tx,p,r,c,e:r):
                updated = Neo4jKnowledgeReviewService._reviewed_record_tx(None, _principal(), record, request)
            self.assertEqual(updated.fact_distinction.reason, request.fact_reason)
            self.assertEqual(updated.fact_distinction.reviewed_by, _principal().principal_id)
            self.assertEqual(updated.evidence, record.evidence)
            self.assertEqual(updated.trust.authority, record.trust.authority)
            props = _revision_properties(updated) | _entity_properties(updated.subject, "subject_") | dict(
                predicate=updated.predicate, subject_mention_revision_id=updated.subject_mention_revision_id,
                object_kind=updated.object_kind, literal_value=updated.literal_value,
                object_mention_revision_id=updated.object_mention_revision_id)
            if updated.object_entity:
                props.update(_entity_properties(updated.object_entity, "object_"))
            if updated.literal_semantics:
                props.update(updated.literal_semantics.to_flat_properties())
            self.assertEqual(_stored_assertion(props), updated)
            self.assertEqual(replace(updated, revision=RecordRevision.next(updated.record_id,2)).fact_distinction, updated.fact_distinction)

    def test_distinction_separates_publication_and_duplicate_guidance(self):
        batch=make_knowledge_batch(); original=batch.assertions[0]
        separate=replace(original, revision=RecordRevision.next("separate-record",0),
            fact_distinction=FactDistinction("separate-record","不同事件", "reviewer", NOW))
        self.assertNotEqual(original.fact_key,separate.fact_key)
        self.assertEqual(classify_facts(separate,(AuthoritativeFactMatch("pub",original),))[0],"READY")
        self.assertEqual(classify_facts(original,(AuthoritativeFactMatch("pub",separate),))[0],"READY")
        snapshot=instance_snapshot((*batch.mentions, original, separate),[])
        self.assertEqual(snapshot["summary"]["relationship_count"],2)
        self.assertEqual(sum(bool(r["fact_distinction"]) for r in snapshot["relationships"]),1)
        with self.assertRaises(ValueError):
            replace(original, fact_distinction=separate.fact_distinction)

    def test_graph_keeps_independent_edge_and_visible_reason(self):
        browser=Neo4jPublishedGraphBrowser(FakeGraphDriver(),cursor_signing_key=b'a'*32)
        with patch('graphrag_prod.graph.browsing.read_graph_state',return_value=(PIN,SCHEMA)):
            view=browser._load(PRINCIPAL,"PUBLISHED_SECONDARY_INCLUSIVE",VersionFilter())
        original=next(v for v in view.assertions.values() if v.object_entity_id)
        provenance=replace(original.evidence.provenance,record_id="separate-record",revision_id="separate-revision")
        separate=replace(original,record_id=provenance.record_id,revision_id=provenance.revision_id,
                         evidence=replace(original.evidence,provenance=provenance))
        source=deepcopy(view.evidence[original.revision_id]); source.update(
            record_id=separate.record_id,revision_id=separate.revision_id,
            fact_distinction=FactDistinction(separate.record_id,"另一安装事件","reviewer",NOW).to_mapping())
        view=replace(view,assertions={original.revision_id:original,separate.revision_id:separate},
                     evidence={**view.evidence,separate.revision_id:source})
        with patch.object(browser,'_load',return_value=view):
            result=browser.query(PRINCIPAL,GraphBrowseQuery(predicates=(original.predicate,)))
        GraphBrowseResponse.model_validate(result)
        self.assertEqual(len(result['edges']),2)
        self.assertEqual(len({e['fact_key'] for e in result['edges']}),2)
        self.assertEqual(next(e for e in result['edges'] if e['fact_distinction'])['fact_distinction']['reason'],'另一安装事件')

    def test_metadata_codec_rejects_missing_audit_and_preserves_timezone(self):
        d=FactDistinction('record','独立事件','reviewer',NOW)
        self.assertEqual(decode_fact_distinction(json.dumps(d.to_mapping())),d)
        with self.assertRaises((ValueError,TypeError,KeyError)):
            decode_fact_distinction('{"reason":"unsupported"}')

    def test_ui_offers_both_choices_and_requires_explicit_reason(self):
        from tests.unit.test_playground_resolution import PlaygroundResolutionTests
        PlaygroundResolutionTests().run_ui(r'''const fact=item('fact',1,'ASSERTION');
state.reviews=[fact];reviewModel();
state.reviewAssessments.set('fact',{revision:1,identityEpoch:0,reviewEpoch:0,status:'DUPLICATE',summary:'同一事实',dependencies:[],matches:[]});
assert.ok(reviewActions(fact,0).includes('确认并追加来源'));
assert.ok(reviewActions(fact,0).includes('作为独立事实确认'));
state.reviewAssessments.get('fact').status='CONFLICT';
assert.equal(reviewApproval(fact).allowed,false);
assert.equal(reviewApproval(fact,true).allowed,true);
state.reviewAssessments.get('fact').status='BLOCKED';
assert.equal(reviewApproval(fact,true).allowed,false);
state.reviewAssessments.get('fact').status='DUPLICATE';
globalThis.prompt=()=>'';
await submitReviews('APPROVED',[0],false,false,true);
assert.equal(requests.length,0);
globalThis.prompt=()=>null;
await submitReviews('APPROVED',[0],false,false,true);
assert.equal(requests.length,0);
globalThis.prompt=()=> '另一次安装事件';
loadReviews=async()=>{};loadPublicationCandidates=async()=>{};
const job=submitReviews('APPROVED',[0],false,false,true);
await flush();
const sent=requests.find(r=>r.url==='/v1/knowledge/reviews:batch');
assert.ok(sent);
const request=JSON.parse(sent.options.body).decisions[0];
assert.equal(request.fact_action,'INDEPENDENT');
assert.equal(request.fact_reason,'另一次安装事件');
assert.equal(request.decision,'APPROVED');
sent.resolve({outcomes:[]});await job;
''')
