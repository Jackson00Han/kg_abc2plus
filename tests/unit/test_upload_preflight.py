"""Upload advice over the permitted pump kit, without providers or writes."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

from graphrag_prod.construction.preflight import (
    MAX_EXACT_MATCHES,
    MAX_SIMILARITY_CANDIDATES,
    MAX_SIMILARITY_CHARACTERS,
    Neo4jUploadPreflight,
    UploadPreflightUnavailable,
    _BOUNDARY,
    _EXACT,
    _SIMILAR,
    _TEXT,
    _first_difference,
    _jaccard,
    _shingles,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum


KIT = (Path(__file__).resolve().parents[2] / "src/graphrag_prod/playground/static/industrial-demo-v1")
PUMP = (KIT / "authoritative_source.txt").read_text(encoding="utf-8")
HOMONYM = (KIT / "homonym_report.txt").read_text(encoding="utf-8")
URI = "https://example.test/industrial-demo-v1/authoritative_source.txt"
PRINCIPAL = Principal("pump-reviewer", "pump-preflight-unit", frozenset({"engineering"}),
                      frozenset({"knowledge:construct"}))


def _row(text=PUMP, number=0, **changes):
    row = {
        "document_id": f"pump-document-{number}",
        "version_id": f"pump-version-{number}",
        "title": "循环水泵设备台账",
        "canonical_uri": URI,
        "checksum": content_checksum(text),
        "original_checksum": content_checksum(text),
        "characters": len(text),
        "_policy_id": "pump-engineering-policy",
        "_policy_version": 1,
        "_groups": ["engineering"],
        "_generation": 1,
        "_snapshot_ids": ["pump-snapshot"],
    }
    row.update(changes)
    return row


class _Transaction:
    def __init__(self, *, exact=(), candidates=(), texts=None):
        self.exact = list(exact)
        self.candidates = list(candidates)
        self.texts = dict(texts or {})
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if query == _EXACT:
            return deepcopy(self.exact)
        if query == _SIMILAR:
            return deepcopy(self.candidates)
        if query == _TEXT:
            return [{"version_id": key, "checksum": content_checksum(self.texts[key]),
                     "text": self.texts[key]}
                    for key in parameters["version_ids"] if key in self.texts]
        raise AssertionError("unexpected preflight query")


class _Session:
    def __init__(self, tx):
        self.tx = tx

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute_read(self, work, *args):
        return work(self.tx, *args)


def _reader(tx):
    return Neo4jUploadPreflight(SimpleNamespace(session=lambda **_: _Session(tx)))


class UploadPreflightTests(unittest.TestCase):
    def test_renamed_exact_upload_returns_existing_source_without_source_text(self):
        tx = _Transaction(exact=[_row()])
        result = _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI + ".renamed")
        match = result["exact_matches"][0]
        self.assertEqual(match["document_id"], "pump-document-0")
        self.assertEqual(match["match_kind"], "EXACT")
        self.assertEqual(match["similarity"], 1.0)
        self.assertIsNone(match["difference"])
        self.assertNotIn("text", match)
        self.assertNotIn("_groups", match)
        self.assertFalse(any(query == _TEXT for query, _ in tx.calls))
        self.assertTrue(result["similarity_checked"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["compared_versions"], 0)

    def test_line_endings_and_bom_use_existing_parser_normalization(self):
        content = b"\xef\xbb\xbf" + PUMP.replace("\n", "\r\n").encode()
        tx = _Transaction(exact=[_row()])
        result = _reader(tx).check(PRINCIPAL, content, canonical_uri=URI)
        self.assertEqual(result["checksum"], content_checksum(PUMP))
        self.assertEqual(result["original_checksum"], content_checksum(content))
        self.assertNotEqual(result["checksum"], result["original_checksum"])
        parameters = tx.calls[0][1]
        self.assertEqual(parameters["tenant_id"], PRINCIPAL.tenant_id)
        self.assertEqual(parameters["principal_groups"], ["engineering"])

    def test_changed_power_is_only_similar_and_difference_retains_both_numbers(self):
        changed = PUMP.replace("37.5 kW", "38.5 kW")
        tx = _Transaction(candidates=[_row()], texts={"pump-version-0": PUMP})
        result = _reader(tx).check(PRINCIPAL, changed.encode(), canonical_uri=URI)
        self.assertFalse(result["exact_matches"])
        match = result["similar_matches"][0]
        self.assertEqual(match["match_kind"], "SIMILAR")
        self.assertLess(match["similarity"], 1.0)
        self.assertGreaterEqual(match["similarity"], 0.85)
        self.assertIn("37.5 kW", match["difference"]["before"])
        self.assertIn("38.5 kW", match["difference"]["after"])
        self.assertLessEqual(sum(map(len, match["difference"].values())), 160)
        self.assertEqual(result["compared_versions"], 1)

    def test_same_equipment_name_in_different_report_does_not_imply_duplicate(self):
        tx = _Transaction(candidates=[_row(HOMONYM)], texts={"pump-version-0": HOMONYM})
        result = _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)
        self.assertFalse(result["similar_matches"])
        self.assertFalse(result["exact_matches"])

    def test_candidate_overflow_reads_at_most_fifty_full_versions_and_reports_bound(self):
        original = PUMP.replace("37.5 kW", "38.5 kW")
        candidates = [_row(original, index) for index in range(MAX_SIMILARITY_CANDIDATES + 1)]
        tx = _Transaction(candidates=candidates, texts={row["version_id"]: original for row in candidates})
        result = _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reasons"], ["SIMILARITY_CANDIDATE_LIMIT"])
        self.assertEqual(result["compared_versions"], 50)
        self.assertEqual(len(result["similar_matches"]), 50)
        text_parameters = next(params for query, params in tx.calls if query == _TEXT)
        self.assertEqual(len(text_parameters["version_ids"]), 50)
        self.assertNotIn("pump-version-50", text_parameters["version_ids"])

    def test_exact_overflow_returns_twenty_and_never_counts_invisible_sources(self):
        tx = _Transaction(exact=[_row(number=index) for index in range(MAX_EXACT_MATCHES + 1)])
        result = _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)
        self.assertEqual(len(result["exact_matches"]), 20)
        self.assertTrue(result["truncated"])
        self.assertIn("EXACT_RESULT_LIMIT", result["truncation_reasons"])
        self.assertNotIn("total_documents", result)
        self.assertTrue(all(params["limit"] in (21, 51) for _, params in tx.calls))

    def test_large_input_still_checks_exact_but_never_claims_similarity_checked(self):
        large = (PUMP * (MAX_SIMILARITY_CHARACTERS // len(PUMP) + 1)).encode()
        tx = _Transaction()
        result = _reader(tx).check(PRINCIPAL, large, canonical_uri=URI)
        self.assertFalse(result["similarity_checked"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reasons"], ["INPUT_SIMILARITY_CHARACTER_LIMIT"])
        self.assertTrue(all(query == _EXACT for query, _ in tx.calls))

    def test_changed_authorization_before_response_fails_closed(self):
        class Revoked(_Transaction):
            def run(self, query, **parameters):
                result = super().run(query, **parameters)
                if query == _EXACT and sum(q == _EXACT for q, _ in self.calls) > 1:
                    return []
                return result

        with self.assertRaises(UploadPreflightUnavailable):
            _reader(Revoked(exact=[_row()])).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)

    def test_partial_visibility_or_changed_source_never_exposes_diff(self):
        candidate = _row()
        tx = _Transaction(candidates=[candidate])
        with self.assertRaises(UploadPreflightUnavailable):
            _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)
        tx = _Transaction(candidates=[candidate], texts={candidate["version_id"]: HOMONYM})
        with self.assertRaises(UploadPreflightUnavailable):
            _reader(tx).check(PRINCIPAL, PUMP.encode(), canonical_uri=URI)

    def test_permission_and_parser_errors_happen_before_database_reads(self):
        tx = _Transaction()
        unauthorized = Principal("viewer", PRINCIPAL.tenant_id, PRINCIPAL.groups)
        with self.assertRaises(PermissionError):
            _reader(tx).check(unauthorized, PUMP.encode(), canonical_uri=URI)
        with self.assertRaises(ValueError):
            _reader(tx).check(PRINCIPAL, b"", canonical_uri=URI)
        self.assertFalse(tx.calls)

    def test_standard_similarity_and_linear_difference_bounds(self):
        self.assertEqual(_jaccard({"循环水泵", "机械密封"}, {"循环水泵"}), 0.5)
        self.assertEqual(_shingles("循环水泵"), {"循环水泵"})
        self.assertEqual(_shingles("循环水泵台账"), {"循环水泵台", "环水泵台账"})
        self.assertIsNone(_first_difference(PUMP, PUMP))
        difference = _first_difference(PUMP, PUMP + "设备编码 BC-P-202")
        self.assertIn("BC-P-202", difference["after"])
        self.assertLessEqual(sum(map(len, difference.values())), 160)

    def test_query_boundary_checks_complete_source_acl_and_current_publication_pins(self):
        for required in (
            "ACTIVE_SNAPSHOT", "ACTIVE_VERSION", "ACTIVE_KNOWLEDGE_PUBLICATION",
            "USES_KNOWLEDGE_SNAPSHOT", "status: 'ACTIVE'", "HAS_VERSION",
            "snapshot.version_id = version.version_id", "version.document_id = document.document_id",
            "document.retirement_request_fingerprint IS NULL", "snapshot.retirement_id IS NULL",
            "version.retirement_id IS NULL", "chunk.retirement_id IS NOT NULL",
            "chunk.access_groups <> document.access_groups", "chunk.access_policy_version <> document.access_policy_version",
            "COUNT { MATCH (version)-[:HAS_CHUNK]->() } = snapshot.expected_chunk_count",
            "chunk.char_end <> size(version.normalized_text)", "previous.char_end = chunk.char_start",
        ):
            self.assertIn(required, _BOUNDARY)
        for query in (_EXACT, _SIMILAR, _TEXT):
            self.assertTrue(query.startswith(_BOUNDARY))
            self.assertNotIn(" SET ", query)
            self.assertNotIn(" MERGE ", query)
        self.assertNotIn("AS text", _EXACT)
        self.assertNotIn("AS text", _SIMILAR)


if __name__ == "__main__":
    unittest.main()
