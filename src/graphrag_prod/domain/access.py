"""Default-deny tenant and access-group authorization primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib


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
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be empty")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        object.__setattr__(self, "groups", _normalized_set(self.groups, "groups"))
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("capabilities must be a frozenset")
        if any(not isinstance(capability, str) for capability in self.capabilities):
            raise TypeError("capabilities must contain only strings")
        object.__setattr__(
            self,
            "capabilities",
            frozenset(
                capability.strip()
                for capability in self.capabilities
                if capability.strip()
            ),
        )


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


def retrieval_scope_token(kind: str, value: str) -> str:
    """Return a Lucene-safe, non-reversible token for an authorization scope."""

    if kind not in {"tenant", "group"}:
        raise ValueError("retrieval scope kind must be tenant or group")
    normalized = value.strip()
    if not normalized:
        raise ValueError("retrieval scope value must not be empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"grscope{kind}{digest}"


def active_retrieval_scope(tenant_id: str, groups: frozenset[str]) -> str:
    """Build the indexed tenant/ACL partition attached only to active Chunks."""

    normalized_groups = _normalized_set(groups, "groups")
    tokens = ["grscopeactive", retrieval_scope_token("tenant", tenant_id)]
    tokens.extend(
        retrieval_scope_token("group", group) for group in sorted(normalized_groups)
    )
    return " ".join(tokens)
