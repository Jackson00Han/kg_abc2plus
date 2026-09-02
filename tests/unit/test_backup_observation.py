"""Regression tests for Stage 9 backup/restore evidence construction."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from scripts.build_backup_observation import main


ROOT = Path(__file__).parents[2]


class BackupObservationTests(unittest.TestCase):
    def _arguments(
        self,
        root: Path,
        *,
        started_ns: int,
        finished_ns: int,
        output: Path,
    ) -> list[str]:
        state = {
            "business_node_count": 3,
            "business_relationship_count": 4,
            "schema_and_indexes_verified": True,
            "sha256": "a" * 64,
        }
        resources = {
            "passed": True,
            "schema_version": "production-container-resource-observation-v1",
        }
        (root / "source.json").write_text(json.dumps(state), encoding="utf-8")
        (root / "restored.json").write_text(json.dumps(state), encoding="utf-8")
        (root / "source-resources.json").write_text(
            json.dumps(resources), encoding="utf-8"
        )
        (root / "restored-resources.json").write_text(
            json.dumps(resources), encoding="utf-8"
        )
        (root / "neo4j.dump").write_bytes(b"non-empty test dump")
        return [
            "build_backup_observation",
            "--source-state",
            str(root / "source.json"),
            "--restored-state",
            str(root / "restored.json"),
            "--started-ns",
            str(started_ns),
            "--finished-ns",
            str(finished_ns),
            "--dump-file",
            str(root / "neo4j.dump"),
            "--database",
            "neo4j",
            "--dump-command",
            "neo4j-admin database dump neo4j --to-path=/backups --overwrite-destination=true",
            "--load-command",
            "neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true",
            "--source-container-resources",
            str(root / "source-resources.json"),
            "--restored-container-resources",
            str(root / "restored-resources.json"),
            "--output",
            str(output),
        ]

    def test_increasing_timing_is_recorded_exactly(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "observation.json"
            arguments = self._arguments(
                root, started_ns=1_000, finished_ns=3_000, output=output
            )
            with patch.object(sys, "argv", arguments):
                self.assertEqual(main(), 0)
            observation = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(observation["started_ns"], 1_000)
            self.assertEqual(observation["finished_ns"], 3_000)
            self.assertEqual(observation["latency_ms"], 0.002)
            self.assertTrue(observation["passed"])

    def test_nonpositive_equal_and_decreasing_timing_fail_without_output(self) -> None:
        for started_ns, finished_ns in ((0, 1), (1, 1), (2, 1)):
            with self.subTest(started_ns=started_ns, finished_ns=finished_ns):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    output = root / "observation.json"
                    arguments = self._arguments(
                        root,
                        started_ns=started_ns,
                        finished_ns=finished_ns,
                        output=output,
                    )
                    with patch.object(sys, "argv", arguments), self.assertRaisesRegex(
                        ValueError, "positive and monotonic"
                    ):
                        main()
                    self.assertFalse(output.exists())

    def test_project_interpreter_monotonic_clock_is_cross_process_comparable(self) -> None:
        command = [sys.executable, "-c", "import time; print(time.monotonic_ns())"]
        first = int(subprocess.check_output(command, text=True).strip())
        time.sleep(0.001)
        second = int(subprocess.check_output(command, text=True).strip())
        self.assertGreater(second, first)

    def test_runner_uses_project_clock_and_orders_the_backup_samples(self) -> None:
        runner = (ROOT / "scripts" / "run_stage9_validation.sh").read_text(
            encoding="utf-8"
        )
        function_start = runner.index("monotonic_ns() {")
        function_end = runner.index("\n}", function_start)
        clock_function = runner[function_start:function_end]
        self.assertIn("uv run --locked python -", clock_function)
        self.assertNotIn("python3 -", clock_function)
        events = (
            'docker stop --time 60 "$source_container"',
            "backup_started_ns=$(monotonic_ns)",
            'docker start --attach "$dump_container"',
            'docker start --attach "$load_container"',
            'run_with_neo4j uv run --locked python -m scripts.load_production_corpus \\\n  --fingerprint --output "$restored_graph_state"',
            "backup_finished_ns=$(monotonic_ns)",
        )
        positions = [runner.index(event) for event in events]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
