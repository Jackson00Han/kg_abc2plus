"""Revision-bound correction of one property's subject, retaining source evidence."""

from dataclasses import asdict, replace

from neo4j import unit_of_work

from .models import EntityMentionRecord, RecordRevision, knowledge_record_id
from .review import (
    KnowledgeConflict, KnowledgeReviewUnavailable, Neo4jKnowledgeReviewService,
    ReviewRecordKind, ReviewRequest, _CURRENT_REVIEW_QUERY,
)
from .review_context import (
    _TARGET_QUERY, _TARGET_ONE_QUERY, _target_parameters, _conflict, identity_values_tx,
    _resolution_revision_query,
)
from .store import Neo4jKnowledgeStore, _stored_assertion, _stored_mention
from .trust import GovernanceStatus


def _load_property(tx, principal, request):
    row = tx.run(_CURRENT_REVIEW_QUERY[ReviewRecordKind.ASSERTION],
        tenant_id=principal.tenant_id, groups=sorted(principal.groups),
        record_id=request.record_id, expected_revision=request.expected_revision, limit=1).single()
    if row is None:
        raise KnowledgeReviewUnavailable('property revision is unavailable')
    fact = _stored_assertion(dict(row['revision']))
    if fact.object_entity is not None or fact.trust.status not in {
        GovernanceStatus.CANDIDATE, GovernanceStatus.QUARANTINED,
    }:
        raise KnowledgeReviewUnavailable('property is not awaiting review')
    return fact


def _candidate(fact):
    # Selection corrects this fact, rather than merging its previous source mention
    # (which may have incorrectly grouped several homonymous objects).
    return EntityMentionRecord(fact.revision, fact.tenant_id, fact.subject,
        fact.evidence, fact.confidence, fact.trust, fact.created_at)


def _identity_constraint(tx, fact):
    row = tx.run('''MATCH (t:TBoxVersion {tenant_id:$tenant_id, tbox_id:$tbox_id})
        -[:DECLARES_ENTITY_TYPE]->(d:TBoxEntityType {name:$entity_type})
        RETURN coalesce(d.identity_properties, []) AS properties''',
        tenant_id=fact.tenant_id, tbox_id=fact.trust.ontology_version_id,
        entity_type=fact.subject.entity_type).single()
    if row is None:
        raise KnowledgeReviewUnavailable('property ontology is unavailable')
    literal = fact.literal_semantics
    return {fact.predicate: {(literal.datatype, literal.canonical_value, literal.canonical_unit)}} \
        if fact.predicate in row['properties'] and literal else {}


def _target_payload(tx, principal, mention, constraint):
    values = identity_values_tx(tx, principal, mention, confirmed_entity=True)
    conflict = _conflict(constraint, values)
    return dict(record_id=mention.record_id, revision=mention.revision.revision,
        entity={key: value for key, value in asdict(mention.entity).items() if key != 'tenant_id'},
        status=mention.trust.status.value, authority=mention.trust.authority.value,
        evidence={key: getattr(mention.evidence, key) for key in
            ('document_id', 'version_id', 'chunk_id', 'char_start', 'char_end', 'quoted_text')},
        identity_properties=[dict(name=name, value=value[1], unit=value[2])
            for name, entries in sorted(values.items()) for value in sorted(entries, key=str)],
        selectable=not conflict,
        reason='此属性与目标实体的身份编号存在冲突，请先核查原文。' if conflict else
               '请根据本条属性的原文确认归属。')


def _search_identity_ids(tx, principal, candidate, query):
    if not query:
        return []
    statement = _resolution_revision_query(ReviewRecordKind.ASSERTION, one_record=False,
        extra_where="""
        AND revision.ontology_version_id = $ontology_version_id
        AND EXISTS {
            MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(d:TBoxEntityType {name:$entity_type})
            WHERE revision.predicate IN coalesce(d.identity_properties, [])
        }
        AND toLower(revision.literal_value) CONTAINS toLower($search_text)
        AND (revision.governance_status <> 'PUBLISHED' OR EXISTS {
            MATCH (:KnowledgePublicationState {tenant_id:$tenant_id})
                -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(:KnowledgePublication {tenant_id:$tenant_id,status:'ACTIVE'})
                -[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
        })
        """, projection='DISTINCT revision.subject_entity_id AS entity_id')
    statement = statement.replace('ORDER BY revision.created_at, revision.record_id', 'ORDER BY entity_id')
    parameters = _target_parameters(principal, candidate, query)
    parameters['statuses'] = ['CANDIDATE', 'QUARANTINED', 'APPROVED', 'PUBLISHED']
    return [row['entity_id'] for row in tx.run(statement, **parameters)]


@unit_of_work(timeout=25)
def property_assignment_tx(tx, principal, request):
    fact = _load_property(tx, principal, request)
    candidate = _candidate(fact)
    constraint = _identity_constraint(tx, fact)
    parameters = _target_parameters(principal, candidate, request.query)
    identity_ids = _search_identity_ids(tx, principal, candidate, request.query)
    parameters['identity_ids'] = identity_ids
    query = _TARGET_QUERY.replace("$search_text = '' OR any", "$search_text = '' OR revision.entity_id IN $identity_ids OR any")
    rows = list(tx.run(query, **parameters))
    targets = [_target_payload(tx, principal, _stored_mention(dict(row['revision'])), constraint)
               for row in rows[:20]]
    current_parameters = _target_parameters(principal, candidate)
    current_parameters['entity_id'] = fact.subject.entity_id
    current_query = _TARGET_QUERY.replace('AND revision.entity_type = $entity_type',
        'AND revision.entity_type = $entity_type AND revision.entity_id = $entity_id')
    current_rows = list(tx.run(current_query, **current_parameters))
    current = _target_payload(tx, principal, _stored_mention(dict(current_rows[0]['revision'])), constraint) \
        if current_rows else None
    return dict(record_id=fact.record_id, revision=fact.revision.revision,
        current=current, items=targets, truncated=len(rows) > 20 or len(identity_ids) > 20)


@unit_of_work(timeout=25)
def apply_property_assignment_tx(tx, principal, request, reviewed_at):
    service = Neo4jKnowledgeReviewService
    service._lock_tenant_corpus_tx(tx, principal.tenant_id, reviewed_at)
    lock = ReviewRequest(ReviewRecordKind.ASSERTION, request.record_id,
        request.expected_revision, GovernanceStatus.QUARANTINED, reviewed_at, request.notes)
    service._lock_review_head_tx(tx, principal, lock)
    fact = _load_property(tx, principal, request)
    target_lock = ReviewRequest(ReviewRecordKind.ENTITY_MENTION, request.target_record_id,
        request.target_expected_revision, GovernanceStatus.APPROVED, reviewed_at, request.notes)
    service._lock_review_head_tx(tx, principal, target_lock)
    parameters = _target_parameters(principal, _candidate(fact))
    parameters.update(record_id=request.target_record_id,
        expected_revision=request.target_expected_revision, limit=1)
    row = tx.run(_TARGET_ONE_QUERY, **parameters).single()
    if row is None:
        raise KnowledgeReviewUnavailable('selected identity is unavailable')
    target = _stored_mention(dict(row['revision']))
    if target.entity.entity_id != request.target_entity_id:
        raise KnowledgeConflict('selected identity changed')
    if target.entity.entity_id == fact.subject.entity_id:
        raise KnowledgeConflict('property already belongs to the selected identity')
    if not _target_payload(tx, principal, target, _identity_constraint(tx, fact))['selectable']:
        raise KnowledgeConflict('identity property conflict; check source evidence')
    notes = (f'Property subject correction; from={fact.subject.entity_id}; '
        f'to={target.entity.entity_id}; source_revision={fact.revision_id}; '
        f'target_record={target.record_id}; target_revision={target.revision.revision}. {request.notes}')
    trust = replace(fact.trust, status=GovernanceStatus.QUARANTINED,
        reviewed_by=principal.principal_id, reviewed_at=reviewed_at, review_notes=notes)
    # A dedicated source mention keeps the same-Chunk endpoint invariant without
    # rebinding any sibling facts or borrowing the target document's evidence.
    mention_id = knowledge_record_id(fact.tenant_id, 'ENTITY_MENTION',
        f'property-assignment:v1:{fact.revision_id}:{target.entity.entity_id}')
    mention = EntityMentionRecord(RecordRevision.next(mention_id, 1), fact.tenant_id,
        target.entity, fact.evidence, fact.confidence,
        trust.transition_to(GovernanceStatus.APPROVED, reviewed_by=principal.principal_id,
            reviewed_at=reviewed_at, review_notes=notes), fact.created_at,
        assignment_source_revision_id=fact.revision_id)
    updated = replace(fact, revision=RecordRevision.next(fact.record_id, fact.revision.revision),
        subject=target.entity, subject_mention_revision_id=mention.revision_id, trust=trust)
    service._validate_record_tbox_tx(tx, mention)
    service._validate_record_tbox_tx(tx, updated)
    initial = replace(mention, revision=RecordRevision.next(mention_id, 0),
        trust=replace(fact.trust, status=GovernanceStatus.CANDIDATE,
            reviewed_by=None, reviewed_at=None, review_notes=None))
    Neo4jKnowledgeStore._lock_head_tx(tx, initial.revision, initial.tenant_id,
        "ENTITY_MENTION", initial.created_at)
    Neo4jKnowledgeStore._create_mention_revision_tx(tx, initial, link_canonical_entity=False)
    Neo4jKnowledgeStore._lock_head_tx(tx, mention.revision, mention.tenant_id,
        "ENTITY_MENTION", mention.created_at)
    Neo4jKnowledgeStore._create_mention_revision_tx(tx, mention, link_canonical_entity=False)
    Neo4jKnowledgeStore._create_assertion_revision_tx(tx, updated, link_canonical_entities=False)
    return dict(outcomes=[dict(record_kind=kind, record_id=record.record_id,
        previous_revision_id=previous, revision_id=record.revision_id,
        revision=record.revision.revision, status=record.trust.status.value)
        for kind, record, previous in [('ENTITY_MENTION', mention, initial.revision_id),
                                      ('ASSERTION', updated, fact.revision_id)]])
