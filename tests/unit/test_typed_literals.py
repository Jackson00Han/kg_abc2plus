"""Typed literal, unit, temporal, and flat persistence invariants."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import unittest

from graphrag_prod.construction.literals import (
    LiteralNormalizationError,
    TBoxLiteralNormalizer,
)
from graphrag_prod.domain import TypedLiteralValue
from graphrag_prod.ontology import (
    Cardinality,
    PropertyDataType,
    PropertyDefinition,
)


def _property(
    datatype: PropertyDataType,
    *,
    unit: str | None = None,
) -> PropertyDefinition:
    return PropertyDefinition(
        name="measuredValue",
        datatype=datatype,
        required=False,
        cardinality=Cardinality.ZERO_OR_ONE,
        unit=unit,
    )


class TypedLiteralNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = TBoxLiteralNormalizer()

    def _normalize(
        self,
        datatype: PropertyDataType,
        raw_value: str,
        *,
        definition_unit: str | None = None,
        raw_unit: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        observed_at: str | None = None,
    ) -> TypedLiteralValue:
        return self.normalizer.normalize(
            _property(datatype, unit=definition_unit),
            raw_value=raw_value,
            raw_unit=raw_unit,
            valid_from=valid_from,
            valid_to=valid_to,
            observed_at=observed_at,
        )

    def test_decimal_unit_conversion_is_precise_and_deterministic(self) -> None:
        first = self._normalize(
            PropertyDataType.DECIMAL,
            "100",
            definition_unit="kPa",
            raw_unit="psi",
        )
        second = self._normalize(
            PropertyDataType.DECIMAL,
            "100",
            definition_unit="kPa",
            raw_unit="psi",
        )

        self.assertEqual(
            first.canonical_value,
            "689.4757293168361336722673443",
        )
        self.assertEqual(first.typed_value, first.canonical_value)
        self.assertEqual(first.identity_reference, second.identity_reference)
        self.assertEqual(first.canonical_unit, "kPa")
        self.assertEqual(first.raw_unit, "psi")

    def test_supported_scalar_datatypes_are_normalized(self) -> None:
        cases = (
            (PropertyDataType.INTEGER, "42", 42, "42"),
            (PropertyDataType.FLOAT, "1.25", 1.25, "1.25"),
            (PropertyDataType.DECIMAL, "1.2300", "1.23", "1.23"),
            (PropertyDataType.BOOLEAN, "TRUE", True, "true"),
            (PropertyDataType.STRING, "Pump-7", "Pump-7", "Pump-7"),
            (PropertyDataType.DATE, "2025-03-04", "2025-03-04", "2025-03-04"),
            (
                PropertyDataType.DATETIME,
                "2025-03-04T10:30:00+08:00",
                "2025-03-04T02:30:00Z",
                "2025-03-04T02:30:00Z",
            ),
            (PropertyDataType.DURATION, "PT15M", "PT15M", "PT15M"),
            (
                PropertyDataType.URI,
                "https://example.com/assets/P-7",
                "https://example.com/assets/P-7",
                "https://example.com/assets/P-7",
            ),
            (
                PropertyDataType.JSON,
                '{"b":2,"a":1}',
                '{"a":1,"b":2}',
                '{"a":1,"b":2}',
            ),
        )
        for datatype, raw, typed, canonical in cases:
            with self.subTest(datatype=datatype):
                value = self._normalize(datatype, raw)
                self.assertEqual(value.typed_value, typed)
                self.assertEqual(value.canonical_value, canonical)

    def test_temporal_qualifiers_preserve_raw_and_canonical_values(self) -> None:
        value = self._normalize(
            PropertyDataType.DECIMAL,
            "2.5",
            valid_from="2025-01-01T08:00:00+08:00",
            valid_to="2025-02-01T00:00:00Z",
            observed_at="2025-01-02T12:34:56.120Z",
        )

        self.assertEqual(value.valid_from, datetime(2025, 1, 1, tzinfo=UTC))
        self.assertEqual(value.raw_valid_from, "2025-01-01T08:00:00+08:00")
        self.assertEqual(
            value.to_mapping()["observed_at"],
            "2025-01-02T12:34:56.120000Z",
        )

    def test_invalid_values_units_and_temporal_ranges_are_rejected(self) -> None:
        cases = (
            (
                lambda: self._normalize(PropertyDataType.INTEGER, "1.5"),
                "INVALID_LITERAL_VALUE",
            ),
            (
                lambda: self._normalize(PropertyDataType.FLOAT, "NaN"),
                "INVALID_LITERAL_VALUE",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.DECIMAL,
                    "5",
                    definition_unit="kPa",
                ),
                "MISSING_UNIT",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.DECIMAL,
                    "5",
                    raw_unit="psi",
                ),
                "UNEXPECTED_UNIT",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.DECIMAL,
                    "5",
                    definition_unit="kPa",
                    raw_unit="second",
                ),
                "INCOMPATIBLE_UNIT",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.DATE,
                    "20250101",
                ),
                "INVALID_LITERAL_VALUE",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.DATETIME,
                    "2025-01-01 00:00:00Z",
                ),
                "INVALID_TEMPORAL_QUALIFIER",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.STRING,
                    "running",
                    valid_from="2025-02-01T00:00:00Z",
                    valid_to="2025-01-01T00:00:00Z",
                ),
                "INVALID_TEMPORAL_RANGE",
            ),
            (
                lambda: self._normalize(
                    PropertyDataType.JSON,
                    '{"x":1,"x":2}',
                ),
                "INVALID_LITERAL_VALUE",
            ),
        )
        for operation, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(LiteralNormalizationError) as captured:
                    operation()
                self.assertEqual(captured.exception.code, expected_code)


class TypedLiteralValuePersistenceTests(unittest.TestCase):
    def test_mapping_and_flat_scalar_round_trips_are_lossless(self) -> None:
        value = TypedLiteralValue(
            datatype="DECIMAL",
            typed_value="12.5",
            raw_value="12500",
            raw_unit="Pa",
            canonical_value="12.5",
            canonical_unit="kPa",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=datetime(2026, 1, 1, tzinfo=UTC),
            observed_at=datetime(2025, 2, 1, tzinfo=UTC),
            raw_valid_from="2025-01-01T00:00:00Z",
            raw_valid_to="2026-01-01T00:00:00Z",
            raw_observed_at="2025-02-01T00:00:00Z",
        )

        self.assertEqual(TypedLiteralValue.from_mapping(value.to_mapping()), value)
        flat = value.to_flat_properties()
        self.assertTrue(all(not isinstance(item, (dict, list)) for item in flat.values()))
        self.assertEqual(TypedLiteralValue.from_flat_properties(flat), value)
        self.assertIsNone(TypedLiteralValue.from_flat_properties({"legacy": True}))

    def test_partial_flat_group_and_mutation_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required"):
            TypedLiteralValue.from_flat_properties({"literal_datatype": "DECIMAL"})

        value = TypedLiteralValue(
            datatype="INTEGER",
            typed_value=7,
            raw_value="7",
            canonical_value="7",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.canonical_value = "8"  # type: ignore[misc]

        with self.assertRaisesRegex(ValueError, "must match"):
            dataclasses.replace(value, canonical_value="8")

        with self.assertRaisesRegex(ValueError, "does not match"):
            TypedLiteralValue(
                datatype="STRING",
                typed_value="running",
                raw_value="running",
                canonical_value="running",
                observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                raw_observed_at="2025-01-02T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
