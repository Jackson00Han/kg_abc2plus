"""Source context and explicit, revision-bound identity selection for reviewers."""

from dataclasses import asdict

from .review import (
    KnowledgeConflict, KnowledgeReviewUnavailable, Neo4jKnowledgeReviewService,
    ReviewRecordKind, ReviewRequest, _active_revision_query,
)
from .store import _stored_assertion, _stored_mention
from .trust import GovernanceStatus


def _resolution_revision_query(kind, *, one_record, extra_where="", projection="revision {.*} AS revision"):
    """Read review drafts or evidence pinned by the active publication.

    A newer upload advances document pointers without replacing the publication.
    Only published revisions and their release snapshots can use older sources;
    unconfirmed drafts must still belong to the current ingestion snapshot.
    """
    query = _active_revision_query(kind, one_record=one_record,
                                   extra_where=extra_where, projection=projection)
    query = query.replace("""MATCH (document:Document {tenant_id: $tenant_id})
              -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                  tenant_id: $tenant_id,
                  build_state: 'PUBLISHED'
              })-[:INCLUDES_CHUNK]->(chunk)
        MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {""", """MATCH (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id})
              -[:INCLUDES_CHUNK]->(chunk)
        MATCH (document:Document {tenant_id: $tenant_id})
              -[:HAS_VERSION]->(version:DocumentVersion {""")
    query = query.replace("MATCH (:TBoxCatalog {tenant_id: $tenant_id})", """WITH DISTINCT head, revision, chunk, document, snapshot, version
        MATCH (:TBoxCatalog {tenant_id: $tenant_id})""")
    return query.replace("WHERE revision.ontology_version_id = tbox.tbox_id", """WHERE revision.ontology_version_id = tbox.tbox_id
          AND snapshot.document_id = document.document_id
          AND snapshot.version_id = version.version_id
          AND chunk.char_start <= revision.evidence_char_start
          AND revision.evidence_char_start < revision.evidence_char_end
          AND revision.evidence_char_end <= chunk.char_end
          AND version.document_id = document.document_id
          AND chunk.document_id = document.document_id AND chunk.version_id = version.version_id
          AND EXISTS { MATCH (version)-[:HAS_CHUNK]->(chunk) }
          AND EXISTS { MATCH (document)-[:ACTIVE_VERSION]->(:DocumentVersion {tenant_id:$tenant_id}) }
          AND snapshot.build_state IN ['PUBLISHED', 'RETIRED']
          AND snapshot.retirement_id IS NULL AND version.retirement_id IS NULL
          AND document.retirement_id IS NULL AND document.retirement_request_fingerprint IS NULL
          AND coalesce(document.lifecycle_status, 'ACTIVE') = 'ACTIVE'
          AND coalesce(version.lifecycle_status, 'ACTIVE') = 'ACTIVE'
          AND ((snapshot.build_state = 'PUBLISHED'
              AND EXISTS { MATCH (document)-[:ACTIVE_VERSION]->(version) }
              AND EXISTS { MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot) })
            OR (revision.governance_status = 'PUBLISHED' AND EXISTS {
              MATCH (:KnowledgePublicationState {tenant_id:$tenant_id})
                -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication:KnowledgePublication {tenant_id:$tenant_id,status:'ACTIVE'})
                -[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
              MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
            }))""")


_TARGET_FILTER = """
AND revision.entity_type = $entity_type
AND revision.ontology_version_id = $ontology_version_id
AND revision.record_id <> $candidate_record_id
AND (revision.governance_status = 'APPROVED' OR EXISTS {
    MATCH (:KnowledgePublicationState {tenant_id:$tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(:KnowledgePublication {tenant_id:$tenant_id, status:'ACTIVE'})
      -[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
})
AND ($search_text = '' OR any(value IN [revision.canonical_name, revision.canonical_key,
    revision.entity_id] + coalesce(revision.aliases, [])
    WHERE toLower(value) CONTAINS toLower($search_text)))
"""
_TARGET_QUERY = _resolution_revision_query(
    ReviewRecordKind.ENTITY_MENTION, one_record=False, extra_where=_TARGET_FILTER + """
    WITH revision ORDER BY CASE WHEN revision.governance_status = 'PUBLISHED' THEN 0 ELSE 1 END,
                           revision.record_id
    WITH revision.entity_id AS selected_entity_id, collect(revision)[0] AS revision
    """,
)
_TARGET_ONE_QUERY = _resolution_revision_query(
    ReviewRecordKind.ENTITY_MENTION, one_record=True, extra_where=_TARGET_FILTER +
    "AND revision.governance_status IN ['APPROVED', 'PUBLISHED']",
)


def _target_parameters(principal, candidate, query=""):
    return dict(tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                entity_type=candidate.entity.entity_type,
                ontology_version_id=candidate.trust.ontology_version_id,
                candidate_record_id=candidate.record_id, search_text=query,
                statuses=['APPROVED', 'PUBLISHED'], limit=21)


def identity_values_tx(tx, principal, mention, *, confirmed_entity=False):
    """Read current, visible identity facts, including earlier mention revisions."""
    mention_ids = [mention.record_id]
    if confirmed_entity:
        # The displayed representative can change after another source is linked.
        # Its entity's identity evidence must not disappear with that change.
        mentions_query = _resolution_revision_query(
            ReviewRecordKind.ENTITY_MENTION, one_record=False,
            extra_where=_TARGET_FILTER + "AND revision.entity_id = $entity_id",
        )
        parameters = _target_parameters(principal, mention)
        parameters.update(entity_id=mention.entity.entity_id, limit=101)
        rows = list(tx.run(mentions_query, **parameters))
        if len(rows) > 100:
            raise KnowledgeConflict("identity sources exceed review limit")
        mention_ids.extend(row['revision']['record_id'] for row in rows)
    query = _resolution_revision_query(
        ReviewRecordKind.ASSERTION, one_record=False, extra_where="""
        AND revision.ontology_version_id = $ontology_version_id
        AND revision.subject_entity_id = $entity_id
        AND EXISTS {
          MATCH (m:GovernedEntityMentionRevision {tenant_id:$tenant_id})
          WHERE m.record_id IN $mention_ids
            AND m.revision_id = revision.subject_mention_revision_id
        }
        AND EXISTS {
          MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(definition:TBoxEntityType {name:$entity_type})
          WHERE revision.predicate IN coalesce(definition.identity_properties, [])
        }
        """,
    )
    rows = list(tx.run(query, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
        ontology_version_id=mention.trust.ontology_version_id,
        entity_id=mention.entity.entity_id, entity_type=mention.entity.entity_type,
        mention_ids=mention_ids, statuses=['CANDIDATE', 'QUARANTINED', 'APPROVED', 'PUBLISHED'],
        limit=101))
    if len(rows) > 100:
        raise KnowledgeConflict("identity evidence exceeds review limit")
    values = {}
    for row in rows:
        fact = _stored_assertion(dict(row['revision']))
        if fact.object_entity is None and fact.literal_semantics:
            literal = fact.literal_semantics
            values.setdefault(fact.predicate, set()).add(
                (literal.datatype, literal.canonical_value, literal.canonical_unit))
    return values


def _conflict(candidate_values, target_values):
    # Missing evidence is uncertainty. Two distinct known values are a conflict.
    return any(len(values) > 1 for values in (*candidate_values.values(), *target_values.values())) or any(
        candidate_values[name] != target_values[name]
        for name in candidate_values.keys() & target_values.keys())


def _target_payload(mention, target_values, *, conflict=False):
    return dict(
        record_id=mention.record_id, revision=mention.revision.revision,
        entity={key: value for key, value in asdict(mention.entity).items() if key != 'tenant_id'},
        status=mention.trust.status.value, authority=mention.trust.authority.value,
        evidence={key: getattr(mention.evidence, key) for key in
            ('document_id', 'version_id', 'chunk_id', 'char_start', 'char_end', 'quoted_text')},
        identity_properties=[dict(name=name, value=value[1], unit=value[2])
            for name, values in sorted(target_values.items()) for value in sorted(values, key=str)],
        selectable=not conflict,
        reason='身份属性存在不同值，请先核查并修正。' if conflict else
               '请核对双方上下文，明确确认是否为同一实体。',
    )


def resolution_targets_tx(tx, principal, candidate, query):
    rows = list(tx.run(_TARGET_QUERY, **_target_parameters(principal, candidate, query)))
    candidate_values = identity_values_tx(tx, principal, candidate)
    targets = []
    for row in rows[:20]:
        mention = _stored_mention(dict(row['revision']))
        target_values = identity_values_tx(tx, principal, mention, confirmed_entity=True)
        conflict = _conflict(candidate_values, target_values)
        targets.append(_target_payload(mention, target_values, conflict=conflict))
    from .review import _DEPENDENT_ASSERTION_QUERY, MAX_REVIEW_BATCH
    from .identity_review import impact_digest
    facts = list(tx.run(_DEPENDENT_ASSERTION_QUERY, tenant_id=principal.tenant_id,
        groups=sorted(principal.groups), ontology_version_id=candidate.trust.ontology_version_id,
        mention_revision_id=candidate.revision_id, limit=MAX_REVIEW_BATCH))
    impact = [dict(record_id=row['revision']['record_id'], revision=row['revision']['revision'],
        predicate=row['revision']['predicate']) for row in facts]
    return {'items': targets, 'truncated': len(rows) > 20,
            'dependent_facts': impact, 'impact_token': impact_digest(impact)}


def resolution_target_tx(tx, principal, candidate, record_id, expected_revision):
    """Fetch only the revision explicitly selected by the reviewer.

    Identity conflict and freshness are checked again while holding the review
    locks in confirmed_target_tx. This read neither enumerates other entities
    nor recomputes dependent-fact previews that the apply response does not use.
    """
    parameters = _target_parameters(principal, candidate)
    parameters.update(record_id=record_id, expected_revision=expected_revision, limit=1)
    row = tx.run(_TARGET_ONE_QUERY, **parameters).single()
    if row is None:
        return None
    mention = _stored_mention(dict(row['revision']))
    values = identity_values_tx(tx, principal, mention, confirmed_entity=True)
    return _target_payload(mention, values)


def validate_existing_identity_tx(tx, principal, candidate, target):
    """Apply known-contradiction checks to legacy approval and suggested links too."""
    parameters = _target_parameters(principal, candidate, target.entity_id)
    query = _TARGET_QUERY.replace(
        "AND revision.entity_type = $entity_type",
        "AND revision.entity_type = $entity_type AND revision.entity_id = $target_entity_id")
    parameters['target_entity_id'] = target.entity_id
    rows = list(tx.run(query, **parameters))
    if len(rows) > 20:
        raise KnowledgeConflict('identity targets exceed review limit')
    if rows:
        values = identity_values_tx(tx, principal, candidate)
        for row in rows:
            mention = _stored_mention(dict(row['revision']))
            if _conflict(values, identity_values_tx(tx, principal, mention, confirmed_entity=True)):
                raise KnowledgeConflict('identity property conflict; correct the evidence before linking')


def confirmed_target_tx(tx, principal, candidate, record_id, revision, *, reviewed_at):
    request = ReviewRequest(ReviewRecordKind.ENTITY_MENTION, record_id, revision,
                            GovernanceStatus.APPROVED, reviewed_at, 'Validate selected identity.')
    Neo4jKnowledgeReviewService._lock_review_head_tx(tx, principal, request)
    parameters = _target_parameters(principal, candidate)
    parameters.update(record_id=record_id, expected_revision=revision, limit=1)
    row = tx.run(_TARGET_ONE_QUERY, **parameters).single()
    if row is None:
        raise KnowledgeReviewUnavailable('selected identity is unavailable')
    target = _stored_mention(dict(row['revision']))
    if _conflict(identity_values_tx(tx, principal, candidate),
                 identity_values_tx(tx, principal, target, confirmed_entity=True)):
        raise KnowledgeConflict('identity property conflict; correct the evidence before linking')
    return target


def context_window(text, start, end, *, view, offset=0, page_size=8000):
    """Absolute Unicode code-point positions, retained independently of display."""
    if view == 'document':
        left = min(offset, max(0, len(text) - 1))
        right = min(len(text), left + page_size)
    else:
        left, right = max(0, start - 2000), min(len(text), end + 2000)
        if view == 'paragraph':
            previous = text.rfind('\n', left, start)
            following = text.find('\n', end, right)
            left = previous + 1 if previous >= 0 else left
            right = following if following >= 0 else right
    return left, right


def evidence_context_tx(tx, principal, request):
    projection = """revision {.*} AS revision, document.title AS title,
        document.canonical_uri AS source_uri, chunk.text AS chunk_text,
        chunk.char_start AS chunk_start, size(version.normalized_text) AS total,
        CASE WHEN NOT EXISTS {
          MATCH (version)-[:HAS_CHUNK]->(other:Chunk)
          WHERE other.tenant_id IS NULL OR other.tenant_id <> $tenant_id OR
            NOT any(group IN $groups WHERE group IN coalesce(other.access_groups, []))
        } THEN substring(version.normalized_text, $window_start, $window_length)
          ELSE null END AS document_text,
        substring(version.normalized_text, revision.evidence_char_start,
          revision.evidence_char_end - revision.evidence_char_start) AS document_quote
    """
    # First resolve the exact source record. Client-supplied positions never
    # select protected text or change the stored evidence range.
    row = None
    for kind in ReviewRecordKind:
        query = _resolution_revision_query(kind, one_record=True)
        row = tx.run(query, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
            record_id=request.record_id, expected_revision=request.expected_revision, limit=1).single()
        if row is not None:
            break
    if row is None:
        raise KnowledgeReviewUnavailable('source revision is unavailable')
    record = dict(row['revision'])
    start, end = record['evidence_char_start'], record['evidence_char_end']
    window_start = request.offset if request.view == 'document' else max(0, start - 2000)
    window_length = 8000 if request.view == 'document' else end - window_start + 2000
    query = _resolution_revision_query(kind, one_record=True, projection=projection)
    row = tx.run(query, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
        record_id=request.record_id, expected_revision=request.expected_revision, limit=1,
        window_start=window_start, window_length=window_length).single()
    if row is None or dict(row['revision']) != record or row['document_quote'] != record['evidence_text']:
        raise KnowledgeReviewUnavailable('source text is unavailable or changed')
    if request.view == 'document' and window_start >= row['total']:
        raise KnowledgeReviewUnavailable('document page is outside the source version')
    whole_document = row['document_text'] is not None
    if request.view == 'document' and not whole_document:
        raise KnowledgeReviewUnavailable('complete document is not accessible')
    text = row['document_text'] if whole_document else row['chunk_text']
    base = window_start if whole_document else row['chunk_start']
    left, right = context_window(text, start-base, end-base, view=request.view)
    if request.view != 'document' and text[start-base:end-base] != record['evidence_text']:
        raise KnowledgeReviewUnavailable('source context changed')
    return dict(record_id=request.record_id, revision=request.expected_revision,
        document_id=record['document_id'], version_id=record['version_id'],
        chunk_id=record['chunk_id'], document_title=row['title'] or record['document_id'],
        source_uri=row['source_uri'], text=text[left:right],
        context_start=base+left, context_end=base+right,
        char_start=start, char_end=end, quoted_text=record['evidence_text'],
        total_characters=row['total'], document_accessible=whole_document, view=request.view,
        has_previous=base+left > 0, has_next=base+right < row['total'])
