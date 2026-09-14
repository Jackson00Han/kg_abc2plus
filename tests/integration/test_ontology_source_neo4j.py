"""Real, disposable Neo4j checks for source ontology import and A-Box guards."""
from __future__ import annotations

from dataclasses import replace
import json

from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.knowledge import KnowledgeSchemaError, Neo4jKnowledgeStore
from graphrag_prod.ontology import TBoxConflict, TBoxStatus
from tests.fixtures.knowledge import make_knowledge_batch
from tests.integration import test_ontology_neo4j as legacy
from tests.unit.test_ontology_source import tbox


class Neo4jSourceOntologyTests(legacy.Neo4jTBoxStoreIntegrationTests):
    def _source(self):
        value = tbox(tenant_id="source-ontology-integration").with_status(TBoxStatus.DRAFT)
        return self.store.save_and_activate(value)

    def _batch(self, value, subject_type="Project", object_type="Busway", predicate="CONTAINS"):
        batch = make_knowledge_batch(authoritative=False, tenant_id=value.tenant_id, ontology_version_id=value.tbox_id)
        edge = next(item for item in batch.assertions if item.object_entity is not None)
        def changed(entity, kind):
            return replace(entity, entity_type=kind, entity_id=entity_id(entity.tenant_id, kind, entity.canonical_key))
        subject, target = changed(edge.subject, subject_type), changed(edge.object_entity, object_type)
        identities = {edge.subject.entity_id: subject, edge.object_entity.entity_id: target}
        mentions = tuple(replace(item, entity=identities[item.entity.entity_id]) for item in batch.mentions if item.entity.entity_id in identities)
        return replace(batch, mentions=mentions, assertions=(replace(edge, subject=subject, object_entity=target, predicate=predicate),))

    def _validate(self, batch):
        with self.driver.session(database=self.database) as session:
            session.execute_write(Neo4jKnowledgeStore._validate_tbox_tx, batch)

    def test_source_save_enable_replay_registry_and_immutable_definition(self):
        value = self._source()
        self.assertEqual(value.status, TBoxStatus.PUBLISHED)
        self.assertEqual(self.store.active(value.tenant_id, value.key), value)
        self.assertEqual(self.store.save_and_activate(value.with_status(TBoxStatus.DRAFT)), value)
        self.assertEqual(self.store.get(value.tenant_id, value.tbox_id).source_contract_json, value.source_contract_json)
        self.assertEqual(self.store.get(value.tenant_id, value.tbox_id).rule_reference_registry_json, value.rule_reference_registry_json)
        rows, _, _ = self.driver.execute_query("MATCH (type:TBoxEntityType {tenant_id:$tenant,name:'EngineeringComponent'}) RETURN type.instance_allowed AS allowed", tenant=value.tenant_id, database_=self.database)
        self.assertFalse(rows[0]["allowed"])
        changed = replace(value, rule_reference_registry_json=None, status=TBoxStatus.DRAFT)
        with self.assertRaises(TBoxConflict):
            self.store.save_and_activate(changed, expected_active_tbox_id=value.tbox_id)
        with self.assertRaises(KeyError):
            self.store.get("another-tenant", value.tbox_id)

    def test_tbox_write_guard_preserves_exact_pairs_and_denies_type_templates(self):
        value = self._source()
        self._validate(self._batch(value))
        for source_type, target_type, predicate in (("Project", "Joint", "CONTAINS"), ("Joint", "Phenomenon", "MAY_EXHIBIT"), ("Joint", "Joint", "PRECEDES"), ("EngineeringContainer", "Busway", "CONTAINS")):
            with self.subTest(pattern=(source_type, target_type, predicate)), self.assertRaises(KnowledgeSchemaError):
                self._validate(self._batch(value, source_type, target_type, predicate))

    def test_durable_literal_enum_is_checked_even_for_prevalidated_client_value(self):
        value = self._source()
        batch = self._batch(value, "Port", "Busway", "CONNECTS_TO_COMPONENT")
        relationship = batch.assertions[0]
        definition = next(prop for item in value.entity_types if item.name == "Port" for prop in item.properties if prop.name == "role")
        literal = TBoxLiteralNormalizer().normalize(replace(definition, constraints_json=None), raw_value="inlet", raw_unit=None, valid_from=None, valid_to=None, observed_at=None)
        evidence = replace(relationship.evidence, quoted_text="Port role inlet input", char_end=relationship.evidence.char_start + len("Port role inlet input"))
        assertion = replace(relationship, evidence=evidence, predicate="role", object_entity=None, object_mention_revision_id=None, literal_value="inlet", literal_semantics=literal)
        batch = replace(batch, mentions=(batch.mentions[0],), assertions=(assertion,))
        with self.assertRaises(KnowledgeSchemaError):
            self._validate(batch)
        literal = TBoxLiteralNormalizer().normalize(definition, raw_value="input", raw_unit=None, valid_from=None, valid_to=None, observed_at=None)
        self._validate(replace(batch, assertions=(replace(assertion, literal_value="input", literal_semantics=literal),)))

    def test_complete_source_manifest_requires_all_declared_properties(self):
        from graphrag_prod.knowledge.review import Neo4jKnowledgePublicationService, KnowledgePublicationConflict
        value = self._source()
        batch = self._batch(value)
        edge = batch.assertions[0]
        records = [*batch.mentions, edge]
        normalizer = TBoxLiteralNormalizer()
        properties = {item.name: {prop.name: prop for prop in item.properties} for item in value.entity_types}
        for entity, code, mention in ((edge.subject, "P", batch.mentions[0]), (edge.object_entity, "B", batch.mentions[1])):
            for predicate, raw in (("project_id", "P"), ("full_code", code), ("code", code)):
                literal = normalizer.normalize(properties[entity.entity_type][predicate], raw_value=raw, raw_unit=None, valid_from=None, valid_to=None, observed_at=None)
                text = f"{entity.canonical_name} {predicate} {raw}"
                evidence = replace(edge.evidence, quoted_text=text, char_end=edge.evidence.char_start + len(text))
                records.append(replace(edge, evidence=evidence, subject=entity, subject_mention_revision_id=mention.revision_id, predicate=predicate, object_entity=None, object_mention_revision_id=None, literal_value=raw, literal_semantics=literal))
        with self.driver.session(database=self.database) as session:
            result = session.execute_write(Neo4jKnowledgePublicationService._validate_property_cardinality_tx, value.tenant_id, tuple(records))
            self.assertEqual(result, value.tbox_id)
            with self.assertRaises(KnowledgePublicationConflict):
                session.execute_write(Neo4jKnowledgePublicationService._validate_property_cardinality_tx, value.tenant_id, tuple(records[:-1]))
