"""Exact lifecycle aliases for assertion endpoints across publication batches."""
from dataclasses import replace

from .models import EntityMentionRecord, RecordRevision
from .trust import GovernanceStatus


def published_mention_predecessor_alias(
    previous: EntityMentionRecord,
    published: EntityMentionRecord,
) -> tuple[str, str] | None:
    """Recognize only the unchanged APPROVED -> PUBLISHED direct predecessor.

    Callers must independently load both revisions under their normal tenant,
    evidence and ACL guards, and require the published revision in the active
    manifest. This never matches by entity name or accepts an edited ancestor.
    """
    if not isinstance(previous, EntityMentionRecord) or not isinstance(
        published, EntityMentionRecord
    ):
        return None
    if (
        previous.trust.status is not GovernanceStatus.APPROVED
        or published.trust.status is not GovernanceStatus.PUBLISHED
        or previous.record_id != published.record_id
        or published.revision != RecordRevision.next(
            previous.record_id, previous.revision.revision
        )
    ):
        return None
    expected = replace(
        previous,
        revision=published.revision,
        trust=previous.trust.transition_to(GovernanceStatus.PUBLISHED),
    )
    if expected != published:
        return None
    return previous.revision_id, published.revision_id
