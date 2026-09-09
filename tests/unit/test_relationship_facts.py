"""Small offline checks for source-independent relationship identity."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest

from graphrag_prod.domain.facts import relationship_fact_key
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.knowledge import RecordRevision
from graphrag_prod.knowledge.publication_preview import instance_snapshot
from tests.fixtures.knowledge import make_knowledge_batch


class RelationshipFactTests(unittest.TestCase):
    def test_identity_preserves_tenant_direction_type_and_ontology(self):
        args = ("tenant", "ontology", "pump", "HAS_COMPONENT", "seal")
        key = relationship_fact_key(*args)
        for index in range(len(args)):
            changed = list(args)
            changed[index] += "-different"
            self.assertNotEqual(key, relationship_fact_key(*changed))
        self.assertNotEqual(key, relationship_fact_key("tenant", "ontology", "seal", "HAS_COMPONENT", "pump"))

    def test_qualifiers_use_meaning_and_time_not_source_evidence(self):
        args = ("tenant", "ontology", "pump", "HAS_COMPONENT", "seal")
        time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        value = TypedLiteralValue(datatype="STRING", typed_value="installed", raw_value="installed",
            canonical_value="installed", observed_at=time, raw_observed_at=time.isoformat())
        prop = SimpleNamespace(name="state", literal_semantics=value, evidence_chunk_id="source-a")
        key = relationship_fact_key(*args, (prop,))
        other = SimpleNamespace(name="state", literal_semantics=replace(value,
            observed_at=time.astimezone(timezone(timedelta(hours=8)))), evidence_chunk_id="source-b")
        self.assertEqual(key, relationship_fact_key(*args, (other, prop)))
        other.literal_semantics = replace(value, observed_at=time + timedelta(days=1),
            raw_observed_at=(time + timedelta(days=1)).isoformat())
        self.assertNotEqual(key, relationship_fact_key(*args, (other,)))
        self.assertNotEqual(key, relationship_fact_key(*args))

    def test_publication_snapshot_keeps_one_relation_and_all_sources(self):
        batch = make_knowledge_batch()
        relation = batch.assertions[0]
        duplicate = replace(relation, revision=RecordRevision.next("second-source-record", 0))
        result = instance_snapshot((*batch.mentions, relation, duplicate), [])
        self.assertEqual(result["summary"]["relationship_count"], 1)
        edge = result["relationships"][0]
        self.assertEqual(edge["relationship_id"], relation.fact_key)
        self.assertEqual(edge["source_count"], 2)
        self.assertEqual(set(edge["evidence_ids"]), {relation.revision_id, duplicate.revision_id})
