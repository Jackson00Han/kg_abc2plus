"""Default-deny tenant and access-group authorization primitives."""

from __future__ import annotations

from dataclasses import dataclass


def _normalized_set(values: frozenset[str], name: str) -> frozenset[str]:
    normalized = frozenset(value.strip() for value in values if value.strip())
    if not normalized:
        raise ValueError(f"{name} must contain at least one non-empty value")
    return normalized


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    tenant_id: str
    groups: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be empty")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        object.__setattr__(self, "groups", _normalized_set(self.groups, "groups"))


def can_access(
    principal: Principal,
    resource_tenant_id: str,
    allowed_groups: frozenset[str],
) -> bool:
    """Authorize only a same-tenant principal with an intersecting group."""
    if principal.tenant_id != resource_tenant_id:
        return False
    normalized_allowed = frozenset(
        group.strip() for group in allowed_groups if group.strip()
    )
    return bool(normalized_allowed & principal.groups)
