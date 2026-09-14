"""Final latest-code test against the verified owned, empty disposable fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
info=json.loads(subprocess.check_output(['docker','container','inspect','sample-graphrag-stage8-neo4j-9643'],text=True))[0]
assert info['Config']['Labels'].get('com.sample-graphrag.stage8-run')
binding=info['NetworkSettings']['Ports']['7687/tcp'][0]
assert binding['HostIp']=='127.0.0.1' and binding['HostPort']=='59014'
for name in ('OPENAI_API_KEY','OPENAI_BASE_URL','NEO4J_URI','NEO4J_PASSWORD'):
    os.environ.pop(name,None)
os.environ.update(TEST_NEO4J_URI='bolt://127.0.0.1:59014',TEST_NEO4J_USER='neo4j',
    TEST_NEO4J_PASSWORD='stage8-test-password',TEST_NEO4J_DATABASE='neo4j',
    GRAPHRAG_ALLOW_DISPOSABLE_DB='1',GRAPHRAG_EVALUATION_OUTPUT_DIR='.local/context-projection/observations-targeted')
from scripts.run_test_suite import RecordingRunner
from tests.integration.test_context_projection_neo4j import ContextProjectionNeo4jTests
suite=unittest.TestSuite([ContextProjectionNeo4jTests(
    'test_additive_repair_preserves_old_heads_evidence_audit_and_manual_decisions')])
started=time.monotonic()
with open('.local/context-projection-targeted.log','w') as stream:
    result=RecordingRunner(stream=stream,verbosity=2).run(suite)
payload={'schema_version':'unittest-suite-result-v1','tests_run':result.testsRun,
    'passed_test_ids':result.passed_ids,'errors':[test.id() for test,_ in result.errors],
    'failures':[test.id() for test,_ in result.failures],'skipped':[test.id() for test,_ in result.skipped],
    'seconds':round(time.monotonic()-started,3)}
Path('.local/context-projection/result-targeted.json').write_text(json.dumps(payload,sort_keys=True)+'\n')
print(json.dumps(payload,sort_keys=True))
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
