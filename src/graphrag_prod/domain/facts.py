"""Source-independent semantic identity; evidence revisions keep their own IDs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from .models import RelationshipPropertyValue, TypedLiteralValue


def literal_signature(value: TypedLiteralValue | None) -> tuple | None:
    if value is None:
        return None
    def instant(item: datetime | None) -> str | None:
        return None if item is None else item.astimezone(timezone.utc).isoformat()
    return (value.datatype, value.canonical_value, value.canonical_unit,
            instant(value.valid_from), instant(value.valid_to), instant(value.observed_at))


def relationship_signature(tenant_id: str, ontology_version_id: str, subject_id: str,
                           predicate: str, object_id: str,
                           properties: tuple[RelationshipPropertyValue, ...] = ()) -> tuple:
    # Repeated evidence for a property must not change the fact's meaning.
    qualifiers = tuple(sorted({(item.name, literal_signature(item.literal_semantics))
                               for item in properties}, key=repr))
    return (tenant_id, ontology_version_id, subject_id, predicate, object_id, qualifiers)


def relationship_fact_key(tenant_id: str, ontology_version_id: str, subject_id: str,
                          predicate: str, object_id: str,
                          properties: tuple[RelationshipPropertyValue, ...] = (),
                          *, independent_record_id: str | None = None) -> str:
    signature = relationship_signature(tenant_id, ontology_version_id, subject_id,
                                       predicate, object_id, properties)
    if independent_record_id is not None:
        signature += (("HUMAN_DISTINCTION", independent_record_id),)
    encoded = json.dumps(signature, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "relationship-v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FactDistinction:
    """Human governance metadata, never a claim extracted from source text."""
    record_id: str
    reason: str
    reviewed_by: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        for name, limit in (("record_id", 256), ("reason", 2000), ("reviewed_by", 256)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"invalid independent fact {name}")
        if not isinstance(self.reviewed_at, datetime) or self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("independent fact review time must be timezone aware")

    def to_mapping(self) -> dict:
        return {"record_id": self.record_id, "reason": self.reason,
                "reviewed_by": self.reviewed_by, "reviewed_at": self.reviewed_at.isoformat()}


def decode_fact_distinction(encoded: str | None) -> FactDistinction | None:
    if encoded is None:
        return None
    if not isinstance(encoded, str) or len(encoded) > 16000:
        raise ValueError("invalid fact distinction metadata")
    value = json.loads(encoded)
    value["reviewed_at"] = datetime.fromisoformat(value["reviewed_at"])
    return FactDistinction(**value)
