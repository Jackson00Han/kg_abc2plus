"""Committed extraction-quality assets and Stage 8 wiring tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphrag_prod.evaluation.knowledge_quality import (
    KNOWLEDGE_REPORT_SCHEMA_VERSION,
    build_knowledge_quality_report,
)
from graphrag_prod.evaluation.runner import _load_knowledge_quality_evidence


ROOT = Path(__file__).parents[2]
ASSET_ROOT = ROOT / "evaluation" / "knowledge-quality-v1"


def _load(name: str) -> dict[str, object]:
    value = json.loads((ASSET_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{name} must contain one JSON object")
    return value


def _asset_paths(root: Path = ASSET_ROOT) -> dict[str, Path]:
    return {
        name: root / f"{name}.json"
        for name in ("gold", "predictions", "policy", "baseline")
    }


class KnowledgeQualityAssetTests(unittest.TestCase):
    def test_committed_assets_are_adjudicated_complete_and_pass_locked_gate(
        self,
    ) -> None:
        gold = _load("gold.json")
        predictions = _load("predictions.json")
        policy = _load("policy.json")
        baseline = _load("baseline.json")

        first = build_knowledge_quality_report(
            gold=gold,
            predictions=predictions,
            policy=policy,
            baseline=baseline,
        )
        second = build_knowledge_quality_report(
            gold=_load("gold.json"),
            predictions=_load("predictions.json"),
            policy=_load("policy.json"),
            baseline=_load("baseline.json"),
        )

        self.assertEqual(first, second)
        self.assertTrue(first["passed"], first["failures"])
        self.assertEqual(first["schema_version"], KNOWLEDGE_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            first["coverage"]["case_class_counts"],
            {"negative": 1, "positive": 1, "security": 1},
        )
        self.assertEqual(first["extraction"]["overall"]["f1"], 1.0)
        self.assertEqual(first["resolution"]["positive_pairs"], 1)
        self.assertEqual(first["resolution"]["negative_pairs"], 1)
        self.assertEqual(first["violations"]["evidence_violation_count"], 0)
        self.assertEqual(first["violations"]["authority_contamination_count"], 0)
        self.assertTrue(baseline["locked"])

        positive = gold["cases"][0]
        fact = positive["property_facts"][0]
        self.assertEqual((fact["raw_value"], fact["raw_unit"]), ("1200", "kPa"))
        self.assertEqual(
            (fact["canonical_value"], fact["canonical_unit"]),
            ("12", "bar"),
        )
        self.assertEqual(fact["valid_from"], "2025-01-01T00:00:00Z")
        self.assertEqual(fact["valid_to"], "2025-06-01T00:00:00Z")
        self.assertEqual(fact["observed_at"], "2025-02-03T01:30:00Z")

    def test_committed_cli_run_writes_the_same_passing_report(self) -> None:
        expected = build_knowledge_quality_report(
            gold=_load("gold.json"),
            predictions=_load("predictions.json"),
            policy=_load("policy.json"),
            baseline=_load("baseline.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "knowledge-quality-report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "evaluate_knowledge_quality.py"),
                    "--gold",
                    str(ASSET_ROOT / "gold.json"),
                    "--predictions",
                    str(ASSET_ROOT / "predictions.json"),
                    "--policy",
                    str(ASSET_ROOT / "policy.json"),
                    "--baseline",
                    str(ASSET_ROOT / "baseline.json"),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)
            self.assertIn(expected["report_digest"], completed.stdout)

    def test_unified_evidence_recomputes_report_and_binds_prediction_identity(
        self,
    ) -> None:
        assets = {name: _load(f"{name}.json") for name in _asset_paths()}
        original = build_knowledge_quality_report(
            gold=assets["gold"],
            predictions=assets["predictions"],
            policy=assets["policy"],
            baseline=assets["baseline"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            paths = _asset_paths(directory)
            for name, path in paths.items():
                path.write_text(
                    json.dumps(assets[name], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            report_path = directory / "report.json"
            report_path.write_text(
                json.dumps(original, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            identity, diagnostics = _load_knowledge_quality_evidence(
                report_path,
                paths,
            )
            self.assertEqual(identity["report_digest"], original["report_digest"])
            self.assertEqual(
                identity["prediction_digest"],
                original["identity"]["prediction_digest"],
            )
            self.assertTrue(diagnostics["knowledge_passed"])

            forged = dict(original)
            forged["report_digest"] = "0" * 64
            report_path.write_text(
                json.dumps(forged, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exact assets"):
                _load_knowledge_quality_evidence(report_path, paths)

            changed_predictions = dict(assets["predictions"])
            changed_predictions["extractor_version"] = "reference-predictions-v2"
            paths["predictions"].write_text(
                json.dumps(changed_predictions, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            changed = build_knowledge_quality_report(
                gold=assets["gold"],
                predictions=changed_predictions,
                policy=assets["policy"],
                baseline=assets["baseline"],
            )
            report_path.write_text(
                json.dumps(changed, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            changed_identity, _ = _load_knowledge_quality_evidence(
                report_path,
                paths,
            )
            self.assertNotEqual(identity, changed_identity)

    def test_stage8_runs_the_locked_gate_for_every_iteration(self) -> None:
        runner = (ROOT / "scripts" / "run_stage8_validation.sh").read_text(
            encoding="utf-8"
        )
        gate_call = "uv run --locked python scripts/evaluate_knowledge_quality.py"
        self.assertEqual(runner.count(gate_call), 1)
        self.assertLess(
            runner.index("while [ \"$iteration\" -le \"$repeat\" ]"),
            runner.index(gate_call),
        )
        self.assertLess(
            runner.index(gate_call),
            runner.index("iteration=$((iteration + 1))"),
        )
        for name in ("gold", "predictions", "policy", "baseline"):
            self.assertIn(
                f"--{name} evaluation/knowledge-quality-v1/{name}.json",
                runner,
            )
        self.assertIn(
            '--output "$run_dir/knowledge-quality-report.json"',
            runner,
        )
        self.assertEqual(
            runner.count(
                '--knowledge-quality-report "$run_dir/knowledge-quality-report.json"'
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
