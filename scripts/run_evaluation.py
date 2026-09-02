#!/usr/bin/env python3
"""Build and gate one unified Stage 8 evaluation report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graphrag_prod.evaluation.runner import build_evaluation_report


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations-dir", type=Path, required=True)
    parser.add_argument("--suite-results-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--baseline-candidate", type=Path)
    parser.add_argument(
        "--gold-manifest",
        type=Path,
        default=ROOT / "evaluation" / "gold-v1" / "manifest.json",
    )
    args = parser.parse_args()
    suite_paths = {
        name: args.suite_results_dir / f"{name}.json"
        for name in ("unit", "integration", "e2e", "security", "regression")
    }
    report, candidate = build_evaluation_report(
        gold_manifest=args.gold_manifest,
        graph_results_path=(
            ROOT / "evaluation" / "observations" / "graph-system-v1.json"
        ),
        retrieval_results_path=args.observations_dir / "retrieval-results.json",
        answer_results_path=args.observations_dir / "answer-results.jsonl",
        conflict_results_path=args.observations_dir / "conflict-results.jsonl",
        operational_path=(
            ROOT / "evaluation" / "observations" / "dev-mini-operational-v1.json"
        ),
        contract_path=ROOT / "contracts" / "acceptance.v1.json",
        profile_path=ROOT / "contracts" / "profiles" / "dev-mini.v1.json",
        policy_path=ROOT / "evaluation" / "regression-policy.v1.json",
        suite_result_paths=suite_paths,
        security_manifest_path=ROOT / "evaluation" / "security-suite.v1.json",
        baseline_path=args.baseline,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.baseline_candidate is not None:
        args.baseline_candidate.parent.mkdir(parents=True, exist_ok=True)
        args.baseline_candidate.write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        f"evaluation passed={report['passed']} "
        f"semantic_digest={report['semantic_digest']}"
    )
    if report["failures"]:
        for failure in report["failures"]:
            print(f"FAIL: {failure}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
