"""Legacy recovery scans cannot mistake damaged or reformatted audits for absence."""

from __future__ import annotations

import json
import unittest

from graphrag_prod.construction import (
    ConstructionAuthorizationError, ConstructionConflict, Neo4jConstructionAuditStore,
)
from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import content_checksum, derivation_artifact_id
from graphrag_prod.ingestion.models import _fingerprint
from tests.unit.test_construction_extraction import _chunk, _profile


TENANT = "tenant-industrial"
KIND = "ONTOLOGY_EXTRACTION_AUDIT"
PROFILE = _profile().profile_id
JOB = "reader-job"
PARENT = "reader-parent"


def _attempt(*, job=JOB):
    return {"format_version": 1, "audit_type": "VALIDATION_ATTEMPT", "job_id": job,
            "chunk_id": _chunk().chunk_id, "parent_artifact_id": PARENT if job == JOB else "other-" + job}


def _parent():
    return {"format_version": 1, "disposition": "CANDIDATE", "ontology_version_id": "tbox",
            "ontology_checksum": "a" * 64, "extractor_version": "extractor", "prompt_version": "prompt",
            "model": "fixture", "extracted_at": "2026-01-01T00:00:00+00:00", "findings": [], "output": {}}


def _row(payload, *, pretty=False, input_hash=None):
    raw = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None,
                     separators=None if pretty else (",", ":"))
    output = _fingerprint(payload)
    digest = output if input_hash is None else input_hash
    return {"artifact_id": derivation_artifact_id(TENANT, KIND, digest, PROFILE),
            "tenant_id": TENANT, "kind": KIND, "profile_id": PROFILE,
            "input_hash": digest, "output_checksum": output,
            "payload_chars": len(raw), "payload_json": raw}


class _Result:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)


class _Transaction:
    def __init__(self, rows, *, authorizations=(True, True)):
        self.rows = rows
        self.authorizations = iter(authorizations)
        self.authorization_reads = 0
        self.artifact_reads = 0
        self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        if "AS authorized_chunk_id" in query:
            self.authorization_reads += 1
            visible = next(self.authorizations)
            return _Result([{"authorized_chunk_id": _chunk().chunk_id}] if visible else [])
        self.artifact_reads += 1
        return _Result(self.rows)


class _Session:
    def __init__(self, tx): self.tx = tx; self.transaction_options = None
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def begin_transaction(self, **options):
        self.transaction_options = options
        return self.tx


class _Driver:
    def __init__(self, tx): self.session_value = _Session(tx); self.session_options = None
    def session(self, **options):
        self.session_options = options
        return self.session_value


def _read(rows, *, authorizations=(True, True)):
    tx = _Transaction(rows, authorizations=authorizations)
    driver = _Driver(tx)
    store = Neo4jConstructionAuditStore(driver)
    principal = Principal("reader", TENANT, frozenset({"engineers"}), frozenset({"knowledge:construct"}))
    return store, principal, driver, tx


def _invoke(store, principal):
    return store.read_validation_attempts(principal, job_id=JOB, chunk=_chunk(),
                                          parent_artifact_id=PARENT, profile_id=PROFILE)


class ConstructionAuditReaderTests(unittest.TestCase):
    def test_pretty_historical_json_is_found_and_parent_uses_distinct_input_hash(self):
        attempt = _row(_attempt(), pretty=True)
        parent = _row(_parent(), input_hash=content_checksum("parent request"))
        unrelated = _row(_attempt(job="other-job"), pretty=True)
        store, principal, driver, tx = _read([parent, unrelated, attempt])
        rows = _invoke(store, principal)
        self.assertEqual([row["artifact_id"] for row in rows], [attempt["artifact_id"]])
        self.assertEqual(rows[0]["payload"], _attempt())
        self.assertEqual(tx.authorization_reads, 2)
        self.assertEqual(driver.session_options["default_access_mode"], "READ")
        self.assertEqual(driver.session_options["fetch_size"], 1)
        self.assertEqual(driver.session_value.transaction_options, {"timeout": 15.0})
        artifact_query = tx.queries[1][0]
        self.assertNotIn("CONTAINS", artifact_query)
        self.assertNotIn("OPTIONAL MATCH", artifact_query)
        self.assertNotIn("markers", tx.queries[1][1])

    def test_missing_or_revoked_current_source_is_not_an_empty_recovery(self):
        for authorization in ((False,), (True, False)):
            with self.subTest(authorization=authorization):
                store, principal, _driver, tx = _read([], authorizations=authorization)
                with self.assertRaises(ConstructionAuthorizationError):
                    _invoke(store, principal)
                self.assertEqual(tx.artifact_reads, 0 if authorization == (False,) else 1)
        store, principal, _, _ = _read([])
        self.assertEqual(_invoke(store, principal), [])

    def test_broken_json_without_markers_and_unknown_parent_fail_before_selection(self):
        for raw in ("{broken", '{"format_version":1,"format_version":1}', '{"v":NaN}', '[]'):
            with self.subTest(raw=raw):
                row = _row(_attempt(job="unrelated"))
                row.update(payload_json=raw, payload_chars=len(raw))
                store, principal, _, _ = _read([row])
                with self.assertRaises(ConstructionConflict):
                    _invoke(store, principal)
        for payload in ({"format_version": 1}, {"format_version": 1, "audit_type": "unknown"}):
            store, principal, _, _ = _read([_row(payload)])
            with self.assertRaises(ConstructionConflict):
                _invoke(store, principal)

    def test_unrelated_corrupt_envelopes_cannot_be_silently_filtered(self):
        mutations = {"output_checksum": "f" * 64, "artifact_id": "wrong-id", "tenant_id": "other",
                     "profile_id": "other", "input_hash": "0" * 64}
        for key, value in mutations.items():
            with self.subTest(field=key):
                row = _row(_attempt(job="unrelated")); row[key] = value
                store, principal, _, _ = _read([row])
                with self.assertRaises(ConstructionConflict):
                    _invoke(store, principal)
        parent_like_attempt = _row(_attempt(), input_hash=content_checksum("wrong attempt input"))
        store, principal, _, _ = _read([parent_like_attempt])
        with self.assertRaises(ConstructionConflict):
            _invoke(store, principal)

    def test_scope_fields_must_be_structural_strings_before_filtering(self):
        for name in ("job_id", "chunk_id", "parent_artifact_id"):
            for value in (None, [], 1, ""):
                payload = _attempt(job="unrelated"); payload[name] = value
                store, principal, _, _ = _read([_row(payload)])
                with self.subTest(field=name, value=value), self.assertRaises(ConstructionConflict):
                    _invoke(store, principal)

    def test_partially_changed_scope_is_a_conflict_not_an_unrelated_record(self):
        for name in ("job_id", "chunk_id", "parent_artifact_id"):
            payload = _attempt(); payload[name] = "changed-scope"
            store, principal, _, _ = _read([_row(payload, pretty=True)])
            with self.subTest(field=name), self.assertRaisesRegex(ConstructionConflict, "scope conflicts"):
                _invoke(store, principal)

    def test_artifact_count_limit_never_returns_a_partial_recovery(self):
        rows = [_row(_attempt(job=f"other-{index}")) for index in range(257)]
        store, principal, _, _ = _read(rows)
        with self.assertRaisesRegex(ConstructionConflict, "256-artifact"):
            _invoke(store, principal)
        store, principal, _, _ = _read(rows[:256])
        self.assertEqual(_invoke(store, principal), [])

    def test_matching_attempt_limit_is_separate_from_profile_scan_limit(self):
        rows = []
        for index in range(33):
            payload = _attempt(); payload["validation_run_id"] = str(index)
            rows.append(_row(payload, pretty=True))
        store, principal, _, _ = _read(rows)
        with self.assertRaisesRegex(ConstructionConflict, "32-attempt"):
            _invoke(store, principal)

    def test_oversized_or_malformed_payload_lengths_fail_closed(self):
        for raw, size in ((None, 8 * 1024 * 1024 + 1), ([], 0), ("{}", 3), ("{}", True),
                          ("汉" * (8 * 1024 * 1024 // 3 + 1), 8 * 1024 * 1024 // 3 + 1)):
            row = _row(_attempt()); row.update(payload_json=raw, payload_chars=size)
            store, principal, _, _ = _read([row])
            with self.subTest(size=size), self.assertRaises(ConstructionConflict):
                _invoke(store, principal)

    def test_cumulative_byte_limit_prevents_partial_success(self):
        payload = _parent(); payload["padding"] = "x" * (7 * 1024 * 1024)
        rows = (_row(payload, input_hash=content_checksum(f"parent-{index}")) for index in range(5))
        store, principal, _, _ = _read(rows)
        with self.assertRaisesRegex(ConstructionConflict, "byte scan limit"):
            _invoke(store, principal)


if __name__ == "__main__":
    unittest.main()
