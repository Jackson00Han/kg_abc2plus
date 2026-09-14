"""Executable source-import semantics and negative cases, using authorized files."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from graphrag_prod.api.knowledge import _tbox_payload
from graphrag_prod.api.knowledge_contracts import OntologyImportRequest, OntologyVersionResponse
from graphrag_prod.construction import ExtractionRejected, OpenAICompatibleOntologyExtractor
from graphrag_prod.construction.literals import LiteralNormalizationError, TBoxLiteralNormalizer
from graphrag_prod.ontology.models import TBoxVersion
from graphrag_prod.ontology.source import compile_source_contract, source_version_number, validate_conditional_properties
from graphrag_prod.ontology.source_validation import validate_source_publication
from tests.unit.test_construction_extraction import FakeCompletions, _chunk, _profile

ROOT = Path(__file__).resolve().parents[2]


def source() -> dict:
    return json.loads((ROOT / "busway_files/ontology.source.json").read_text())


def registry() -> dict:
    return json.loads((ROOT / "busway_files/rule.references.json").read_text())


def tbox(*, tenant_id: str = "tenant-industrial", with_registry: bool = True) -> TBoxVersion:
    value = source()
    if with_registry:
        value["rule_reference_registry"] = registry()
    request = OntologyImportRequest.model_validate(value)
    mapping = request.model_dump(exclude={"activate", "expected_checksum", "expected_active_tbox_id"}, exclude_none=True)
    return TBoxVersion.from_mapping({**mapping, "tenant_id": tenant_id, "status": "PUBLISHED"})


class OntologySourceTests(unittest.TestCase):
    def test_original_source_compiles_and_preserves_independent_checksums(self) -> None:
        value = tbox()
        self.assertEqual(value.key, "ai_power.busway.ontology")
        self.assertEqual(value.version, 5_000_000)
        self.assertEqual(json.loads(value.source_contract_json), source())
        self.assertEqual(value, TBoxVersion.from_mapping(value.to_mapping()))
        response = OntologyVersionResponse.model_validate(_tbox_payload(value))
        self.assertEqual(response.import_capabilities.source_version, "5.0.0")
        self.assertEqual(response.rule_reference_registry, registry())
        self.assertNotEqual(value.checksum, tbox(with_registry=False).checksum)
        self.assertEqual(value.source_contract_json, tbox(with_registry=False).source_contract_json)

    def test_transport_fields_are_excluded_from_original_source(self) -> None:
        raw = {**source(), "activate": True, "expected_checksum": "a" * 64, "expected_active_tbox_id": "old-tbox", "rule_reference_registry": registry()}
        request = OntologyImportRequest.model_validate(raw)
        self.assertEqual(json.loads(request.source_contract_json), source())
        self.assertTrue(request.activate)

    def test_source_cannot_be_replayed_with_weakened_native_schema(self) -> None:
        mapping = tbox().to_mapping()
        relation = next(item for item in mapping["relationship_types"] if item["name"] == "CONTAINS")
        relation.pop("allowed_type_pairs")
        with self.assertRaisesRegex(ValueError, "differs"):
            TBoxVersion.from_mapping(mapping)

    def test_semver_encoding_is_distinct_monotone_and_bounded(self) -> None:
        self.assertLess(source_version_number("5.0.0"), source_version_number("5.0.1"))
        self.assertLess(source_version_number("5.999.999"), source_version_number("6.0.0"))
        for invalid in ("5", "5.0.0-beta", "05.0.0", "2147.0.0", "5.1000.0", "0.0.0"):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                source_version_number(invalid)

    def test_unknown_constraints_cycles_and_unresolved_refs_reject(self) -> None:
        mutations = [
            lambda raw: raw["property_definitions"]["entity_types"]["DiagnosticParameter"].update(threshold_value={"value_type": "number", "required": False}),
            lambda raw: raw["property_definitions"]["entity_types"]["Port"]["ordinal"].update(minimum="not-a-number"),
            lambda raw: raw["property_definitions"]["relation_types"]["PRECEDES"]["evidence_port_refs"].update(max_items=-1),
            lambda raw: raw["property_definitions"]["entity_types"]["Port"]["role"].update(unknown_validator="ignored"),
            lambda raw: raw["type_hierarchy"]["subtype_of"].append({"child": "EngineeringContainer", "parent": "Project"}),
            lambda raw: raw["entity_types"]["Port"]["property_schema"].update({"$ref": "https://example.invalid/schema"}),
            lambda raw: raw["relation_types"]["CONTAINS"]["to"]["allowed_types"].append("Unknown"),
        ]
        for mutation in mutations:
            raw = source()
            mutation(raw)
            with self.assertRaises((ValueError, ValidationError)):
                request = OntologyImportRequest.model_validate(raw)
                mapping = request.model_dump(exclude={"activate", "expected_checksum", "expected_active_tbox_id"}, exclude_none=True)
                TBoxVersion.from_mapping({**mapping, "tenant_id": "fixture", "status": "DRAFT"})

    def test_abstract_type_template_derived_and_cartesian_pair_guards(self) -> None:
        value = tbox()
        entities = {item.name: item for item in value.entity_types}
        relations = {item.name: item for item in value.relationship_types}
        self.assertFalse(entities["EngineeringComponent"].instance_allowed)
        self.assertFalse(entities["DiagnosticScopeInstance"].instance_allowed)
        for name in ("MAY_EXHIBIT", "MAY_AFFECT", "EXPECTED_OBSERVABLE", "PRECEDES"):
            self.assertFalse(relations[name].instance_allowed)
        self.assertTrue(relations["CONTAINS"].allows_instances("Project", "Busway"))
        self.assertFalse(relations["CONTAINS"].allows_instances("Project", "Joint"))
        self.assertFalse(relations["MAY_EXHIBIT"].allows_instances("Joint", "Phenomenon"))
        policy = value.compile_governance_policy()
        self.assertTrue(policy.allows_relationship("CONTAINS", "Project", "entity", "Busway"))
        self.assertFalse(policy.allows_relationship("CONTAINS", "Project", "entity", "Joint"))

    def test_enum_const_array_and_numeric_constraints_are_enforced(self) -> None:
        value = tbox()
        properties = {(owner.name, item.name): item for owner in (*value.entity_types, *value.relationship_types) for item in owner.properties}
        normalizer = TBoxLiteralNormalizer()
        cases = [("Port", "role", "input", "inlet"), ("ReferenceStatistic", "not_for_diagnostic_scoring", "true", "false"), ("PRECEDES", "evidence_port_refs", '["port-1"]', '[]'), ("TOPOLOGICALLY_ADJACENT_TO", "path_length", "2", "3")]
        for owner, name, valid, invalid in cases:
            definition = properties[owner, name]
            normalize = lambda raw: normalizer.normalize(definition, raw_value=raw, raw_unit=None, valid_from=None, valid_to=None, observed_at=None)
            normalize(valid)
            with self.subTest(property=name), self.assertRaises(LiteralNormalizationError):
                normalize(invalid)
        relation = next(item for item in value.relationship_types if item.name == "CHARACTERIZED_BY")
        with self.assertRaisesRegex(ValueError, "rule_clause"):
            validate_conditional_properties(relation.properties, {"criterion_role": "rule_criterion"})
        validate_conditional_properties(relation.properties, {"criterion_role": "contextual_only", "criterion_note": "context"})

    def test_model_prompt_excludes_source_body_and_noninstance_types(self) -> None:
        fake = FakeCompletions({"entities": [], "relationships": [], "property_facts": []})
        extractor = OpenAICompatibleOntologyExtractor(client=SimpleNamespace(chat=SimpleNamespace(completions=fake)), model="fixture", active_tbox=tbox(), prompt_version="source-profile-v1")
        extractor(artifact_id="a", input_hash="i", chunk=_chunk(), profile=_profile())
        prompt = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
        self.assertNotIn("source_contract_json", prompt)
        self.assertNotIn("governance_constraints", prompt)
        self.assertNotIn('"name": "EngineeringComponent"', prompt)
        self.assertNotIn('"name": "MAY_EXHIBIT"', prompt)
        self.assertIn("allowed_type_pairs", prompt)
        self.assertIn("constraints_json", prompt)

    def test_registered_rules_have_exact_source_version_hash_and_locators(self) -> None:
        value = registry()["documents"][0]
        raw = (ROOT / "new_files/rule_v2.md").read_bytes()
        self.assertEqual(value["sha256"], hashlib.sha256(raw).hexdigest())
        lines = raw.decode().splitlines()
        ids = {item["id"] for item in value["clauses"]}
        self.assertEqual(ids, {*(f"D{i}" for i in range(1, 7)), *(f"C{i}" for i in range(1, 6)), *(f"G{i}" for i in range(1, 6)), *(f"M{i}" for i in range(2, 8))})
        for clause in value["clauses"]:
            self.assertIn(clause["id"], lines[clause["line"] - 1])
        altered = copy.deepcopy(registry())
        altered["documents"][0]["threshold"] = 55
        with self.assertRaises(ValidationError):
            OntologyImportRequest.model_validate({**source(), "rule_reference_registry": altered})

    def test_diagnostic_manifest_requires_scope_and_resolved_rule_clause(self) -> None:
        entity = lambda identity, kind: SimpleNamespace(entity_id=identity, entity_type=kind)
        p, r, scope = entity("phenomenon", "Phenomenon"), entity("rule", "RuleVersion"), entity("scope", "DiagnosticScope")
        entities = {item.entity_id: item for item in (p, r, scope)}
        values = {p.entity_id: {"rule_evaluation_status": "produced_by_rule"}, r.entity_id: {"document": "rule_v2.md", "document_version": "2.0.0", "profile_id": "diagnostic64_v2"}}
        def edge(subject, predicate, target, props):
            return SimpleNamespace(subject=subject, predicate=predicate, object_entity=target, relationship_properties=[SimpleNamespace(name=k, literal_semantics=SimpleNamespace(typed_value=v)) for k, v in props.items()])
        relationships = [edge(p, "APPLICABLE_TO_SCOPE", scope, {"scope_role": "localization"}), edge(p, "EVALUATED_BY", r, {"rule_clause": "D1", "clause_role": "produces_phenomenon"})]
        with self.assertRaisesRegex(ValueError, "registry"):
            validate_source_publication(source(), entities, values, relationships)
        validate_source_publication(source(), entities, values, relationships, registry())
        with self.assertRaisesRegex(ValueError, "localization"):
            validate_source_publication(source(), entities, values, relationships[1:], registry())
        with self.assertRaisesRegex(ValueError, "comparison basis"):
            validate_source_publication(source(), entities, values, [*relationships, edge(p, "APPLICABLE_TO_SCOPE", scope, {"scope_role": "comparison_basis"})], registry())
        values[r.entity_id]["document_version"] = "3.0.0"
        with self.assertRaisesRegex(ValueError, "registry"):
            validate_source_publication(source(), entities, values, relationships, registry())

    def test_owner_scoped_identity_cannot_publish_without_owner_evidence(self) -> None:
        port = SimpleNamespace(entity_id="port", entity_type="Port")
        with self.assertRaisesRegex(ValueError, "exactly one evidenced owner"):
            validate_source_publication(source(), {"port": port}, {"port": {"ordinal": 1}}, [], registry())
