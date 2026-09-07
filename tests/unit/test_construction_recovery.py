"""Saved terminal responses are revalidated without repeating provider work."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from graphrag_prod.construction import (
    ConstructionAuthorizationError, ConstructionConflict, ExtractionLimits, ExtractionRejected,
)
from graphrag_prod.domain import Principal
from graphrag_prod.ingestion.models import _fingerprint
from tests.unit.test_construction_validation_feedback import (
    _bad_payload, _feedback_extractor, _valid_payload,
)
from tests.unit.test_construction_workflow import SOURCE, _AuditStore, _metadata, _workflow


class RecoveryStore(_AuditStore):
    def read_validation_attempts(self, principal, *, job_id, chunk, parent_artifact_id, profile_id):
        return [{"artifact_id": key, "input_hash": _fingerprint(value), "payload": value}
                for key, value in self.artifacts.items()
                if value.get("audit_type") == "VALIDATION_ATTEMPT"
                and value["job_id"] == job_id and value["chunk_id"] == chunk.chunk_id
                and value["parent_artifact_id"] == parent_artifact_id
                and value["profile_id"] == profile_id]


class ConstructionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal("engineer:alice", "tenant-industrial",
                                   frozenset({"engineers"}), frozenset({"knowledge:construct"}))

    def interrupted(self, responses, failed_function="_audited_payload", limits=None):
        extractor, client = _feedback_extractor(responses, limits=limits)
        workflow, audit, knowledge, _ = _workflow(extractor=extractor, audit=RecoveryStore())
        with patch("graphrag_prod.construction.workflow." + failed_function,
                   side_effect=RuntimeError("parent assembly interrupted")):
            with self.assertRaises(RuntimeError):
                workflow.run(self.principal, SOURCE, _metadata())
        self.assertFalse(audit.outcomes)
        self.assertEqual(knowledge.candidate_writes, 0)
        return workflow, audit, knowledge, client

    def test_complete_two_attempt_run_recovers_and_replays_without_provider(self):
        workflow, audit, knowledge, client = self.interrupted([_bad_payload(), _valid_payload()])
        attempts = copy.deepcopy(audit.artifacts)
        recovered = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertTrue(attempts.items() <= audit.artifacts.items())
        replay = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(replay.job_id, recovered.job_id)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(knowledge.candidate_writes, 1)

    def test_saved_final_rejection_remains_rejected_without_fresh_model_call(self):
        workflow, _audit, knowledge, client = self.interrupted(
            [_bad_payload(), _bad_payload()], "_rejected_payload")
        recovered = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(recovered.chunks[0].status, "REJECTED")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(knowledge.candidate_writes, 0)

    def test_revoked_job_access_blocks_before_ingestion_and_preserves_saved_work(self):
        for mode in ("groups", "tenant"):
            with self.subTest(mode=mode):
                workflow, audit, knowledge, client = self.interrupted([_bad_payload(), _valid_payload()])
                job_id, state = next(iter(audit.jobs.items()))
                audit.jobs[job_id] = replace(
                    state, **({"access_groups": frozenset({"restricted"})}
                              if mode == "groups" else {"tenant_id": "other-tenant"}),
                )
                artifacts = copy.deepcopy(audit.artifacts)
                prepared = len(workflow.pipeline.requests)
                with self.assertRaises(ConstructionAuthorizationError):
                    workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(workflow.pipeline.requests), prepared)
                self.assertEqual(len(client.calls), 2)
                self.assertEqual(knowledge.candidate_writes, 0)
                self.assertEqual(audit.artifacts, artifacts)
                self.assertFalse(audit.outcomes)
                audit.jobs[job_id] = state
                recovered = workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
                self.assertEqual(len(client.calls), 2)

    def test_changed_or_ambiguous_saved_candidate_never_starts_provider(self):
        for mode in ("scope", "lineage", "response", "multiple"):
            with self.subTest(mode=mode):
                workflow, audit, knowledge, client = self.interrupted([_bad_payload(), _valid_payload()])
                key, last = next((key, value) for key, value in audit.artifacts.items()
                                 if value["status"] == "CANDIDATE")
                if mode == "scope":
                    last["access_groups"] = ["outsiders"]
                elif mode == "lineage":
                    last["previous_attempt_artifact_id"] = "missing"
                elif mode == "response":
                    last["response"] = "{}"
                else:
                    audit.artifacts[key + "-branch"] = copy.deepcopy(last)
                with self.assertRaises(ConstructionConflict):
                    workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(client.calls), 2)
                self.assertFalse(audit.outcomes)
                self.assertEqual(knowledge.candidate_writes, 0)

    def test_oversized_terminal_with_unsaved_response_never_starts_new_provider(self):
        workflow, audit, knowledge, client = self.interrupted(
            ["x" * 1000], "_rejected_payload", ExtractionLimits(max_response_chars=64))
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(ConstructionConflict):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(audit.outcomes)
        self.assertEqual(knowledge.candidate_writes, 0)

    def test_corrupt_incomplete_run_cannot_trigger_a_fresh_provider_call(self):
        for mode in ("checksum", "finding_codes", "predecessor"):
            with self.subTest(mode=mode):
                extractor, client = _feedback_extractor(
                    [_bad_payload(), TimeoutError("provider timeout"), _valid_payload()])
                workflow, audit, knowledge, _ = _workflow(extractor=extractor, audit=RecoveryStore())
                with self.assertRaises(ExtractionRejected):
                    workflow.run(self.principal, SOURCE, _metadata())
                first = next(value for value in audit.artifacts.values() if value["attempt"] == 1)
                last = next(value for value in audit.artifacts.values() if value["attempt"] == 2)
                if mode == "checksum":
                    first["response"] = "{}"
                elif mode == "finding_codes":
                    first["finding_codes"] = []
                else:
                    last["previous_attempt_artifact_id"] = "missing"
                with self.assertRaises(ConstructionConflict):
                    workflow.run(self.principal, SOURCE, _metadata())
                self.assertEqual(len(client.calls), 2)
                self.assertFalse(audit.outcomes)
                self.assertEqual(knowledge.candidate_writes, 0)

    def test_real_provider_failure_with_absent_response_can_resume_bounded_run(self):
        extractor, client = _feedback_extractor(
            [_bad_payload(), TimeoutError("provider timeout"), _valid_payload()])
        workflow, audit, knowledge, _ = _workflow(extractor=extractor, audit=RecoveryStore())
        with self.assertRaises(ExtractionRejected):
            workflow.run(self.principal, SOURCE, _metadata())
        failed = next(value for value in audit.artifacts.values() if value["status"] == "PROVIDER_ERROR")
        self.assertIsNone(failed["response"])
        self.assertIsNone(failed["response_checksum"])
        self.assertIsNone(failed["response_chars"])
        old = copy.deepcopy(audit.artifacts)
        recovered = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(old.items() <= audit.artifacts.items())
        self.assertEqual(knowledge.candidate_writes, 1)
