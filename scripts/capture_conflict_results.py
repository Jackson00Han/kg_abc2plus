#!/usr/bin/env python3
"""Capture deterministic Stage 8 conflict results through generation service."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from graphrag_prod.evaluation.datasets import load_jsonl
from graphrag_prod.generation import (
    AnswerModelRequest,
    GenerationRequest,
    GroundedGenerationService,
)
from graphrag_prod.retrieval import Citation, RetrievedChunk


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = ROOT / "evaluation" / "gold-v1" / "conflict-answers.jsonl"
DEFAULT_SOURCES = ROOT / "evaluation" / "gold-v1" / "conflict-sources.jsonl"


class _FixtureModel:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def generate(self, request: AnswerModelRequest) -> object:
        del request
        return self.payload


def _chunk(source: dict[str, Any]) -> RetrievedChunk:
    return RetrievedChunk(
        text=source["text"],
        citation=Citation(
            chunk_id=source["chunk_id"],
            chunk_checksum=source["chunk_checksum"],
            document_id=source["document_id"],
            canonical_uri=source["canonical_uri"],
            source_name=source["source_name"],
            version_id=source["version_id"],
            version_checksum=source["version_checksum"],
            version_number=source["version_number"],
            ordinal=source["ordinal"],
            char_start=source["char_start"],
            char_end=source["char_end"],
            page_number=source["page_number"],
            section=source["section"],
            document_title=source["document_title"],
            published_at=datetime.fromisoformat(source["published_at"]),
        ),
        role="ranked",
        score=1.0,
        reasons=("stage8-conflict-fixture",),
    )


def _claim_payload(
    claim: dict[str, Any], labels: dict[str, str], sources: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    citation_ids = [labels[chunk_id] for chunk_id in claim["evidence_chunk_ids"]]
    return {
        "citation_ids": citation_ids,
        "evidence": [
            {
                "citation_id": labels[chunk_id],
                "quote": sources[chunk_id]["text"],
            }
            for chunk_id in claim["evidence_chunk_ids"]
        ],
        "inference": claim["inference"],
        "material": True,
        "text": claim["reference_text"],
    }


def capture(
    gold_items: tuple[dict[str, Any], ...],
    source_items: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    sources = {item["chunk_id"]: item for item in source_items}
    results: list[dict[str, Any]] = []
    for gold in gold_items:
        selected_ids = [source["chunk_id"] for source in gold["evidence"]]
        labels = {
            chunk_id: f"S{index}"
            for index, chunk_id in enumerate(selected_ids, start=1)
        }
        chunks = tuple(_chunk(sources[chunk_id]) for chunk_id in selected_ids)
        claim_payloads = [
            _claim_payload(claim, labels, sources) for claim in gold["claims"]
        ]
        if gold["expected_status"] == "conflict":
            payload = {
                "claims": [],
                "conflicts": [
                    {
                        "alternatives": [
                            {
                                "citation_ids": claim["citation_ids"],
                                "evidence": claim["evidence"],
                                "text": claim["text"],
                            }
                            for claim in claim_payloads
                        ],
                        "topic": "Apple Inc. reported revenue",
                    }
                ],
                "status": "conflict",
            }
        else:
            payload = {
                "claims": claim_payloads,
                "conflicts": [],
                "status": "answered",
            }
        result = GroundedGenerationService(_FixtureModel(payload)).generate(
            GenerationRequest(gold["query"], chunks)
        )
        if result.failure_code is not None:
            raise RuntimeError(
                f"conflict capture failed for {gold['id']}: {result.failure_code}"
            )
        record = result.as_dict()
        record["id"] = gold["id"]
        results.append(record)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = capture(load_jsonl(args.gold), load_jsonl(args.sources))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in results
        ),
        encoding="utf-8",
    )
    print(f"captured {len(results)} conflict evaluation results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
