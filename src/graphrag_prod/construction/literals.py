"""Strict T-Box literal parsing, unit normalization, and temporal semantics."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit

import pint

from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.ontology.models import PropertyDataType, PropertyDefinition


_NUMBER = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_INTEGER = re.compile(r"^[+-]?(?:0|[1-9]\d*)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DURATION = re.compile(
    r"^P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?"
    r"(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
)
_UNIT_TEXT = re.compile(r"^[A-Za-z0-9_µμ°%./*^()+\- ]{1,64}$")
_WHITESPACE = re.compile(r"\s")
_UNIT_REGISTRY = pint.UnitRegistry(
    autoconvert_offset_to_baseunit=True,
    non_int_type=Decimal,
)


class LiteralNormalizationError(ValueError):
    """A model literal cannot safely satisfy its declared T-Box property."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _strict_json(value: str) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        value,
        object_pairs_hook=unique,
        parse_constant=reject_constant,
    )


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise LiteralNormalizationError(
            "INVALID_LITERAL_VALUE",
            "numeric literal must be finite",
        )
    if value == 0:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", "+0"} else text


def _decimal(value: str, *, integer: bool = False) -> Decimal:
    pattern = _INTEGER if integer else _NUMBER
    if pattern.fullmatch(value) is None:
        raise LiteralNormalizationError(
            "INVALID_LITERAL_VALUE",
            "numeric literal must use a locale-independent decimal representation",
        )
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise LiteralNormalizationError(
            "INVALID_LITERAL_VALUE",
            "numeric literal cannot be parsed",
        ) from exc
    if not result.is_finite():
        raise LiteralNormalizationError(
            "INVALID_LITERAL_VALUE",
            "numeric literal must be finite",
        )
    return result


def _instant(value: str, field_name: str) -> tuple[datetime, str]:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _RFC3339.fullmatch(value) is None
    ):
        raise LiteralNormalizationError(
            "INVALID_TEMPORAL_QUALIFIER",
            f"{field_name} must be an exact RFC3339 token",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise LiteralNormalizationError(
            "INVALID_TEMPORAL_QUALIFIER",
            f"{field_name} must be RFC3339 date-time text",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiteralNormalizationError(
            "INVALID_TEMPORAL_QUALIFIER",
            f"{field_name} must include an explicit UTC offset",
        )
    canonical = parsed.astimezone(UTC)
    return canonical, canonical.isoformat().replace("+00:00", "Z")


class TBoxLiteralNormalizer:
    """Normalize literals with a fixed Pint registry and no custom definitions."""

    def __init__(self) -> None:
        # Decimal arithmetic prevents binary-float drift in canonical unit values.
        self._units = _UNIT_REGISTRY

    def normalize(
        self,
        definition: PropertyDefinition,
        *,
        raw_value: str,
        raw_unit: str | None,
        valid_from: str | None,
        valid_to: str | None,
        observed_at: str | None,
    ) -> TypedLiteralValue:
        if not isinstance(definition, PropertyDefinition):
            raise TypeError("definition must be a PropertyDefinition")
        if (
            not isinstance(raw_value, str)
            or not raw_value
            or raw_value != raw_value.strip()
            or len(raw_value) > 4_096
        ):
            raise LiteralNormalizationError(
                "INVALID_LITERAL_VALUE",
                "raw_literal must be a bounded exact source token without edge whitespace",
            )
        if raw_unit is not None and (
            not isinstance(raw_unit, str)
            or raw_unit != raw_unit.strip()
            or _UNIT_TEXT.fullmatch(raw_unit) is None
        ):
            raise LiteralNormalizationError(
                "INVALID_UNIT",
                "unit must be a bounded exact source token",
            )

        datatype = definition.datatype
        canonical_unit = definition.unit
        if canonical_unit is None and raw_unit is not None:
            raise LiteralNormalizationError(
                "UNEXPECTED_UNIT",
                "the T-Box property does not declare a unit",
            )
        if canonical_unit is not None and raw_unit is None:
            raise LiteralNormalizationError(
                "MISSING_UNIT",
                "the T-Box property requires an explicit source unit",
            )

        typed_value, canonical_value = self._value(
            datatype,
            raw_value,
            raw_unit=raw_unit,
            canonical_unit=canonical_unit,
        )
        parsed_temporals: dict[str, datetime | None] = {}
        for name, value in (
            ("valid_from", valid_from),
            ("valid_to", valid_to),
            ("observed_at", observed_at),
        ):
            if value is None:
                parsed_temporals[name] = None
            else:
                parsed_temporals[name] = _instant(value, name)[0]
        start = parsed_temporals["valid_from"]
        end = parsed_temporals["valid_to"]
        if start is not None and end is not None and start >= end:
            raise LiteralNormalizationError(
                "INVALID_TEMPORAL_RANGE",
                "valid_from must be earlier than valid_to",
            )
        return TypedLiteralValue(
            datatype=datatype.value,
            typed_value=typed_value,
            raw_value=raw_value,
            raw_unit=raw_unit,
            canonical_value=canonical_value,
            canonical_unit=canonical_unit,
            valid_from=start,
            valid_to=end,
            observed_at=parsed_temporals["observed_at"],
            raw_valid_from=valid_from,
            raw_valid_to=valid_to,
            raw_observed_at=observed_at,
        )

    def validate_declared_unit(self, definition: PropertyDefinition) -> None:
        """Fail early when a published T-Box contains an unusable unit token."""

        if definition.unit is None:
            return
        if _UNIT_TEXT.fullmatch(definition.unit) is None:
            raise LiteralNormalizationError(
                "INVALID_CANONICAL_UNIT",
                f"T-Box unit {definition.unit!r} contains unsupported characters",
            )
        try:
            self._units.parse_units(definition.unit)
        except (pint.PintError, ValueError) as exc:
            raise LiteralNormalizationError(
                "INVALID_CANONICAL_UNIT",
                f"T-Box unit {definition.unit!r} is not recognized",
            ) from exc

    def _value(
        self,
        datatype: PropertyDataType,
        raw_value: str,
        *,
        raw_unit: str | None,
        canonical_unit: str | None,
    ) -> tuple[str | int | float | bool, str]:
        if datatype in {
            PropertyDataType.INTEGER,
            PropertyDataType.FLOAT,
            PropertyDataType.DECIMAL,
        }:
            magnitude = _decimal(raw_value, integer=datatype is PropertyDataType.INTEGER)
            if raw_unit is not None and canonical_unit is not None:
                try:
                    source = self._units.parse_units(raw_unit)
                    target = self._units.parse_units(canonical_unit)
                    magnitude = self._units.Quantity(magnitude, source).to(target).magnitude
                except pint.DimensionalityError as exc:
                    raise LiteralNormalizationError(
                        "INCOMPATIBLE_UNIT",
                        f"unit {raw_unit!r} is incompatible with {canonical_unit!r}",
                    ) from exc
                except (pint.PintError, ValueError, TypeError) as exc:
                    raise LiteralNormalizationError(
                        "INVALID_UNIT",
                        f"unit {raw_unit!r} cannot be converted to {canonical_unit!r}",
                    ) from exc
            canonical = _decimal_text(magnitude)
            if datatype is PropertyDataType.INTEGER:
                if magnitude != magnitude.to_integral_value():
                    raise LiteralNormalizationError(
                        "INVALID_LITERAL_VALUE",
                        "unit conversion does not yield an integer canonical value",
                    )
                return int(magnitude), str(int(magnitude))
            if datatype is PropertyDataType.FLOAT:
                floating = float(magnitude)
                if not math.isfinite(floating):
                    raise LiteralNormalizationError(
                        "INVALID_LITERAL_VALUE",
                        "float literal exceeds the finite storage range",
                    )
                return floating, canonical
            return canonical, canonical

        if raw_unit is not None or canonical_unit is not None:
            raise LiteralNormalizationError(
                "INVALID_UNIT",
                "units are allowed only for numeric T-Box datatypes",
            )
        if datatype is PropertyDataType.STRING:
            return raw_value, unicodedata.normalize("NFC", raw_value)
        if datatype is PropertyDataType.BOOLEAN:
            normalized = raw_value.casefold()
            if normalized not in {"true", "false"}:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "boolean literal must be true or false",
                )
            value = normalized == "true"
            return value, normalized
        if datatype is PropertyDataType.DATE:
            if _DATE.fullmatch(raw_value) is None:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "date literal must use ISO 8601 YYYY-MM-DD text",
                )
            try:
                parsed_date = date.fromisoformat(raw_value)
            except ValueError as exc:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "date literal must use ISO 8601 YYYY-MM-DD text",
                ) from exc
            canonical = parsed_date.isoformat()
            return canonical, canonical
        if datatype is PropertyDataType.DATETIME:
            parsed, canonical = _instant(raw_value, "raw_literal")
            del parsed
            return canonical, canonical
        if datatype is PropertyDataType.DURATION:
            if _DURATION.fullmatch(raw_value) is None:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "duration literal must be a bounded ISO 8601 duration",
                )
            return raw_value, raw_value
        if datatype is PropertyDataType.URI:
            if _WHITESPACE.search(raw_value) or not urlsplit(raw_value).scheme:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "URI literal must be absolute and contain no whitespace",
                )
            return raw_value, raw_value
        if datatype is PropertyDataType.JSON:
            try:
                decoded = _strict_json(raw_value)
            except (json.JSONDecodeError, ValueError, RecursionError) as exc:
                raise LiteralNormalizationError(
                    "INVALID_LITERAL_VALUE",
                    "JSON literal must be one strict finite JSON value",
                ) from exc
            canonical = json.dumps(
                decoded,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            return canonical, canonical
        raise LiteralNormalizationError(
            "INVALID_LITERAL_VALUE",
            f"unsupported T-Box datatype {datatype.value}",
        )


__all__ = ["LiteralNormalizationError", "TBoxLiteralNormalizer"]
