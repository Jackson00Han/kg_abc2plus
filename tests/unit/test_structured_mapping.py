"""Structured mapping: coverage, exact evidence, joins, scope and replay."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from graphrag_prod.construction import ConstructionConfig, ExtractionRejected, OpenAICompatibleOntologyExtractor
from graphrag_prod.construction.structured import (
    VERSION, MappingExecutor, StructuredDocumentParser, StructuredMappingExtractor,
    collections, locate_json, select_structured_extractor,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum
from tests.unit.test_construction_extraction import FakeCompletions, _chunk, _profile, _tbox
from tests.unit.test_construction_workflow import _workflow, _metadata


def fixture(count=12):
    data = {"organizations": [{"key": "org-A"}], "machines": [
        {"key": f"asset-{i}", "owner": "org-A", "serial": f"原始\\编号-{i}"} for i in range(count)]}
    plan = {"version": VERSION, "collections": [
        {"path": "/organizations", "id_field": "/key", "entity_type": "Company", "type_field": None,
         "type_map": {}, "properties": [], "relations": [], "ignored_fields": {}},
        {"path": "/machines", "id_field": "/key", "entity_type": "Asset", "type_field": None,
         "type_map": {}, "properties": [{"field": "/serial", "property": "serialNumber"}],
         "relations": [{"field": "/owner", "target_collection": "/organizations", "target_field": "/key",
                        "type": "OWNS", "direction": "in", "properties": []}], "ignored_fields": {}}]}
    return data, plan


def parsed(data, size=4000):
    return StructuredDocumentParser(json_max_chars=size).parse(
        json.dumps(data, ensure_ascii=True, indent=2).encode(), mime_type="application/json")


def base(plan, tbox=None):
    calls = FakeCompletions(plan)
    obj = OpenAICompatibleOntologyExtractor(
        client=SimpleNamespace(chat=SimpleNamespace(completions=calls)), model="fixture",
        active_tbox=tbox or _tbox(), prompt_version="industrial-prompt:v1")
    return obj, calls


def topology_plan():
    """Reviewed test mapping; device-specific choices stay outside runtime code."""
    def props(*names):
        return [{"field": "/"+name, "property": name} for name in names]
    def relation(field, kind, direction, values=()):
        return {"field": "/"+field, "target_collection": "/assets", "target_field": "/asset_ref",
                "type": kind, "direction": direction,
                "properties": [{"field": "/"+a, "property": b} for a,b in values]}
    return {"version": VERSION, "collections": [
        {"path": "/assets", "id_field": "/asset_ref", "entity_type": None, "type_field": "/entity_type",
         "type_map": {n:n for n in ["Project","Busway","Section","EndFeedUnit","Flange","Joint","PlugInUnit","StraightSection"]},
         "properties": props("full_code","code","display_name","full_name","description","field_code",
                             "element_type_code","temp_type_code","source_algorithm_route_code","source_annotations"),
         "relations": [relation("parent_asset_ref","CONTAINS","in")],
         "ignored_fields": {"/"+n:"producer metadata outside this ontology property set" for n in
             ["entity_type_name","source_node_type_code","source_xpath","source_standard",
              "source_element_type_description","source_ventilation_coefficient"]}},
        {"path": "/ports", "id_field": "/port_ref", "entity_type": "Port", "type_field": None, "type_map": {},
         "properties": props("ordinal","name","display_name","port_type_code","role","enabled","source_load_full_code"),
         "relations": [relation("owner_asset_ref","HAS_PORT","in"),
                       relation("connected_asset_ref","CONNECTS_TO_COMPONENT","out",[("role","port_role"),("enabled","port_enabled")])],
         "ignored_fields": {"/source_xpath":"source location"}},
        {"path": "/measurement_points", "id_field": "/point_ref", "entity_type": "MeasurementPoint",
         "type_field": None, "type_map": {}, "properties": props("item","variable_name","measurement_system_ref"),
         "relations": [relation("owner_asset_ref","HAS_POINT","in")],
         "ignored_fields": {"/source_xpath":"source location","/source_comment":"producer comment"}},
    ]}


def chunk_for(seed):
    return replace(_chunk(), chunk_id=f"chunk-{seed.ordinal}", ordinal=seed.ordinal,
                   text=seed.text, checksum=content_checksum(seed.text),
                   char_start=seed.char_start, char_end=seed.char_end)


class StructuredMappingTests(unittest.TestCase):
    def test_json_literal_encoding_roundtrip_and_server_revalidation(self):
        from graphrag_prod.construction.literals import TBoxLiteralNormalizer
        from graphrag_prod.domain.models import TypedLiteralValue
        from graphrag_prod.knowledge.store import _validate_literal_semantics, KnowledgeSchemaError
        definition = next(p for e in _tbox().entity_types for p in e.properties if p.name == "serialNumber")
        raw = json.dumps('原始\\编号', ensure_ascii=True)
        literal = TBoxLiteralNormalizer().normalize(definition, raw_value=raw, source_encoding="JSON_STRING",
            raw_unit=None, valid_from=None, valid_to=None, observed_at=None)
        self.assertEqual(literal.canonical_value, '原始\\编号')
        self.assertEqual(literal.raw_value, raw)
        self.assertEqual(TypedLiteralValue.from_mapping(literal.to_mapping()), literal)
        self.assertEqual(TypedLiteralValue.from_flat_properties(literal.to_flat_properties()), literal)
        _validate_literal_semantics(literal, definition)
        with self.assertRaises(KnowledgeSchemaError):
            _validate_literal_semantics(replace(literal, canonical_value="invented", typed_value="invented"), definition)
        with self.assertRaises(KnowledgeSchemaError):
            _validate_literal_semantics(replace(literal, source_encoding="TEXT"), definition)

    def test_one_call_all_records_joins_and_exact_json_escape_values(self):
        data, plan = fixture(40)
        doc = parsed(data, 800)
        model, calls = base(plan)
        extractor = StructuredMappingExtractor(model, doc)
        audit = {}
        extractor.prepare_document(read=audit.get, persist=audit.__setitem__, before_model_call=lambda: None)
        outputs = [extractor.extract_audited(artifact_id="a", input_hash="h", chunk=chunk_for(s), profile=_profile()).output for s in doc.chunks]
        entities = {e.entity_id for out in outputs for e in out.entities}
        self.assertEqual(len(entities), 41)
        self.assertEqual(sum(a.predicate == "OWNS" for out in outputs for a in out.assertions), 40)
        values = {a.literal_semantics.canonical_value for out in outputs for a in out.assertions if a.literal_semantics}
        self.assertEqual(values, {row["serial"] for row in data["machines"]})
        self.assertEqual(len(calls.calls), 1)
        self.assertTrue(all(not a.accepted for out in outputs for a in out.assertions))
        for seed, out in zip(doc.chunks, outputs):
            for mention in out.mentions:
                self.assertEqual(doc.normalized_text[mention.char_start:mention.char_end], mention.surface)
            for a in out.assertions:
                self.assertGreaterEqual(a.evidence_char_start, seed.char_start)
                self.assertLessEqual(a.evidence_char_end, seed.char_end)
        replay = StructuredMappingExtractor(model, doc)
        replay.prepare_document(read=audit.get, persist=audit.__setitem__, before_model_call=lambda: self.fail("replay called model"))

    def test_record_order_and_collection_names_are_not_hardcoded(self):
        data, plan = fixture()
        data = {"different": {"devices": data["machines"], "owners": data["organizations"]}}
        plan["collections"][0]["path"] = "/different/owners"
        plan["collections"][1]["path"] = "/different/devices"
        plan["collections"][1]["relations"][0]["target_collection"] = "/different/owners"
        executor = MappingExecutor(parsed(data), _tbox(), plan)
        self.assertEqual(len(executor.records), 13)

    def test_missing_collections_fields_duplicate_ids_and_unresolved_refs_reject(self):
        for mutation, code in [
            (lambda d, p: p["collections"].pop(), "MAPPING_COLLECTION_MISSING"),
            (lambda d, p: d["machines"][0].update(unknown=42), "MAPPING_FIELD_COVERAGE"),
            (lambda d, p: d["machines"].append(d["machines"][0]), "MAPPING_DUPLICATE_ID"),
            (lambda d, p: d["machines"][0].update(owner="missing"), "MAPPING_REFERENCE_UNRESOLVED"),
            (lambda d, p: p["collections"][1]["relations"][0].update(direction="out"), "MAPPING_ENDPOINT_INVALID"),
            (lambda d, p: p.update(script="arbitrary code"), "MAPPING_SCHEMA_INVALID"),
        ]:
            with self.subTest(code=code):
                data, plan = fixture()
                mutation(data, plan)
                with self.assertRaisesRegex(ExtractionRejected, code):
                    MappingExecutor(parsed(data), _tbox(), plan)

    def test_rare_dynamic_type_must_be_mapped_across_all_records(self):
        data, plan = fixture(50)
        for row in data["machines"]:
            row["kind"] = "regular"
        data["machines"][-1]["kind"] = "rare"
        plan["collections"][1].update(entity_type=None, type_field="/kind", type_map={"regular": "Asset"})
        with self.assertRaisesRegex(ExtractionRejected, "MAPPING_TYPE_UNMAPPED"):
            MappingExecutor(parsed(data), _tbox(), plan)

    def test_duplicate_json_keys_rejected_and_text_keeps_existing_path(self):
        with self.assertRaisesRegex(ExtractionRejected, "MAPPING_DUPLICATE_KEY"):
            locate_json('{"x":1,"x":2}')
        model, _ = base(fixture()[1])
        doc = StructuredDocumentParser().parse(b"unstructured text", mime_type="text/plain")
        self.assertIs(select_structured_extractor(model, doc), model)
        doc = parsed({"articles": [{"id": "doc1", "body": "narrative without explicit references"}]})
        self.assertIs(select_structured_extractor(model, doc), model)

    def test_authorized_topology_matches_source_identifiers_and_all_explicit_relations(self):
        from tests.unit.test_ontology_source import tbox
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / "busway_files/topology.source.json").read_text())
        document = StructuredDocumentParser().parse((root / "busway_files/topology.source.json").read_bytes(), mime_type="application/json")
        model, _ = base(topology_plan(), tbox())
        obj = StructuredMappingExtractor(model, document)
        obj.prepare_document(read=lambda _:None, persist=lambda *_:None, before_model_call=lambda:None)
        outputs = [obj.extract_audited(artifact_id="a", input_hash="h", chunk=chunk_for(s), profile=_profile()).output for s in document.chunks]
        entities = {e.entity_id:e.canonical_name for out in outputs for e in out.entities}
        expected_ids = {r[ref] for group,ref in [("assets","asset_ref"),("ports","port_ref"),("measurement_points","point_ref")] for r in data[group]}
        self.assertEqual(set(entities.values()), expected_ids)
        expected_relations = set()
        for row in data["assets"]:
            if row.get("parent_asset_ref"):
                expected_relations.add((row["parent_asset_ref"],"CONTAINS",row["asset_ref"]))
        for row in data["ports"]:
            expected_relations.add((row["owner_asset_ref"],"HAS_PORT",row["port_ref"]))
            if row.get("connected_asset_ref"):
                expected_relations.add((row["port_ref"],"CONNECTS_TO_COMPONENT",row["connected_asset_ref"]))
        for row in data["measurement_points"]:
            expected_relations.add((row["owner_asset_ref"],"HAS_POINT",row["point_ref"]))
        actual_relations={(entities[a.subject_entity_id],a.predicate,entities[a.object_entity_id])
                          for out in outputs for a in out.assertions if a.object_entity_id}
        self.assertEqual(actual_relations, expected_relations)
        actual_vars={entities[a.subject_entity_id]:a.literal_semantics.canonical_value for out in outputs for a in out.assertions
                     if a.predicate=="variable_name"}
        self.assertEqual(actual_vars,{r["point_ref"]:r["variable_name"] for r in data["measurement_points"]})
        doc = StructuredDocumentParser().parse(b'{"text":"long narrative"}', mime_type="application/json")
        self.assertIs(select_structured_extractor(model, doc), model)

    def test_corrective_mapping_is_bounded_and_both_responses_retained(self):
        data, plan = fixture()
        bad = deepcopy(plan)
        bad["collections"][0]["ignored_fields"] = {"/key": "used as identity"}
        model, calls = base(bad)
        create = calls.create
        def respond(**kwargs):
            calls.payload = bad if not calls.calls else plan
            return create(**kwargs)
        calls.create = respond
        audit = {}
        obj = StructuredMappingExtractor(model, parsed(data))
        obj.prepare_document(read=audit.get, persist=audit.__setitem__, before_model_call=lambda: None)
        self.assertEqual(len(calls.calls), 2)
        self.assertEqual(len(audit), 2)
        self.assertEqual([row["attempt"] for row in audit.values()], [1, 2])

    def test_identity_stable_within_source_but_not_across_documents_or_tenants(self):
        data, plan = fixture(1)
        doc = parsed(data)
        model, _ = base(plan)
        obj = StructuredMappingExtractor(model, doc)
        obj.prepare_document(read=lambda _: None, persist=lambda *_: None, before_model_call=lambda: None)
        chunk = chunk_for(doc.chunks[0])
        run = lambda c: obj.extract_audited(artifact_id="a", input_hash="h", chunk=c, profile=_profile())
        old = run(chunk).output.entities
        other = run(replace(chunk, document_id="another-source")).output.entities
        self.assertTrue({e.entity_id for e in old}.isdisjoint(e.entity_id for e in other))
        with self.assertRaisesRegex(ExtractionRejected, "MAPPING_NOT_PREPARED"):
            run(replace(chunk, tenant_id="another-tenant"))

    def test_workflow_replay_keeps_candidates_and_mapping_audit(self):
        data, plan = fixture(30)
        model, calls = base(plan)
        flow, audit, knowledge, _ = _workflow(extractor=model, parser=StructuredDocumentParser(json_max_chars=800),
            config=ConstructionConfig(extractor_signature="mapping-fixture:v1", prompt_signature=model.prompt_version,
                                      max_chunks=128, max_model_calls=2, max_concurrency=4))
        flow.document_extractor_selector = select_structured_extractor
        principal = Principal("fixture-user", "tenant-industrial", frozenset({"engineers"}), frozenset({"knowledge:construct"}))
        metadata = replace(_metadata(), mime_type="application/json")
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
        first = flow.run(principal, payload, metadata)
        self.assertTrue(all(c.status in {"CANDIDATE", "EMPTY"} for c in first.chunks))
        self.assertEqual(len(calls.calls), 1)
        counts = len(knowledge.mentions), len(knowledge.assertions)
        second = flow.run(principal, payload, metadata)
        self.assertTrue(all(c.replayed for c in second.chunks))
        self.assertEqual(len(calls.calls), 1)
        self.assertEqual(counts, (len(knowledge.mentions), len(knowledge.assertions)))
        self.assertEqual(sum(a.predicate == "OWNS" for a in knowledge.assertions.values()), 30)


if __name__ == "__main__":
    unittest.main()
