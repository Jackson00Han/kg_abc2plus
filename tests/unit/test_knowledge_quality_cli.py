"""Command-line contract tests for the offline knowledge-quality gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_knowledge_quality import (
    knowledge_gold,
    knowledge_policy,
    knowledge_predictions,
    locked_baseline,
)

ROOT = Path(__file__).parents[2]


class KnowledgeQualityCliTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, value: object) -> Path:
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _run(
        self,
        directory: Path,
        *,
        predictions: dict[str, object],
        baseline: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        gold = knowledge_gold()
        policy = knowledge_policy()
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "evaluate_knowledge_quality.py"),
                "--gold",
                str(self._write(directory, "gold.json", gold)),
                "--predictions",
                str(self._write(directory, "predictions.json", predictions)),
                "--policy",
                str(self._write(directory, "policy.json", policy)),
                "--baseline",
                str(self._write(directory, "baseline.json", baseline)),
                "--output",
                str(directory / "report.json"),
                "--baseline-candidate",
                str(directory / "candidate.json"),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_cli_writes_report_and_unlocked_candidate_and_returns_zero(self) -> None:
        gold = knowledge_gold()
        predictions = knowledge_predictions(gold)
        baseline = locked_baseline(gold, predictions, knowledge_policy())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            completed = self._run(directory, predictions=predictions, baseline=baseline)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((directory / "report.json").read_text())
            candidate = json.loads((directory / "candidate.json").read_text())
            self.assertTrue(report["passed"])
            self.assertFalse(candidate["locked"])
            self.assertIn(report["report_digest"], completed.stdout)

    def test_cli_returns_one_for_gate_failure_and_malformed_input(self) -> None:
        gold = knowledge_gold()
        good = knowledge_predictions(gold)
        baseline = locked_baseline(gold, good, knowledge_policy())
        failing = knowledge_predictions(gold)
        failing["cases"][0]["relationships"] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            completed = self._run(directory, predictions=failing, baseline=baseline)
            self.assertEqual(completed.returncode, 1)
            self.assertFalse(
                json.loads((directory / "report.json").read_text())["passed"]
            )

            (directory / "baseline.json").write_text("[]", encoding="utf-8")
            malformed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "evaluate_knowledge_quality.py"),
                    "--gold",
                    str(directory / "gold.json"),
                    "--predictions",
                    str(directory / "predictions.json"),
                    "--policy",
                    str(directory / "policy.json"),
                    "--baseline",
                    str(directory / "baseline.json"),
                    "--output",
                    str(directory / "malformed-report.json"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(malformed.returncode, 1)
            self.assertIn("failed closed", malformed.stderr)


if __name__ == "__main__":
    unittest.main()
