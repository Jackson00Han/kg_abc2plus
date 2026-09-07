"""Bounded validation correction with real extraction and injected dependencies."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from graphrag_prod.api.knowledge import _construction_chunk_payload
from graphrag_prod.api.knowledge_contracts import ConstructionChunkResponse
from graphrag_prod.construction import (
    BoundedDocumentParser,
    ChunkingConfig,
    ConstructionBudgetExceeded,
    ConstructionConfig,
    ConstructionConflict,
    ConstructionDeadlineExceeded,
    ExtractionLimits,
    ExtractionRejected,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.construction.workflow import _chunk_result_from_payload, _chunk_result_payload
from graphrag_prod.domain import Principal, content_checksum
from graphrag_prod.knowledge import AuthorityLevel, GovernanceStatus, KnowledgeOrigin
from graphrag_prod.ontology.models import TBoxVersion
from graphrag_prod.playground.industrial_demo import get_industrial_demo_kit
from tests.unit.test_construction_extraction import (
    _chunk, _profile, _repeated_entity_case, _tbox, _valid_payload,
)
from tests.unit.test_construction_workflow import SOURCE, _AuditStore, _metadata, _workflow


class _Responses:
    def __init__(self, responses, before_response=None):
        self.responses = list(responses)
        self.calls = []
        self.before_response = before_response

    def create(self, **request):
        self.calls.append(copy.deepcopy(request))
        if self.before_response is not None:
            self.before_response(len(self.calls))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        raw = value if isinstance(value, str) else json.dumps(value)
        return {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}


def _feedback_extractor(
    responses, *, attempts=2, limits=None, before_response=None, active_tbox=None,
):
    client = _Responses(responses, before_response)
    extractor = OpenAICompatibleOntologyExtractor(
        client=SimpleNamespace(chat=SimpleNamespace(completions=client)),
        model="qwen-plus", active_tbox=active_tbox or _tbox(),
        prompt_version="industrial-prompt:v1",
        limits=limits or ExtractionLimits(timeout_seconds=30),
        max_validation_attempts=attempts,
    )
    return extractor, client


def _bad_payload():
    payload = _valid_payload()
    payload["relationships"][0]["evidence"] = {"text": "owns Pump-7", "start": 5, "end": 16}
    return payload


def _attempts(audit):
    return [p for p in audit.artifacts.values() if p.get("audit_type") == "VALIDATION_ATTEMPT"]


def _homonym_fixture():
    fixture = json.loads((
        Path(__file__).parents[1] / "fixtures" / "homonym-span-failure.v1.json"
    ).read_text(encoding="utf-8"))
    kit = get_industrial_demo_kit()
    source = next(item for item in kit["files"] if item["id"] == "homonym_report")
    if source["text"] != fixture["source_text"]:
        raise AssertionError("homonym regression requires the original source text")
    if content_checksum(source["text"]) != fixture["source_sha256"]:
        raise AssertionError("homonym regression source checksum changed")
    chunk = replace(
        _chunk(), text=source["text"], checksum=source["sha256"],
        char_start=0, char_end=len(source["text"]),
    )
    tbox = TBoxVersion.from_mapping({
        **kit["ontology"], "tenant_id": chunk.tenant_id, "status": "PUBLISHED",
    })
    return fixture, chunk, tbox


class ConstructionValidationFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal(
            "engineer:alice", "tenant-industrial", frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )

    def test_recorded_homonym_responses_still_fail_closed_with_two_audited_attempts(self):
        fixture, chunk, tbox = _homonym_fixture()
        raw_responses = [attempt["response"] for attempt in fixture["attempts"]]
        extractor, client = _feedback_extractor(
            [*raw_responses, fixture["corrected_response"]], active_tbox=tbox,
        )
        attempts = []
        with self.assertRaises(ExtractionRejected):
            extractor.extract_audited_bounded(
                artifact_id="homonym-historical", input_hash="homonym-input",
                chunk=chunk, profile=_profile(), on_validation_attempt=attempts.append,
            )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(client.responses), 1)  # No third call silently repairs history.
        self.assertEqual([attempt.status for attempt in attempts], ["REJECTED"] * 2)
        for recorded, actual in zip(fixture["attempts"], attempts, strict=True):
            with self.subTest(attempt=recorded["attempt"]):
                self.assertEqual(content_checksum(recorded["response"]), recorded["response_checksum"])
                self.assertEqual(actual.response, recorded["response"])
                self.assertEqual(actual.response_checksum, recorded["response_checksum"])
                self.assertEqual(
                    [(finding.code, finding.path, finding.action) for finding in actual.findings],
                    [(finding["code"], finding["path"], finding["action"]) for finding in recorded["findings"]],
                )

    def test_homonym_corrected_model_response_keeps_exact_evidence_and_distinct_code(self):
        fixture, chunk, tbox = _homonym_fixture()
        corrected = fixture["corrected_response"]
        for historical in fixture["attempts"]:
            with self.subTest(correcting_attempt=historical["attempt"]):
                extractor, client = _feedback_extractor(
                    [historical["response"], corrected], active_tbox=tbox,
                )
                attempts = []
                result = extractor.extract_audited_bounded(
                    artifact_id="homonym-corrected", input_hash="homonym-input",
                    chunk=chunk, profile=_profile(), on_validation_attempt=attempts.append,
                )
                self.assertEqual(len(client.calls), 2)
                self.assertEqual([attempt.status for attempt in attempts], ["REJECTED", "CANDIDATE"])
                self.assertEqual(attempts[0].response, historical["response"])
                self.assertEqual(attempts[1].response, json.dumps(corrected))
                self.assertEqual(result.authority, AuthorityLevel.SECONDARY)
                self.assertEqual(result.origin, KnowledgeOrigin.LLM_EXTRACTED)
                self.assertEqual(result.status, GovernanceStatus.CANDIDATE)
                self.assertEqual(len(result.output.entities), 1)
                self.assertEqual(len(result.output.mentions), 1)
                mention = result.output.mentions[0]
                self.assertEqual((mention.surface, mention.char_start, mention.char_end), ("循环水泵", 47, 51))
                facts = {assertion.predicate: assertion for assertion in result.output.assertions}
                self.assertEqual(set(facts), {"EquipmentCode", "RatedPower"})
                self.assertEqual(facts["EquipmentCode"].literal_value, "BC-P-202")
                self.assertEqual(facts["EquipmentCode"].literal_semantics.canonical_value, "BC-P-202")
                power = facts["RatedPower"].literal_semantics
                self.assertEqual((power.raw_value, power.raw_unit), ("22.0", "kW"))
                self.assertEqual((power.canonical_value, power.canonical_unit), ("22", "kW"))
                for predicate, end in (("EquipmentCode", 81), ("RatedPower", 107)):
                    fact = facts[predicate]
                    self.assertEqual(fact.subject_entity_id, mention.entity_id)
                    self.assertEqual((fact.evidence_char_start, fact.evidence_char_end), (47, end))
                    self.assertIn(mention.surface, chunk.text[47:end])
                    self.assertIn(fact.literal_value, chunk.text[47:end])
                self.assertNotIn("BC-P-101", json.dumps(corrected))
                self.assertEqual(chunk.checksum, fixture["source_sha256"])

    def test_repeated_entity_missing_mention_corrected_only_by_second_model_response(self):
        chunk, valid = _repeated_entity_case()
        invalid = copy.deepcopy(valid)
        invalid["entities"][1]["mentions"].pop()
        extractor, client = _feedback_extractor([invalid, valid])
        attempts = []
        calls_reserved = []
        result = extractor.extract_audited_bounded(
            artifact_id="audit", input_hash="input", chunk=chunk, profile=_profile(),
            before_model_call=lambda: calls_reserved.append(True),
            on_validation_attempt=attempts.append,
        )
        self.assertEqual([a.status for a in attempts], ["REJECTED", "CANDIDATE"])
        self.assertEqual(len(result.output.mentions), 5)
        self.assertEqual(len(calls_reserved), 2)
        self.assertEqual(attempts[0].response, json.dumps(invalid))
        feedback = json.loads(client.calls[1]["messages"][-1]["content"])
        self.assertIn("ENDPOINT_OUTSIDE_EVIDENCE", {f["code"] for f in feedback["findings"]})
        self.assertEqual(client.calls[1]["messages"][:-2], client.calls[0]["messages"])
        self.assertEqual(client.calls[1]["messages"][-2]["content"], json.dumps(invalid))
        self.assertEqual(client.calls[0]["timeout"], client.calls[1]["timeout"])

    def test_two_invalid_responses_remain_rejected_without_repair_or_third_call(self):
        extractor, client = _feedback_extractor([_bad_payload(), _bad_payload(), _valid_payload()])
        workflow, audit, knowledge, _ = _workflow(extractor=extractor)
        result = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(result.chunks[0].status, "REJECTED")
        self.assertEqual([a.status for a in result.chunks[0].validation_attempts], ["REJECTED"] * 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertEqual(len(_attempts(audit)), 2)

    def test_attempt_is_durable_before_correction_and_final_audit_recovers_without_model(self):
        audit = _AuditStore()
        workflow, _, knowledge, _ = _workflow(
            extractor=_feedback_extractor([_valid_payload()])[0], audit=audit,
        )

        def before_response(number):
            self.assertEqual(knowledge.candidate_writes, 0)
            if number == 2:
                self.assertEqual([a["status"] for a in _attempts(audit)], ["REJECTED"])

        extractor, client = _feedback_extractor([_bad_payload(), _valid_payload()], before_response=before_response)
        workflow.extractor_factory = lambda _: extractor
        first = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertEqual(len(audit.artifacts), 3)
        for item in _attempts(audit):
            self.assertEqual(item["tenant_id"], self.principal.tenant_id)
            self.assertEqual(item["job_id"], first.job_id)
            self.assertEqual(item["chunk_id"], first.chunks[0].chunk_id)
            self.assertEqual(item["access_groups"], ["engineers"])
            self.assertEqual(item["response_checksum"], content_checksum(item["response"]))
            self.assertGreaterEqual(item["provider_seconds"], 0)
        audit.outcomes.clear()  # Crash after artifact/candidates but before durable outcome.
        recovered = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(audit.artifacts), 3)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertEqual(first.chunks[0].validation_attempts, recovered.chunks[0].validation_attempts)
        roundtrip = _chunk_result_from_payload(_chunk_result_payload(recovered.chunks[0]), replayed=True)
        self.assertEqual(roundtrip.validation_attempts, first.chunks[0].validation_attempts)
        public = _construction_chunk_payload(first.chunks[0])
        response = ConstructionChunkResponse.model_validate(public).model_dump(mode="json")
        self.assertEqual(set(response["validation_attempts"][0]), {
            "attempt", "status", "finding_codes", "response_checksum",
        })
        self.assertNotIn("Acme", json.dumps(response))

    def test_provider_failure_is_not_corrected_and_manual_recovery_preserves_both_runs(self):
        extractor, client = _feedback_extractor([_bad_payload(), TimeoutError("private"), _valid_payload()])
        workflow, audit, knowledge, _ = _workflow(extractor=extractor)
        with self.assertRaises(ExtractionRejected):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(knowledge.candidate_writes, 0)
        prior = copy.deepcopy(_attempts(audit))
        self.assertEqual([a["status"] for a in prior], ["REJECTED", "PROVIDER_ERROR"])
        self.assertIsNone(prior[-1]["response"])
        self.assertIsNone(prior[-1]["response_checksum"])
        self.assertNotIn("private", json.dumps(prior))
        recovered = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(_attempts(audit)[:2], prior)
        self.assertEqual(len({a["validation_run_id"] for a in _attempts(audit)}), 2)

    def test_first_dependency_error_and_oversize_never_get_a_corrective_call(self):
        for value in (TimeoutError(), RuntimeError(), "x" * 16385):
            with self.subTest(value=type(value).__name__):
                extractor, client = _feedback_extractor(
                    [value, _valid_payload()], limits=ExtractionLimits(timeout_seconds=30, max_response_chars=16384),
                )
                workflow, audit, knowledge, _ = _workflow(extractor=extractor)
                if isinstance(value, Exception):
                    with self.assertRaises(ExtractionRejected):
                        workflow.run(self.principal, SOURCE, _metadata())
                else:
                    self.assertEqual(workflow.run(self.principal, SOURCE, _metadata()).chunks[0].status, "REJECTED")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(knowledge.candidate_writes, 0)
                attempt = _attempts(audit)[0]
                self.assertIsNone(attempt["response"])
                if isinstance(value, str):
                    self.assertEqual(attempt["response_chars"], len(value))
                    self.assertEqual(attempt["response_checksum"], content_checksum(value))

    def test_json_parse_failure_can_be_corrected_but_legacy_default_stays_single_call(self):
        for count in (1, 2):
            with self.subTest(count=count):
                extractor, client = _feedback_extractor(["{", _valid_payload()], attempts=count)
                workflow, audit, knowledge, _ = _workflow(extractor=extractor)
                result = workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(client.calls), count)
                self.assertEqual(result.chunks[0].status, "REJECTED" if count == 1 else "CANDIDATE")
                self.assertEqual(len(_attempts(audit)), 0 if count == 1 else 2)
                self.assertEqual(knowledge.candidate_writes, count - 1)

    def test_preflight_reserves_worst_case_calls_before_document_or_job_writes(self):
        extractor, client = _feedback_extractor([_valid_payload()])
        workflow, audit, _, pipeline = _workflow(
            extractor=extractor,
            config=ConstructionConfig("qwen-ontology:v1", "industrial-prompt:v1", max_model_calls=1),
        )
        with self.assertRaises(ConstructionBudgetExceeded):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertFalse(audit.jobs)
        self.assertFalse(audit.observed_principals)
        self.assertFalse(client.calls)
        self.assertEqual(pipeline.requests, [])

    def test_two_chunks_use_at_most_four_actual_calls_and_third_chunk_is_rejected(self):
        parser = BoundedDocumentParser(chunking=ChunkingConfig(max_chars=7, minimum_boundary_ratio=1))
        extractor, client = _feedback_extractor(["{"] * 4)
        workflow, audit, knowledge, _ = _workflow(
            extractor=extractor, parser=parser,
            config=ConstructionConfig("qwen-ontology:v1", "industrial-prompt:v1", max_model_calls=4),
        )
        result = workflow.run(self.principal, b"First. Second.", _metadata())
        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all(len(c.validation_attempts) == 2 for c in result.chunks))
        self.assertEqual(knowledge.candidate_writes, 0)
        before = len(audit.jobs)
        with self.assertRaises(ConstructionBudgetExceeded):
            workflow.run(self.principal, b"First. Second. Third.", _metadata(operation_key="three"))
        self.assertEqual(len(audit.jobs), before)
        self.assertEqual(len(client.calls), 4)

    def test_deadline_rechecked_before_correction_retains_initial_rejection(self):
        clock = [0.0]
        extractor, client = _feedback_extractor(
            [_bad_payload(), _valid_payload()], before_response=lambda _: clock.__setitem__(0, 61),
        )
        workflow, audit, knowledge, _ = _workflow(
            extractor=extractor, monotonic=lambda: clock[0],
            config=ConstructionConfig("qwen-ontology:v1", "industrial-prompt:v1", deadline_seconds=90),
        )
        with self.assertRaises(ConstructionDeadlineExceeded):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([a["status"] for a in _attempts(audit)], ["REJECTED"])
        self.assertEqual(knowledge.candidate_writes, 0)

    def test_changed_or_missing_attempt_recovery_fails_closed_before_candidate_persistence(self):
        for mutation, completed in (("delete", False), ("raw", False), ("tenant", False), ("delete", True), ("raw", True)):
            with self.subTest(mutation=mutation, completed=completed):
                extractor, client = _feedback_extractor([_bad_payload(), _valid_payload()])
                workflow, audit, knowledge, _ = _workflow(extractor=extractor)
                workflow.run(self.principal, SOURCE, _metadata())
                if not completed:
                    audit.outcomes.clear()
                key = next(k for k, v in audit.artifacts.items() if v.get("audit_type") == "VALIDATION_ATTEMPT")
                if mutation == "delete":
                    del audit.artifacts[key]
                else:
                    audit.artifacts[key]["response" if mutation == "raw" else "tenant_id"] = "changed"
                with self.assertRaises(ConstructionConflict):
                    workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(client.calls), 2)
                self.assertEqual(knowledge.candidate_writes, 1)

    def test_attempt_write_conflict_stops_before_correction_and_all_candidate_writes(self):
        class ConflictingAudit(_AuditStore):
            def persist_artifact(self, **kwargs):
                if kwargs["payload"].get("audit_type") == "VALIDATION_ATTEMPT":
                    raise ConstructionConflict("immutable collision")
                return super().persist_artifact(**kwargs)

        extractor, client = _feedback_extractor([_bad_payload(), _valid_payload()])
        workflow, audit, knowledge, _ = _workflow(extractor=extractor, audit=ConflictingAudit())
        with self.assertRaises(ConstructionConflict):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertFalse(audit.outcomes)

    def test_completed_outcome_must_exactly_match_aggregate_status_and_record_ids(self):
        for change in ({"status": "EMPTY"}, {"mention_record_ids": ()}, {"assertion_record_ids": ()}):
            with self.subTest(change=change):
                extractor, client = _feedback_extractor([_valid_payload()])
                workflow, audit, knowledge, _ = _workflow(extractor=extractor)
                workflow.run(self.principal, SOURCE, _metadata())
                key = next(iter(audit.outcomes))
                audit.outcomes[key] = replace(audit.outcomes[key], **change)
                with self.assertRaises(ConstructionConflict):
                    workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(knowledge.candidate_writes, 1)

    def test_default_policy_compatible_but_feedback_mode_conflicts_under_same_operation(self):
        first, _ = _feedback_extractor([_valid_payload()], attempts=1)
        workflow, audit, _, _ = _workflow(extractor=first)
        workflow.run(self.principal, SOURCE, _metadata())
        second, client = _feedback_extractor([_valid_payload()], attempts=2)
        self.assertNotEqual(first.request_policy_signature, second.request_policy_signature)
        workflow.extractor_factory = lambda _: second
        with self.assertRaises(ConstructionConflict):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertFalse(client.calls)
        third, _ = _feedback_extractor([], attempts=1)
        self.assertEqual(first.request_policy_signature, third.request_policy_signature)

    def test_outbound_schema_rejects_raw_responses_and_source_only_attempts(self):
        extractor, _ = _feedback_extractor([_valid_payload()])
        workflow, _, _, _ = _workflow(extractor=extractor)
        result = workflow.run(self.principal, SOURCE, _metadata()).chunks[0]
        payload = _construction_chunk_payload(result)
        payload["validation_attempts"][0]["response"] = "private"
        with self.assertRaises(ValidationError):
            ConstructionChunkResponse.model_validate(payload)
        source = replace(result, status="SOURCE_ONLY", finding_codes=(), mention_record_ids=(), assertion_record_ids=())
        with self.assertRaises(ValidationError):
            ConstructionChunkResponse.model_validate(_construction_chunk_payload(source))


class ExactQuoteFeedbackTests(unittest.TestCase):
    def feedback(self, code, path, source, payload):
        from graphrag_prod.construction.extraction import ExtractionFinding, _contains_exact_token
        from graphrag_prod.construction.validation_feedback import build_validation_feedback
        return json.loads(build_validation_feedback((ExtractionFinding(code, "REJECT", path, "exact span differs"),),
            source=source, payload=payload, contains_token=_contains_exact_token))

    def test_mention_quote_coordinates_do_not_repair_without_a_new_valid_response(self):
        invalid = _valid_payload()
        invalid["entities"][0]["mentions"][0]["start"] = 1
        original = copy.deepcopy(invalid)
        extractor, client = _feedback_extractor([invalid, invalid, _valid_payload()])
        attempts = []
        with self.assertRaises(ExtractionRejected):
            extractor.extract_audited_bounded(artifact_id="mention-spans", input_hash="mention-spans",
                chunk=_chunk(), profile=_profile(), on_validation_attempt=attempts.append)
        feedback = json.loads(client.calls[1]["messages"][-1]["content"])
        hint = feedback["findings"][0]["quote_coordinates"]
        self.assertEqual(hint["quote"], "Acme")
        self.assertEqual(hint["occurrences"], [{"start": 0, "end": 4}])
        self.assertFalse(hint["occurrences_truncated"])
        self.assertEqual(invalid, original)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([a.status for a in attempts], ["REJECTED", "REJECTED"])
        self.assertEqual(len(client.responses), 1)

    def test_nested_relationship_property_and_entity_fact_paths_are_supported(self):
        payload = {"relationships": [{"properties": [{"evidence": {"text": "owns", "start": 4, "end": 8}}]}],
                   "property_facts": [{"evidence": {"text": "Acme owns", "start": 1, "end": 9}}]}
        for path, expected in (("$.relationships[0].properties[0].evidence", [{"start": 5, "end": 9}]),
                               ("$.property_facts[0].evidence", [{"start": 0, "end": 9}])):
            with self.subTest(path=path):
                value = self.feedback("EVIDENCE_SPAN_MISMATCH", path, _chunk().text, payload)["findings"][0]["quote_coordinates"]
                self.assertEqual(value["occurrences"], expected)
                self.assertEqual(value["hint_kind"], "exact_quote_coordinates_only_not_verified_entailment")
                self.assertNotIn("selected_span", value)

    def test_repeated_overlapping_occurrences_are_bounded_and_never_selected(self):
        source = "a" * 30
        payload = {"relationships": [{"evidence": {"text": "aa", "start": 0, "end": 1}}]}
        value = self.feedback("EVIDENCE_SPAN_MISMATCH", "$.relationships[0].evidence", source, payload)["findings"][0]["quote_coordinates"]
        self.assertEqual(value["occurrences"], [{"start": i, "end": i + 2} for i in range(8)])
        self.assertTrue(value["occurrences_truncated"])
        self.assertNotIn("selected_span", value)

    def test_missing_quote_has_no_fuzzy_match_and_invalid_or_large_paths_have_no_hint(self):
        payload = {"relationships": [{"evidence": {"text": "Owns", "start": 5, "end": 9}}]}
        value = self.feedback("EVIDENCE_SPAN_MISMATCH", "$.relationships[0].evidence", _chunk().text, payload)["findings"][0]["quote_coordinates"]
        self.assertEqual(value["occurrences"], [])
        self.assertFalse(value["occurrences_truncated"])
        for path in ("$.relationships[-1].evidence", "$.relationships[999999].evidence", "$.relationships[0].evidence.text",
                     "$.relationships[" + "9" * 300 + "].evidence"):
            self.assertNotIn("quote_coordinates", self.feedback("EVIDENCE_SPAN_MISMATCH", path, _chunk().text, payload)["findings"][0])
        payload["relationships"][0]["evidence"]["text"] = "a" * 513
        self.assertNotIn("quote_coordinates", self.feedback("EVIDENCE_SPAN_MISMATCH", "$.relationships[0].evidence", "a" * 1000, payload)["findings"][0])

    def test_quote_diagnostics_and_findings_keep_complete_feedback_under_8192_chars(self):
        from graphrag_prod.construction.extraction import ExtractionFinding, _contains_exact_token
        from graphrag_prod.construction.validation_feedback import build_validation_feedback
        payload = {"property_facts": [{"evidence": {"text": "x" * 500, "start": 1, "end": 501}} for _ in range(40)]}
        findings = tuple(ExtractionFinding("EVIDENCE_SPAN_MISMATCH", "REJECT", f"$.property_facts[{i}].evidence", "detail" * 100) for i in range(40))
        before = copy.deepcopy(payload)
        raw = build_validation_feedback(findings, source="x" * 1500, payload=payload, contains_token=_contains_exact_token)
        self.assertLessEqual(len(raw), 8192)
        self.assertLessEqual(len(json.loads(raw)["findings"]), 32)
        self.assertEqual(json.loads(raw)["total_findings"], 40)
        self.assertEqual(payload, before)
