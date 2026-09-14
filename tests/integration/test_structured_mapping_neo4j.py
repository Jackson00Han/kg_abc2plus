"""Structured construction on a disposable database, never the user's corpus."""
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import unittest

from graphrag_prod.construction.structured import StructuredDocumentParser, select_structured_extractor
from graphrag_prod.construction.workflow import ConstructionAuthorizationError, ConstructionConflict
from graphrag_prod.domain.access import Principal
from graphrag_prod.ontology.models import TBoxStatus
from graphrag_prod.ontology.store import Neo4jTBoxStore
from tests.integration import test_construction_workflow_neo4j as existing
from tests.unit.test_structured_mapping import base, fixture, topology_plan


class StructuredMappingNeo4jTests(unittest.TestCase):
    setUpClass = classmethod(existing.Neo4jConstructionWorkflowIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(existing.Neo4jConstructionWorkflowIntegrationTests.tearDownClass.__func__)
    setUp = existing.Neo4jConstructionWorkflowIntegrationTests.setUp
    tearDown = existing.Neo4jConstructionWorkflowIntegrationTests.tearDown

    def test_durable_mapping_replay_and_scoped_candidates(self):
        data, plan = fixture(30)
        plan["collections"][1]["properties"] = []
        plan["collections"][1]["ignored_fields"] = {"/serial": "not declared by this test ontology"}
        model, calls = base(plan, self.tbox)
        self.workflow.extractor_factory = lambda _: model
        self.workflow.document_extractor_selector = select_structured_extractor
        self.workflow.parser = StructuredDocumentParser(json_max_chars=800)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
        metadata = replace(self.metadata, mime_type="application/json")
        started = time.monotonic()
        result = self.workflow.run(self.principal, payload, metadata)
        self.assertEqual(len(calls.calls), 1)
        self.assertTrue(all(c.status in {"EMPTY", "CANDIDATE"} for c in result.chunks))
        self.assertEqual(sum(len(c.assertion_record_ids) for c in result.chunks), 30)
        summaries = [c.mapping_summary for c in result.chunks if c.mapping_summary]
        self.assertEqual(summaries[0]["record_count"], 31)
        second = self.workflow.run(self.principal, payload, metadata)
        self.assertEqual(len(calls.calls), 1)
        self.assertTrue(all(c.replayed for c in second.chunks))
        self.assertEqual([c.mapping_summary for c in result.chunks], [c.mapping_summary for c in second.chunks])
        for principal in [replace(self.principal, tenant_id="other-tenant"),
                          replace(self.principal, groups=frozenset({"unauthorized"}))]:
            with self.assertRaises((ConstructionAuthorizationError, ConstructionConflict)):
                self.workflow.run(principal, payload, metadata)
        self.assertEqual(len(calls.calls), 1)
        print(json.dumps({"structured_fixture_seconds": round(time.monotonic()-started, 3),
                          "chunks": len(result.chunks), "model_calls": len(calls.calls)}), flush=True)

    def test_complete_authorized_topology_with_recorded_mapping(self):
        # Default to a reviewed deterministic fixture; optionally replay a real
        # provider mapping. Both write exclusively to this disposable database.
        plan_path = os.getenv("STRUCTURED_MAPPING_PLAN_PATH")
        from tests.unit.test_ontology_source import tbox
        selected = tbox(tenant_id=self.tenant_id)
        store = Neo4jTBoxStore(self.driver, self.database)
        draft = replace(selected, status=TBoxStatus.DRAFT)
        store.import_version(draft)
        published = store.publish(self.tenant_id, draft.tbox_id, expected_active_tbox_id=None)
        plan = json.loads(json.loads(Path(plan_path).read_text())["response"]) if plan_path else topology_plan()
        model, calls = base(plan, published)
        self.workflow.extractor_factory = lambda _: model
        self.workflow.document_extractor_selector = select_structured_extractor
        self.workflow.parser = StructuredDocumentParser()
        self.workflow.config = replace(self.workflow.config, max_chunks=128, max_model_calls=128,
                                       deadline_seconds=4200, max_total_extraction_chars=262144)
        live_embeddings = os.getenv("STRUCTURED_MAPPING_LIVE_EMBEDDINGS") == "1"
        if live_embeddings:
            from dotenv import load_dotenv
            from openai import OpenAI
            from scripts.run_playground import _OpenAICompatibleEmbedder
            from graphrag_prod.ingestion.pipeline import EmbeddingProfile
            load_dotenv(Path(__file__).resolve().parents[2] / ".env")
            embedder = _OpenAICompatibleEmbedder(
                client=OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"],
                              max_retries=0, timeout=30), provider="dashscope", model=os.environ["EMBEDDING_MODEL"],
                revision="structured-benchmark:v1", dimensions=int(os.environ["EMBEDDING_DIMENSIONS"]))
            self.workflow.embedding_provider = embedder
            self.workflow.embedding_profile = EmbeddingProfile(embedder.provider, embedder.model, embedder.revision,
                                                               embedder.dimensions, embedder.normalization)
        previous = self.workflow
        self.workflow = type(previous)(driver=self.driver, database=self.database,
            pipeline=previous.pipeline, embedding_provider=previous.embedding_provider,
            embedding_profile=previous.embedding_profile, extractor_factory=previous.extractor_factory,
            config=replace(previous.config, max_concurrency=4), parser=previous.parser,
            document_extractor_selector=select_structured_extractor, clock=previous.clock)
        payload = (Path(__file__).resolve().parents[2] / "busway_files/topology.source.json").read_bytes()
        metadata = replace(self.metadata, mime_type="application/json", tbox_key=published.key,
                           canonical_uri="urn:structured-benchmark:topology", operation_key="full-topology")
        start = time.monotonic()
        result = self.workflow.run(self.principal, payload, metadata)
        elapsed = time.monotonic()-start
        self.assertEqual(len(calls.calls), 1)
        self.assertTrue(all(c.status in {"EMPTY", "CANDIDATE"} for c in result.chunks))
        ids = tuple(x for c in result.chunks for x in c.mention_record_ids)
        assertions = tuple(x for c in result.chunks for x in c.assertion_record_ids)
        self.assertGreater(len(assertions), 500)
        rows, _, _ = self.driver.execute_query(
            "MATCH (n:GovernedEntityMentionRevision) WHERE n.record_id IN $ids RETURN count(DISTINCT n.entity_id) AS entities",
            ids=list(ids), database_=self.database)
        self.assertEqual(rows[0]["entities"], 139)
        report = {"database_seconds": round(elapsed, 3), "chunks": len(result.chunks),
                  "entities": 139, "mentions": len(ids), "assertions": len(assertions),
                  "mapping_provider": "recorded real mapping, deterministic replay",
                  "live_embeddings": live_embeddings, "rejected_chunks": 0}
        output = os.getenv("STRUCTURED_MAPPING_BENCHMARK_OUTPUT")
        if output:
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
