"""Reviewed evidence, scope, permissions and gold identity before predictions."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.gold import (
    CORPUS_CHECKSUM, DEFAULT_GOLD_PATH, DEFAULT_PDF_GOLD_PATH, EvidenceAnchor,
    GoldPrincipal, GoldValidationError, GradedEvidence, complete_evidence_covered,
    compute_gold_checksum, gold_report, gold_to_dict, load_gold, load_pdf_gold,
    recall_ceiling, validate_gold,
)


class IndustrialGoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = build_corpus()
        cls.gold = load_gold(corpus=cls.corpus)
        cls.cases = {case.case_id: case for case in cls.gold.cases}

    def case(self, number: int):
        return self.cases[f"industrial-gold-{number:03d}"]

    def assert_rejected(self, body: dict, *, pdf: bool = False) -> None:
        # Re-sign deliberate semantic mutations so this tests the invariant,
        # not just the checksum mismatch that any accidental edit would cause.
        digest_body = {key: value for key, value in body.items() if key != "checksum"}
        body["checksum"] = hashlib.sha256(json.dumps(digest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gold.json"
            path.write_text(json.dumps(body, ensure_ascii=False))
            with self.assertRaises(GoldValidationError):
                (load_pdf_gold if pdf else load_gold)(path)

    def test_committed_gold_rebuild_identity_and_manifest(self) -> None:
        again = load_gold()
        self.assertEqual(self.gold, again)
        self.assertEqual(self.gold.corpus_checksum, CORPUS_CHECKSUM)
        self.assertEqual(self.gold.checksum, compute_gold_checksum(self.gold))
        self.assertEqual(gold_report(self.gold), json.loads(DEFAULT_GOLD_PATH.with_name("manifest.json").read_text()))
        self.assertEqual(gold_to_dict(self.gold), json.loads(DEFAULT_GOLD_PATH.read_text()))
        with self.assertRaises(FrozenInstanceError):
            self.case(1).question = "changed"

    def test_all_classes_event_groups_and_security_controls_remain_in_report(self) -> None:
        self.assertEqual(len(self.gold.cases), 72)
        self.assertEqual(Counter(case.split for case in self.gold.cases), {"dev": 36, "holdout": 36})
        self.assertEqual(len({case.question_class for case in self.gold.cases}), 8)
        self.assertEqual(sum(case.answerability == "DENIED" for case in self.gold.cases), 8)
        self.assertEqual(sum(case.answerability == "INSUFFICIENT" for case in self.gold.cases), 8)
        for group in {case.event_group for case in self.gold.cases}:
            self.assertEqual(len({case.split for case in self.gold.cases if case.event_group == group}), 1)
        # Identity facts and product references are intentionally shared. This is
        # an event-group split, not a claim of unseen-document generalization.
        holdout = self.case(3)
        self.assertIn("bkt-a01-register#aliases", holdout.relevance)
        self.assertEqual(holdout.split, "holdout")

    def test_every_anchor_resolves_to_exact_immutable_text_and_current_acl(self) -> None:
        for case in self.gold.cases:
            for anchor in [item.anchor for item in case.evidence] + list(case.forbidden_sections):
                document = self.corpus.document(anchor.document_key)
                section = document.section(anchor.section_key)
                self.assertEqual(document.text[anchor.char_start:anchor.char_end], document.section_text(section.key))
                self.assertEqual(hashlib.sha256(document.text.encode()).hexdigest(), anchor.document_checksum)
            for item in case.evidence:
                document = self.corpus.document(item.anchor.document_key)
                self.assertEqual(case.principal.tenant_id, self.corpus.tenant_id)
                self.assertTrue(set(case.principal.access_groups) & set(document.access_groups))
                self.assertEqual(document.family, case.user_scope.family)

    def test_identity_scope_is_not_an_oracle_and_other_scope_is_explicit(self) -> None:
        for case in self.gold.cases:
            if case.question_class == "identity":
                self.assertEqual(case.user_scope.asset_keys, ())
                self.assertEqual(case.user_scope.origin, "FAMILY_ONLY_IDENTITY")
            else:
                self.assertEqual(case.user_scope.origin, "USER_SUPPLIED")
        raw = gold_to_dict(self.gold)
        raw["cases"][0]["user_scope"]["asset_keys"] = ["asset-bkt-a01"]
        self.assert_rejected(raw)
        raw = gold_to_dict(self.gold)
        raw["cases"][8]["user_scope"]["asset_keys"] = ["asset-hvx-a01"]
        self.assert_rejected(raw)

    def test_answerability_does_not_erase_positive_missing_evidence(self) -> None:
        for case in self.gold.cases:
            if case.answerability == "INSUFFICIENT":
                self.assertTrue(case.evidence)
                self.assertTrue(case.required_evidence_sets)
        self.assertEqual(self.case(3).answerability, "AMBIGUOUS")
        self.assertEqual(self.case(28).answerability, "SUPPORTED")
        self.assertEqual(self.case(52).answerability, "INSUFFICIENT")

    def test_complete_sets_are_alternatives_not_all_relevant_sections(self) -> None:
        identity = self.case(1)
        self.assertTrue(complete_evidence_covered(identity, ["bkt-a01-register#identity"]))
        self.assertTrue(complete_evidence_covered(identity, ["bkt-a01-register#site", "bkt-a01-register#component"]))
        self.assertFalse(complete_evidence_covered(identity, ["bkt-a01-register#site"]))
        values = self.case(10)
        self.assertTrue(complete_evidence_covered(values, ["bkt-a02-inspection#observation", "bkt-a02-followup#instrument-compare"]))
        self.assertFalse(complete_evidence_covered(values, ["bkt-a02-inspection#observation", "bkt-a02-followup#evidence-chain"]))
        self.assertFalse(complete_evidence_covered(self.case(57), ["bkt-a01-register#identity"]))
        self.assertTrue(complete_evidence_covered(self.case(3), ["bkt-a01-handover#identity-result"]))
        self.assertTrue(complete_evidence_covered(self.case(6), ["hvx-a02-register#site", "hvx-b02-register#identity"]))

    def test_standard_recall_ceiling_is_visible_without_changing_targets(self) -> None:
        self.assertEqual(recall_ceiling(1), 1.0)
        self.assertEqual(recall_ceiling(5), 1.0)
        self.assertAlmostEqual(recall_ceiling(6), 5 / 6)
        self.assertAlmostEqual(recall_ceiling(10), 0.5)
        self.assertIsNone(recall_ceiling(0))
        self.assertGreater(len(self.case(1).evidence), 5)
        report = gold_report(self.gold)
        self.assertLess(report["splits"]["dev"]["mean_recall_at_5_ceiling"], 1.0)
        self.assertEqual(report["splits"]["all"]["positive_target_cases"], 64)
        self.assertEqual(len(report["case_recall_ceilings"]), 72)
        for invalid in (True, 1.5, -1):
            with self.assertRaises(GoldValidationError):
                recall_ceiling(invalid)

    def test_cutoff_is_initial_publication_and_later_evidence_is_rejected(self) -> None:
        case = self.case(33)
        self.assertEqual(case.user_scope.published_at_lte, "2026-04-11T18:00:00+08:00")
        for item in case.evidence:
            self.assertLessEqual(datetime.fromisoformat(self.corpus.document(item.anchor.document_key).published_at), datetime.fromisoformat(case.user_scope.published_at_lte))
        raw = gold_to_dict(self.gold)
        future = next(item for item in raw["cases"][24]["evidence"] if item["anchor"]["document_key"] == "bkt-a01-followup")
        raw["cases"][32]["evidence"].append(future)
        raw["cases"][32]["evidence"].sort(key=lambda item: item["anchor"]["document_key"] + "#" + item["anchor"]["section_key"])
        self.assert_rejected(raw)

    def test_denied_pairs_preserve_allowed_population_and_permitted_shared_facts(self) -> None:
        for n in range(57, 65):
            denied, allowed = self.case(n), self.case(n + 8)
            self.assertEqual(denied.question, allowed.question)
            self.assertEqual(denied.user_scope, allowed.user_scope)
            self.assertEqual(denied.split, allowed.split)
            self.assertEqual(denied.paired_case_id, allowed.case_id)
            self.assertFalse(denied.evidence)
            self.assertTrue(allowed.evidence)
        foreign = self.case(64)
        self.assertEqual(foreign.principal, GoldPrincipal("tenant-alpha", ("public",)))
        # Raw CHMAP rows are protected; these registration facts are separately
        # authorized and therefore cannot be a blanket forbidden-token list.
        visible = self.corpus.document("bkt-b02-register")
        self.assertIn("SN-T09", visible.text)
        self.assertTrue(set(visible.access_groups) & set(self.case(60).principal.access_groups))
        self.assertNotIn(visible.key, {anchor.document_key for anchor in self.case(60).forbidden_sections})

    def test_tampered_source_range_pin_grade_or_acl_fails_even_when_resigned(self) -> None:
        for mutation in ("range", "document", "grade", "acl", "corpus", "shape", "unexpected"):
            with self.subTest(mutation=mutation):
                raw = gold_to_dict(self.gold)
                if mutation == "range":
                    raw["cases"][0]["evidence"][0]["anchor"]["char_end"] += 1
                elif mutation == "document":
                    raw["cases"][0]["evidence"][0]["anchor"]["document_checksum"] = "0" * 64
                elif mutation == "grade":
                    raw["cases"][0]["evidence"][0]["grade"] = True
                elif mutation == "acl":
                    raw["cases"][8]["principal"]["access_groups"] = ["public"]
                elif mutation == "corpus":
                    raw["corpus_checksum"] = "0" * 64
                elif mutation == "shape":
                    raw["cases"][0]["principal"]["access_groups"] = {"engineering": "ignored", "public": "ignored"}
                else:
                    raw["cases"][0]["evidence"][0]["prediction"] = "must not be silently dropped"
                self.assert_rejected(raw)

    def test_missing_case_split_pair_or_redundant_set_is_rejected(self) -> None:
        for mutation in ("missing", "split", "pair", "redundant"):
            raw = gold_to_dict(self.gold)
            if mutation == "missing":
                raw["cases"].pop()
            elif mutation == "split":
                raw["cases"][0]["split"] = "holdout"
                raw["cases"][2]["split"] = "dev"
            elif mutation == "pair":
                raw["cases"][56]["paired_case_id"] = "industrial-gold-066"
            else:
                raw["cases"][0]["required_evidence_sets"].append(["bkt-a01-register#identity", "bkt-a01-register#site"])
            with self.subTest(mutation=mutation):
                self.assert_rejected(raw)

    def test_gold_and_supplied_corpus_cached_checksums_are_not_trusted(self) -> None:
        with self.assertRaises(GoldValidationError):
            validate_gold(replace(self.gold, checksum="0" * 64), corpus=self.corpus)
        changed = replace(self.corpus.documents[0], title="Changed source identity")
        corpus = replace(self.corpus, documents=(changed,) + self.corpus.documents[1:])
        with self.assertRaises(GoldValidationError):
            validate_gold(self.gold, corpus=corpus)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gold.json"
            path.write_text('{"version":"one","version":"two"}')
            with self.assertRaisesRegex(GoldValidationError, "duplicate"):
                load_gold(path)

    def test_pdf_cases_pin_multifragment_sources_but_do_not_require_cache(self) -> None:
        raw = load_pdf_gold()
        self.assertEqual(len(raw["cases"]), 3)
        self.assertTrue(raw["requires_external_cache"])
        self.assertFalse(raw["included_in_primary_gold_metrics"])
        self.assertEqual(raw["cases"][0]["physical_pages"], [71, 72])
        self.assertEqual([chunk["ordinal"] for chunk in raw["cases"][1]["required_chunks"]], [15, 16])
        self.assertEqual([chunk["ordinal"] for chunk in raw["cases"][2]["required_chunks"]], [15, 16, 17])
        corrupted = deepcopy(raw)
        corrupted["cases"][0]["original_checksum"] = "0" * 64
        self.assert_rejected(corrupted, pdf=True)
        corrupted = deepcopy(raw)
        corrupted["cases"][1]["required_chunks"][0]["page_number"] = True
        self.assert_rejected(corrupted, pdf=True)
        self.assertTrue(DEFAULT_PDF_GOLD_PATH.is_file())

    def test_source_scalar_types_reject_bool_and_fractional_locations(self) -> None:
        anchor = self.case(1).evidence[0].anchor
        for start in (True, 0.5):
            with self.assertRaises(GoldValidationError):
                EvidenceAnchor(anchor.document_key, anchor.section_key, start, anchor.char_end, anchor.document_checksum)
        for grade in (False, 1.0, 0, 4):
            with self.assertRaises(GoldValidationError):
                GradedEvidence(anchor, grade, "source relevance")


if __name__ == "__main__":
    unittest.main()
