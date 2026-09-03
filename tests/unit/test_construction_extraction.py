"""Ontology-constrained OpenAI-compatible extraction tests with a fake client."""

from __future__ import annotations

import copy
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
    EntityTypeDefinition,
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
            EntityTypeDefinition("Company", ("company-id",)),
            EntityTypeDefinition("Asset", ("asset-id",)),
        ),
        relationship_types=(
            RelationshipTypeDefinition("OWNS", ("Company",), ("Asset",)),
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
) -> tuple[OpenAICompatibleOntologyExtractor, FakeCompletions]:
    completions = FakeCompletions(payload)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    extractor = OpenAICompatibleOntologyExtractor(
        client=client,
        model="qwen-plus",
        active_tbox=_tbox(),
        prompt_version="industrial-extraction-prompt:v1",
        limits=limits,
    )
    return extractor, completions


class ConstructionExtractionTests(unittest.TestCase):
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
        schema = request["response_format"]["json_schema"]["schema"]  # type: ignore[index]
        self.assertEqual(
            schema["properties"]["entities"]["items"]["properties"]["type"]["enum"],  # type: ignore[index]
            ["Company", "Asset"],
        )
        prompt = request["messages"][0]["content"]  # type: ignore[index]
        self.assertIn("Never return database IDs", prompt)
        self.assertNotIn("api_key", request)

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


if __name__ == "__main__":
    unittest.main()
