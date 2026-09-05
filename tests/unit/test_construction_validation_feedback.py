"""Bounded validation correction with real extraction and injected dependencies."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
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
from tests.unit.test_construction_extraction import (
    _profile, _repeated_entity_case, _tbox, _valid_payload,
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


def _feedback_extractor(responses, *, attempts=2, limits=None, before_response=None):
    client = _Responses(responses, before_response)
    extractor = OpenAICompatibleOntologyExtractor(
        client=SimpleNamespace(chat=SimpleNamespace(completions=client)),
        model="qwen-plus", active_tbox=_tbox(), prompt_version="industrial-prompt:v1",
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


class ConstructionValidationFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal(
            "engineer:alice", "tenant-industrial", frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )

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
