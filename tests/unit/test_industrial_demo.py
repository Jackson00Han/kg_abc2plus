"""Offline source, provenance, schema, and identity checks for the browser kit."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import unittest

from graphrag_prod.api.knowledge_contracts import (
    AuthoritativeImportRequest,
    OntologyImportRequest,
)
from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.construction.parser import BoundedDocumentParser
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.knowledge import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
)
from graphrag_prod.knowledge.entity_resolution import (
    AuthoritativeEntityProfile,
    AuthoritativeEvidence,
    EntityResolutionService,
    ExactAuthoritativeMatch,
    IdentityPropertyValue,
    ResolutionOutcome,
)
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus
from graphrag_prod.ontology.models import TBoxVersion
from graphrag_prod.playground.industrial_demo import (
    INDUSTRIAL_DEMO_DIRECTORY,
    build_authoritative_import,
    get_industrial_demo_kit,
)


TENANT = "tenant-alpha"
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _identity(entity: dict[str, object]) -> EntityIdentity:
    return EntityIdentity(
        tenant_id=TENANT,
        entity_id=entity_id(TENANT, entity["entity_type"], entity["canonical_key"]),
        entity_type=entity["entity_type"],
        canonical_key=entity["canonical_key"],
        canonical_name=entity["canonical_name"],
        aliases=tuple(entity["aliases"]),
    )


class _KitAuthoritySource:
    """One exact typed identity, with every name-based fallback forbidden."""

    def __init__(self, profile: AuthoritativeEntityProfile) -> None:
        self.profile = profile

    def find_exact_canonical_key(self, principal: Principal, **kwargs: object):
        return ExactAuthoritativeMatch(0)

    def find_exact_identity_properties(
        self,
        principal: Principal,
        *,
        identity_properties: tuple[IdentityPropertyValue, ...],
        **kwargs: object,
    ) -> ExactAuthoritativeMatch:
        if identity_properties == (
            IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-101"),
        ):
            return ExactAuthoritativeMatch(
                1, self.profile, "EquipmentCode=STRING:BC-P-101"
            )
        return ExactAuthoritativeMatch(0)

    def find_exact_governed_alias(self, *args: object, **kwargs: object):
        raise AssertionError("declared device identity must precede alias matching")

    def list_authoritative_entities(self, *args: object, **kwargs: object):
        raise AssertionError("declared device identity must precede fuzzy matching")


class IndustrialDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = get_industrial_demo_kit()
        self.files = {item["id"]: item for item in self.kit["files"]}
        self.source = self.files["authoritative_source"]
        self.tbox = TBoxVersion.from_mapping(
            {
                **self.kit["ontology"],
                "tenant_id": TENANT,
                "status": "PUBLISHED",
            }
        )
        self.binding = {
            "tbox_id": self.tbox.tbox_id,
            "document_id": "document-real-upload",
            "version_id": "version-real-upload",
            "source_bytes": self.source["text"].encode("utf-8"),
            "chunks": [
                {
                    "chunk_id": "chunk-real-upload",
                    "char_start": 0,
                    "char_end": self.source["characters"],
                    "text": self.source["text"],
                }
            ],
        }

    def test_sources_match_locked_hashes_and_default_gapless_chunks(self) -> None:
        parser = BoundedDocumentParser()
        for file in self.files.values():
            with self.subTest(file=file["filename"]):
                raw = (INDUSTRIAL_DEMO_DIRECTORY / file["filename"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), file["sha256"])
                self.assertEqual(raw.decode("utf-8"), file["text"])
                self.assertLess(file["characters"], 600)
                parsed = parser.parse(raw, mime_type=file["mime_type"])
                self.assertEqual(parsed.normalized_text, file["text"])
                self.assertEqual(len(parsed.chunks), file["expected_chunks"])
                self.assertEqual(parsed.chunks[0].char_start, 0)
                self.assertEqual(parsed.chunks[0].char_end, file["characters"])
        self.assertLess(self.files["maintenance_report"]["characters"], 250)
        # The introductory record deliberately has one equipment occurrence:
        # identity facts cannot be borrowed from a different mention revision.
        report = self.files["maintenance_report"]["text"]
        self.assertEqual(report.count("北辰一号循环水泵"), 1)
        for fact in ("EquipmentCode", "BC-P-101", "37.5 kW", "BC-P-101机械密封", "机械密封渗漏风险"):
            self.assertIn(fact, report)

    def test_fresh_manifest_cannot_be_mutated_through_a_previous_response(self) -> None:
        self.kit["ontology"]["entity_types"].clear()
        self.kit["files"][0]["text"] = "changed"
        self.assertGreater(len(get_industrial_demo_kit()["ontology"]["entity_types"]), 0)
        self.assertEqual(
            get_industrial_demo_kit()["files"][0]["text"], self.binding["source_bytes"].decode()
        )

    def test_ontology_compiles_and_preserves_typed_device_identity(self) -> None:
        request = OntologyImportRequest.model_validate(self.kit["ontology"])
        self.assertEqual(request.key, "pump-maintenance-demo")
        self.tbox.compile_governance_policy()
        equipment = next(item for item in self.tbox.entity_types if item.name == "Equipment")
        properties = {item.name: item for item in equipment.properties}
        self.assertEqual(equipment.identity_properties, ("EquipmentCode",))
        self.assertEqual(properties["EquipmentCode"].datatype.value, "STRING")
        self.assertTrue(properties["EquipmentCode"].required)
        self.assertEqual(properties["RatedPower"].datatype.value, "DECIMAL")
        self.assertEqual(properties["RatedPower"].unit, "kW")
        self.assertEqual(self.source["construction_mode"], "SOURCE_ONLY")
        self.assertEqual(self.files["maintenance_report"]["construction_mode"], "LLM")

    def test_bound_import_satisfies_api_and_exact_evidence_contract(self) -> None:
        payload = build_authoritative_import(**self.binding)
        request = AuthoritativeImportRequest.model_validate(payload)
        self.assertEqual(len(request.mentions), 3)
        self.assertEqual(len(request.assertions), 4)
        self.assertEqual(request.ontology_version_id, self.tbox.tbox_id)
        for record in (*request.mentions, *request.assertions):
            evidence = record.evidence
            self.assertEqual(evidence.document_id, self.binding["document_id"])
            self.assertEqual(evidence.version_id, self.binding["version_id"])
            self.assertEqual(evidence.chunk_id, "chunk-real-upload")
            self.assertEqual(
                self.source["text"][evidence.char_start : evidence.char_end],
                evidence.quoted_text,
            )
        self.assertEqual(build_authoritative_import(**self.binding), payload)

    def test_changed_source_bytes_are_refused_before_binding(self) -> None:
        for raw in (b"wrong source", self.binding["source_bytes"] + b"\n"):
            with self.subTest(raw_length=len(raw)):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    build_authoritative_import(**{**self.binding, "source_bytes": raw})

    def test_missing_split_shifted_or_modified_chunk_is_refused(self) -> None:
        chunk = self.binding["chunks"][0]
        cases = (
            [],
            [chunk, chunk],
            [{"chunk_id": "missing-evidence"}],
            [{**chunk, "char_start": 1}],
            [{**chunk, "char_start": False}],
            [{**chunk, "char_end": chunk["char_end"] - 1}],
            [{**chunk, "text": "wrong chunk"}],
        )
        for chunks in cases:
            with self.subTest(chunks=chunks):
                with self.assertRaisesRegex(ValueError, "Chunk"):
                    build_authoritative_import(**{**self.binding, "chunks": chunks})

    def test_runtime_ids_cannot_be_empty_or_placeholder_values(self) -> None:
        for key in ("tbox_id", "document_id", "version_id"):
            for value in ("", "  ", "__SOURCE_DOCUMENT_ID__"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "runtime identifier"):
                        build_authoritative_import(**{**self.binding, key: value})
        chunks = deepcopy(self.binding["chunks"])
        chunks[0]["chunk_id"] = "__SOURCE_CHUNK_ID__"
        with self.assertRaisesRegex(ValueError, "runtime identifier"):
            build_authoritative_import(**{**self.binding, "chunks": chunks})

    def test_expert_batch_has_valid_typed_literals_and_mention_dependencies(self) -> None:
        payload = build_authoritative_import(**self.binding)
        trust = authoritative_import_trust(
            ontology_version_id=self.tbox.tbox_id,
            imported_by="demo-expert",
            imported_at=NOW,
            review_notes="Reviewed local test source",
        )

        def evidence(item: dict[str, object]) -> EvidenceReference:
            return EvidenceReference(
                **item["evidence"],
                tenant_id=TENANT,
                access_policy_id="policy-demo",
                access_policy_version=1,
                access_groups=frozenset({"finance"}),
            )

        mentions = {
            item["source_key"]: EntityMentionRecord(
                revision=RecordRevision.next(
                    knowledge_record_id(TENANT, "ENTITY_MENTION", item["source_key"]), 0
                ),
                tenant_id=TENANT,
                entity=_identity(item["entity"]),
                evidence=evidence(item),
                confidence=item["confidence"],
                trust=trust,
                created_at=NOW,
            )
            for item in payload["mentions"]
        }
        definitions = {
            prop.name: prop
            for entity in self.tbox.entity_types
            for prop in entity.properties
        }
        assertions = []
        for item in payload["assertions"]:
            subject = mentions[item["subject_mention_source_key"]]
            target = mentions.get(item.get("object_mention_source_key"))
            literal = item.get("literal")
            normalized = (
                None
                if literal is None
                else TBoxLiteralNormalizer().normalize(
                    definitions[item["predicate"]],
                    raw_value=literal["raw_literal"],
                    raw_unit=literal.get("raw_unit"),
                    valid_from=None,
                    valid_to=None,
                    observed_at=None,
                )
            )
            assertions.append(
                AssertionRecord(
                    revision=RecordRevision.next(
                        knowledge_record_id(TENANT, "ASSERTION", item["source_key"]), 0
                    ),
                    tenant_id=TENANT,
                    subject=subject.entity,
                    subject_mention_revision_id=subject.revision_id,
                    predicate=item["predicate"],
                    evidence=evidence(item),
                    confidence=item["confidence"],
                    trust=trust,
                    created_at=NOW,
                    object_entity=None if target is None else target.entity,
                    object_mention_revision_id=None if target is None else target.revision_id,
                    literal_value=None if literal is None else literal["raw_literal"],
                    literal_semantics=normalized,
                )
            )
        batch = ABoxRecordBatch(TENANT, tuple(mentions.values()), tuple(assertions))
        values = {
            item.predicate: item.literal_semantics
            for item in batch.assertions
            if item.literal_semantics is not None
        }
        self.assertEqual(values["EquipmentCode"].canonical_value, "BC-P-101")
        self.assertEqual(values["RatedPower"].canonical_value, "37.5")
        self.assertEqual(values["RatedPower"].canonical_unit, "kW")

    def test_same_code_links_but_same_name_different_code_cannot_merge(self) -> None:
        payload = build_authoritative_import(**self.binding)
        pump = next(
            item for item in payload["mentions"] if item["entity"]["entity_type"] == "Equipment"
        )
        profile = AuthoritativeEntityProfile(
            entity=_identity(pump["entity"]),
            ontology_version_id=self.tbox.tbox_id,
            authority=AuthorityLevel.AUTHORITATIVE,
            status=GovernanceStatus.PUBLISHED,
            evidence=(AuthoritativeEvidence(mention_revision_id="mention-1", **pump["evidence"]),),
        )
        service = EntityResolutionService(_KitAuthoritySource(profile), active_tbox=self.tbox)
        candidate = _identity({**pump["entity"], "canonical_key": "llm-candidate:report-pump"})
        principal = Principal("reviewer", TENANT, frozenset({"finance"}))
        for code, outcome in (
            ("BC-P-101", ResolutionOutcome.AUTO_LINK),
            ("BC-P-202", ResolutionOutcome.NO_MATCH),
        ):
            with self.subTest(code=code):
                suggestions = service.suggest(
                    principal,
                    candidate,
                    identity_properties=(IdentityPropertyValue("EquipmentCode", "STRING", code),),
                )
                self.assertEqual(suggestions[0].outcome, outcome)
        self.assertIn("BC-P-101", self.files["maintenance_report"]["text"])
        self.assertIn("BC-P-202", self.files["homonym_report"]["text"])
        self.assertIn(candidate.canonical_name, self.files["homonym_report"]["text"])


if __name__ == "__main__":
    unittest.main()
