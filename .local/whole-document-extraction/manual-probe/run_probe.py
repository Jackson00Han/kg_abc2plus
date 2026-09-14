from pathlib import Path
import json
import sys
import unittest

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from scripts.run_test_suite import RecordingRunner
suite = unittest.defaultTestLoader.loadTestsFromName(
    "tests.integration.test_construction_workflow_neo4j.Neo4jConstructionWorkflowIntegrationTests.test_manual_source_requires_complete_active_publication_and_keeps_provenance")
result = RecordingRunner(stream=sys.stderr, verbosity=2).run(suite)
payload = {"scope": "manual_source_formal_retrieval_probe", "tests_run": result.testsRun,
    "passed_test_ids": result.passed_ids, "failures": [t.id() for t, _ in result.failures],
    "errors": [t.id() for t, _ in result.errors], "skipped": [t.id() for t, _ in result.skipped]}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2) + "\n")
raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
