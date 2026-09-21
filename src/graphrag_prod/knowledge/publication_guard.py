"""Small, bounded publication member checks shared by formal read paths.

This checks the immutable revision membership, not the reader's document ACL.
Each caller retains its source, evidence, lifecycle and authorization guards.
"""

from __future__ import annotations

import re


# A publication retains prior approved knowledge; its total is not a write batch.
MAX_PUBLICATION_SELECTION_RECORDS = 10_000
# A replacement carries both the new revision ID and the replaced record ID.
MAX_PUBLICATION_CHANGE_RECORDS = 2 * MAX_PUBLICATION_SELECTION_RECORDS
MAX_PUBLICATION_MANIFEST_RECORDS = 20_000
# Compatibility export: the former name continues to mean a single change set.
MAX_PUBLICATION_RECORDS = MAX_PUBLICATION_CHANGE_RECORDS


def publication_members_guard(publication: str) -> str:
    """Return a Cypher boolean expression over a trusted local node alias.

    The first count stops at the cumulative manifest limit plus one. Only when
    the total count equals the bounded manifest length do we inspect its members.
    Equal total and distinct valid-ID counts prove exact set equality, including
    no duplicate manifest IDs, duplicate bindings, missing or substituted members.
    Empty revision manifests remain valid for source-only publications.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", publication) is None:
        raise ValueError("publication requires a local Cypher identifier")
    return f"""CASE
    WHEN {publication}.published_revision_ids IS NULL
      OR size({publication}.published_revision_ids) > {MAX_PUBLICATION_MANIFEST_RECORDS}
    THEN false
    WHEN COUNT {{
        MATCH ({publication})-[:PUBLISHES_KNOWLEDGE_REVISION]->(publication_member)
        RETURN publication_member LIMIT {MAX_PUBLICATION_MANIFEST_RECORDS + 1}
    }} <> size({publication}.published_revision_ids)
    THEN false
    ELSE COUNT {{
        MATCH ({publication})-[:PUBLISHES_KNOWLEDGE_REVISION]->(publication_member)
        WHERE publication_member.tenant_id = $tenant_id
          AND (publication_member:GovernedEntityMentionRevision
               OR publication_member:GovernedAssertionRevision)
          AND publication_member.governance_status = 'PUBLISHED'
          AND publication_member.ontology_version_id = {publication}.ontology_version_id
          AND publication_member.revision_id <> ''
          AND toStringOrNull(publication_member.revision_id) = publication_member.revision_id
          AND publication_member.revision_id IN {publication}.published_revision_ids
        RETURN DISTINCT publication_member.revision_id LIMIT {MAX_PUBLICATION_MANIFEST_RECORDS + 1}
    }} = size({publication}.published_revision_ids)
END"""
