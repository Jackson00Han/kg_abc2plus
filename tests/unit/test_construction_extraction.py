"""Ontology-constrained OpenAI-compatible extraction tests with a fake client."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from graphrag_prod.construction import (
    ExtractionLimits,
    ExtractionQuarantined,
    ExtractionRejected,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.domain import content_checksum, pipeline_profile_id
from graphrag_prod.domain.models import Chunk, GraphPipelineProfile
from graphrag_prod.knowledge import AuthorityLevel, GovernanceStatus, KnowledgeOrigin
from graphrag_prod.ontology.models import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)


SOURCE = "Acme owns Pump-7."


def _tbox(*, status: TBoxStatus = TBoxStatus.PUBLISHED) -> TBoxVersion:
    return TBoxVersion(
        tenant_id="tenant-industrial",
        key="industrial-assets",
        version=1,
        status=status,
        entity_types=(
            EntityTypeDefinition("Company", ("company-id", "llm-candidate")),
            EntityTypeDefinition(
                "Asset",
                ("asset-id", "llm-candidate"),
                properties=(
                    PropertyDefinition(
                        "pressure",
                        PropertyDataType.DECIMAL,
                        False,
                        Cardinality.ZERO_OR_ONE,
                        "kPa",
                    ),
                    PropertyDefinition(
                        "serialNumber",
                        PropertyDataType.STRING,
                        False,
                        Cardinality.ZERO_OR_ONE,
                    ),
                ),
            ),
        ),
        relationship_types=(
            RelationshipTypeDefinition(
                "OWNS",
                ("Company",),
                ("Asset",),
                properties=(
                    PropertyDefinition(
                        "basis",
                        PropertyDataType.STRING,
                        False,
                        Cardinality.ZERO_OR_ONE,
                    ),
                ),
            ),
        ),
    )


def _profile() -> GraphPipelineProfile:
    values = (
        "unicode-nfc:v1",
        "bounded-boundary:v1",
        "qwen-ontology-extractor:v1",
        "industrial-extraction-prompt:v1",
        "industrial-assets:v1",
        "construction-core:v1",
    )
    return GraphPipelineProfile(pipeline_profile_id(*values), *values)


def _chunk() -> Chunk:
    return Chunk(
        chunk_id="chunk-industrial-1",
        version_id="version-industrial-1",
        document_id="document-industrial-1",
        tenant_id="tenant-industrial",
        access_policy_id="tenant-industrial:engineers",
        access_policy_version=1,
        access_groups=frozenset({"engineers"}),
        ordinal=1,
        text=SOURCE,
        checksum=content_checksum(SOURCE),
        char_start=100,
        char_end=100 + len(SOURCE),
        page_number=2,
        section="Asset ownership",
        splitter_version="bounded-boundary:v1",
    )


PROPERTY_SOURCE = (
    "Pump-7 pressure was 100 psi valid 2025-01-01T00:00:00Z to "
    "2026-01-01T00:00:00Z observed 2025-02-01T00:00:00Z."
)


def _property_chunk() -> Chunk:
    return Chunk(
        chunk_id="chunk-industrial-property-1",
        version_id="version-industrial-1",
        document_id="document-industrial-1",
        tenant_id="tenant-industrial",
        access_policy_id="tenant-industrial:engineers",
        access_policy_version=1,
        access_groups=frozenset({"engineers"}),
        ordinal=1,
        text=PROPERTY_SOURCE,
        checksum=content_checksum(PROPERTY_SOURCE),
        char_start=200,
        char_end=200 + len(PROPERTY_SOURCE),
        page_number=1,
        section="Telemetry",
        splitter_version="bounded-boundary:v1",
    )


def _property_payload() -> dict[str, object]:
    return {
        "entities": [
            {
                "ref": "asset",
                "type": "Asset",
                "mentions": [
                    {"text": "Pump-7", "start": 0, "end": 6, "confidence": 0.99}
                ],
            }
        ],
        "relationships": [],
        "property_facts": [
            {
                "entity_ref": "asset",
                "property": "pressure",
                "raw_literal": "100",
                "unit": "psi",
                "valid_from": "2025-01-01T00:00:00Z",
                "valid_to": "2026-01-01T00:00:00Z",
                "observed_at": "2025-02-01T00:00:00Z",
                "evidence": {
                    "text": PROPERTY_SOURCE,
                    "start": 0,
                    "end": len(PROPERTY_SOURCE),
                },
                "confidence": 0.97,
            }
        ],
    }


def _valid_payload() -> dict[str, object]:
    return {
        "entities": [
            {
                "ref": "company",
                "type": "Company",
                "mentions": [
                    {"text": "Acme", "start": 0, "end": 4, "confidence": 0.97}
                ],
            },
            {
                "ref": "asset",
                "type": "Asset",
                "mentions": [
                    {
                        "text": "Pump-7",
                        "start": 10,
                        "end": 16,
                        "confidence": 0.96,
                    }
                ],
            },
        ],
        "relationships": [
            {
                "type": "OWNS",
                "source_ref": "company",
                "target_ref": "asset",
                "evidence": {"text": SOURCE, "start": 0, "end": len(SOURCE)},
                "confidence": 0.95,
            }
        ],
        "property_facts": [],
    }


class FakeCompletions:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self.payload)},
                }
            ]
        }


def _extractor(
    payload: object,
    *,
    limits: ExtractionLimits | None = None,
    response_format_mode: str = "schema",
    enable_thinking: bool | None = None,
    include_span_hints: bool = False,
) -> tuple[OpenAICompatibleOntologyExtractor, FakeCompletions]:
    completions = FakeCompletions(payload)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    extractor = OpenAICompatibleOntologyExtractor(
        client=client,
        model="qwen-plus",
        active_tbox=_tbox(),
        prompt_version="industrial-extraction-prompt:v1",
        limits=limits,
        response_format_mode=response_format_mode,  # type: ignore[arg-type]
        enable_thinking=enable_thinking,
        include_span_hints=include_span_hints,
    )
    return extractor, completions


class ConstructionExtractionTests(unittest.TestCase):
    def test_thinking_control_is_explicit_and_provider_neutral_by_default(self) -> None:
        for thinking in (None, False, True):
            with self.subTest(thinking=thinking):
                extractor, calls = _extractor(_valid_payload(), enable_thinking=thinking)
                extractor(artifact_id="a", input_hash="i", chunk=_chunk(), profile=_profile())
                request = calls.calls[0]
                if thinking is None:
                    self.assertNotIn("extra_body", request)
                else:
                    self.assertEqual(request["extra_body"], {"enable_thinking": thinking})
                self.assertNotIn("api_key", request)

    def test_runtime_policy_options_require_actual_booleans(self) -> None:
        for option, invalids in (
            ("enable_thinking", (0, 1, "false", {})),
            ("include_span_hints", (None, 0, 1, "true", {})),
        ):
            for invalid in invalids:
                with self.subTest(option=option, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, option):
                        _extractor(_valid_payload(), **{option: invalid})

    def test_request_policy_signature_is_stable_complete_and_secret_free(self) -> None:
        first, _ = _extractor(_valid_payload())
        same, _ = _extractor(_valid_payload())
        self.assertEqual(first.request_policy_signature, same.request_policy_signature)
        self.assertRegex(first.request_policy_signature, r"^[0-9a-f]{64}$")
        for option, value in (
            ("model", "qwen3.8-max"), ("prompt_version", "next"),
            ("seed", None), ("response_format_mode", "none"),
            ("enable_thinking", False), ("include_span_hints", True),
            ("limits", ExtractionLimits(max_output_tokens=2048)),
        ):
            changed, _ = _extractor(_valid_payload())
            setattr(changed, option, value)
            self.assertNotEqual(first.request_policy_signature, changed.request_policy_signature)

    def test_span_hints_are_exact_chunk_relative_unicode_coordinates(self) -> None:
        text = "泵-7 🏭\nPump-7 and Pump-7."
        chunk = replace(_chunk(), text=text, char_end=100 + len(text), checksum=content_checksum(text))
        extractor, _ = _extractor(_valid_payload(), include_span_hints=True)
        source = json.loads(extractor._messages(chunk, response_schema=extractor.response_schema())[1]["content"])
        self.assertEqual(source["chunk_text"], text)
        spans = source["chunk_token_spans"]
        self.assertLessEqual(len(spans), len(text))
        for span in spans:
            self.assertEqual(set(span), {"text", "start", "end"})
            self.assertEqual(span["text"], text[span["start"]:span["end"]])
        self.assertEqual([s["start"] for s in spans if s["text"] == "Pump"], [6, 17])
        self.assertEqual("".join(s["text"] for s in spans), "".join(text.split()))
        # Continuous CJK text must not hide an equipment ID or place boundary
        # inside one giant Unicode-word token.
        chinese = "设备ZX58安装于上海工厂，额定功率为13kW。"
        chunk = replace(chunk, text=chinese, char_end=100 + len(chinese), checksum=content_checksum(chinese))
        messages = extractor._messages(chunk, response_schema=extractor.response_schema())
        spans = json.loads(messages[1]["content"])["chunk_token_spans"]
        self.assertIn({"text": "ZX58", "start": 2, "end": 6}, spans)
        starts, ends = {s["start"] for s in spans}, {s["end"] for s in spans}
        for term in ("上海工厂", "额定功率", "13kW"):
            start = chinese.index(term)
            self.assertIn(start, starts)
            self.assertIn(start + len(term), ends)
        self.assertIn("do not restrict valid spans inside a token", messages[0]["content"])

    def test_span_hints_do_not_repair_or_accept_wrong_model_evidence(self) -> None:
        payload = _valid_payload()
        payload["entities"][0]["mentions"][0]["start"] = 1
        extractor, _ = _extractor(payload, enable_thinking=False, include_span_hints=True)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(artifact_id="a", input_hash="i", chunk=_chunk(), profile=_profile())
        self.assertIn("MENTION_SPAN_MISMATCH", {f.code for f in captured.exception.findings})

    def test_real_sdk_timeout_has_distinct_redacted_finding(self) -> None:
        import httpx
        from openai import APITimeoutError
        from unittest.mock import Mock

        extractor, _ = _extractor(_valid_payload())
        extractor.client = Mock()
        extractor.client.chat.completions.create.side_effect = APITimeoutError(
            request=httpx.Request("POST", "https://example.invalid/private-document")
        )
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(artifact_id="a", input_hash="i", chunk=_chunk(), profile=_profile())
        self.assertEqual([f.code for f in captured.exception.findings], ["MODEL_CALL_TIMEOUT"])
        self.assertNotIn("private-document", str(captured.exception.findings))

    def test_valid_response_becomes_secondary_candidate_with_server_ids(self) -> None:
        extractor, completions = _extractor(_valid_payload())
        result = extractor.extract_audited(
            artifact_id="artifact-1",
            input_hash="input-1",
            chunk=_chunk(),
            profile=_profile(),
        )

        self.assertEqual(result.origin, KnowledgeOrigin.LLM_EXTRACTED)
        self.assertEqual(result.authority, AuthorityLevel.SECONDARY)
        self.assertEqual(result.status, GovernanceStatus.CANDIDATE)
        self.assertEqual(result.ontology_version_id, _tbox().tbox_id)
        self.assertEqual(len(result.output.entities), 2)
        self.assertEqual(
            [(mention.char_start, mention.char_end) for mention in result.output.mentions],
            [(100, 104), (110, 116)],
        )
        assertion = result.output.assertions[0]
        self.assertEqual((assertion.evidence_char_start, assertion.evidence_char_end), (100, 117))
        self.assertFalse(assertion.accepted)
        self.assertTrue(
            all(
                entity.canonical_key.startswith("llm-candidate:")
                for entity in result.output.entities
            )
        )

        request = completions.calls[0]
        self.assertEqual(request["timeout"], 60.0)
        schema = request["response_format"]["json_schema"]["schema"]  # type: ignore[index]
        self.assertEqual(
            schema["properties"]["entities"]["items"]["properties"]["type"]["enum"],  # type: ignore[index]
            ["Company", "Asset"],
        )
        prompt = request["messages"][0]["content"]  # type: ignore[index]
        self.assertIn("Never return database IDs", prompt)
        self.assertNotIn("api_key", request)

    def test_relationship_property_is_typed_and_bound_to_own_exact_span(self) -> None:
        payload = _valid_payload()
        payload["relationships"][0]["properties"] = [  # type: ignore[index]
            {
                "property": "basis",
                "raw_literal": "owns",
                "unit": None,
                "valid_from": None,
                "valid_to": None,
                "observed_at": None,
                "evidence": {"text": "owns", "start": 5, "end": 9},
                "confidence": 0.94,
            }
        ]
        extractor, _ = _extractor(payload)
        result = extractor.extract_audited(
            artifact_id="artifact-relationship-property",
            input_hash="input-relationship-property",
            chunk=_chunk(),
            profile=_profile(),
        )
        value = result.output.assertions[0].relationship_properties[0]
        self.assertEqual(value.name, "basis")
        self.assertEqual(value.literal_semantics.canonical_value, "owns")
        self.assertEqual((value.evidence_char_start, value.evidence_char_end), (105, 109))
        self.assertEqual(value.evidence_text, "owns")
        self.assertEqual(value.confidence, 0.94)

        forged = copy.deepcopy(payload)
        forged["relationships"][0]["properties"][0]["evidence"] = {  # type: ignore[index]
            "text": "Acme",
            "start": 0,
            "end": 4,
        }
        extractor, _ = _extractor(forged)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-forged-property",
                input_hash="input-forged-property",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn(
            "FACT_TOKEN_OUTSIDE_EVIDENCE",
            {item.code for item in captured.exception.findings},
        )

    def test_provider_neutral_response_format_modes_are_explicit(self) -> None:
        for mode, expected_format in (
            ("schema", "json_schema"),
            ("json_object", "json_object"),
            ("none", None),
        ):
            with self.subTest(mode=mode):
                extractor, completions = _extractor(
                    _valid_payload(),
                    response_format_mode=mode,
                )
                extractor.extract_audited(
                    artifact_id=f"artifact-{mode}",
                    input_hash=f"input-{mode}",
                    chunk=_chunk(),
                    profile=_profile(),
                )
                request = completions.calls[0]
                if expected_format is None:
                    self.assertNotIn("response_format", request)
                else:
                    self.assertEqual(
                        request["response_format"]["type"],  # type: ignore[index]
                        expected_format,
                    )
                prompt = request["messages"][0]["content"]  # type: ignore[index]
                if mode == "schema":
                    self.assertNotIn("\nresponse_schema=", prompt)
                else:
                    marker = "\nresponse_schema="
                    self.assertIn(marker, prompt)
                    embedded = json.loads(prompt.split(marker, 1)[1])
                    self.assertEqual(embedded, extractor.response_schema())

    def test_response_format_mode_rejects_unknown_and_non_string_values(self) -> None:
        for mode in ("tool", "", True, None):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "response_format_mode"):
                    OpenAICompatibleOntologyExtractor(
                        client=object(),
                        model="qwen-plus",
                        active_tbox=_tbox(),
                        prompt_version="v1",
                        response_format_mode=mode,  # type: ignore[arg-type]
                    )

    def test_model_local_references_never_determine_persistent_ids(self) -> None:
        first, _ = _extractor(_valid_payload())
        renamed = copy.deepcopy(_valid_payload())
        renamed["entities"][0]["ref"] = "x"  # type: ignore[index]
        renamed["entities"][1]["ref"] = "y"  # type: ignore[index]
        renamed["relationships"][0]["source_ref"] = "x"  # type: ignore[index]
        renamed["relationships"][0]["target_ref"] = "y"  # type: ignore[index]
        second, _ = _extractor(renamed)

        first_output = first(
            artifact_id="artifact-1",
            input_hash="input-1",
            chunk=_chunk(),
            profile=_profile(),
        )
        second_output = second(
            artifact_id="artifact-1",
            input_hash="input-1",
            chunk=_chunk(),
            profile=_profile(),
        )
        self.assertEqual(
            [item.entity_id for item in first_output.entities],
            [item.entity_id for item in second_output.entities],
        )
        self.assertEqual(
            first_output.assertions[0].assertion_id,
            second_output.assertions[0].assertion_id,
        )

    def test_invalid_mention_and_evidence_spans_are_rejected(self) -> None:
        bad_mention = _valid_payload()
        bad_mention["entities"][0]["mentions"][0]["start"] = 1  # type: ignore[index]
        extractor, _ = _extractor(bad_mention)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn("MENTION_SPAN_MISMATCH", {item.code for item in captured.exception.findings})

        bad_evidence = _valid_payload()
        bad_evidence["relationships"][0]["evidence"] = {  # type: ignore[index]
            "text": "Acme owns",
            "start": 0,
            "end": 9,
        }
        extractor, _ = _extractor(bad_evidence)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn("ENDPOINT_OUTSIDE_EVIDENCE", {item.code for item in captured.exception.findings})

    def test_unknown_types_predicates_and_wrong_direction_are_rejected(self) -> None:
        bad_type = _valid_payload()
        bad_type["entities"][0]["type"] = "Person"  # type: ignore[index]
        extractor, _ = _extractor(bad_type)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn("ENTITY_TYPE_NOT_ALLOWED", {item.code for item in captured.exception.findings})

        bad_predicate = _valid_payload()
        bad_predicate["relationships"][0]["type"] = "BUILT"  # type: ignore[index]
        extractor, _ = _extractor(bad_predicate)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn(
            "RELATIONSHIP_TYPE_NOT_ALLOWED",
            {item.code for item in captured.exception.findings},
        )

        reversed_relation = _valid_payload()
        reversed_relation["relationships"][0]["source_ref"] = "asset"  # type: ignore[index]
        reversed_relation["relationships"][0]["target_ref"] = "company"  # type: ignore[index]
        extractor, _ = _extractor(reversed_relation)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn(
            "RELATIONSHIP_ENDPOINT_NOT_ALLOWED",
            {item.code for item in captured.exception.findings},
        )

    def test_low_confidence_is_explicitly_quarantined(self) -> None:
        payload = _valid_payload()
        payload["relationships"][0]["confidence"] = 0.4  # type: ignore[index]
        extractor, _ = _extractor(payload)
        result = extractor.extract_audited(
            artifact_id="artifact-1",
            input_hash="input-1",
            chunk=_chunk(),
            profile=_profile(),
        )
        self.assertEqual(result.status, GovernanceStatus.QUARANTINED)
        self.assertIn("LOW_RELATIONSHIP_CONFIDENCE", {item.code for item in result.findings})
        with self.assertRaises(ExtractionQuarantined):
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )

    def test_unpublished_tbox_and_model_supplied_ids_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "active published"):
            OpenAICompatibleOntologyExtractor(
                client=object(),
                model="qwen-plus",
                active_tbox=_tbox(status=TBoxStatus.DRAFT),
                prompt_version="v1",
            )

        payload = _valid_payload()
        payload["entities"][0]["entity_id"] = "chosen-by-model"  # type: ignore[index]
        extractor, _ = _extractor(payload)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-1",
                input_hash="input-1",
                chunk=_chunk(),
                profile=_profile(),
            )
        self.assertIn("UNKNOWN_FIELDS", {item.code for item in captured.exception.findings})

    def test_provisional_namespace_must_be_approvable_for_every_entity_type(self) -> None:
        incompatible = TBoxVersion(
            tenant_id="tenant-industrial",
            key="industrial-assets",
            version=1,
            status=TBoxStatus.PUBLISHED,
            entity_types=(
                EntityTypeDefinition(
                    "Company",
                    ("company-id", "llm-candidate"),
                ),
                EntityTypeDefinition("Asset", ("asset-id",)),
            ),
            relationship_types=(
                RelationshipTypeDefinition("OWNS", ("Company",), ("Asset",)),
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "every extractable entity type; missing from: Asset",
        ):
            OpenAICompatibleOntologyExtractor(
                client=object(),
                model="qwen-plus",
                active_tbox=incompatible,
                prompt_version="v1",
            )

        for custom_namespace in ("auto-candidate", "LLM-CANDIDATE"):
            with self.subTest(custom_namespace=custom_namespace):
                with self.assertRaisesRegex(ValueError, "system-reserved"):
                    OpenAICompatibleOntologyExtractor(
                        client=object(),
                        model="qwen-plus",
                        active_tbox=_tbox(),
                        prompt_version="v1",
                        provisional_namespace=custom_namespace,
                    )

    def test_property_fact_is_typed_unit_normalized_temporal_and_auditable(self) -> None:
        extractor, completions = _extractor(_property_payload())

        result = extractor.extract_audited(
            artifact_id="artifact-property",
            input_hash="input-property",
            chunk=_property_chunk(),
            profile=_profile(),
        )

        self.assertEqual(result.status, GovernanceStatus.CANDIDATE)
        self.assertEqual(len(result.output.assertions), 1)
        assertion = result.output.assertions[0]
        self.assertEqual(assertion.predicate, "pressure")
        self.assertEqual(assertion.literal_value, "100")
        self.assertIsNotNone(assertion.literal_semantics)
        literal = assertion.literal_semantics
        assert literal is not None
        self.assertEqual(literal.datatype, "DECIMAL")
        self.assertEqual(literal.raw_unit, "psi")
        self.assertEqual(literal.canonical_unit, "kPa")
        self.assertTrue(literal.canonical_value.startswith("689.475729"))
        self.assertEqual(literal.raw_valid_from, "2025-01-01T00:00:00Z")
        self.assertEqual(literal.valid_from.isoformat(), "2025-01-01T00:00:00+00:00")
        self.assertEqual(literal.identity_reference, assertion.object_reference)
        self.assertEqual(
            (assertion.evidence_char_start, assertion.evidence_char_end),
            (200, 200 + len(PROPERTY_SOURCE)),
        )

        schema = completions.calls[0]["response_format"]["json_schema"]["schema"]  # type: ignore[index]
        self.assertIn("property_facts", schema["required"])  # type: ignore[index]
        property_schema = schema["properties"]["property_facts"]["items"]  # type: ignore[index]
        self.assertIn("pressure", property_schema["properties"]["property"]["enum"])

    def test_property_type_unit_and_temporal_fabrication_are_rejected(self) -> None:
        cases = []
        unknown = copy.deepcopy(_property_payload())
        unknown["property_facts"][0]["property"] = "temperature"  # type: ignore[index]
        cases.append((unknown, "PROPERTY_NOT_ALLOWED"))

        incompatible = copy.deepcopy(_property_payload())
        incompatible["property_facts"][0]["unit"] = "s"  # type: ignore[index]
        cases.append((incompatible, "INCOMPATIBLE_UNIT"))

        wrong_type = copy.deepcopy(_property_payload())
        wrong_type["property_facts"][0]["raw_literal"] = "Pump-7"  # type: ignore[index]
        cases.append((wrong_type, "INVALID_LITERAL_VALUE"))

        fabricated_time = copy.deepcopy(_property_payload())
        fabricated_time["property_facts"][0]["observed_at"] = "2030-01-01T00:00:00Z"  # type: ignore[index]
        cases.append((fabricated_time, "FACT_TOKEN_OUTSIDE_EVIDENCE"))

        invalid_range = copy.deepcopy(_property_payload())
        invalid_range["property_facts"][0]["valid_from"] = "2026-01-01T00:00:00Z"  # type: ignore[index]
        invalid_range["property_facts"][0]["valid_to"] = "2025-01-01T00:00:00Z"  # type: ignore[index]
        cases.append((invalid_range, "INVALID_TEMPORAL_RANGE"))

        for payload, code in cases:
            with self.subTest(code=code):
                extractor, _ = _extractor(payload)
                with self.assertRaises(ExtractionRejected) as captured:
                    extractor(
                        artifact_id="artifact-property",
                        input_hash="input-property",
                        chunk=_property_chunk(),
                        profile=_profile(),
                    )
                self.assertIn(code, {item.code for item in captured.exception.findings})

    def test_single_value_cardinality_conflict_and_low_confidence_are_explicit(self) -> None:
        conflict = copy.deepcopy(_property_payload())
        second = copy.deepcopy(conflict["property_facts"][0])  # type: ignore[index]
        second["raw_literal"] = "2025"
        conflict["property_facts"].append(second)  # type: ignore[union-attr]
        extractor, _ = _extractor(conflict)
        with self.assertRaises(ExtractionRejected) as captured:
            extractor(
                artifact_id="artifact-property",
                input_hash="input-property",
                chunk=_property_chunk(),
                profile=_profile(),
            )
        self.assertIn(
            "PROPERTY_CARDINALITY_CONFLICT",
            {item.code for item in captured.exception.findings},
        )

        low = copy.deepcopy(_property_payload())
        low["property_facts"][0]["confidence"] = 0.2  # type: ignore[index]
        extractor, _ = _extractor(low)
        result = extractor.extract_audited(
            artifact_id="artifact-property",
            input_hash="input-property",
            chunk=_property_chunk(),
            profile=_profile(),
        )
        self.assertEqual(result.status, GovernanceStatus.QUARANTINED)
        self.assertIn("LOW_PROPERTY_CONFIDENCE", {item.code for item in result.findings})


if __name__ == "__main__":
    unittest.main()
