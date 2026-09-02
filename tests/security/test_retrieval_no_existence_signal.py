"""Paired regressions for retrieval existence-signal resistance.

The fixture models Neo4j index-procedure semantics precisely enough to expose
candidate-window crowd-out: the procedure takes its bounded top-N window
before the surrounding Cypher applies tenant, lifecycle, and ACL predicates.
The same authorized request is evaluated with and without higher-scoring
invisible candidates.  Neither the returned evidence nor its trace may change.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY,
    BM25_RECALL_QUERY,
    CANDIDATE_VECTOR_QUERY,
    CORPUS_STATE_QUERY,
    GRAPH_EXPANSION_QUERY,
    HYDRATE_QUERY,
    Neo4jRetrievalEngine,
    VECTOR_RECALL_QUERY,
)
from graphrag_prod.retrieval.models import (
    RetrievalLimits,
    RetrievalRequest,
)


ROOT = Path(__file__).parents[2]
TENANT_ID = "tenant-visible"
GROUP = "readers"
AUTHORIZED_ID = "authorized-chunk"


def _candidate(identifier: str, visibility: str, score: float) -> dict[str, object]:
    return {"chunk_id": identifier, "score": score, "visibility": visibility}


class _IndexWindowTransaction:
    """Small query-semantic harness with an optional invisible population."""

    def __init__(self, invisible_kind: str | None) -> None:
        self.invisible_kind = invisible_kind
        self.authorized = _candidate(AUTHORIZED_ID, "authorized", 0.90)
        self.invisible = tuple(
            _candidate(f"{invisible_kind}-{ordinal}", invisible_kind, 1.0)
            for ordinal in range(8)
        ) if invisible_kind is not None else ()

    @staticmethod
    def _visible(candidate: dict[str, object]) -> bool:
        return candidate["visibility"] == "authorized"

    @staticmethod
    def _ranked(candidates: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
        return sorted(
            candidates,
            key=lambda row: (-float(row["score"]), str(row["chunk_id"])),
        )

    def _procedure_window(
        self,
        candidates: tuple[dict[str, object], ...],
        *,
        scan_limit: int,
        return_limit: int,
    ) -> list[dict[str, object]]:
        scanned = self._ranked(candidates)[:scan_limit]
        return [row for row in scanned if self._visible(row)][:return_limit]

    def run(self, query: str, **parameters: object) -> list[dict[str, object]]:
        if query == CORPUS_STATE_QUERY:
            return [
                {
                    "corpus_revision": 7,
                    "generation_id": "generation-visible",
                    "embedding_space_id": "space-visible",
                    "dimensions": 2,
                    "index_name": "tenant-visible-vector-index",
                    "similarity": "cosine",
                }
            ]

        if query == VECTOR_RECALL_QUERY:
            # Exact cosine is evaluated only after the active tenant and ACL
            # predicates, so invisible vectors never enter a candidate window.
            return [self.authorized][: int(parameters["limit"])]

        if query == BM25_RECALL_QUERY:
            # The Lucene query itself requires the hashed tenant, active, and
            # ACL tokens, so global-index truncation happens after isolation.
            lucene_query = str(parameters["lucene_query"])
            assert "retrieval_scope:grscopeactive" in lucene_query
            assert TENANT_ID not in lucene_query
            assert GROUP not in lucene_query
            return [self.authorized][: int(parameters["limit"])]

        if query == GRAPH_EXPANSION_QUERY or query == ADJACENT_QUERY:
            return []

        if query == CANDIDATE_VECTOR_QUERY:
            candidate_ids = set(parameters["candidate_ids"])
            return (
                [{"chunk_id": AUTHORIZED_ID, "score": 0.90}]
                if AUTHORIZED_ID in candidate_ids
                else []
            )

        if query == HYDRATE_QUERY:
            requested = set(parameters["chunk_ids"])
            if AUTHORIZED_ID not in requested:
                return []
            text = "Authorized evidence."
            return [
                {
                    "chunk_id": AUTHORIZED_ID,
                    "text": text,
                    "chunk_checksum": "authorized-checksum",
                    "ordinal": 0,
                    "char_start": 0,
                    "char_end": len(text),
                    "page_number": 1,
                    "section": "Evidence",
                    "document_id": "authorized-document",
                    "canonical_uri": "urn:test:authorized-document",
                    "source_name": "security fixture",
                    "document_title": "Authorized document",
                    "version_id": "authorized-version",
                    "version_checksum": "authorized-version-checksum",
                    "version_number": 1,
                    "published_at": None,
                }
            ]

        raise AssertionError(f"unexpected retrieval query: {query[:80]!r}")


def _retrieve(invisible_kind: str | None):
    limits = RetrievalLimits(
        top_k=1,
        vector_recall_k=1,
        bm25_recall_k=1,
        bm25_scan_k=1,
        seed_k=1,
        graph_entities_per_seed=1,
        graph_edges_per_seed=1,
        graph_candidates_per_seed=1,
        candidate_limit=2,
        anchor_k=1,
        adjacent_window=0,
        max_context_chars=1_000,
    )
    request = RetrievalRequest(
        query_text="authorized evidence",
        query_vector=(1.0, 0.0),
        principal=Principal("reader", TENANT_ID, frozenset({GROUP})),
        query_embedding_space_id="space-visible",
        limits=limits,
    )
    return Neo4jRetrievalEngine._retrieve_tx(
        _IndexWindowTransaction(invisible_kind), request
    )


def _observable(result) -> dict[str, object]:
    """Return exactly the evidence and trace exposed at the retrieval boundary."""

    return {
        "chunks": [
            {
                "chunk_id": item.citation.chunk_id,
                "text": item.text,
                "score": item.score,
                "reasons": item.reasons,
            }
            for item in result.chunks
        ],
        "trace": result.trace.as_dict(),
    }


class RetrievalNoExistenceSignalTests(unittest.TestCase):
    def assert_invisible_population_has_no_signal(self, kind: str) -> None:
        baseline = _observable(_retrieve(None))
        populated = _observable(_retrieve(kind))
        self.assertTrue(baseline["chunks"], "paired baseline must be answerable")
        self.assertEqual(
            populated,
            baseline,
            f"{kind} candidates changed the authorized response or trace",
        )

    def test_same_tenant_acl_hidden_candidates_do_not_change_authorized_result(self) -> None:
        self.assert_invisible_population_has_no_signal("acl-hidden")

    def test_historical_candidates_do_not_change_authorized_result(self) -> None:
        self.assert_invisible_population_has_no_signal("historical")

    def test_cross_tenant_candidates_do_not_change_authorized_result(self) -> None:
        self.assert_invisible_population_has_no_signal("cross-tenant")

    def test_load_v1_contains_every_paired_population_at_reference_scale(self) -> None:
        manifest = json.loads(
            (ROOT / "datasets" / "load-v1" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        counts = manifest["counts"]
        coverage = manifest["coverage"]
        self.assertGreaterEqual(counts["historical_chunks"], 10_000)
        self.assertGreaterEqual(
            coverage["primary_tenant"]["protected_active_chunks"], 5_000
        )
        self.assertGreaterEqual(len(coverage["cross_tenant_chunk_ids"]), 4)


if __name__ == "__main__":
    unittest.main()
