"""Cross-file industrial scope and source-edition regression boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from graphrag_prod.industrial.contract import load_industrial_contract
from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.ontology import build_industrial_tbox
from graphrag_prod.industrial.sources import load_source_catalog
from graphrag_prod.industrial.validation import validate_industrial_model
from graphrag_prod.ontology import Cardinality


ROOT = Path(__file__).resolve().parents[2]


class IndustrialModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = build_corpus()
        cls.catalog = load_source_catalog(ROOT / "datasets/industrial-v1/sources.json")
        cls.contract = load_industrial_contract(ROOT / "contracts/industrial_knowledge.v1.json")
        cls.tbox = build_industrial_tbox(cls.corpus.tenant_id)

    def validate(self, corpus=None, tbox=None):
        return validate_industrial_model(
            corpus or self.corpus, self.catalog, self.contract, tbox or self.tbox,
        )

    def with_reference(self, *, family=None, **changes):
        document = next(item for item in self.corpus.documents if item.source_refs
                        and (family is None or item.family == family))
        reference = replace(document.source_refs[0], **changes)
        changed = replace(document, source_refs=(reference, *document.source_refs[1:]))
        return replace(self.corpus, documents=tuple(
            changed if item.key == document.key else item for item in self.corpus.documents
        ))

    def test_composed_corpus_meets_scale_and_keeps_validation_nonqualifying(self) -> None:
        result = self.validate()
        self.assertGreaterEqual(result["counts"]["documents"], 32)
        self.assertGreaterEqual(result["counts"]["chunks"], 300)
        self.assertEqual(len(result["hierarchies"]), 2)
        self.assertTrue(all(item["edges"] > 0 for item in result["hierarchies"]))
        self.assertFalse(result["production_candidate_eligible"])
        self.assertEqual(result["technical_sme_review"], "PENDING")

    def test_similar_hvx_manual_cannot_replace_the_reviewed_source_variant(self) -> None:
        source = self.catalog.get("evopact-hvx-china-12kv-maintenance")
        corpus = self.with_reference(
            family="evopact-hvx-up24",
            source_id=source.source_id, embedded_revision=source.embedded_revision,
            physical_pages=(21,),
        )
        with self.assertRaisesRegex(ValueError, "product family"):
            self.validate(corpus)

    def test_embedded_revision_and_exact_physical_pages_are_not_interchangeable(self) -> None:
        with self.assertRaisesRegex(ValueError, "embedded edition"):
            self.validate(self.with_reference(embedded_revision="portal-version-is-not-edition"))
        with self.assertRaisesRegex(ValueError, "physical document"):
            self.validate(self.with_reference(physical_pages=(999,)))

    def test_tenant_and_relationship_semantics_must_match_the_composed_tbox(self) -> None:
        with self.assertRaisesRegex(ValueError, "same tenant"):
            self.validate(tbox=replace(self.tbox, tenant_id="other-industrial-tenant"))
        relation = self.corpus.relationships[0]
        changed = replace(relation, predicate="PROVES_CAUSALITY")
        corpus = replace(self.corpus, relationships=(changed, *self.corpus.relationships[1:]))
        with self.assertRaisesRegex(ValueError, "ontology direction"):
            self.validate(corpus)

    def test_omitting_hierarchy_declarations_cannot_bypass_the_model_gate(self) -> None:
        with self.assertRaisesRegex(ValueError, "hierarchy contracts are required"):
            self.validate(tbox=replace(self.tbox, hierarchies=()))

    def test_declared_endpoint_cardinality_is_checked_before_ingestion(self) -> None:
        # The dataset has several assets per project model. Requiring only one
        # asset at the model endpoint must fail before a loader starts writes.
        changed = tuple(
            replace(item, target_cardinality=Cardinality.ZERO_OR_ONE)
            if item.name == "INSTANCE_OF" else item for item in self.tbox.relationship_types
        )
        with self.assertRaisesRegex(ValueError, "endpoint cardinality"):
            self.validate(tbox=replace(self.tbox, relationship_types=changed))

    def test_stale_corpus_checksum_cannot_identify_changed_source_metadata(self) -> None:
        document = replace(self.corpus.documents[0], title="Changed but still valid source title")
        corpus = replace(self.corpus, documents=(document, *self.corpus.documents[1:]))
        with self.assertRaisesRegex(ValueError, "manifest checksum differs"):
            self.validate(corpus)


if __name__ == "__main__":
    unittest.main()
