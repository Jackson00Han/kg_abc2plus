#!/usr/bin/env python3
"""Validate or run the independent semantic retrieval holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from graphrag_prod.evaluation.semantic_holdout import (
    load_semantic_holdout,
    run_live_semantic_holdout,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evaluation" / "semantic-holdout-v1" / "manifest.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the answer-free holdout, or submit its query text to the "
            "local authenticated retrieval API for live provider embeddings."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="versioned semantic holdout manifest",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate assets and source bindings without making provider calls",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="explicit loopback origin of the running local Playground",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="per-request timeout, greater than zero and at most 120 seconds",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the answer-free JSON evidence report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dataset = load_semantic_holdout(args.manifest, repository_root=ROOT)
        if args.validate_only:
            payload = {
                "dataset_id": dataset.manifest["dataset_id"],
                "dataset_version": dataset.manifest["version"],
                "item_count": len(dataset.questions),
                "question_artifact_sha256": dataset.manifest["artifact"]["sha256"],
                "validated": True,
            }
            exit_code = 0
        else:
            payload = run_live_semantic_holdout(
                dataset,
                base_url=args.base_url,
                timeout_seconds=args.timeout_seconds,
            )
            exit_code = 0 if payload["passed"] else 1
    except (OSError, RuntimeError, ValueError) as error:
        print(f"semantic holdout failed: {error}", file=sys.stderr)
        return 2

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
