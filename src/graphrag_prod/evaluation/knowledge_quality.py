"""Deterministic extraction-quality metrics, drift detection, and gates.

This module evaluates extraction observations against independently adjudicated
gold.  It deliberately has no provider or database dependency: routine gates
must be reproducible offline and must never promote model output to an
authoritative graph layer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Any
from urllib.parse import urlsplit

import pint

from .datasets import canonical_json_bytes

KNOWLEDGE_GOLD_SCHEMA_VERSION = "knowledge-extraction-gold-v1"
KNOWLEDGE_PREDICTION_SCHEMA_VERSION = "knowledge-extraction-predictions-v1"
KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION = "knowledge-quality-gate-policy-v1"
KNOWLEDGE_BASELINE_SCHEMA_VERSION = "knowledge-quality-baseline-v1"
KNOWLEDGE_REPORT_SCHEMA_VERSION = "knowledge-quality-report-v1"

CASE_CLASSES = frozenset({"positive", "negative", "security"})
REQUIRED_NEGATIVE_CLASSES = frozenset({"negative", "security"})
FAMILIES = ("entity", "relationship", "property")
DATATYPES = frozenset(
    {
        "STRING",
        "INTEGER",
        "FLOAT",
        "DECIMAL",
        "BOOLEAN",
        "DATE",
        "DATETIME",
        "DURATION",
        "URI",
        "JSON",
    }
)
REVIEW_STATUSES = frozenset({"approved", "pending", "quarantined", "rejected"})
ORIGINS = frozenset({"expert", "llm", "rule"})
AUTHORITY_LEVELS = frozenset({"authoritative", "secondary"})

HARD_MAX_CASES = 10_000
HARD_MAX_ARTIFACTS_PER_CASE = 1_000
HARD_MAX_RESOLUTION_PAIRS_PER_CASE = 1_000
HARD_MAX_TOTAL_TEXT_CHARS = 50_000_000
HARD_MAX_ONTOLOGY_TYPES = 10_000
MAX_ID_CHARS = 256
MAX_TYPE_CHARS = 128
MAX_VALUE_CHARS = 16_384

_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DURATION = re.compile(
    r"^P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?"
    r"(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
)
_NUMBER = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_INTEGER = re.compile(r"^[+-]?(?:0|[1-9]\d*)$")
_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_UNIT_REGISTRY = pint.UnitRegistry(
    autoconvert_offset_to_baseunit=True,
    non_int_type=Decimal,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return value


def _text(value: Any, field: str, *, maximum: int = MAX_ID_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty bounded text")
    return value


def _identifier(value: Any, field: str) -> str:
    result = _text(value, field)
    if any(character.isspace() for character in result):
        raise ValueError(f"{field} must not contain whitespace")
    return result


def _type_name(value: Any, field: str) -> str:
    result = _text(value, field, maximum=MAX_TYPE_CHARS)
    if _TYPE_NAME.fullmatch(result) is None:
        raise ValueError(f"{field} is not a valid property-graph type name")
    return result


def _ratio(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field} must be a ratio")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{field} must be in [0, 1]")
    return result


def _bounded_int(value: Any, field: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{field} must be an integer in [1, {maximum}]")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        raise ValueError(
            f"{field} fields are invalid: missing={missing}, extra={extra}"
        )


def _validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(
        policy,
        required={
            "schema_version",
            "version",
            "limits",
            "thresholds",
            "drift",
            "high_risk_types",
        },
        field="knowledge quality policy",
    )
    if policy.get("schema_version") != KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION:
        raise ValueError("knowledge quality policy schema is invalid")
    version = _text(policy.get("version"), "policy.version")
    limits = _object(policy.get("limits"), "policy.limits")
    _exact_fields(
        limits,
        required={
            "max_cases",
            "max_artifacts_per_case",
            "max_resolution_pairs_per_case",
            "max_total_text_chars",
        },
        field="policy.limits",
    )
    checked_limits = {
        "max_cases": _bounded_int(
            limits["max_cases"], "policy.limits.max_cases", maximum=HARD_MAX_CASES
        ),
        "max_artifacts_per_case": _bounded_int(
            limits["max_artifacts_per_case"],
            "policy.limits.max_artifacts_per_case",
            maximum=HARD_MAX_ARTIFACTS_PER_CASE,
        ),
        "max_resolution_pairs_per_case": _bounded_int(
            limits["max_resolution_pairs_per_case"],
            "policy.limits.max_resolution_pairs_per_case",
            maximum=HARD_MAX_RESOLUTION_PAIRS_PER_CASE,
        ),
        "max_total_text_chars": _bounded_int(
            limits["max_total_text_chars"],
            "policy.limits.max_total_text_chars",
            maximum=HARD_MAX_TOTAL_TEXT_CHARS,
        ),
    }

    thresholds = _object(policy.get("thresholds"), "policy.thresholds")
    required_thresholds = {
        "min_overall_f1",
        "min_entity_f1",
        "min_relationship_f1",
        "min_property_f1",
        "max_schema_violation_rate",
        "max_evidence_violation_rate",
        "max_authority_contamination_rate",
        "max_resolution_false_merge_rate",
        "max_resolution_missed_merge_rate",
        "min_low_risk_review_sample_rate",
    }
    _exact_fields(
        thresholds,
        required=required_thresholds,
        optional={"per_type_min_f1"},
        field="policy.thresholds",
    )
    checked_thresholds = {
        name: _ratio(thresholds[name], f"policy.thresholds.{name}")
        for name in sorted(required_thresholds)
    }
    per_type_raw = thresholds.get("per_type_min_f1", {})
    per_type = _object(per_type_raw, "policy.thresholds.per_type_min_f1")
    checked_thresholds["per_type_min_f1"] = {
        _type_key(key, "policy per-type key"): _ratio(
            value, f"policy.thresholds.per_type_min_f1.{key}"
        )
        for key, value in sorted(per_type.items())
    }

    drift = _object(policy.get("drift"), "policy.drift")
    required_drift = {
        "max_f1_drop",
        "max_per_type_f1_drop",
        "max_schema_violation_rate_increase",
        "max_evidence_violation_rate_increase",
        "max_authority_contamination_rate_increase",
        "max_resolution_false_merge_rate_increase",
        "max_resolution_missed_merge_rate_increase",
        "max_review_reject_rate_increase",
        "max_review_quarantine_rate_increase",
    }
    _exact_fields(drift, required=required_drift, field="policy.drift")
    checked_drift = {
        name: _ratio(drift[name], f"policy.drift.{name}")
        for name in sorted(required_drift)
    }

    high_risk = _object(policy.get("high_risk_types"), "policy.high_risk_types")
    _exact_fields(high_risk, required=set(FAMILIES), field="policy.high_risk_types")
    checked_high_risk: dict[str, tuple[str, ...]] = {}
    for family in FAMILIES:
        values = _list(high_risk[family], f"policy.high_risk_types.{family}")
        checked = tuple(
            sorted(
                {
                    _text(value, f"high-risk {family}", maximum=MAX_TYPE_CHARS)
                    for value in values
                }
            )
        )
        if len(checked) != len(values):
            raise ValueError(f"policy high-risk {family} types must be unique")
        checked_high_risk[family] = checked
    return {
        "schema_version": KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION,
        "version": version,
        "limits": checked_limits,
        "thresholds": checked_thresholds,
        "drift": checked_drift,
        "high_risk_types": checked_high_risk,
    }


def _type_key(value: Any, field: str) -> str:
    text = _text(value, field, maximum=MAX_TYPE_CHARS + 16)
    family, separator, type_name = text.partition(":")
    if separator != ":" or family not in FAMILIES or not type_name:
        raise ValueError(f"{field} must use entity:, relationship:, or property:")
    _text(type_name, field, maximum=MAX_TYPE_CHARS)
    return text


def _validate_ontology(value: Any) -> dict[str, Any]:
    ontology = _object(value, "gold.ontology")
    _exact_fields(
        ontology,
        required={"entity_types", "relationship_types", "property_types"},
        field="gold.ontology",
    )
    entity_values = _list(ontology["entity_types"], "gold.ontology.entity_types")
    relationship_values = _list(
        ontology["relationship_types"], "gold.ontology.relationship_types"
    )
    property_values = _list(ontology["property_types"], "gold.ontology.property_types")
    if (
        len(entity_values) + len(relationship_values) + len(property_values)
        > HARD_MAX_ONTOLOGY_TYPES
    ):
        raise ValueError("ontology type count exceeds the hard bound")
    entities = tuple(_type_name(item, "entity type") for item in entity_values)
    if not entities or len(set(entities)) != len(entities):
        raise ValueError("ontology entity types must be non-empty and unique")
    entity_set = set(entities)

    relationships: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(relationship_values):
        item = _object(raw, f"relationship type {index}")
        _exact_fields(
            item,
            required={"type", "source_entity_types", "target_entity_types"},
            field=f"relationship type {index}",
        )
        name = _type_name(item["type"], "relationship type")
        sources = tuple(
            _type_name(value, "source entity type")
            for value in _list(item["source_entity_types"], "source_entity_types")
        )
        targets = tuple(
            _type_name(value, "target entity type")
            for value in _list(item["target_entity_types"], "target_entity_types")
        )
        if (
            name in relationships
            or not sources
            or not targets
            or len(set(sources)) != len(sources)
            or len(set(targets)) != len(targets)
            or not set(sources) <= entity_set
            or not set(targets) <= entity_set
        ):
            raise ValueError(f"relationship type {name} is invalid")
        relationships[name] = {
            "sources": frozenset(sources),
            "targets": frozenset(targets),
        }

    properties: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(property_values):
        item = _object(raw, f"property type {index}")
        _exact_fields(
            item,
            required={"type", "owner_entity_types", "datatype", "canonical_unit"},
            field=f"property type {index}",
        )
        name = _type_name(item["type"], "property type")
        owners = tuple(
            _type_name(value, "property owner type")
            for value in _list(item["owner_entity_types"], "owner_entity_types")
        )
        datatype = item["datatype"]
        canonical_unit = item["canonical_unit"]
        if canonical_unit is not None:
            canonical_unit = _text(canonical_unit, "canonical unit", maximum=64)
            try:
                _UNIT_REGISTRY.parse_units(canonical_unit)
            except (pint.PintError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"property type {name} canonical unit is invalid"
                ) from exc
        if (
            name in properties
            or not owners
            or len(set(owners)) != len(owners)
            or not set(owners) <= entity_set
            or datatype not in DATATYPES
            or (
                canonical_unit is not None
                and datatype not in {"INTEGER", "FLOAT", "DECIMAL"}
            )
        ):
            raise ValueError(f"property type {name} is invalid")
        properties[name] = {
            "owners": frozenset(owners),
            "datatype": datatype,
            "canonical_unit": canonical_unit,
        }
    return {
        "entities": frozenset(entities),
        "relationships": relationships,
        "properties": properties,
    }


def _validate_evidence_shape(evidence: Any, field: str) -> Mapping[str, Any]:
    item = _object(evidence, field)
    _exact_fields(
        item,
        required={"document_id", "chunk_id", "start", "end", "quote"},
        field=field,
    )
    _identifier(item["document_id"], f"{field}.document_id")
    _identifier(item["chunk_id"], f"{field}.chunk_id")
    start = item["start"]
    end = item["end"]
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        raise ValueError(f"{field} span is structurally invalid")
    _text(item["quote"], f"{field}.quote", maximum=MAX_VALUE_CHARS)
    return item


def _evidence_valid(evidence: Mapping[str, Any], case: Mapping[str, Any]) -> bool:
    start = evidence["start"]
    end = evidence["end"]
    text = case["chunk_text"]
    return (
        evidence["document_id"] == case["document_id"]
        and evidence["chunk_id"] == case["chunk_id"]
        and end <= len(text)
        and text[start:end] == evidence["quote"]
    )


def _parse_instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_canonical(value: str) -> bool:

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
        canonical = json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return False
    return canonical == value


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", "+0"} else text


def _typed_value_valid(artifact: Mapping[str, Any], datatype: str) -> bool:
    typed = artifact.get("typed_value")
    canonical = artifact.get("canonical_value")
    if (
        not isinstance(canonical, str)
        or not canonical
        or len(canonical) > MAX_VALUE_CHARS
    ):
        return False
    if datatype == "STRING":
        return isinstance(typed, str) and typed == artifact.get("raw_value")
    if datatype == "BOOLEAN":
        return isinstance(typed, bool) and canonical == str(typed).lower()
    if datatype == "INTEGER":
        return (
            isinstance(typed, int)
            and not isinstance(typed, bool)
            and canonical == str(typed)
        )
    if datatype == "FLOAT":
        if (
            not isinstance(typed, float)
            or not math.isfinite(typed)
            or _NUMBER.fullmatch(canonical) is None
        ):
            return False
        try:
            decimal_value = Decimal(canonical)
            parsed = float(decimal_value)
        except (InvalidOperation, ValueError):
            return False
        return (
            decimal_value.is_finite()
            and math.isfinite(parsed)
            and parsed == typed
            and _decimal_text(decimal_value) == canonical
        )
    if datatype == "DECIMAL":
        if (
            not isinstance(typed, str)
            or typed != canonical
            or _NUMBER.fullmatch(canonical) is None
        ):
            return False
        try:
            decimal_value = Decimal(canonical)
            return (
                decimal_value.is_finite() and _decimal_text(decimal_value) == canonical
            )
        except InvalidOperation:
            return False
    if datatype == "DATE":
        if not isinstance(typed, str) or typed != canonical:
            return False
        try:
            return date.fromisoformat(canonical).isoformat() == canonical
        except ValueError:
            return False
    if datatype == "DATETIME":
        return (
            isinstance(typed, str)
            and typed == canonical
            and canonical.endswith("Z")
            and _parse_instant(canonical) is not None
        )
    if datatype == "DURATION":
        return (
            isinstance(typed, str)
            and typed == canonical
            and _DURATION.fullmatch(canonical) is not None
        )
    if datatype == "URI":
        return (
            isinstance(typed, str)
            and typed == canonical
            and not any(character.isspace() for character in canonical)
            and bool(urlsplit(canonical).scheme)
        )
    if datatype == "JSON":
        return (
            isinstance(typed, str)
            and typed == canonical
            and _strict_json_canonical(canonical)
        )
    return False


def _raw_value_matches(artifact: Mapping[str, Any], datatype: str) -> bool:
    raw = artifact["raw_value"]
    canonical = artifact["canonical_value"]
    has_unit = artifact["raw_unit"] is not None
    if datatype == "STRING":
        return canonical == unicodedata.normalize("NFC", raw)
    if datatype == "BOOLEAN":
        return raw.casefold() in {"true", "false"} and canonical == raw.casefold()
    if datatype == "INTEGER":
        if _INTEGER.fullmatch(raw) is None:
            return False
        return has_unit or canonical == str(int(raw))
    if datatype in {"FLOAT", "DECIMAL"}:
        if _NUMBER.fullmatch(raw) is None:
            return False
        try:
            expected = _decimal_text(Decimal(raw))
        except InvalidOperation:
            return False
        return has_unit or canonical == expected
    if datatype == "DATE":
        try:
            return canonical == date.fromisoformat(raw).isoformat()
        except ValueError:
            return False
    if datatype == "DATETIME":
        parsed = _parse_instant(raw)
        if parsed is None:
            return False
        expected = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return canonical == expected
    if datatype == "DURATION":
        return _DURATION.fullmatch(raw) is not None and canonical == raw
    if datatype == "URI":
        return canonical == raw
    if datatype == "JSON":
        try:
            decoded = json.loads(
                raw,
                object_pairs_hook=_unique_json_pairs,
                parse_constant=_reject_json_constant,
            )
            expected = json.dumps(
                decoded,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return False
        return canonical == expected
    return False


def _unit_value_matches(artifact: Mapping[str, Any], datatype: str) -> bool:
    raw_unit = artifact["raw_unit"]
    canonical_unit = artifact["canonical_unit"]
    if raw_unit is None or canonical_unit is None:
        return raw_unit is canonical_unit is None
    if datatype not in {"INTEGER", "FLOAT", "DECIMAL"}:
        return False
    try:
        magnitude = Decimal(artifact["raw_value"])
        source_unit = _UNIT_REGISTRY.parse_units(raw_unit)
        target_unit = _UNIT_REGISTRY.parse_units(canonical_unit)
        converted = (
            _UNIT_REGISTRY.Quantity(magnitude, source_unit).to(target_unit).magnitude
        )
    except (
        InvalidOperation,
        pint.PintError,
        TypeError,
        ValueError,
    ):
        return False
    if datatype == "INTEGER" and converted != converted.to_integral_value():
        return False
    return artifact["canonical_value"] == _decimal_text(converted)


def _artifact_fields(family: str, *, prediction: bool) -> tuple[set[str], set[str]]:
    common = {"id", "evidence"}
    if family == "entity":
        required = common | {"entity_type", "canonical_name", "mention_text"}
        optional: set[str] = set()
    elif family == "relationship":
        required = common | {
            "relationship_type",
            "source_mention_id",
            "target_mention_id",
        }
        optional = set()
    else:
        required = common | {
            "property_type",
            "owner_mention_id",
            "datatype",
            "typed_value",
            "raw_value",
            "raw_unit",
            "canonical_value",
            "canonical_unit",
            "valid_from",
            "valid_to",
            "observed_at",
            "raw_valid_from",
            "raw_valid_to",
            "raw_observed_at",
        }
        optional = set()
    if prediction:
        required |= {"origin", "authority_level", "review_status"}
    return required, optional


def _artifact_type(family: str, artifact: Mapping[str, Any]) -> str:
    field = {
        "entity": "entity_type",
        "relationship": "relationship_type",
        "property": "property_type",
    }[family]
    value = artifact.get(field)
    return value if isinstance(value, str) else "<invalid>"


def _artifact_key(family: str, artifact: Mapping[str, Any]) -> tuple[Any, ...]:
    evidence = artifact["evidence"]
    evidence_key = (
        evidence["document_id"],
        evidence["chunk_id"],
        evidence["start"],
        evidence["end"],
        evidence["quote"],
    )
    if family == "entity":
        core = (
            artifact["entity_type"],
            artifact["canonical_name"],
            artifact["mention_text"],
        )
    elif family == "relationship":
        core = (
            artifact["relationship_type"],
            artifact["source_mention_id"],
            artifact["target_mention_id"],
        )
    else:
        core = (
            artifact["property_type"],
            artifact["owner_mention_id"],
            artifact["datatype"],
            artifact["typed_value"],
            artifact["raw_value"],
            artifact["raw_unit"],
            artifact["canonical_value"],
            artifact["canonical_unit"],
            artifact["valid_from"],
            artifact["valid_to"],
            artifact["observed_at"],
            artifact["raw_valid_from"],
            artifact["raw_valid_to"],
            artifact["raw_observed_at"],
        )
    return core + evidence_key


def _artifact_schema_valid(
    family: str,
    artifact: Mapping[str, Any],
    *,
    ontology: Mapping[str, Any],
    mentions: Mapping[str, Mapping[str, Any]],
) -> bool:
    artifact_type = _artifact_type(family, artifact)
    if family == "entity":
        evidence_quote = artifact["evidence"]["quote"]
        return (
            artifact_type in ontology["entities"]
            and isinstance(artifact.get("canonical_name"), str)
            and bool(artifact["canonical_name"].strip())
            and len(artifact["canonical_name"]) <= MAX_VALUE_CHARS
            and isinstance(artifact.get("mention_text"), str)
            and bool(artifact["mention_text"].strip())
            and len(artifact["mention_text"]) <= MAX_VALUE_CHARS
            and artifact["mention_text"] in evidence_quote
        )
    if family == "relationship":
        definition = ontology["relationships"].get(artifact_type)
        source = mentions.get(artifact.get("source_mention_id"))
        target = mentions.get(artifact.get("target_mention_id"))
        return bool(
            definition
            and source
            and target
            and source.get("entity_type") in definition["sources"]
            and target.get("entity_type") in definition["targets"]
            and source.get("mention_text") in artifact["evidence"]["quote"]
            and target.get("mention_text") in artifact["evidence"]["quote"]
        )

    definition = ontology["properties"].get(artifact_type)
    owner = mentions.get(artifact.get("owner_mention_id"))
    if (
        not definition
        or not owner
        or owner.get("entity_type") not in definition["owners"]
    ):
        return False
    datatype = artifact.get("datatype")
    if (
        datatype != definition["datatype"]
        or not _typed_value_valid(artifact, datatype)
        or not _raw_value_matches(artifact, datatype)
        or not _unit_value_matches(artifact, datatype)
    ):
        return False
    raw = artifact.get("raw_value")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_VALUE_CHARS:
        return False
    raw_unit = artifact.get("raw_unit")
    canonical_unit = artifact.get("canonical_unit")
    if (raw_unit is None) != (canonical_unit is None):
        return False
    if canonical_unit != definition["canonical_unit"]:
        return False
    if raw_unit is not None and (
        not isinstance(raw_unit, str)
        or not raw_unit
        or not isinstance(canonical_unit, str)
        or datatype not in {"INTEGER", "FLOAT", "DECIMAL"}
    ):
        return False
    quote = artifact["evidence"]["quote"]
    raw_tokens = [raw, raw_unit]
    canonical_temporals: list[datetime | None] = []
    for canonical_name, raw_name in (
        ("valid_from", "raw_valid_from"),
        ("valid_to", "raw_valid_to"),
        ("observed_at", "raw_observed_at"),
    ):
        canonical_token = artifact.get(canonical_name)
        raw_token = artifact.get(raw_name)
        if (canonical_token is None) != (raw_token is None):
            return False
        if canonical_token is None:
            canonical_temporals.append(None)
            continue
        canonical_instant = _parse_instant(canonical_token)
        raw_instant = _parse_instant(raw_token)
        if (
            canonical_instant is None
            or raw_instant is None
            or not canonical_token.endswith("Z")
            or canonical_instant.astimezone(UTC) != raw_instant.astimezone(UTC)
        ):
            return False
        canonical_temporals.append(canonical_instant)
        raw_tokens.append(raw_token)
    if any(token is not None and token not in quote for token in raw_tokens):
        return False
    start, end = canonical_temporals[:2]
    return not (start is not None and end is not None and start >= end)


def _validate_artifact_structure(
    family: str, raw: Any, field: str, *, prediction: bool
) -> Mapping[str, Any]:
    artifact = _object(raw, field)
    required, optional = _artifact_fields(family, prediction=prediction)
    _exact_fields(artifact, required=required, optional=optional, field=field)
    _identifier(artifact["id"], f"{field}.id")
    _validate_evidence_shape(artifact["evidence"], f"{field}.evidence")
    _text(_artifact_type(family, artifact), f"{field}.type", maximum=MAX_TYPE_CHARS)
    if family == "entity":
        _text(
            artifact["canonical_name"],
            f"{field}.canonical_name",
            maximum=MAX_VALUE_CHARS,
        )
        _text(
            artifact["mention_text"], f"{field}.mention_text", maximum=MAX_VALUE_CHARS
        )
    elif family == "relationship":
        _identifier(artifact["source_mention_id"], f"{field}.source_mention_id")
        _identifier(artifact["target_mention_id"], f"{field}.target_mention_id")
    else:
        _identifier(artifact["owner_mention_id"], f"{field}.owner_mention_id")
        _text(artifact["datatype"], f"{field}.datatype", maximum=32)
        _text(artifact["raw_value"], f"{field}.raw_value", maximum=MAX_VALUE_CHARS)
        for name in ("typed_value", "canonical_value"):
            value = artifact[name]
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"{field}.{name} must be a JSON scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{field}.{name} must be finite")
        for name in (
            "raw_unit",
            "canonical_unit",
            "valid_from",
            "valid_to",
            "observed_at",
            "raw_valid_from",
            "raw_valid_to",
            "raw_observed_at",
        ):
            if artifact.get(name) is not None:
                _text(artifact[name], f"{field}.{name}", maximum=MAX_VALUE_CHARS)
    if prediction:
        if artifact["origin"] not in ORIGINS:
            raise ValueError(f"{field}.origin is invalid")
        if artifact["authority_level"] not in AUTHORITY_LEVELS:
            raise ValueError(f"{field}.authority_level is invalid")
        if artifact["review_status"] not in REVIEW_STATUSES:
            raise ValueError(f"{field}.review_status is invalid")
    return artifact


def _validate_case(
    raw: Any,
    field: str,
    *,
    prediction: bool,
    ontology: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    limits: Mapping[str, int],
) -> dict[str, Any]:
    case = _object(raw, field)
    if prediction:
        _exact_fields(
            case,
            required={
                "case_id",
                "entities",
                "relationships",
                "property_facts",
                "resolution_pairs",
            },
            field=field,
        )
    else:
        _exact_fields(
            case,
            required={
                "case_id",
                "case_class",
                "document_id",
                "chunk_id",
                "chunk_text",
                "entities",
                "relationships",
                "property_facts",
                "resolution_pairs",
            },
            field=field,
        )
    case_id = _identifier(case["case_id"], f"{field}.case_id")
    if context is None:
        case_class = case["case_class"]
        if case_class not in CASE_CLASSES:
            raise ValueError(f"{field}.case_class is invalid")
        document_id = _identifier(case["document_id"], f"{field}.document_id")
        chunk_id = _identifier(case["chunk_id"], f"{field}.chunk_id")
        chunk_text = _text(
            case["chunk_text"],
            f"{field}.chunk_text",
            maximum=limits["max_total_text_chars"],
        )
        current_context: Mapping[str, Any] = {
            "case_id": case_id,
            "case_class": case_class,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "chunk_text": chunk_text,
        }
    else:
        current_context = context

    artifacts: dict[str, tuple[Mapping[str, Any], ...]] = {}
    seen_artifact_ids: set[str] = set()
    for family, source_field in (
        ("entity", "entities"),
        ("relationship", "relationships"),
        ("property", "property_facts"),
    ):
        values = _list(case[source_field], f"{field}.{source_field}")
        if len(values) > limits["max_artifacts_per_case"]:
            raise ValueError(f"{field}.{source_field} exceeds the configured bound")
        checked: list[Mapping[str, Any]] = []
        semantic_keys: set[tuple[Any, ...]] = set()
        for index, value in enumerate(values):
            artifact = _validate_artifact_structure(
                family, value, f"{field}.{source_field}[{index}]", prediction=prediction
            )
            artifact_id = artifact["id"]
            if artifact_id in seen_artifact_ids:
                raise ValueError(f"{field} artifact IDs must be unique across families")
            seen_artifact_ids.add(artifact_id)
            key = _artifact_key(family, artifact)
            if not prediction and key in semantic_keys:
                raise ValueError(
                    f"{field}.{source_field} contains duplicate gold facts"
                )
            semantic_keys.add(key)
            checked.append(artifact)
        artifacts[family] = tuple(checked)
    mentions = {item["id"]: item for item in artifacts["entity"]}

    resolution_values = _list(case["resolution_pairs"], f"{field}.resolution_pairs")
    if len(resolution_values) > limits["max_resolution_pairs_per_case"]:
        raise ValueError(f"{field}.resolution_pairs exceeds the configured bound")
    resolution_pairs: list[Mapping[str, Any]] = []
    seen_pair_ids: set[str] = set()
    for index, raw_pair in enumerate(resolution_values):
        pair = _object(raw_pair, f"{field}.resolution_pairs[{index}]")
        if prediction:
            _exact_fields(
                pair,
                required={"id", "predicted_merge"},
                field=f"{field}.resolution_pairs[{index}]",
            )
        else:
            _exact_fields(
                pair,
                required={"id", "left_mention_id", "right_mention_id", "should_merge"},
                field=f"{field}.resolution_pairs[{index}]",
            )
        pair_id = _identifier(pair["id"], f"{field}.resolution_pairs[{index}].id")
        if pair_id in seen_pair_ids:
            raise ValueError(f"{field} resolution pair IDs must be unique")
        seen_pair_ids.add(pair_id)
        decision_field = "predicted_merge" if prediction else "should_merge"
        if not isinstance(pair[decision_field], bool):
            raise TypeError(f"{field} resolution decision must be boolean")
        if not prediction:
            left = _identifier(pair["left_mention_id"], f"{field}.left_mention_id")
            right = _identifier(pair["right_mention_id"], f"{field}.right_mention_id")
            if left == right or left not in mentions or right not in mentions:
                raise ValueError(f"{field} resolution pair endpoints are invalid")
        resolution_pairs.append(pair)

    assert ontology is not None
    semantic_violations: list[str] = []
    evidence_violations: list[str] = []
    for family in FAMILIES:
        for artifact in artifacts[family]:
            if not _artifact_schema_valid(
                family, artifact, ontology=ontology, mentions=mentions
            ):
                semantic_violations.append(artifact["id"])
            if not _evidence_valid(artifact["evidence"], current_context):
                evidence_violations.append(artifact["id"])
    if not prediction and (semantic_violations or evidence_violations):
        raise ValueError(
            f"{field} gold violates ontology/evidence: "
            f"schema={semantic_violations}, evidence={evidence_violations}"
        )
    if (
        not prediction
        and current_context["case_class"] in REQUIRED_NEGATIVE_CLASSES
        and (any(artifacts[family] for family in FAMILIES) or resolution_pairs)
    ):
        raise ValueError(f"{field} negative/security gold must be artifact-free")
    return {
        **current_context,
        "artifacts": artifacts,
        "resolution_pairs": tuple(resolution_pairs),
        "schema_violations": tuple(sorted(semantic_violations)),
        "evidence_violations": tuple(sorted(evidence_violations)),
    }


def _validate_inputs(
    gold: Mapping[str, Any], predictions: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    checked_policy = _validate_policy(policy)
    _exact_fields(
        gold,
        required={
            "schema_version",
            "dataset_id",
            "version",
            "contains_predictions",
            "adjudication",
            "ontology",
            "cases",
        },
        field="knowledge gold",
    )
    if gold.get("schema_version") != KNOWLEDGE_GOLD_SCHEMA_VERSION:
        raise ValueError("knowledge gold schema is invalid")
    if gold.get("contains_predictions") is not False:
        raise ValueError("knowledge gold must declare contains_predictions=false")
    adjudication = _object(gold.get("adjudication"), "gold.adjudication")
    _exact_fields(
        adjudication,
        required={"status", "protocol_version", "approved_case_ids"},
        field="gold.adjudication",
    )
    if adjudication.get("status") != "approved":
        raise ValueError("knowledge gold adjudication must be approved")
    _text(adjudication.get("protocol_version"), "gold.adjudication.protocol_version")
    dataset_id = _identifier(gold.get("dataset_id"), "gold.dataset_id")
    gold_version = _text(gold.get("version"), "gold.version")
    ontology = _validate_ontology(gold.get("ontology"))
    gold_values = _list(gold.get("cases"), "gold.cases")
    if not gold_values or len(gold_values) > checked_policy["limits"]["max_cases"]:
        raise ValueError("gold case count is empty or exceeds the configured bound")
    gold_cases: dict[str, dict[str, Any]] = {}
    total_chars = 0
    for index, raw in enumerate(gold_values):
        checked = _validate_case(
            raw,
            f"gold.cases[{index}]",
            prediction=False,
            ontology=ontology,
            context=None,
            limits=checked_policy["limits"],
        )
        if checked["case_id"] in gold_cases:
            raise ValueError("gold case IDs must be unique")
        gold_cases[checked["case_id"]] = checked
        total_chars += len(checked["chunk_text"])
    if total_chars > checked_policy["limits"]["max_total_text_chars"]:
        raise ValueError("gold text exceeds the configured total bound")
    present_classes = {case["case_class"] for case in gold_cases.values()}
    if not REQUIRED_NEGATIVE_CLASSES <= present_classes:
        raise ValueError("gold must include negative and security cases")
    approved_values = _list(
        adjudication.get("approved_case_ids"),
        "gold.adjudication.approved_case_ids",
    )
    approved_case_ids = [
        _identifier(value, "gold.adjudication.approved_case_id")
        for value in approved_values
    ]
    if len(set(approved_case_ids)) != len(approved_case_ids) or set(
        approved_case_ids
    ) != set(gold_cases):
        raise ValueError("gold adjudication must approve every case exactly once")

    _exact_fields(
        predictions,
        required={
            "schema_version",
            "dataset_id",
            "gold_version",
            "extractor_version",
            "cases",
        },
        field="knowledge predictions",
    )
    if predictions.get("schema_version") != KNOWLEDGE_PREDICTION_SCHEMA_VERSION:
        raise ValueError("knowledge prediction schema is invalid")
    if (
        predictions.get("dataset_id") != dataset_id
        or predictions.get("gold_version") != gold_version
    ):
        raise ValueError("predictions do not bind the exact gold dataset version")
    extractor_version = _text(
        predictions.get("extractor_version"), "predictions.extractor_version"
    )
    prediction_values = _list(predictions.get("cases"), "predictions.cases")
    prediction_cases: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(prediction_values):
        raw_mapping = _object(raw, f"predictions.cases[{index}]")
        case_id = _identifier(
            raw_mapping.get("case_id"), f"predictions.cases[{index}].case_id"
        )
        if case_id not in gold_cases:
            raise ValueError(f"predictions contain unknown case {case_id}")
        checked = _validate_case(
            raw_mapping,
            f"predictions.cases[{index}]",
            prediction=True,
            ontology=ontology,
            context=gold_cases[case_id],
            limits=checked_policy["limits"],
        )
        if case_id in prediction_cases:
            raise ValueError("prediction case IDs must be unique")
        prediction_cases[case_id] = checked
    if set(prediction_cases) != set(gold_cases):
        missing = sorted(set(gold_cases) - set(prediction_cases))
        raise ValueError(f"prediction case coverage is incomplete: missing={missing}")

    allowed_type_keys = {
        *(f"entity:{name}" for name in ontology["entities"]),
        *(f"relationship:{name}" for name in ontology["relationships"]),
        *(f"property:{name}" for name in ontology["properties"]),
    }
    configured_type_keys = {
        f"{family}:{name}"
        for family, names in checked_policy["high_risk_types"].items()
        for name in names
    }
    configured_type_keys |= set(checked_policy["thresholds"]["per_type_min_f1"])
    unknown_policy_types = sorted(configured_type_keys - allowed_type_keys)
    if unknown_policy_types:
        raise ValueError(
            f"policy references unknown ontology types: {unknown_policy_types}"
        )
    return (
        {
            "dataset_id": dataset_id,
            "version": gold_version,
            "ontology": ontology,
            "cases": gold_cases,
            "digest": _digest(gold),
        },
        {
            "extractor_version": extractor_version,
            "cases": prediction_cases,
            "digest": _digest(predictions),
        },
        {**checked_policy, "digest": _digest(policy)},
    )


def _metric(
    expected: Counter[tuple[Any, ...]], predicted: Counter[tuple[Any, ...]]
) -> dict[str, Any]:
    true_positive = sum((expected & predicted).values())
    expected_count = sum(expected.values())
    predicted_count = sum(predicted.values())
    precision = (
        true_positive / predicted_count
        if predicted_count
        else (1.0 if not expected_count else 0.0)
    )
    recall = true_positive / expected_count if expected_count else 1.0
    f1 = (
        0.0
        if precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return {
        "expected": expected_count,
        "predicted": predicted_count,
        "true_positive": true_positive,
        "false_positive": predicted_count - true_positive,
        "false_negative": expected_count - true_positive,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def _extraction_metrics(
    gold_cases: Mapping[str, Mapping[str, Any]],
    prediction_cases: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    gold_by_family: dict[str, Counter[tuple[Any, ...]]] = {
        family: Counter() for family in FAMILIES
    }
    predicted_by_family: dict[str, Counter[tuple[Any, ...]]] = {
        family: Counter() for family in FAMILIES
    }
    gold_by_type: dict[str, Counter[tuple[Any, ...]]] = defaultdict(Counter)
    predicted_by_type: dict[str, Counter[tuple[Any, ...]]] = defaultdict(Counter)
    class_false_positives = {case_class: 0 for case_class in CASE_CLASSES}
    for case_id in sorted(gold_cases):
        gold_case = gold_cases[case_id]
        predicted_case = prediction_cases[case_id]
        for family in FAMILIES:
            for artifact in gold_case["artifacts"][family]:
                key = (case_id, family, *_artifact_key(family, artifact))
                type_key = f"{family}:{_artifact_type(family, artifact)}"
                gold_by_family[family][key] += 1
                gold_by_type[type_key][key] += 1
            expected = Counter(
                (case_id, family, *_artifact_key(family, artifact))
                for artifact in gold_case["artifacts"][family]
            )
            predicted = Counter(
                (case_id, family, *_artifact_key(family, artifact))
                for artifact in predicted_case["artifacts"][family]
            )
            class_false_positives[gold_case["case_class"]] += sum(
                (predicted - expected).values()
            )
            for artifact in predicted_case["artifacts"][family]:
                key = (case_id, family, *_artifact_key(family, artifact))
                type_key = f"{family}:{_artifact_type(family, artifact)}"
                predicted_by_family[family][key] += 1
                predicted_by_type[type_key][key] += 1
    by_family = {
        family: _metric(gold_by_family[family], predicted_by_family[family])
        for family in FAMILIES
    }
    all_gold: Counter[tuple[Any, ...]] = Counter()
    all_predicted: Counter[tuple[Any, ...]] = Counter()
    for family in FAMILIES:
        all_gold.update(gold_by_family[family])
        all_predicted.update(predicted_by_family[family])
    type_keys = sorted(set(gold_by_type) | set(predicted_by_type))
    return (
        {
            "overall": _metric(all_gold, all_predicted),
            "by_family": by_family,
            "per_type": {
                key: _metric(gold_by_type[key], predicted_by_type[key])
                for key in type_keys
            },
        },
        class_false_positives,
    )


def _resolution_metrics(
    gold_cases: Mapping[str, Mapping[str, Any]],
    prediction_cases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    positives = negatives = false_merges = missed_merges = 0
    for case_id in sorted(gold_cases):
        gold_pairs = {
            item["id"]: item for item in gold_cases[case_id]["resolution_pairs"]
        }
        predicted_pairs = {
            item["id"]: item for item in prediction_cases[case_id]["resolution_pairs"]
        }
        if set(gold_pairs) != set(predicted_pairs):
            missing = sorted(set(gold_pairs) - set(predicted_pairs))
            extra = sorted(set(predicted_pairs) - set(gold_pairs))
            raise ValueError(
                f"resolution pair coverage mismatch for {case_id}: missing={missing}, extra={extra}"
            )
        for pair_id in sorted(gold_pairs):
            expected = gold_pairs[pair_id]["should_merge"]
            predicted = predicted_pairs[pair_id]["predicted_merge"]
            if expected:
                positives += 1
                missed_merges += int(not predicted)
            else:
                negatives += 1
                false_merges += int(predicted)
    if positives == 0 or negatives == 0:
        raise ValueError(
            "gold must include positive and negative entity-resolution pairs"
        )
    return {
        "positive_pairs": positives,
        "negative_pairs": negatives,
        "false_merge_count": false_merges,
        "false_merge_rate": false_merges / negatives,
        "missed_merge_count": missed_merges,
        "missed_merge_rate": missed_merges / positives,
    }


def _governance_metrics(
    prediction_cases: Mapping[str, Mapping[str, Any]], policy: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    artifacts: list[tuple[str, str, Mapping[str, Any]]] = []
    schema_ids: list[str] = []
    evidence_ids: list[str] = []
    for case_id, case in prediction_cases.items():
        schema_ids.extend(
            f"{case_id}/{item_id}" for item_id in case["schema_violations"]
        )
        evidence_ids.extend(
            f"{case_id}/{item_id}" for item_id in case["evidence_violations"]
        )
        for family in FAMILIES:
            artifacts.extend(
                (case_id, family, item) for item in case["artifacts"][family]
            )
    total = len(artifacts)
    contaminated = [
        f"{case_id}/{item['id']}"
        for case_id, _, item in artifacts
        if item["origin"] != "expert" and item["authority_level"] == "authoritative"
    ]
    violations = {
        "artifact_count": total,
        "schema_violation_count": len(schema_ids),
        "schema_violation_rate": _rate(len(schema_ids), total),
        "evidence_violation_count": len(evidence_ids),
        "evidence_violation_rate": _rate(len(evidence_ids), total),
        "authority_contamination_count": len(contaminated),
        "authority_contamination_rate": _rate(len(contaminated), total),
    }

    status_counts = Counter(item["review_status"] for _, _, item in artifacts)
    high_risk = {
        f"{family}:{name}"
        for family, names in policy["high_risk_types"].items()
        for name in names
    }
    high_risk_items = [
        (case_id, item)
        for case_id, family, item in artifacts
        if f"{family}:{_artifact_type(family, item)}" in high_risk
    ]
    high_risk_pending = [
        f"{case_id}/{item['id']}"
        for case_id, item in high_risk_items
        if item["review_status"] == "pending"
    ]
    low_risk_items = [
        item
        for _, family, item in artifacts
        if f"{family}:{_artifact_type(family, item)}" not in high_risk
    ]
    low_risk_reviewed = sum(
        item["review_status"] != "pending" for item in low_risk_items
    )
    review = {
        "artifact_count": total,
        "approved_count": status_counts["approved"],
        "approved_rate": _rate(status_counts["approved"], total),
        "rejected_count": status_counts["rejected"],
        "rejected_rate": _rate(status_counts["rejected"], total),
        "quarantined_count": status_counts["quarantined"],
        "quarantined_rate": _rate(status_counts["quarantined"], total),
        "pending_count": status_counts["pending"],
        "pending_rate": _rate(status_counts["pending"], total),
        "high_risk_count": len(high_risk_items),
        "high_risk_pending_count": len(high_risk_pending),
        "low_risk_count": len(low_risk_items),
        "low_risk_reviewed_count": low_risk_reviewed,
        "low_risk_review_sample_rate": _rate(low_risk_reviewed, len(low_risk_items)),
    }
    diagnostics = {
        "schema_violation_ids": sorted(schema_ids),
        "evidence_violation_ids": sorted(evidence_ids),
        "authority_contamination_ids": sorted(contaminated),
        "high_risk_pending_ids": sorted(high_risk_pending),
    }
    return violations, review, diagnostics


def _threshold_failures(
    metrics: Mapping[str, Any],
    violations: Mapping[str, Any],
    resolution: Mapping[str, Any],
    review: Mapping[str, Any],
    class_false_positives: Mapping[str, int],
    policy: Mapping[str, Any],
) -> list[str]:
    thresholds = policy["thresholds"]
    failures: list[str] = []
    minimums = {
        "overall": (metrics["overall"]["f1"], thresholds["min_overall_f1"]),
        "entity": (metrics["by_family"]["entity"]["f1"], thresholds["min_entity_f1"]),
        "relationship": (
            metrics["by_family"]["relationship"]["f1"],
            thresholds["min_relationship_f1"],
        ),
        "property": (
            metrics["by_family"]["property"]["f1"],
            thresholds["min_property_f1"],
        ),
    }
    for name, (observed, target) in minimums.items():
        if observed < target:
            failures.append(f"{name} F1 {observed:.6f} is below {target:.6f}")
    maximums = {
        "schema violation rate": (
            violations["schema_violation_rate"],
            thresholds["max_schema_violation_rate"],
        ),
        "evidence violation rate": (
            violations["evidence_violation_rate"],
            thresholds["max_evidence_violation_rate"],
        ),
        "authority contamination rate": (
            violations["authority_contamination_rate"],
            thresholds["max_authority_contamination_rate"],
        ),
        "resolution false-merge rate": (
            resolution["false_merge_rate"],
            thresholds["max_resolution_false_merge_rate"],
        ),
        "resolution missed-merge rate": (
            resolution["missed_merge_rate"],
            thresholds["max_resolution_missed_merge_rate"],
        ),
    }
    for name, (observed, target) in maximums.items():
        if observed > target:
            failures.append(f"{name} {observed:.6f} exceeds {target:.6f}")
    for type_key, target in thresholds["per_type_min_f1"].items():
        observed = metrics["per_type"].get(type_key, {}).get("f1", 0.0)
        if observed < target:
            failures.append(f"{type_key} F1 {observed:.6f} is below {target:.6f}")
    if review["high_risk_pending_count"]:
        failures.append("high-risk artifacts require a terminal human review decision")
    if (
        review["low_risk_count"]
        and review["low_risk_review_sample_rate"]
        < thresholds["min_low_risk_review_sample_rate"]
    ):
        failures.append(
            "low-risk human-review sample rate "
            f"{review['low_risk_review_sample_rate']:.6f} is below "
            f"{thresholds['min_low_risk_review_sample_rate']:.6f}"
        )
    if class_false_positives["security"]:
        failures.append("security cases produced extracted artifacts")
    return failures


def _baseline_projection(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "overall_f1": report["extraction"]["overall"]["f1"],
        "family_f1": {
            family: report["extraction"]["by_family"][family]["f1"]
            for family in FAMILIES
        },
        "per_type_f1": {
            key: value["f1"]
            for key, value in sorted(report["extraction"]["per_type"].items())
        },
        "schema_violation_rate": report["violations"]["schema_violation_rate"],
        "evidence_violation_rate": report["violations"]["evidence_violation_rate"],
        "authority_contamination_rate": report["violations"][
            "authority_contamination_rate"
        ],
        "resolution_false_merge_rate": report["resolution"]["false_merge_rate"],
        "resolution_missed_merge_rate": report["resolution"]["missed_merge_rate"],
        "review_rejected_rate": report["review"]["rejected_rate"],
        "review_quarantined_rate": report["review"]["quarantined_rate"],
    }


def knowledge_baseline_candidate(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return an explicitly *unlocked* candidate requiring human approval."""

    if report.get("schema_version") != KNOWLEDGE_REPORT_SCHEMA_VERSION:
        raise ValueError("knowledge quality report schema is invalid")
    identity = _object(report.get("identity"), "report.identity")
    return {
        "schema_version": KNOWLEDGE_BASELINE_SCHEMA_VERSION,
        "version": "1.0.0",
        "locked": False,
        "dataset_id": identity["dataset_id"],
        "gold_version": identity["gold_version"],
        "gold_digest": identity["gold_digest"],
        "policy_version": identity["policy_version"],
        "policy_digest": identity["policy_digest"],
        "metrics": _baseline_projection(report),
    }


def _drift_failures(
    baseline: Mapping[str, Any] | None,
    *,
    identity: Mapping[str, Any],
    projection: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if baseline is None:
        return {"compared": False, "baseline_version": None, "deltas": {}}, [
            "a locked knowledge-quality baseline is required"
        ]
    _exact_fields(
        baseline,
        required={
            "schema_version",
            "version",
            "locked",
            "dataset_id",
            "gold_version",
            "gold_digest",
            "policy_version",
            "policy_digest",
            "metrics",
        },
        field="knowledge quality baseline",
    )
    if baseline.get("schema_version") != KNOWLEDGE_BASELINE_SCHEMA_VERSION:
        raise ValueError("knowledge quality baseline schema is invalid")
    baseline_version = _text(baseline.get("version"), "baseline.version")
    if baseline.get("locked") is not True:
        raise ValueError("knowledge quality baseline must be explicitly locked")
    for key in (
        "dataset_id",
        "gold_version",
        "gold_digest",
        "policy_version",
        "policy_digest",
    ):
        if baseline.get(key) != identity[key]:
            raise ValueError(f"knowledge quality baseline {key} is stale")
    expected = _object(baseline.get("metrics"), "baseline.metrics")
    required_fields = set(projection)
    _exact_fields(expected, required=required_fields, field="baseline.metrics")
    family_f1 = _object(expected["family_f1"], "baseline.metrics.family_f1")
    per_type_f1 = _object(expected["per_type_f1"], "baseline.metrics.per_type_f1")
    if set(family_f1) != set(FAMILIES):
        raise ValueError("baseline family metrics are incomplete")
    scalar_fields = required_fields - {"family_f1", "per_type_f1"}
    checked_expected: dict[str, Any] = {
        field: _ratio(expected[field], f"baseline.metrics.{field}")
        for field in scalar_fields
    }
    checked_expected["family_f1"] = {
        family: _ratio(family_f1[family], f"baseline.metrics.family_f1.{family}")
        for family in FAMILIES
    }
    checked_expected["per_type_f1"] = {
        _type_key(key, "baseline per-type key"): _ratio(
            value, f"baseline.metrics.per_type_f1.{key}"
        )
        for key, value in sorted(per_type_f1.items())
    }

    drift = policy["drift"]
    failures: list[str] = []
    deltas: dict[str, Any] = {}
    baseline_types = set(checked_expected["per_type_f1"])
    observed_types = set(projection["per_type_f1"])
    missing_types = sorted(baseline_types - observed_types)
    new_types = sorted(observed_types - baseline_types)
    if missing_types or new_types:
        failures.append(
            "per-type metric inventory changed: "
            f"missing={missing_types}, new={new_types}"
        )
    deltas["per_type_inventory"] = {
        "missing": missing_types,
        "new": new_types,
    }
    f1_drop = checked_expected["overall_f1"] - projection["overall_f1"]
    deltas["overall_f1_drop"] = f1_drop
    if f1_drop > drift["max_f1_drop"]:
        failures.append(
            f"overall F1 drift {f1_drop:.6f} exceeds {drift['max_f1_drop']:.6f}"
        )
    family_drops = {
        family: checked_expected["family_f1"][family] - projection["family_f1"][family]
        for family in FAMILIES
    }
    per_type_drops = {
        key: checked_expected["per_type_f1"][key]
        - projection["per_type_f1"].get(key, 0.0)
        for key in sorted(checked_expected["per_type_f1"])
    }
    deltas["family_f1_drop"] = family_drops
    deltas["per_type_f1_drop"] = per_type_drops
    for key, drop in sorted({**family_drops, **per_type_drops}.items()):
        if drop > drift["max_per_type_f1_drop"]:
            failures.append(
                f"{key} F1 drift {drop:.6f} exceeds {drift['max_per_type_f1_drop']:.6f}"
            )
    increase_specs = {
        "schema_violation_rate": "max_schema_violation_rate_increase",
        "evidence_violation_rate": "max_evidence_violation_rate_increase",
        "authority_contamination_rate": "max_authority_contamination_rate_increase",
        "resolution_false_merge_rate": "max_resolution_false_merge_rate_increase",
        "resolution_missed_merge_rate": "max_resolution_missed_merge_rate_increase",
        "review_rejected_rate": "max_review_reject_rate_increase",
        "review_quarantined_rate": "max_review_quarantine_rate_increase",
    }
    increases: dict[str, float] = {}
    for metric_name, threshold_name in increase_specs.items():
        increase = projection[metric_name] - checked_expected[metric_name]
        increases[metric_name] = increase
        if increase > drift[threshold_name]:
            failures.append(
                f"{metric_name} drift {increase:.6f} exceeds {drift[threshold_name]:.6f}"
            )
    deltas["rate_increase"] = increases
    return {
        "compared": True,
        "baseline_version": baseline_version,
        "deltas": deltas,
    }, failures


def build_knowledge_quality_report(
    *,
    gold: Mapping[str, Any],
    predictions: Mapping[str, Any],
    policy: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one deterministic, serializable, fail-closed quality report."""

    checked_gold, checked_predictions, checked_policy = _validate_inputs(
        gold, predictions, policy
    )
    extraction, class_false_positives = _extraction_metrics(
        checked_gold["cases"], checked_predictions["cases"]
    )
    resolution = _resolution_metrics(
        checked_gold["cases"], checked_predictions["cases"]
    )
    violations, review, diagnostics = _governance_metrics(
        checked_predictions["cases"], checked_policy
    )
    case_class_counts = Counter(
        case["case_class"] for case in checked_gold["cases"].values()
    )
    identity = {
        "dataset_id": checked_gold["dataset_id"],
        "gold_version": checked_gold["version"],
        "gold_digest": checked_gold["digest"],
        "prediction_digest": checked_predictions["digest"],
        "extractor_version": checked_predictions["extractor_version"],
        "policy_version": checked_policy["version"],
        "policy_digest": checked_policy["digest"],
    }
    report: dict[str, Any] = {
        "schema_version": KNOWLEDGE_REPORT_SCHEMA_VERSION,
        "identity": identity,
        "coverage": {
            "gold_case_count": len(checked_gold["cases"]),
            "prediction_case_count": len(checked_predictions["cases"]),
            "case_class_counts": {
                key: case_class_counts[key] for key in sorted(CASE_CLASSES)
            },
            "case_class_false_positive_counts": {
                key: class_false_positives[key] for key in sorted(CASE_CLASSES)
            },
            "negative_and_security_complete": True,
        },
        "extraction": extraction,
        "violations": violations,
        "review": review,
        "resolution": resolution,
        "diagnostics": diagnostics,
    }
    failures = _threshold_failures(
        extraction,
        violations,
        resolution,
        review,
        class_false_positives,
        checked_policy,
    )
    projection = _baseline_projection(report)
    drift, drift_failures = _drift_failures(
        baseline,
        identity=identity,
        projection=projection,
        policy=checked_policy,
    )
    failures.extend(drift_failures)
    report["drift"] = drift
    report["failures"] = sorted(set(failures))
    report["passed"] = not report["failures"]
    report["report_digest"] = _digest(report)
    return report


__all__ = [
    "KNOWLEDGE_BASELINE_SCHEMA_VERSION",
    "KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION",
    "KNOWLEDGE_GOLD_SCHEMA_VERSION",
    "KNOWLEDGE_PREDICTION_SCHEMA_VERSION",
    "KNOWLEDGE_REPORT_SCHEMA_VERSION",
    "build_knowledge_quality_report",
    "knowledge_baseline_candidate",
]
