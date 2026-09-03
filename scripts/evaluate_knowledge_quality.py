#!/usr/bin/env python3
"""Evaluate governed knowledge extraction against adjudicated gold and baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from graphrag_prod.evaluation.knowledge_quality import (
    build_knowledge_quality_report,
    knowledge_baseline_candidate,
)

MAX_INPUT_BYTES = 32 * 1024 * 1024


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"input file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_INPUT_BYTES:
        raise ValueError(f"input file size is outside the supported bound: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"input must contain a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--baseline-candidate",
        type=Path,
        help="write an unlocked candidate for separate human review",
    )
    args = parser.parse_args(argv)
    try:
        report = build_knowledge_quality_report(
            gold=_load_object(args.gold),
            predictions=_load_object(args.predictions),
            policy=_load_object(args.policy),
            baseline=_load_object(args.baseline),
        )
        _write_json(args.output, report)
        if args.baseline_candidate is not None:
            _write_json(args.baseline_candidate, knowledge_baseline_candidate(report))
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"knowledge quality gate failed closed: {exc}", file=sys.stderr)
        return 1

    print(
        f"knowledge-quality passed={report['passed']} "
        f"report_digest={report['report_digest']}"
    )
    for failure in report["failures"]:
        print(f"FAIL: {failure}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
