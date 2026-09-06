"""Industrial bootstrap contracts and hostile normalized-artifact checks."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.loading import (
    IndustrialLoadConflict, Neo4jIndustrialLoader, _prepare_industrial_load,
    _bounded_regular_bytes, prepare_industrial_load, validate_normalized_artifact,
    _require_reference_revision, industrial_loader_principal, REVIEW_NOTE,
)
from graphrag_prod.industrial.normalization import normalize_industrial_source
from graphrag_prod.industrial.provenance import digest
from graphrag_prod.industrial.sources import load_source_catalog
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.models import RecordRevision
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus, KnowledgeOrigin
from tests.fixtures.industrial_loading import NOW, PROFILE, tiny_corpus
from tests.unit.test_pdf_parser import _pdf, _text


ROOT = Path(__file__).resolve().parents[2]


class IndustrialLoadingTests(unittest.TestCase):
    def test_medium_corpus_produces_bounded_source_and_complete_governed_manifest(self):
        corpus = build_corpus()
        kwargs = dict(ingested_at=NOW, catalog=load_source_catalog(ROOT / "datasets/industrial-v1/sources.json"),
                      contract=json.loads((ROOT / "contracts/industrial_knowledge.v1.json").read_text()))
        first = prepare_industrial_load(corpus, PROFILE, **kwargs)
        second = prepare_industrial_load(corpus, PROFILE, **kwargs)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.report()["chunks"], 300)
        self.assertLessEqual(first.report()["publication_records"], 500)
        self.assertGreater(first.report()["authoritative_records"], 0)
        self.assertGreater(first.report()["secondary_records"], 0)
        self.assertEqual(first.report()["authored_documents"], len(corpus.documents))
        self.assertEqual(first.report()["official_originals"], 0)
        self.assertEqual(first.report()["official_excerpts"], 0)
        for source in first.sources:
            self.assertEqual(source.request.normalized_text, "".join(seed.text for seed in source.request.chunks))
            self.assertNotIn("finance", source.request.access_groups)
            authored = corpus.document(source.key)
            provenance = json.loads(source.provenance_json)
            self.assertEqual(provenance["family"], authored.family)
            self.assertEqual(provenance["asset_keys"], list(authored.asset_keys))
            self.assertEqual(provenance["sections"], [asdict(section) for section in authored.sections])
        for batch in first.batches:
            for record in (*batch.mentions, *batch.assertions):
                request = next(item.request for item in first.sources if item.request.document_id == record.evidence.document_id)
                self.assertEqual(request.normalized_text[record.evidence.char_start:record.evidence.char_end], record.evidence.quoted_text)

    def test_public_load_rejects_small_fixture_before_any_database_or_provider_call(self):
        driver, provider = Mock(), Mock()
        with self.assertRaises(ValueError):
            Neo4jIndustrialLoader(driver).load(tiny_corpus(), PROFILE, provider,
                catalog=load_source_catalog(ROOT / "datasets/industrial-v1/sources.json"),
                contract=json.loads((ROOT / "contracts/industrial_knowledge.v1.json").read_text()))
        driver.assert_not_called()
        self.assertEqual(driver.mock_calls, [])
        provider.assert_not_called()

    def test_rule_lane_preserves_secondary_and_rejects_authority_or_llm_mislabel(self):
        plan = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW)
        store = Neo4jKnowledgeStore(Mock())
        store._write_batch = Mock(return_value="persisted")
        secondary = next(batch for batch in plan.batches if batch.mentions[0].trust.authority is AuthorityLevel.SECONDARY)
        self.assertEqual(store.persist_rule_candidates(secondary), "persisted")
        self.assertTrue(all(item.trust.origin is KnowledgeOrigin.RULE_DERIVED for item in (*secondary.mentions, *secondary.assertions)))
        authoritative = next(batch for batch in plan.batches if batch.mentions[0].trust.authority is AuthorityLevel.AUTHORITATIVE)
        with self.assertRaises(ValueError):
            store.persist_rule_candidates(authoritative)
        with self.assertRaises(ValueError):
            store.persist_llm_candidates(secondary)

    def test_same_number_independent_review_and_payload_changes_are_not_reference_replays(self):
        plan = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW)
        original = next(batch.mentions[0] for batch in plan.batches
                        if batch.mentions[0].trust.authority is AuthorityLevel.SECONDARY)
        principal = industrial_loader_principal()
        reviewed = replace(original,
            revision=RecordRevision.next(original.record_id, 1),
            trust=original.trust.transition_to(GovernanceStatus.APPROVED,
                reviewed_by=principal.principal_id, reviewed_at=NOW, review_notes=REVIEW_NOTE))
        _require_reference_revision(original, reviewed, principal, NOW)
        independently_reviewed = replace(reviewed,
            trust=replace(reviewed.trust, reviewed_by="independent-expert"))
        with self.assertRaisesRegex(IndustrialLoadConflict, "independent reviewer"):
            _require_reference_revision(original, independently_reviewed, principal, NOW)
        changed = replace(reviewed, confidence=0.7)
        with self.assertRaisesRegex(IndustrialLoadConflict, "payload"):
            _require_reference_revision(original, changed, principal, NOW)

    def test_missing_exact_endpoint_and_wrong_tenant_fail_preparation(self):
        corpus = tiny_corpus()
        with self.assertRaises(IndustrialLoadConflict):
            _prepare_industrial_load(replace(corpus, tenant_id="finance"), PROFILE, ingested_at=NOW)
        entities = list(corpus.entities)
        entities[-1] = replace(entities[-1], name="AbsentSite")
        with self.assertRaisesRegex(IndustrialLoadConflict, "absent"):
            _prepare_industrial_load(replace(corpus, entities=tuple(entities)), PROFILE, ingested_at=NOW)

    def test_original_reparse_rejects_forged_but_self_consistent_text(self):
        payload = _pdf((_text("A reliable source with stable numeric value 1250 A."),))
        catalog = load_source_catalog(ROOT / "datasets/industrial-v1/sources.json")
        from graphrag_prod.domain.ids import content_checksum
        source = replace(catalog.sources[0], sha256=content_checksum(payload), byte_size=len(payload),
                         physical_pages=1, selected_page_ranges=())
        catalog = replace(catalog, sources=(source,))
        artifact = normalize_industrial_source(source, payload, selected_pages=(1,), chunk_chars=400)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / f"{source.source_id}.pdf").write_bytes(payload)
            genuine = validate_normalized_artifact(artifact, catalog, original_cache=cache)
            self.assertIn("1250 A", genuine.normalized_text)
            forged = deepcopy(artifact)
            forged["normalized_text"] = forged["normalized_text"].replace("1250", "9999")
            for chunk in forged["chunks"]:
                chunk["text"] = chunk["text"].replace("1250", "9999")
            forged["normalized_checksum"] = content_checksum(forged["normalized_text"])
            forged["artifact_checksum"] = digest({key: value for key, value in forged.items() if key != "artifact_checksum"})
            with self.assertRaisesRegex(IndustrialLoadConflict, "independently parsed"):
                validate_normalized_artifact(forged, catalog, original_cache=cache)
            injected = {**artifact, "tenant_id": "other"}
            with self.assertRaisesRegex(IndustrialLoadConflict, "unexpected fields"):
                validate_normalized_artifact(injected, catalog, original_cache=cache)
            for field in ("ordinal", "char_start", "char_end", "page_number"):
                for scalar in (True, 1.0):
                    malformed = deepcopy(artifact)
                    malformed["chunks"][0][field] = scalar
                    malformed["artifact_checksum"] = digest({key: value for key, value in malformed.items() if key != "artifact_checksum"})
                    with self.assertRaisesRegex(IndustrialLoadConflict, "must be integers"):
                        validate_normalized_artifact(malformed, catalog, original_cache=cache)
            with self.assertRaisesRegex(IndustrialLoadConflict, "original cache"):
                validate_normalized_artifact(artifact, catalog)
            bad_map = deepcopy(artifact)
            bad_map["chunks"][0]["page_number"] = 2
            bad_map["artifact_checksum"] = digest({key: value for key, value in bad_map.items() if key != "artifact_checksum"})
            with self.assertRaisesRegex(IndustrialLoadConflict, "exact ranges"):
                validate_normalized_artifact(bad_map, catalog, original_cache=cache)

    def test_cache_reads_are_bounded_and_reject_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original"
            source.write_bytes(b"0123456789")
            with self.assertRaises(IndustrialLoadConflict):
                _bounded_regular_bytes(source, 5)
            link = Path(directory) / "linked"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                _bounded_regular_bytes(link, 100)


if __name__ == "__main__":
    unittest.main()
