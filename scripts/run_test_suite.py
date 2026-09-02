#!/usr/bin/env python3
"""Run one unittest discovery suite and emit an exact machine-readable result."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started_ids: list[str] = []
        self.passed_ids: list[str] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.started_ids.append(test.id())
        super().startTest(test)

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self.passed_ids.append(test.id())
        super().addSuccess(test)


class RecordingRunner(unittest.TextTestRunner):
    resultclass = RecordingResult


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=Path, required=True)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-no-skips", action="store_true")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(
        str(args.start), pattern=args.pattern
    )
    result = RecordingRunner(stream=sys.stderr, verbosity=2).run(suite)
    payload = {
        "errors": [test.id() for test, _ in result.errors],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "failures": [test.id() for test, _ in result.failures],
        "passed_test_ids": sorted(result.passed_ids),
        "schema_version": "unittest-suite-result-v1",
        "skipped": [test.id() for test, _ in result.skipped],
        "started_test_ids": sorted(result.started_ids),
        "tests_run": result.testsRun,
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    skipped_failure = bool(result.skipped) and args.require_no_skips
    return 0 if result.wasSuccessful() and not skipped_failure else 1


if __name__ == "__main__":
    raise SystemExit(main())
