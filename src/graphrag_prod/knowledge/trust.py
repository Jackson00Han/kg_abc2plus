"""Immutable trust metadata and the governed-knowledge lifecycle.

This module deliberately has no dependency on persistence or API models. It
defines the vocabulary that later adapters can attach to entities, assertions,
and imported authoritative records without changing its meaning per adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class KnowledgeOrigin(StrEnum):
    """How a knowledge record entered the system."""

    EXPERT_IMPORT = "EXPERT_IMPORT"
    EXPERT_CREATED = "EXPERT_CREATED"
    LLM_EXTRACTED = "LLM_EXTRACTED"
    RULE_DERIVED = "RULE_DERIVED"
    FIXTURE = "FIXTURE"


class AuthorityLevel(StrEnum):
    """Source authority, independent of publication status."""

    AUTHORITATIVE = "AUTHORITATIVE"
    SECONDARY = "SECONDARY"


class GovernanceStatus(StrEnum):
    """Review and publication lifecycle for a knowledge record."""

    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


_ALLOWED_AUTHORITY_BY_ORIGIN: dict[KnowledgeOrigin, AuthorityLevel] = {
    KnowledgeOrigin.EXPERT_IMPORT: AuthorityLevel.AUTHORITATIVE,
    KnowledgeOrigin.EXPERT_CREATED: AuthorityLevel.AUTHORITATIVE,
    KnowledgeOrigin.LLM_EXTRACTED: AuthorityLevel.SECONDARY,
    KnowledgeOrigin.RULE_DERIVED: AuthorityLevel.SECONDARY,
    KnowledgeOrigin.FIXTURE: AuthorityLevel.SECONDARY,
}

_LEGAL_TRANSITIONS: dict[GovernanceStatus, frozenset[GovernanceStatus]] = {
    GovernanceStatus.CANDIDATE: frozenset(
        {
            GovernanceStatus.APPROVED,
            GovernanceStatus.QUARANTINED,
            GovernanceStatus.REJECTED,
        }
    ),
    GovernanceStatus.APPROVED: frozenset(
        {
            GovernanceStatus.PUBLISHED,
            GovernanceStatus.QUARANTINED,
            GovernanceStatus.REJECTED,
        }
    ),
    GovernanceStatus.PUBLISHED: frozenset(
        {
            GovernanceStatus.QUARANTINED,
            GovernanceStatus.SUPERSEDED,
        }
    ),
    GovernanceStatus.QUARANTINED: frozenset(
        {
            GovernanceStatus.CANDIDATE,
            GovernanceStatus.APPROVED,
            GovernanceStatus.REJECTED,
        }
    ),
    GovernanceStatus.REJECTED: frozenset(),
    GovernanceStatus.SUPERSEDED: frozenset(),
}

_REVIEW_REQUIRED = frozenset(
    {
        GovernanceStatus.APPROVED,
        GovernanceStatus.PUBLISHED,
        GovernanceStatus.REJECTED,
        GovernanceStatus.SUPERSEDED,
    }
)


def _require_enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], name: str) -> EnumT:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be an instance of {enum_type.__name__}")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def allowed_governance_transitions(
    status: GovernanceStatus,
) -> frozenset[GovernanceStatus]:
    """Return the immutable set of valid next states for ``status``."""

    normalized = _require_enum(status, GovernanceStatus, "status")
    return _LEGAL_TRANSITIONS[normalized]


def validate_governance_transition(
    current: GovernanceStatus,
    target: GovernanceStatus,
) -> None:
    """Reject no-op and illegal governance transitions."""

    normalized_current = _require_enum(current, GovernanceStatus, "current")
    normalized_target = _require_enum(target, GovernanceStatus, "target")
    if normalized_target not in _LEGAL_TRANSITIONS[normalized_current]:
        raise ValueError(
            "illegal governance transition: "
            f"{normalized_current.value} -> {normalized_target.value}"
        )


@dataclass(frozen=True, slots=True)
class TrustMetadata:
    """Provenance authority and lifecycle state for one knowledge record.

    ``authority`` describes the source, while ``status`` describes whether the
    record has completed governance. Those dimensions are intentionally kept
    separate: a model-extracted assertion remains secondary even after an
    expert approves and publishes it.
    """

    origin: KnowledgeOrigin
    authority: AuthorityLevel
    status: GovernanceStatus
    ontology_version_id: str
    created_at: datetime
    extractor_version: str | None = None
    prompt_version: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None

    def __post_init__(self) -> None:
        origin = _require_enum(self.origin, KnowledgeOrigin, "origin")
        authority = _require_enum(self.authority, AuthorityLevel, "authority")
        status = _require_enum(self.status, GovernanceStatus, "status")
        expected_authority = _ALLOWED_AUTHORITY_BY_ORIGIN[origin]
        if authority is not expected_authority:
            raise ValueError(
                f"{origin.value} knowledge must use {expected_authority.value} authority"
            )

        object.__setattr__(
            self,
            "ontology_version_id",
            _required_text(self.ontology_version_id, "ontology_version_id"),
        )
        object.__setattr__(
            self,
            "extractor_version",
            _optional_text(self.extractor_version, "extractor_version"),
        )
        object.__setattr__(
            self,
            "prompt_version",
            _optional_text(self.prompt_version, "prompt_version"),
        )
        object.__setattr__(
            self,
            "reviewed_by",
            _optional_text(self.reviewed_by, "reviewed_by"),
        )
        object.__setattr__(
            self,
            "review_notes",
            _optional_text(self.review_notes, "review_notes"),
        )

        created_at = _aware_datetime(self.created_at, "created_at")
        if self.reviewed_at is not None:
            reviewed_at = _aware_datetime(self.reviewed_at, "reviewed_at")
            if reviewed_at < created_at:
                raise ValueError("reviewed_at must not precede created_at")

        has_reviewer = self.reviewed_by is not None
        has_reviewed_at = self.reviewed_at is not None
        if has_reviewer != has_reviewed_at:
            raise ValueError("reviewed_by and reviewed_at must be provided together")
        if self.review_notes is not None and not has_reviewer:
            raise ValueError("review_notes require reviewed_by and reviewed_at")
        if status in _REVIEW_REQUIRED and not has_reviewer:
            raise ValueError(f"{status.value} knowledge requires review metadata")
        if status is GovernanceStatus.CANDIDATE and has_reviewer:
            raise ValueError("CANDIDATE knowledge must not carry a completed review")

    @property
    def is_retrieval_eligible(self) -> bool:
        """Whether the record is eligible for normal governed retrieval."""

        return self.status in {
            GovernanceStatus.APPROVED,
            GovernanceStatus.PUBLISHED,
        }

    def transition_to(
        self,
        status: GovernanceStatus,
        *,
        reviewed_by: str | None = None,
        reviewed_at: datetime | None = None,
        review_notes: str | None = None,
    ) -> TrustMetadata:
        """Return a new record in ``status`` after validating the transition.

        Existing review evidence is retained when publishing, quarantining, or
        superseding an already-reviewed record. Moving a quarantined record
        back to the candidate queue clears prior review evidence so that the
        next decision is explicit.
        """

        normalized_status = _require_enum(status, GovernanceStatus, "status")
        validate_governance_transition(self.status, normalized_status)

        if normalized_status is GovernanceStatus.CANDIDATE:
            return replace(
                self,
                status=normalized_status,
                reviewed_by=None,
                reviewed_at=None,
                review_notes=None,
            )

        has_new_review = any(
            value is not None for value in (reviewed_by, reviewed_at, review_notes)
        )
        if has_new_review:
            return replace(
                self,
                status=normalized_status,
                reviewed_by=reviewed_by,
                reviewed_at=reviewed_at,
                review_notes=review_notes,
            )
        return replace(self, status=normalized_status)
