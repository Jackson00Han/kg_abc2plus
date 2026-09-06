"""Industrial opt-in tenancy, server upload context and replay boundaries."""
from dataclasses import replace
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError
from graphrag_prod.api.contracts import IndustrialScopeRequest, RetrievalRequest
from graphrag_prod.api.knowledge_contracts import IndustrialConstructionContextRequest
from graphrag_prod.construction import ConstructionAuthorizationError, ConstructionConflict
from graphrag_prod.domain import Principal
from graphrag_prod.industrial.construction import (
    INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY, IndustrialUploadContext,
    IndustrialUploadRejected, IndustrialUploadUnavailable, Neo4jIndustrialUploadPolicy,
)
from graphrag_prod.industrial.provenance import IndustrialProvenanceConflict, Neo4jIndustrialProvenanceStore
from graphrag_prod.industrial.retrieval import IndustrialScope
from graphrag_prod.playground import PlaygroundCatalog
from graphrag_prod.playground.industrial_runtime import TenantQueryOperations, INDUSTRIAL_RETRIEVAL_LIMITS
from tests.fixtures.dev_corpus import load_dev_corpus_fixture
from tests.unit.test_construction_workflow import _Extractor, _metadata, _tbox, _workflow, SOURCE


PRINCIPAL = Principal("maintainer", INDUSTRIAL_TENANT, frozenset({"maintenance", "public"}),
                      frozenset({"knowledge:construct", "retrieval:read"}))


def metadata(**updates):
    return replace(_metadata(), canonical_uri=f"industrial-upload://{INDUSTRIAL_TENANT}/field-1",
        tbox_key=INDUSTRIAL_TBOX_KEY, access_groups=frozenset({"maintenance"}),
        industrial_context=IndustrialUploadContext("canalis-kt"), extraction_mode="SOURCE_ONLY", **updates)


class IndustrialRuntimeTests(unittest.TestCase):
    def test_industrial_personas_append_without_changing_legacy_identities(self):
        fixture = load_dev_corpus_fixture()
        legacy = PlaygroundCatalog(fixture, b"x" * 32)
        current = PlaygroundCatalog(fixture, b"x" * 32, enable_industrial=True)
        self.assertEqual(legacy.personas, current.personas[:7])
        self.assertEqual(legacy.questions, current.questions)
        self.assertEqual([p.persona_id for p in current.personas[7:]],
                         ["persona-08", "persona-09", "persona-10", "persona-11"])
        self.assertEqual(current.bootstrap()["industrial"]["default_persona_id"], "persona-11")
        self.assertNotIn("asset-bkt-a01", json.dumps(current.bootstrap()["industrial"]))
        for persona in current.personas[7:]:
            self.assertIn("knowledge:graph:read", persona.scopes)
        self.assertNotIn("knowledge:import", current.personas_by_id["persona-10"].scopes)
        self.assertNotIn("knowledge:publish", current.personas_by_id["persona-10"].scopes)

    def test_authenticated_tenant_routes_to_only_its_engine_and_defaults_scope(self):
        legacy, industrial = Mock(), Mock()
        router = TenantQueryOperations(legacy, industrial)
        request = RetrievalRequest(query_text="test")
        router.retrieve(PRINCIPAL, request)
        passed = industrial.retrieve.call_args.args[1]
        self.assertEqual(passed.industrial_scope, IndustrialScopeRequest())
        self.assertEqual(passed.limits.to_domain(), INDUSTRIAL_RETRIEVAL_LIMITS)
        self.assertIsNone(request.industrial_scope)
        legacy.retrieve.assert_not_called()
        router.retrieve(replace(PRINCIPAL, tenant_id="tenant-alpha"), request)
        self.assertIs(legacy.retrieve.call_args.args[1], request)
        self.assertEqual(industrial.retrieve.call_count, 1)

    def test_runtime_default_configuration_matches_the_locked_i3_profile(self):
        from graphrag_prod.industrial.evaluation import limits_for, RERANK_VARIANT
        self.assertEqual(INDUSTRIAL_RETRIEVAL_LIMITS, limits_for(RERANK_VARIANT))

    def test_industrial_restart_only_reuses_sources_and_disables_full_database_reset(self):
        from scripts import run_playground
        from fastapi.testclient import TestClient
        fixture = load_dev_corpus_fixture()
        embedder = Mock(provider="dashscope", model="model", dimensions=4, embedding_space_id="space")
        with (
            patch.object(run_playground, "_reuse_corpus", return_value=fixture) as reuse,
            patch.object(run_playground, "_load_corpus") as load,
            patch.object(run_playground, "_warm_retrieval") as warm,
            patch.object(run_playground, "capture_reset_embeddings") as capture_reset,
            patch("graphrag_prod.playground.industrial_runtime.verify_existing_industrial_runtime") as verify,
            patch("graphrag_prod.playground.industrial_runtime.build_industrial_query_operations", return_value=Mock()),
        ):
            app = run_playground.build_playground_app(Mock(), "neo4j", signing_key=b"industrial-restart-test-signing-key-32-bytes",
                embedder=embedder, enable_industrial=True, reuse_existing_corpus=True,
                skip_provider_warmup=True, enable_reset=True)
            with TestClient(app) as client:
                bootstrap = client.get("/playground/bootstrap").json()
            self.assertFalse(bootstrap.get("local_reset", {}).get("enabled", False))
            self.assertEqual(bootstrap["enabled_tenants"], [INDUSTRIAL_TENANT, "tenant-alpha", "tenant-beta"])
            self.assertTrue(bootstrap["industrial"]["enabled"])
            reuse.assert_called_once()
            verify.assert_called_once()
            load.assert_not_called()
            warm.assert_not_called()
            capture_reset.assert_not_called()
        with self.assertRaisesRegex(ValueError, "reuse_existing_corpus"):
            run_playground.build_playground_app(Mock(), "neo4j", signing_key=b"industrial-restart-test-signing-key-32-bytes",
                embedder=embedder, enable_industrial=True)

    def test_upload_context_is_strict_and_cannot_select_authority(self):
        valid = IndustrialConstructionContextRequest.model_validate({"family": "canalis-kt", "asset_keys": ["asset-bkt-a01"]})
        self.assertEqual(valid.to_domain(), IndustrialUploadContext("canalis-kt", ("asset-bkt-a01",)))
        for raw in ({"family": "finance"}, {"family": "canalis-kt", "source_kind": "OFFICIAL_PUBLICATION"},
                    {"family": "canalis-kt", "asset_keys": ["asset-x", "asset-x"]},
                    {"family": "canalis-kt", "asset_keys": ["BKT-A01"]}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                IndustrialConstructionContextRequest.model_validate(raw)
        self.assertEqual(IndustrialScopeRequest(source_kinds=("USER_UPLOAD",)).to_domain(),
                         IndustrialScope(source_kinds=("USER_UPLOAD",)))

    def test_reserved_seed_uri_wrong_tbox_and_foreign_tenant_fail_before_resolution(self):
        policy = Neo4jIndustrialUploadPolicy(object())
        policy.resolver = Mock()
        for value in (replace(metadata(), canonical_uri=f"industrial://{INDUSTRIAL_TENANT}/seed"),
                      replace(metadata(), tbox_key="old-demo"), replace(metadata(), industrial_context=None)):
            with self.assertRaises(IndustrialUploadRejected):
                policy.preflight(PRINCIPAL, value)
        with self.assertRaises(IndustrialUploadRejected):
            policy.preflight(replace(PRINCIPAL, tenant_id="tenant-alpha"), metadata())
        policy.resolver.resolve.assert_not_called()

    def test_every_upload_reader_group_must_see_its_asset(self):
        policy = Neo4jIndustrialUploadPolicy(object())
        seen = []
        def resolve(principal, scope):
            seen.append((principal.groups, scope))
            return SimpleNamespace(version_filter=SimpleNamespace(match_none=principal.groups == frozenset({"public"})))
        policy.resolver = SimpleNamespace(resolve=resolve)
        value = replace(metadata(), industrial_context=IndustrialUploadContext("canalis-kt", ("asset-bkt-a01",)),
                        access_groups=frozenset({"maintenance", "public"}))
        with self.assertRaises(IndustrialUploadUnavailable):
            policy.preflight(PRINCIPAL, value)
        self.assertEqual({groups for groups, _ in seen}, {frozenset({"maintenance"}), frozenset({"public"})})
        self.assertTrue(all(scope.source_kinds == ("SYNTHETIC_FIELD_RECORD",) for _, scope in seen))

    def test_workflow_binds_context_and_actor_to_replay_before_providers(self):
        tbox = replace(_tbox(INDUSTRIAL_TENANT), key=INDUSTRIAL_TBOX_KEY)
        extractor = _Extractor(tbox)
        workflow, audit, knowledge, pipeline = _workflow(extractor=extractor)
        workflow.industrial_upload_policy = Mock()
        first = workflow.run(PRINCIPAL, SOURCE, metadata())
        state = audit.jobs[first.job_id]
        self.assertEqual(json.loads(state.industrial_context_json)["source_kind"], "USER_UPLOAD")
        self.assertEqual(json.loads(state.industrial_context_json)["principal_id"], PRINCIPAL.principal_id)
        replay = workflow.run(PRINCIPAL, SOURCE, metadata())
        self.assertEqual(replay.job_id, first.job_id)
        self.assertEqual(extractor.calls, 0)
        for principal, changed in ((PRINCIPAL, replace(metadata(), industrial_context=IndustrialUploadContext("evopact-hvx-up24"))),
                                   (replace(PRINCIPAL, principal_id="another-actor"), metadata())):
            with self.assertRaises(ConstructionConflict):
                workflow.run(principal, SOURCE, changed)
        self.assertEqual(workflow.industrial_upload_policy.persist.call_count, 2)

    def test_unconfigured_industrial_workflow_fails_closed(self):
        workflow, audit, _, _ = _workflow(extractor=_Extractor(_tbox(INDUSTRIAL_TENANT)))
        with self.assertRaises(ConstructionConflict):
            workflow.run(PRINCIPAL, SOURCE, metadata())
        self.assertFalse(audit.jobs)

    def test_missing_provenance_is_not_success_and_can_resume(self):
        tbox = replace(_tbox(INDUSTRIAL_TENANT), key=INDUSTRIAL_TBOX_KEY)
        workflow, audit, _, _ = _workflow(extractor=_Extractor(tbox))
        policy = workflow.industrial_upload_policy = Mock()
        policy.persist.side_effect = [IndustrialProvenanceConflict("interrupted"), None]
        with self.assertRaises(ConstructionConflict):
            workflow.run(PRINCIPAL, SOURCE, metadata())
        self.assertFalse(audit.completed_jobs)
        result = workflow.run(PRINCIPAL, SOURCE, metadata())
        self.assertEqual(audit.completed_jobs, [result.job_id])

    def test_construct_only_provenance_does_not_allow_import_or_forged_context(self):
        driver = Mock()
        store = Neo4jIndustrialProvenanceStore(driver)
        with self.assertRaises(PermissionError):
            store.persist(PRINCIPAL, {})
        with self.assertRaises(IndustrialProvenanceConflict):
            store.persist_user_upload(PRINCIPAL, {"tenant_id": INDUSTRIAL_TENANT, "source_kind": "USER_UPLOAD",
                "construction_context": {"family": "evopact-hvx-up24"}, "family": "canalis-kt"},
                snapshot_id="snapshot", context_json="{}")
        driver.session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
