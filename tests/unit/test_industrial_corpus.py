"""Corpus scale, source identity, exact ranges, and honest scenario evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from graphrag_prod.industrial.corpus import (
    ACCESS_GROUPS, DEFAULT_CORPUS_ROOT, FAMILY_TO_CONTRACT, CorpusSection,
    CorpusValidationError, SourceReference, build_corpus, compute_corpus_checksum,
    corpus_report, validate_corpus,
)


class IndustrialCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = build_corpus()

    def test_clean_rebuild_matches_committed_manifest_and_is_deterministic(self) -> None:
        second = build_corpus()
        self.assertEqual(self.corpus, second)
        committed = json.loads((DEFAULT_CORPUS_ROOT / "manifest.json").read_text())
        self.assertEqual(corpus_report(self.corpus), committed)
        self.assertEqual(compute_corpus_checksum(self.corpus), self.corpus.manifest_checksum)

    def test_representative_scale_has_substantial_distinct_context(self) -> None:
        self.assertGreaterEqual(len(self.corpus.documents), 32)
        self.assertTrue(300 <= self.corpus.chunk_count <= 1_000)
        self.assertEqual({d.family for d in self.corpus.documents}, set(FAMILY_TO_CONTRACT))
        self.assertEqual({g for d in self.corpus.documents for g in d.access_groups}, set(ACCESS_GROUPS))
        field = [d for d in self.corpus.documents if d.source_kind == "SYNTHETIC_FIELD_RECORD"]
        sections = [d.section_text(s.key) for d in field for s in d.sections]
        self.assertEqual(len(field), 32)
        self.assertGreaterEqual(min(map(len, sections)), 90)
        self.assertGreater(sum(map(len, sections)), 145 * len(sections))
        self.assertEqual(len(sections), len(set(sections)))
        self.assertLessEqual(corpus_report(self.corpus)["counts"]["record_upper_bound"], 500)

    def test_repeated_policy_sentences_cannot_supply_most_field_content(self) -> None:
        sentences = [
            sentence.strip()
            for document in self.corpus.documents
            if document.source_kind == "SYNTHETIC_FIELD_RECORD"
            for sentence in re.split(r"[。！？\n]+", document.text[document.text.index("## "):])
            if len(sentence.strip()) >= 25 and not sentence.strip().startswith("#")
        ]
        frequencies = Counter(sentences)
        repeated_characters = sum(len(sentence) for sentence in sentences if frequencies[sentence] >= 4)
        self.assertLess(repeated_characters / sum(map(len, sentences)), 0.20)

    def test_every_section_is_an_exact_gapless_source_slice(self) -> None:
        for document in self.corpus.documents:
            with self.subTest(document=document.key):
                self.assertEqual(document.sections[0].char_start, 0)
                self.assertEqual(document.sections[-1].char_end, len(document.text))
                self.assertEqual("".join(document.section_text(s.key) for s in document.sections), document.text)
                for left, right in zip(document.sections, document.sections[1:]):
                    self.assertEqual(left.char_end, right.char_start)
                self.assertTrue(all(s.char_end - s.char_start <= 1_200 for s in document.sections))

    def test_source_kinds_do_not_turn_synthetic_values_into_official_rules(self) -> None:
        for document in self.corpus.documents:
            if document.source_kind == "CURATED_REFERENCE":
                self.assertIn("SME_REVIEW_PENDING", document.text)
                self.assertIn("项目整理（AI辅助，待领域专家审核）", document.text)
                self.assertTrue(document.source_refs)
            else:
                self.assertIn("合成", document.text)
                self.assertIn("SYNTHETIC_FIELD_RECORD", document.text)
                self.assertEqual(document.source_refs, ())
        bus = self.corpus.document("bkt-a01-inspection")
        self.assertIn("48.2 °C", bus.section_text("measurements"))
        self.assertIn("910 A", bus.section_text("measurements"))
        self.assertIn("没有附带制造商阈值", bus.section_text("measurements"))
        self.assertIn("原因待定", bus.section_text("disposition"))

    def test_homonyms_and_permissions_are_real_distinct_cases(self) -> None:
        assets = [e for e in self.corpus.entities if e.type_name == "InstalledAsset"]
        self.assertEqual(len(assets), 8)
        for alias in ("一号母线", "进线断路器"):
            matches = [e for e in assets if alias in e.aliases]
            self.assertEqual(len(matches), 2)
            self.assertEqual(len({e.identity for e in matches}), 2)
        protected = "OPS-A01-THERM-481"
        containing = [d for d in self.corpus.documents if protected in d.text]
        self.assertTrue(containing)
        self.assertTrue(all(d.access_groups == ("maintenance",) for d in containing))

    def test_primary_asset_scope_is_explicit_and_is_not_all_mentions(self) -> None:
        document = self.corpus.document("bkt-a01-register")
        self.assertIn("BKT-B01", document.text)
        self.assertEqual(document.asset_keys, ("asset-bkt-a01",))
        for item in self.corpus.documents:
            self.assertEqual(len(item.asset_keys), int(item.source_kind == "SYNTHETIC_FIELD_RECORD"))
        for keys in ((), ("asset-does-not-exist",), ("component-jb-a01",), ("asset-hvx-a01",)):
            altered = replace(document, asset_keys=keys)
            corpus = replace(self.corpus, documents=tuple(altered if d.key == document.key else d for d in self.corpus.documents))
            with self.assertRaises(CorpusValidationError):
                validate_corpus(corpus)
        changed = replace(document, asset_keys=("asset-bkt-b01",))
        changed_corpus = replace(self.corpus, documents=tuple(changed if d.key == document.key else d for d in self.corpus.documents))
        self.assertNotEqual(compute_corpus_checksum(changed_corpus), self.corpus.manifest_checksum)

    def test_temporal_conflict_and_wrong_family_evidence_remain_visible(self) -> None:
        old = self.corpus.document("bkt-b02-inspection")
        review = self.corpus.document("bkt-b02-followup")
        self.assertIn("52.0 °C", old.text)
        self.assertIn("TT-07", old.text)
        self.assertIn("撤回但原文件不删除", review.text)
        self.assertGreater(review.published_at, old.published_at)
        self.assertIn("SureSeT 5/15 kV", self.corpus.document("hvx-a01-register").text)
        self.assertIn("待 OCR", self.corpus.document("hvx-a02-register").text)
        self.assertIn("HVX-O", self.corpus.document("hvx-b02-register").text)

    def test_positive_mapping_result_preserves_prior_uncertainty_and_sample_times(self) -> None:
        initial = self.corpus.document("bkt-b02-inspection")
        followup = self.corpus.document("bkt-b02-followup")
        self.assertIn("尚未签发映射修正结论", initial.section_text("conflict"))
        self.assertNotIn("归属修正", initial.section_text("missing-evidence"))
        self.assertIn("已确认合成报表的通道映射错误", followup.section_text("candidate-status"))
        samples = followup.section_text("repeat")
        self.assertIn("time=2026-04-14T16:25:00+08:00 | TT-09 | 38.6°C", samples)
        self.assertIn("time=2026-04-18T16:25:00+08:00 | TT-09 | 39.2°C", samples)
        self.assertIn("TT-07 | SN-T07 | TST-B02", samples)
        self.assertIn("TT-09 | SN-T09 | JB-B02", samples)
        self.assertIn("未签注设备健康状态", followup.section_text("candidate-status"))
        hvx = self.corpus.document("hvx-a01-followup")
        self.assertIn("RESOLVED_MODE_GATING", hvx.section_text("status"))
        self.assertIn("REJECT=LOCAL_MODE", hvx.section_text("repeat"))
        self.assertIn("未提交硬件诊断", hvx.section_text("status"))

    def test_later_observations_do_not_appear_in_immutable_first_reports(self) -> None:
        for asset, later_artifact, later_detail in (
            ("bkt-a01", "TH-A01-0415", "44.5 °C"),
            ("bkt-a02", "ENV-A02-0416-01", "地面已干"),
            ("bkt-b01", "VIS-B01-0417", "DAYLIGHT"),
        ):
            initial = self.corpus.document(f"{asset}-inspection")
            later = self.corpus.document(f"{asset}-followup")
            with self.subTest(asset=asset):
                self.assertNotIn(later_artifact, initial.text)
                self.assertNotIn(later_detail, initial.text)
                self.assertIn(later_artifact, later.text)
                self.assertIn(later_detail, later.text)
                self.assertGreater(datetime.fromisoformat(later.published_at), datetime.fromisoformat(initial.published_at))
        for document in self.corpus.documents:
            if not document.key.endswith("-inspection"):
                continue
            event = re.search(r"2026-04-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", document.section_text("event"))
            self.assertIsNotNone(event)
            event_time = datetime.fromisoformat(event.group())
            published_time = datetime.fromisoformat(document.published_at)
            self.assertEqual(event_time.date(), published_time.date())
            self.assertGreater(published_time, event_time)
        self.assertIn("修订草案", self.corpus.document("bkt-a01-inspection").section_text("conflict"))
        self.assertIn("修订完成状态由后续复查记录确认", self.corpus.document("bkt-a02-inspection").section_text("conflict"))
        self.assertIn("索引纠正尚未完成", self.corpus.document("bkt-b01-inspection").section_text("conflict"))

    def test_observations_are_event_scoped_and_all_edges_have_exact_evidence(self) -> None:
        by_key = {e.key: e for e in self.corpus.entities}
        self.assertEqual(sum(e.type_name == "Observation" for e in by_key.values()), 8)
        self.assertEqual(sum(e.type_name == "InspectionEvent" for e in by_key.values()), 8)
        self.assertEqual(sum(r.predicate == "OBSERVES" for r in self.corpus.relationships), 8)
        self.assertFalse(any(r.predicate == "HAS_SYMPTOM" for r in self.corpus.relationships))
        for relation in self.corpus.relationships:
            evidence = self.corpus.document(relation.document_key).section_text(relation.section_key)
            self.assertIn(by_key[relation.subject_key].name, evidence)
            self.assertIn(by_key[relation.object_key].name, evidence)
        self.assertTrue(any(r.predicate == "SUBTYPE_OF" for r in self.corpus.relationships))
        self.assertTrue(any(r.predicate == "CHECKED_BY" for r in self.corpus.relationships))
        connections = [r for r in self.corpus.relationships if r.predicate == "CONNECTS_TO"]
        self.assertEqual(len(connections), 2)
        self.assertTrue(all(by_key[r.subject_key].type_name == "Component" and by_key[r.object_key].type_name == "Component" for r in connections))

    def test_missing_evidence_and_repeated_scale_fail_validation(self) -> None:
        relation = replace(self.corpus.relationships[0], section_key="does-not-exist")
        with self.assertRaises(CorpusValidationError):
            validate_corpus(replace(self.corpus, relationships=(relation,) + self.corpus.relationships[1:]))
        with self.assertRaises(CorpusValidationError):
            validate_corpus(replace(self.corpus, documents=self.corpus.documents[:31]))
        changed = replace(self.corpus, entities=(replace(self.corpus.entities[0], name="unsubstantiated-name"),) + self.corpus.entities[1:])
        with self.assertRaises(CorpusValidationError):
            validate_corpus(changed)
        self.assertNotEqual(compute_corpus_checksum(changed), self.corpus.manifest_checksum)

    def test_path_traversal_and_duplicate_index_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "corpus"
            shutil.copytree(DEFAULT_CORPUS_ROOT, root)
            index = json.loads((root / "index.json").read_text())
            index["documents"][0]["file"] = "../sources.json"
            (root / "index.json").write_text(json.dumps(index))
            with self.assertRaises(CorpusValidationError):
                build_corpus(root)
            (root / "index.json").write_text('{"version":"one","version":"two"}')
            with self.assertRaisesRegex(CorpusValidationError, "duplicate"):
                build_corpus(root)

    def test_scalars_and_immutable_sections_fail_early(self) -> None:
        for args in (("x-section", "title", False, 2), ("x-section", "title", 0.0, 2), ("x-section", "title", 0, 1201)):
            with self.assertRaises(CorpusValidationError):
                CorpusSection(*args)
        with self.assertRaises(CorpusValidationError):
            SourceReference("source-id", (True,), "v1")
        for keys in (["asset-bkt-a01"], (True,), ("asset-bkt-a01", "asset-bkt-a01")):
            with self.assertRaises(CorpusValidationError):
                replace(self.corpus.document("bkt-a01-register"), asset_keys=keys)
        with self.assertRaises(FrozenInstanceError):
            self.corpus.documents[0].title = "changed"


if __name__ == "__main__":
    unittest.main()
