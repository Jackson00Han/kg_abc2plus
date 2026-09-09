"""Business editing boundaries and complete deterministic publication previews."""

import dataclasses
import json
import unittest

from graphrag_prod.api.knowledge_contracts import PublicationPreviewResponse, PublicationRequest
from graphrag_prod.knowledge.models import RecordRevision
from graphrag_prod.knowledge.publication_preview import publication_preview
from tests.fixtures.knowledge import make_knowledge_batch
from tests.unit import test_playground_resolution as ui_checks


class StandardizedPublicationTests(unittest.TestCase):
    def test_preview_groups_same_entity_mentions_and_keeps_source_records(self):
        batch = make_knowledge_batch()
        first = batch.mentions[0]
        second = dataclasses.replace(first,
            revision=RecordRevision.next("another-source-record", 0),
            entity=dataclasses.replace(first.entity, aliases=(*first.entity.aliases, "Another name")))
        preview = publication_preview(publication_id="publication", ontology_version_id="ontology",
            manifest_hash="a" * 64, base_publication_id="previous", before=(first,),
            after=(first, second), source_revision_ids=(second.revision_id,),
            removed_record_ids=(), replaced_record_ids=())
        self.assertEqual(len(preview["entity_changes"]), 1)
        change = preview["entity_changes"][0]
        self.assertEqual(change["operation"], "UPDATE")
        self.assertEqual(change["changes"]["aliases_added"], ["Another name"])
        self.assertEqual(len(change["after"]["evidence_ids"]), 2)
        self.assertEqual(len(preview["records_after"]), 2)
        response = PublicationPreviewResponse.model_validate(preview)
        self.assertEqual(response.model_dump(by_alias=True, mode="json"), preview)
        self.assertEqual(PublicationRequest(approved_revision_ids=["revision"],
            expected_preview_hash=preview["preview_hash"]).expected_preview_hash, preview["preview_hash"])

    def test_complete_instances_preserve_multiple_facts_sources_and_order(self):
        batch = make_knowledge_batch()
        relation = batch.assertions[0]
        properties = tuple(dataclasses.replace(relation,
            revision=RecordRevision.next(f"property-{index}", 0),
            object_entity=None, object_mention_revision_id=None,
            predicate="description", literal_value=relation.evidence.quoted_text)
            for index in range(2))
        records = (*batch.mentions, relation, *properties)
        kwargs = dict(publication_id="publication", ontology_version_id="ontology",
            manifest_hash="a" * 64, base_publication_id=None, before=(),
            source_revision_ids=(), removed_record_ids=(), replaced_record_ids=())
        preview = publication_preview(after=records, **kwargs)
        self.assertEqual(preview, publication_preview(after=tuple(reversed(records)), **kwargs))
        snapshot = preview["instances_after"]
        self.assertEqual(snapshot["summary"], {
            "entity_count": 2, "property_count": 2, "relationship_count": 1})
        entities = {item["entity_id"]: item for item in snapshot["entities"]}
        self.assertEqual(len(entities[relation.subject.entity_id]["properties"]), 2)
        self.assertEqual(snapshot["relationships"][0]["target"]["standard_name"],
                         relation.object_entity.canonical_name)
        evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
        for prop in entities[relation.subject.entity_id]["properties"]:
            self.assertEqual(evidence[prop["evidence_ids"][0]]["quoted_text"], prop["value"])
        empty = publication_preview(after=(), **kwargs)["instances_after"]
        self.assertEqual(empty["entities"], [])
        self.assertEqual(empty["relationships"], [])
        ui_checks.PlaygroundResolutionTests().run_ui(
            "const preview=" + json.dumps(preview) + r""";
const html=publicationPreviewMarkup(preview);
assert.ok(html.includes('查看完整实例 JSON（2 个实体 · 2 条属性 · 1 条关系）'));
assert.ok(html.includes('PUBLICATION_AFTER'));
assert.ok(!html.includes('查看完整变更 JSON'));
""")

    def test_editor_separates_identity_and_facts_and_protects_source_fields(self):
        ui_checks.PlaygroundResolutionTests().run_ui(r'''
const mention=item('pump'); mention.entity.entity_id='pump-id';
const entity=standardizedReview(mention);
assert.equal(entity.standard_name,'pump');
assert.ok(!('properties' in entity)); assert.ok(!('relationships' in entity));
assert.ok(!('authority_level' in entity));
entity.standard_name='corrected';
assert.equal(standardizedEdit(mention,entity).entity.canonical_name,'corrected');
entity.entity_id='forged';assert.throws(()=>standardizedEdit(mention,entity));
entity.entity_id='pump-id';entity.evidence_ids=['forged'];assert.throws(()=>standardizedEdit(mention,entity));
const fact=item('power',1,'ASSERTION');fact.object_entity=null;
fact.subject={entity_id:'pump-id',canonical_name:'pump',entity_type:'Equipment',canonical_key:'code:101'};
fact.subject_mention_revision_id='approved-pump';fact.predicate='RatedPower';
fact.literal_semantics={raw_value:'15',raw_unit:'kW',raw_valid_from:'2026-01-01'};
const property=standardizedReview(fact);property.value='18';
const edit=standardizedEdit(fact,property);
assert.equal(edit.literal.raw_literal,'18');assert.equal(edit.literal.raw_unit,'kW');
assert.equal(edit.subject_mention_revision_id,'approved-pump');
assert.equal(edit.literal.raw_valid_from,'2026-01-01');
property.entity_id='unverified';assert.throws(()=>standardizedEdit(fact,property));
assert.ok(reviewTechnical(mention,0).includes('标准化实体'));
assert.ok(reviewTechnical(fact,1).includes('标准化属性'));
assert.ok(!reviewTechnical(mention,0).includes('技术详情与纠错'));
''')

    def test_publish_requires_preview_and_rejects_changed_selection(self):
        ui_checks.PlaygroundResolutionTests().run_ui(r'''
elements.publicationRevisions.value='approved-1';
await publishKnowledge();assert.equal(requests.length,0);
state.publicationPreview={selection:publicationSelection(),selectionKey:JSON.stringify(publicationSelection()),preview:{preview_hash:'verified'}};
elements.publicationRevisions.value='approved-2';
await publishKnowledge();assert.equal(requests.length,0);assert.equal(state.publicationPreview,null);
state.publicationPreview={selection:publicationSelection(),selectionKey:JSON.stringify(publicationSelection()),preview:{preview_hash:'verified'}};
const pending=publishKnowledge();
assert.equal(JSON.parse(requests[0].options.body).expected_preview_hash,'verified');
assert.ok(!('instances_after' in JSON.parse(requests[0].options.body)));
assert.ok(!('entities' in JSON.parse(requests[0].options.body)));
const duplicate=publishKnowledge();assert.equal(requests.length,1);await duplicate;
requests[0].reject({status:409,message:'stale'});await pending;
assert.equal(state.publicationPreview,null);
assert.ok(elements.publicationOutput.textContent.includes('重新查看预览'));
''')

    def test_fact_tabs_preserve_separate_review_lanes(self):
        ui_checks.PlaygroundResolutionTests().run_ui(r'''
const property=item('property',1,'ASSERTION');property.object_entity=null;property.predicate='RatedPower';property.literal_value='15';
const relation=item('relation',1,'ASSERTION');relation.predicate='INSTALLED_AT';
state.reviews=[property,relation];state.reviewPhase='facts';state.reviewFactTab='properties';
renderReviews();assert.ok(elements.reviewList.innerHTML.includes('review-record-property'));
assert.ok(!elements.reviewList.innerHTML.includes('review-record-relation'));
assert.ok(elements.reviewList.innerHTML.includes('data-review-fact-tab="relationships"'));
state.reviewFactTab='relationships';renderReviews();
assert.ok(elements.reviewList.innerHTML.includes('review-record-relation'));
assert.ok(!elements.reviewList.innerHTML.includes('review-record-property'));
assert.ok(elements.reviewList.innerHTML.includes('标准化关系'));
''')
