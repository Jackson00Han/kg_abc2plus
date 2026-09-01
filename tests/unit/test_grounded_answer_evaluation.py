"""Gold-data and hand-computable tests for grounded-answer evaluation."""

from __future__ import annotations

from collections import Counter
from contextlib import redirect_stderr
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import unittest

from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from scripts.build_dev_corpus import (
    ANSWER_GOLD_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    GENERATOR_VERSION,
    build_dataset,
    validate_build,
)
from scripts.evaluate_grounded_answers import (
    CITATION_LOCATION_FIELDS,
    STANDARD_REFUSAL_ANSWER,
    answer_gate_failures,
    calculate_answer_metrics,
    evaluate_answer_results,
    parse_args,
    validate_gold_dataset,
)


def _citation(source: dict, citation_id: str) -> dict:
    return {
        "citation_id": citation_id,
        **{field: source[field] for field in CITATION_LOCATION_FIELDS},
    }


def _perfect_results(gold_items: tuple[dict, ...]) -> list[dict]:
    """Build runtime-shaped results in memory; these are never gold predictions."""
    results: list[dict] = []
    for gold in gold_items:
        if gold["expected_status"] == "insufficient_context":
            results.append(
                {
                    "answer": STANDARD_REFUSAL_ANSWER,
                    "citations": [],
                    "claims": [],
                    "conflicts": [],
                    "failure_code": None,
                    "id": gold["id"],
                    "output_schema_version": OUTPUT_SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "status": "insufficient_context",
                }
            )
            continue

        evidence = {item["chunk_id"]: item for item in gold["evidence"]}
        citations: list[dict] = []
        citation_ids_by_chunk: dict[str, str] = {}
        claims: list[dict] = []
        rendered: list[str] = []
        for gold_claim in gold["claims"]:
            chunk_id = gold_claim["evidence_chunk_ids"][0]
            citation_id = citation_ids_by_chunk.get(chunk_id)
            if citation_id is None:
                citation_id = f"S{len(citations) + 1}"
                citation_ids_by_chunk[chunk_id] = citation_id
                citations.append(_citation(evidence[chunk_id], citation_id))
            text = gold_claim["reference_text"]
            claims.append(
                {
                    "citation_ids": [citation_id],
                    "inference": gold_claim["inference"],
                    "material": True,
                    "text": text,
                }
            )
            prefix = "Inference: " if gold_claim["inference"] else ""
            rendered.append(f"{prefix}{text} [{citation_id}]")
        conflicts = []
        if gold["expected_status"] == "conflict":
            conflicts.append(
                {
                    "claim_indexes": list(range(len(claims))),
                    "topic": "Unresolved same-scope source statements",
                }
            )
        answer = "\n".join(rendered)
        if conflicts:
            answer = "Conflicting source statements:\n" + answer
        results.append(
            {
                "answer": answer,
                "citations": citations,
                "claims": claims,
                "conflicts": conflicts,
                "failure_code": None,
                "id": gold["id"],
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "status": gold["expected_status"],
            }
        )
    return results


def _source(key: str, document: str) -> dict:
    return {
        "canonical_uri": f"urn:test:{document}",
        "char_end": 100,
        "char_start": 20,
        "chunk_checksum": f"checksum-{key}",
        "chunk_id": key,
        "document_id": document,
        "document_title": f"{document} title",
        "ordinal": 0,
        "page_number": 1,
        "published_at": "2024-11-15T00:00:00+00:00",
        "section": "Facts",
        "source_name": "hand-computable fixture",
        "version_checksum": f"version-checksum-{document}",
        "version_id": f"version-{document}",
        "version_number": 1,
    }


def _claim(text: str, source: dict, *, exact_tokens: tuple[str, ...] = ()) -> dict:
    return {
        "evidence_chunk_ids": [source["chunk_id"]],
        "exact_tokens": list(exact_tokens),
        "inference": False,
        "material": True,
        "reference_text": text,
        "required_terms": [text.split()[0], *exact_tokens],
    }


def _gold_case(
    item_id: str,
    status: str,
    claims: list[dict],
    evidence: list[dict],
) -> dict:
    return {
        "claims": claims,
        "conflict": {"required": True} if status == "conflict" else None,
        "evidence": evidence,
        "expected_status": status,
        "forbidden_answer_terms": [],
        "id": item_id,
        "temporal_comparison": None,
    }


def _answered_result(item_id: str, text: str, source: dict) -> dict:
    return {
        "answer": f"{text} [S1]",
        "citations": [_citation(source, "S1")],
        "claims": [
            {
                "citation_ids": ["S1"],
                "inference": False,
                "material": True,
                "text": text,
            }
        ],
        "conflicts": [],
        "failure_code": None,
        "id": item_id,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "answered",
    }


def _refusal_result(item_id: str, *, failure_code: str | None = None) -> dict:
    return {
        "answer": STANDARD_REFUSAL_ANSWER,
        "citations": [],
        "claims": [],
        "conflicts": [],
        "failure_code": failure_code,
        "id": item_id,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "status": "insufficient_context",
    }


class GroundedAnswerGoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = build_dataset()

    def test_gold_is_independent_versioned_and_binds_all_49_cases(self) -> None:
        answers = self.build.answers
        questions = {item["id"]: item for item in self.build.questions}
        self.assertEqual(len(answers), 49)
        self.assertEqual({item["id"] for item in answers}, set(questions))
        self.assertEqual(
            Counter(item["expected_status"] for item in answers),
            {"answered": 35, "insufficient_context": 14},
        )
        self.assertEqual(
            self.build.manifest["answer_gold"],
            {
                "case_id_field": "id",
                "contains_predictions": False,
                "evidence_policy": "direct_claim_support",
                "evidence_unit": "chunk",
                "path": "answers.jsonl",
                "version": ANSWER_GOLD_VERSION,
            },
        )
        self.assertEqual(DATASET_VERSION, "1.0.1")
        self.assertEqual(ANSWER_GOLD_VERSION, "1.1.0")
        self.assertEqual(GENERATOR_VERSION, "1.3.0")
        self.assertEqual(
            self.build.manifest["generated_by"]["version"], GENERATOR_VERSION
        )
        for item in answers:
            self.assertEqual(item["corpus_id"], DATASET_ID)
            self.assertEqual(item["corpus_version"], DATASET_VERSION)
            self.assertEqual(item["gold_version"], ANSWER_GOLD_VERSION)
            self.assertEqual(item["query"], questions[item["id"]]["query"])
            self.assertFalse(
                {"actual_result", "predicted_answer", "prediction"} & set(item)
            )
        validate_gold_dataset(answers)

    def test_evidence_exactly_binds_complete_citation_provenance(self) -> None:
        chunks = {item["chunk_id"]: item for item in self.build.chunks}
        documents = {item["document_id"]: item for item in self.build.documents}
        for gold in self.build.answers:
            for source in gold["evidence"]:
                chunk = chunks[source["chunk_id"]]
                document = documents[chunk["document_id"]]
                expected = {
                    "canonical_uri": document["canonical_uri"],
                    "char_end": chunk["char_end"],
                    "char_start": chunk["char_start"],
                    "chunk_checksum": chunk["checksum"],
                    "chunk_id": chunk["chunk_id"],
                    "chunk_key": chunk["chunk_key"],
                    "document_id": document["document_id"],
                    "document_title": document["title"],
                    "ordinal": chunk["ordinal"],
                    "page_number": chunk["page_number"],
                    "published_at": document["published_at"],
                    "section": chunk["section"],
                    "source_name": document["source_name"],
                    "version_checksum": document["checksum"],
                    "version_id": document["version_id"],
                    "version_number": document["version_number"],
                }
                self.assertEqual(source, expected)

    def test_temporal_comparisons_are_answers_with_labelled_inference(self) -> None:
        temporal = [
            item
            for item in self.build.answers
            if item["temporal_comparison"] is not None
        ]
        self.assertEqual(len(temporal), 5)
        for item in temporal:
            self.assertEqual(item["expected_status"], "answered")
            self.assertIsNone(item["conflict"])
            inference_claims = [claim for claim in item["claims"] if claim["inference"]]
            self.assertEqual(len(inference_claims), 1)
            self.assertFalse(
                inference_claims[0]["reference_text"].startswith("Inference:")
            )
            self.assertIn(
                f"Inference: {inference_claims[0]['reference_text']}",
                item["reference_answer"],
            )
            for period in item["temporal_comparison"]["required_periods"]:
                self.assertIn(period, item["reference_answer"])

    def test_exact_values_cover_number_date_currency_and_unit_tokens(self) -> None:
        exact_cases = [
            item for item in self.build.answers if item["question_class"] == "exact_value"
        ]
        self.assertEqual(len(exact_cases), 7)
        for item in exact_cases:
            tokens = item["required_exact_tokens"]
            self.assertTrue(any(token.startswith("$") or token.endswith("%") for token in tokens))
            self.assertTrue(any("billion" in token or token.endswith("%") for token in tokens))
            self.assertTrue(any("fiscal year" in token for token in tokens))

    def test_sourced_claim_lexical_drift_is_rejected_offline(self) -> None:
        answers = deepcopy(self.build.answers)
        target = next(
            item
            for item in answers
            if item["claims"] and not item["claims"][0]["inference"]
        )
        original = target["claims"][0]["reference_text"]
        target["claims"][0]["reference_text"] = original + " fabricatedword"
        target["reference_answer"] = target["reference_answer"].replace(
            original, original + " fabricatedword"
        )
        invalid = replace(self.build, answers=tuple(answers))
        self.assertTrue(
            any("tokens absent" in error for error in validate_build(invalid))
        )

    def test_gold_rejects_duplicate_claims_and_indirect_evidence(self) -> None:
        duplicated_answers = deepcopy(self.build.answers)
        duplicate_target = next(
            item for item in duplicated_answers if len(item["claims"]) >= 2
        )
        duplicate = deepcopy(duplicate_target["claims"][0])
        duplicate["claim_id"] = "duplicate-canonical-claim"
        duplicate_target["claims"].append(duplicate)
        duplicate_target["expected_material_claim_count"] += 1
        duplicate_build = replace(self.build, answers=tuple(duplicated_answers))
        self.assertTrue(
            any(
                "repeats a canonical adjudicated claim" in error
                for error in validate_build(duplicate_build)
            )
        )

        indirect_answers = deepcopy(self.build.answers)
        indirect_target = next(
            item
            for item in indirect_answers
            if item["id"] == "cross_chunk-success-03"
        )
        first_claim, second_claim = indirect_target["claims"]
        first_claim["evidence_chunk_ids"] = list(
            second_claim["evidence_chunk_ids"]
        )
        first_claim["evidence_chunk_keys"] = list(
            second_claim["evidence_chunk_keys"]
        )
        indirect_build = replace(self.build, answers=tuple(indirect_answers))
        self.assertTrue(
            any(
                "does not directly support its sourced claim" in error
                for error in validate_build(indirect_build)
            )
        )

        extra_evidence_answers = deepcopy(self.build.answers)
        extra_target = next(item for item in extra_evidence_answers if item["evidence"])
        used_ids = {source["chunk_id"] for source in extra_target["evidence"]}
        extra_source = next(
            source
            for item in extra_evidence_answers
            for source in item["evidence"]
            if source["chunk_id"] not in used_ids
        )
        extra_target["evidence"].append(deepcopy(extra_source))
        extra_build = replace(self.build, answers=tuple(extra_evidence_answers))
        self.assertTrue(
            any(
                "top-level evidence must exactly equal claim evidence" in error
                for error in validate_build(extra_build)
            )
        )

        wrong_direction_answers = deepcopy(self.build.answers)
        direction_target = next(
            item
            for item in wrong_direction_answers
            if item["temporal_comparison"] is not None
            and item["temporal_comparison"]["inference_direction"] == "increased"
        )
        inference = next(
            claim for claim in direction_target["claims"] if claim["inference"]
        )
        inference["reference_text"] = inference["reference_text"].replace(
            "increased", "decreased"
        )
        inference["required_terms"] = [
            "decreased" if term == "increased" else term
            for term in inference["required_terms"]
        ]
        inference["comparison"]["direction"] = "decreased"
        direction_target["reference_answer"] = direction_target[
            "reference_answer"
        ].replace("increased", "decreased")
        direction_target["temporal_comparison"][
            "inference_direction"
        ] = "decreased"
        direction_build = replace(
            self.build,
            answers=tuple(wrong_direction_answers),
        )
        self.assertTrue(
            any(
                "comparison direction does not match its operands" in error
                for error in validate_build(direction_build)
            )
        )


class GroundedAnswerMetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gold = build_dataset().answers
        cls.perfect = _perfect_results(cls.gold)

    def test_perfect_runtime_results_score_one_without_claiming_conflict(self) -> None:
        metrics = evaluate_answer_results(self.gold, self.perfect)
        for value in (
            metrics.supported_claim_rate,
            metrics.citation_precision,
            metrics.citation_coverage,
            metrics.numerical_fidelity,
            metrics.refusal_precision,
            metrics.refusal_recall,
            metrics.refusal_f1,
            metrics.answer_correctness,
            metrics.temporal_comparison_rate,
        ):
            self.assertEqual(value, 1.0)
        self.assertEqual(metrics.expected_refusal_count, 14)
        self.assertEqual(metrics.expected_conflict_count, 0)
        self.assertEqual(metrics.expected_temporal_comparison_count, 5)
        self.assertEqual(metrics.generation_failure_count, 0)
        self.assertIsNone(metrics.conflict_handling_rate)

    def test_json_ready_answer_result_tuples_use_the_same_schema_adapter(self) -> None:
        tuple_shaped = deepcopy(self.perfect)
        for item in tuple_shaped:
            for claim in item["claims"]:
                claim["citation_ids"] = tuple(claim["citation_ids"])
            for conflict in item["conflicts"]:
                conflict["claim_indexes"] = tuple(conflict["claim_indexes"])
            item["claims"] = tuple(item["claims"])
            item["citations"] = tuple(item["citations"])
            item["conflicts"] = tuple(item["conflicts"])
        metrics = evaluate_answer_results(self.gold, tuple_shaped)
        self.assertEqual(metrics.answer_correctness, 1.0)

    def test_hand_computable_fixture_scores_failures_and_refusals(self) -> None:
        source_a = _source("chunk-a", "document-a")
        source_b = _source("chunk-b", "document-b")
        source_d = _source("chunk-d", "document-d")
        text_a = "Alpha reported $10 million for fiscal year 2024."
        text_b = "Beta reported $20 million for fiscal year 2024."
        text_d = "Delta operates a retail segment."
        gold = [
            _gold_case(
                "correct-answer",
                "answered",
                [_claim(text_a, source_a, exact_tokens=("$10 million", "fiscal year 2024"))],
                [source_a],
            ),
            _gold_case(
                "unsupported-answer",
                "answered",
                [_claim(text_b, source_b, exact_tokens=("$20 million", "fiscal year 2024"))],
                [source_b],
            ),
            _gold_case("correct-refusal", "insufficient_context", [], []),
            _gold_case(
                "false-refusal",
                "answered",
                [_claim(text_d, source_d)],
                [source_d],
            ),
            _gold_case("generation-failure", "insufficient_context", [], []),
        ]
        actual = [
            _answered_result("correct-answer", text_a, source_a),
            _answered_result(
                "unsupported-answer",
                "Gamma stated a non-evidenced estimate.",
                source_b,
            ),
            _refusal_result("correct-refusal"),
            _refusal_result("false-refusal"),
            _refusal_result("generation-failure", failure_code="invalid_model_output"),
        ]
        metrics = calculate_answer_metrics(gold, actual)
        self.assertEqual(metrics.item_count, 5)
        self.assertEqual(metrics.material_claim_count, 2)
        self.assertEqual(metrics.citation_attachment_count, 2)
        self.assertEqual(metrics.exact_token_count, 4)
        self.assertEqual(metrics.supported_claim_rate, 0.5)
        self.assertEqual(metrics.citation_precision, 0.5)
        self.assertEqual(metrics.citation_coverage, 0.5)
        self.assertEqual(metrics.numerical_fidelity, 0.5)
        self.assertEqual(metrics.refusal_precision, 0.5)
        self.assertEqual(metrics.refusal_recall, 0.5)
        self.assertEqual(metrics.refusal_f1, 0.5)
        self.assertEqual(metrics.answer_correctness, 0.4)
        self.assertEqual(metrics.generation_failure_count, 1)
        self.assertIsNone(metrics.conflict_handling_rate)

    def test_true_same_scope_conflict_requires_two_supported_provenances(self) -> None:
        first = _source("conflict-a", "conflict-document-a")
        second = _source("conflict-b", "conflict-document-b")
        first_text = "Source Alpha reports $10 million for fiscal year 2024."
        second_text = "Source Beta reports $12 million for fiscal year 2024."
        gold = [
            _gold_case(
                "true-conflict",
                "conflict",
                [_claim(first_text, first), _claim(second_text, second)],
                [first, second],
            )
        ]
        actual = [
            {
                "answer": (
                    "Conflicting source statements:\n"
                    f"{first_text} [S1]\n{second_text} [S2]"
                ),
                "citations": [_citation(first, "S1"), _citation(second, "S2")],
                "claims": [
                    {
                        "citation_ids": ["S1"],
                        "inference": False,
                        "material": True,
                        "text": first_text,
                    },
                    {
                        "citation_ids": ["S2"],
                        "inference": False,
                        "material": True,
                        "text": second_text,
                    },
                ],
                "conflicts": [
                    {"claim_indexes": [0, 1], "topic": "Unresolved FY2024 amount"}
                ],
                "failure_code": None,
                "id": "true-conflict",
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "status": "conflict",
            }
        ]
        metrics = calculate_answer_metrics(gold, actual)
        self.assertEqual(metrics.answer_correctness, 1.0)
        self.assertEqual(metrics.conflict_handling_rate, 1.0)
        self.assertEqual(metrics.expected_conflict_count, 1)
        self.assertTrue(all(not claim["inference"] for claim in actual[0]["claims"]))

    def test_metric_degrades_for_wrong_literal_and_wrong_location(self) -> None:
        wrong_literal = deepcopy(self.perfect)
        result = next(item for item in wrong_literal if item["id"] == "exact_value-success-01")
        token = "$72.1 billion"
        result["claims"][0]["text"] = result["claims"][0]["text"].replace(
            token, "$72.2 billion"
        )
        result["answer"] = result["answer"].replace(token, "$72.2 billion")
        self.assertLess(
            evaluate_answer_results(self.gold, wrong_literal).numerical_fidelity,
            1.0,
        )

        wrong_location = deepcopy(self.perfect)
        result = next(item for item in wrong_location if item["citations"])
        result["citations"][0]["char_start"] += 1
        metrics = evaluate_answer_results(self.gold, wrong_location)
        self.assertLess(metrics.citation_precision, 1.0)
        self.assertLess(metrics.citation_coverage, 1.0)

    def test_adjudicated_entailment_rejects_role_and_direction_reversal(self) -> None:
        source = _source("relation", "relation-document")
        gold = [
            _gold_case(
                "relation",
                "answered",
                [_claim("Apple acquired Beats.", source)],
                [source],
            )
        ]
        reversed_roles = [
            _answered_result("relation", "Beats acquired Apple.", source)
        ]
        metrics = calculate_answer_metrics(gold, reversed_roles)
        self.assertEqual(metrics.supported_claim_rate, 0.0)
        self.assertEqual(metrics.citation_precision, 0.0)
        self.assertEqual(metrics.answer_correctness, 0.0)

        wrong_direction = deepcopy(self.perfect)
        temporal_id = next(
            item["id"]
            for item in self.gold
            if item["temporal_comparison"] is not None
        )
        result = next(
            item
            for item in wrong_direction
            if item["id"] == temporal_id
        )
        inference = next(claim for claim in result["claims"] if claim["inference"])
        self.assertIn("increased", inference["text"])
        inference["text"] = inference["text"].replace("increased", "decreased")
        result["answer"] = result["answer"].replace("increased", "decreased", 1)
        reversed_metrics = evaluate_answer_results(self.gold, wrong_direction)
        self.assertLess(reversed_metrics.supported_claim_rate, 1.0)
        self.assertLess(reversed_metrics.temporal_comparison_rate, 1.0)
        self.assertLess(reversed_metrics.answer_correctness, 1.0)

    def test_regression_gate_cannot_hide_missing_answers_or_failures(self) -> None:
        contract = json.loads(
            Path("contracts/acceptance.v1.json").read_text(encoding="utf-8")
        )
        missing_claim = deepcopy(self.perfect)
        result = next(
            item
            for item in missing_claim
            if len(item["claims"]) >= 2
            and not any(claim["inference"] for claim in item["claims"])
        )
        removed = result["claims"].pop()
        result["answer"] = "\n".join(
            line
            for line in result["answer"].splitlines()
            if removed["text"] not in line
        )
        referenced = {
            citation_id
            for claim in result["claims"]
            for citation_id in claim["citation_ids"]
        }
        result["citations"] = [
            citation
            for citation in result["citations"]
            if citation["citation_id"] in referenced
        ]
        metrics = evaluate_answer_results(self.gold, missing_claim)
        self.assertEqual(metrics.supported_claim_rate, 1.0)
        self.assertTrue(
            any(
                "answer_correctness" in item
                for item in answer_gate_failures(metrics, contract)
            )
        )

        failed = deepcopy(self.perfect)
        result = next(
            item
            for item in failed
            if item["status"] == "insufficient_context"
        )
        result["failure_code"] = "invalid_model_output"
        failure_metrics = evaluate_answer_results(self.gold, failed)
        failures = answer_gate_failures(failure_metrics, contract)
        self.assertTrue(any("generation_failure_count" in item for item in failures))
        self.assertTrue(any("answer_correctness" in item for item in failures))

    def test_evaluator_fails_closed_on_incomplete_or_invalid_external_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            evaluate_answer_results(self.gold, self.perfect[:-1])

        missing_metadata = deepcopy(self.perfect)
        result = next(item for item in missing_metadata if item["citations"])
        result["citations"][0].pop("document_title")
        with self.assertRaisesRegex(ValueError, "document_title"):
            evaluate_answer_results(self.gold, missing_metadata)

        missing_label = deepcopy(self.perfect)
        result = next(
            item
            for item in missing_label
            if any(claim["inference"] for claim in item["claims"])
        )
        result["answer"] = result["answer"].replace("Inference: ", "", 1)
        with self.assertRaisesRegex(ValueError, "label inference"):
            evaluate_answer_results(self.gold, missing_label)

        recorded_prediction = deepcopy(self.gold)
        recorded_prediction[0]["prediction"] = "not permitted"
        with self.assertRaisesRegex(ValueError, "recorded runtime results"):
            validate_gold_dataset(recorded_prediction)

        duplicate_claim = deepcopy(self.perfect)
        result = next(item for item in duplicate_claim if item["claims"])
        result["claims"].append(deepcopy(result["claims"][0]))
        with self.assertRaisesRegex(ValueError, "repeats a canonical claim"):
            evaluate_answer_results(self.gold, duplicate_claim)

        hallucinated_prose = deepcopy(self.perfect)
        result = next(item for item in hallucinated_prose if item["claims"])
        result["answer"] += "\nFabricated revenue was $999 billion."
        with self.assertRaisesRegex(ValueError, "canonical server rendering"):
            evaluate_answer_results(self.gold, hallucinated_prose)

        stale_runtime = deepcopy(self.perfect)
        stale_runtime[0]["prompt_version"] = "stale-prompt"
        with self.assertRaisesRegex(ValueError, "prompt version"):
            evaluate_answer_results(self.gold, stale_runtime)

        duplicated_provenance = deepcopy(self.perfect)
        result = next(item for item in duplicated_provenance if item["citations"])
        duplicate_citation = deepcopy(result["citations"][0])
        duplicate_citation["citation_id"] = "S99"
        result["citations"].append(duplicate_citation)
        result["claims"][0]["citation_ids"].append("S99")
        with self.assertRaisesRegex(ValueError, "repeats citation provenance"):
            evaluate_answer_results(self.gold, duplicated_provenance)

        stale_gold = deepcopy(self.gold)
        for item in stale_gold:
            item["gold_version"] = "stale"
        with self.assertRaisesRegex(ValueError, "stale or unknown"):
            validate_gold_dataset(stale_gold)

    def test_cli_requires_explicit_actual_results(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args([])


if __name__ == "__main__":
    unittest.main()
