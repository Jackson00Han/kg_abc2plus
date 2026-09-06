"""Independent source binding, hand-computable metrics and capture tamper checks."""

from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime
import json
import importlib.util
import io
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from graphrag_prod.domain.ids import chunk_id, content_checksum, document_id, version_id
from graphrag_prod.industrial.contract import load_industrial_contract
from graphrag_prod.industrial.evaluation import (
    BASELINE_COMMIT, RERANK_VARIANT, VARIANTS, IndustrialEvaluationError, assess_acceptance, build_report, cached_query_vector,
    capture_index_pins,
    corpus_bindings, evaluate_captures, json_safe, limits_for, pdf_evaluation_inputs, prepare_requests,
    query_cache_identity, read_json, run_retrieval_worker, validate_report, validate_source_snapshot,
    write_immutable_json,
)
from graphrag_prod.industrial.loading import _prepare_industrial_load
from graphrag_prod.industrial.provenance import canonical_json, digest
from tests.fixtures.industrial_loading import NOW, PROFILE, tiny_corpus


ROOT = Path(__file__).resolve().parents[2]


def fixture():
    corpus = tiny_corpus()
    plan = _prepare_industrial_load(corpus, PROFILE, ingested_at=NOW)
    snapshot = {"documents": [], "chunks": []}
    for source in plan.sources:
        document, version, chunks = source.request.domain_inputs()
        metadata = json.loads(source.provenance_json)
        version_row = json_safe(version)
        version_row.update(industrial_provenance_json=source.provenance_json,
                           industrial_provenance_checksum=content_checksum(source.provenance_json))
        for key in ("family", "contract_family", "asset_keys", "source_kind", "source_key"):
            version_row[f"industrial_{key}"] = metadata[key]
        snapshot["documents"].append({"document": json_safe(document), "version": version_row,
            "snapshot": {"snapshot_id": "fixture-snapshot-" + source.key, "tenant_id": corpus.tenant_id,
                "document_id": document.document_id, "version_id": version.version_id, "build_state": "PUBLISHED"}})
        snapshot["chunks"].extend(json_safe(chunk) for chunk in chunks)
    bindings = corpus_bindings(corpus)
    reference = next(key for key, value in bindings.items() if value["source_kind"] == "CURATED_REFERENCE")
    field = next(key for key, value in bindings.items() if value["source_kind"] == "SYNTHETIC_FIELD_RECORD")
    a, b = bindings[reference]["anchor"], bindings[field]["anchor"]
    scope = SimpleNamespace(family="canalis-kt", asset_keys=(), include_family_references=True,
                            published_at_lte=None, origin="USER_SUPPLIED")
    principal = SimpleNamespace(tenant_id=corpus.tenant_id, access_groups=("engineering", "maintenance", "public"))
    cases = (
        SimpleNamespace(case_id="case-supported", question="Find the classification", split="dev", question_class="structure",
            principal=principal, user_scope=scope, answerability="SUPPORTED", relevance={a: 3, b: 1},
            required_evidence_sets=((a,), (b,)), forbidden_sections=()),
        SimpleNamespace(case_id="case-insufficient", question="Give the absent manufacturer threshold", split="holdout", question_class="unanswerable",
            principal=principal, user_scope=scope, answerability="INSUFFICIENT", relevance={b: 3},
            required_evidence_sets=((b,),), forbidden_sections=()),
        SimpleNamespace(case_id="case-denied", question="Show the protected field row", split="dev", question_class="unauthorized",
            principal=SimpleNamespace(tenant_id=corpus.tenant_id, access_groups=("engineering", "public")),
            user_scope=scope, answerability="DENIED", relevance={}, required_evidence_sets=(),
            forbidden_sections=(SimpleNamespace(key=b),)),
    )
    gold = SimpleNamespace(cases=cases, version="fixture-gold-v1", checksum="a" * 64, corpus_checksum=corpus.manifest_checksum)
    code_files = {"src/graphrag_prod/retrieval/engine.py": "b" * 64}
    code = {"commit": BASELINE_COMMIT, "source_files": code_files, "source_checksum": digest(code_files)}
    pins = {"corpus_checksum": corpus.manifest_checksum, "gold_checksum": gold.checksum, "gold_version": gold.version,
        "source_snapshot_checksum": digest(snapshot), "legacy_source": code, "current_source": code,
        "configurations": {name: json_safe(limits_for(name)) for name in VARIANTS},
        "query_vectors": {case.case_id: digest([1.0, 0.0, 0.0, 0.0]) for case in cases},
        "indexes": [{"tenant_id": corpus.tenant_id, "corpus_revision": 4, "generation_id": "generation-1", "embedding_space_id": PROFILE.embedding_space_id}]}
    sources = validate_source_snapshot(snapshot, corpus)

    def result(identifier):
        source = sources[identifier]
        chunk, version, document = (source[key] for key in ("chunk", "version", "document"))
        return {"text": chunk["text"], "role": "anchor", "score": 1.0, "reasons": [], "citation": {
            "chunk_id": identifier, "chunk_checksum": chunk["checksum"], "document_id": document["document_id"],
            "canonical_uri": document["canonical_uri"], "source_name": document["source_name"],
            "version_id": version["version_id"], "version_checksum": version["checksum"], "version_number": 1,
            "ordinal": chunk["ordinal"], "char_start": chunk["char_start"], "char_end": chunk["char_end"],
            "page_number": None, "section": chunk["section"], "document_title": document["title"], "published_at": version["published_at"]}}
    captures = []
    for variant in VARIANTS:
        for case, ranking in zip(cases, ([field], [reference, field], [reference]), strict=True):
            chunks = [result(identifier) for identifier in ranking]
            trace = {"tenant_id": corpus.tenant_id, "corpus_revision": 4, "embedding_generation_id": "generation-1",
                "embedding_space_id": PROFILE.embedding_space_id, "selected_chunk_ids": ranking,
                "limits": json_safe(limits_for(variant)), "context_chars": sum(len(chunk["text"]) for chunk in chunks)}
            captures.append({"case_id": case.case_id, "variant": variant, "query_checksum": content_checksum(case.question),
                "query_vector_checksum": pins["query_vectors"][case.case_id], "implementation_checksum": "b" * 64,
                "chunks": chunks, "trace": trace, "error": None, "duration_ms": 2.0})
    return corpus, gold, captures, snapshot, pins, reference, field


class IndustrialEvaluationTests(unittest.TestCase):
    def test_listwise_cache_preserves_raw_duplicate_normalization_and_actual_usage(self):
        from graphrag_prod.retrieval.reranking import RerankCandidate
        from graphrag_prod.retrieval.listwise_provider import LISTWISE_PROFILE, decode_listwise_response
        from graphrag_prod.retrieval.rerank_provider import RerankProviderError, provider_configuration, rerank_cache_identity, validate_cached_response
        spec = importlib.util.spec_from_file_location("industrial_listwise_worker_test", ROOT / "scripts/industrial_current_retrieval_worker.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidates = tuple(RerankCandidate(f"chunk-{index}", text, content_checksum(text), source_title="Record", source_section="observation")
                           for index, text in enumerate(("First source.", "Second source.")))
        raw = json.dumps({"object": "chat.completion", "model": provider_configuration(profile=LISTWISE_PROFILE)["model"],
            "id": "fixture-listwise", "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ranking":[1,0,1]}'}}]}).encode()
        response = decode_listwise_response(raw, "question", candidates, duration_ms=3, profile=LISTWISE_PROFILE)
        provider = SimpleNamespace(rerank_with_raw=lambda query, items: (response, raw))
        with tempfile.TemporaryDirectory() as directory:
            def cache(live=None):
                return module.CachedReranker(directory, live, profile=LISTWISE_PROFILE,
                    identity_builder=rerank_cache_identity, response_validator=validate_cached_response)
            live = cache(provider)
            actual = live.rerank("question", candidates)
            self.assertEqual(actual.raw_permutation, (1, 0, 1))
            self.assertEqual(actual.normalization_removed_count, 1)
            self.assertTrue(all(item.score is None for item in actual.scores))
            self.assertEqual(live.raw_response_json, raw.decode())
            self.assertEqual(live.usage["provider_reported_prompt_tokens"], 20)
            self.assertEqual(live.usage["provider_reported_completion_tokens"], 5)
            self.assertEqual(live.usage["provider_reported_total_tokens"], 25)
            replay = cache()
            self.assertEqual(replay.rerank("question", candidates), actual)
            self.assertEqual(replay.raw_response_json, raw.decode())
            self.assertEqual(replay.usage["provider_reported_total_tokens"], 0)
            path = next(Path(directory).glob("*.json"))
            payload = module._read_cache(path)
            payload.pop("raw_response_json")
            payload["cache_checksum"] = module._digest({key: value for key, value in payload.items() if key != "cache_checksum"})
            path.write_text(json.dumps(payload))
            with self.assertRaises(RerankProviderError):
                cache().rerank("question", candidates)

    def test_rerank_cache_pins_profile_full_order_rendered_context_and_response(self):
        from graphrag_prod.retrieval.reranking import RerankCandidate
        from graphrag_prod.retrieval.rerank_provider import (
            CONTEXTUAL_PROFILE, DEFAULT_PROFILE, RERANK_MODEL, RerankProviderError, decode_response,
            rerank_cache_identity, validate_cached_response,
        )
        spec = importlib.util.spec_from_file_location("industrial_current_worker_test", ROOT / "scripts/industrial_current_retrieval_worker.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidates = tuple(RerankCandidate(f"chunk-{index}", text, content_checksum(text),
            source_title="Service record", source_section="inspection", document_id="document", version_id="version")
            for index, text in enumerate(("Recorded condition.", "Missing measurement.")))
        payload = json.dumps({"object": "list", "model": RERANK_MODEL, "id": "fixture-request", "usage": {"total_tokens": 17},
                              "results": [{"index": 1, "relevance_score": .8}, {"index": 0, "relevance_score": .2}]}).encode()
        response = decode_response(payload, "question", candidates, duration_ms=3, profile=CONTEXTUAL_PROFILE)
        calls = []
        provider = SimpleNamespace(rerank=lambda query, items: calls.append(query) or response)
        with tempfile.TemporaryDirectory() as directory:
            def cache(profile=CONTEXTUAL_PROFILE, live=None):
                return module.CachedReranker(directory, live, profile=profile,
                    identity_builder=rerank_cache_identity, response_validator=validate_cached_response)
            live = cache(live=provider)
            self.assertEqual(live.rerank("question", candidates), response)
            self.assertEqual(live.usage["provider_reported_total_tokens"], 17)
            replay = cache()
            self.assertEqual(replay.rerank("question", candidates), response)
            self.assertEqual(replay.usage["cache_hits"], 1)
            self.assertEqual(replay.usage["provider_reported_total_tokens"], 0)
            self.assertEqual(calls, ["question"])
            for query, changed, profile in (
                ("another question", candidates, CONTEXTUAL_PROFILE),
                ("question", candidates[::-1], CONTEXTUAL_PROFILE),
                ("question", candidates, DEFAULT_PROFILE),
                ("question", (replace(candidates[0], source_title="Different record"), candidates[1]), CONTEXTUAL_PROFILE),
                ("question", (replace(candidates[0], text="Changed source.", checksum=content_checksum("Changed source.")), candidates[1]), CONTEXTUAL_PROFILE),
            ):
                with self.assertRaisesRegex(ValueError, "missing rerank cache"):
                    cache(profile).rerank(query, changed)
            path = next(Path(directory).glob("*.json"))
            tampered = module._read_cache(path)
            tampered["response"]["scores"][0]["score"] = .9
            tampered["cache_checksum"] = module._digest({key: value for key, value in tampered.items() if key != "cache_checksum"})
            path.write_text(json.dumps(tampered))
            with self.assertRaises(RerankProviderError):
                cache().rerank("question", candidates)
            self.assertEqual(calls, ["question"])

    def test_v2_dev_scope_keeps_every_dev_negative_and_rejects_holdout_claim(self):
        from graphrag_prod.retrieval.reranking import RerankCandidate
        from graphrag_prod.retrieval.listwise_provider import decode_listwise_response
        from graphrag_prod.retrieval.rerank_provider import provider_configuration, rerank_cache_identity
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        scope = {"split": "dev", "variants": [RERANK_VARIANT]}
        selected_ids = {case.case_id for case in gold.cases if case.split == "dev"}
        captures = [row for row in captures if row["variant"] == "scoped_ranked" and row["case_id"] in selected_ids]
        selected_profile = "listwise-evidence-v1"
        pins["reranker"] = provider_configuration(profile=selected_profile)
        for row in captures:
            row["variant"] = RERANK_VARIANT
            question = next(case.question for case in gold.cases if case.case_id == row["case_id"])
            candidates = tuple(RerankCandidate(chunk["citation"]["chunk_id"], chunk["text"], chunk["citation"]["chunk_checksum"],
                source_title=chunk["citation"]["document_title"], source_section=chunk["citation"]["section"],
                document_id=chunk["citation"]["document_id"], version_id=chunk["citation"]["version_id"]) for chunk in row["chunks"])
            raw_provider = json.dumps({"object": "chat.completion", "model": pins["reranker"]["model"], "id": "fixture-request",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                    "content": json.dumps({"ranking": list(range(len(candidates)))})}}]})
            response = decode_listwise_response(raw_provider.encode(), question, candidates, duration_ms=2, profile=selected_profile)
            response = json_safe(response)
            identity = rerank_cache_identity(question, candidates, profile=selected_profile)
            row["reranker"] = pins["reranker"]
            row["rerank_provider_raw"] = raw_provider
            row["rerank_usage"] = {"provider_calls": 1, "cache_hits": 0, "provider_reported_total_tokens": 12,
                "provider_reported_prompt_tokens": 10, "provider_reported_completion_tokens": 2,
                "calls_without_reported_tokens": 0, "calls_without_separate_token_usage": 0}
            row["rerank_cache_events"] = [{"identity": identity, "cache_key": digest(identity), "cache_hit": False,
                "cache_checksum": digest({"identity": identity, "response": response, "raw_response_json": raw_provider})}]
            row["trace"]["reranking"] = {"status": "RERANKED", "candidate_limit": 50,
                "candidate_chunk_ids": [item.chunk_id for item in candidates], "ranked_chunk_ids": [item.chunk_id for item in candidates], "response": response}
        pins["configurations"] = {RERANK_VARIANT: json_safe(limits_for(RERANK_VARIANT))}
        pins["rerank_usage"] = {"provider_calls": len(captures), "cache_hits": 0,
            "provider_reported_total_tokens": 12 * len(captures), "provider_reported_prompt_tokens": 10 * len(captures),
            "provider_reported_completion_tokens": 2 * len(captures), "calls_without_reported_tokens": 0,
            "calls_without_separate_token_usage": 0, "estimated_cost_usd": None}
        report = build_report(gold, corpus, captures, snapshot, pins, capture_scope=scope)
        result = validate_report(report, gold, corpus)
        self.assertEqual(result["capture_completeness"], "PARTIAL_DEV_ONLY")
        self.assertEqual(result["variants"][RERANK_VARIANT]["dev"]["answerability_counts"]["DENIED"], 1)
        with self.assertRaisesRegex(IndustrialEvaluationError, "dev-only"):
            assess_acceptance(result, load_industrial_contract(ROOT / "contracts/industrial_knowledge.v1.json"),
                selected_variant=RERANK_VARIANT, split="all", report_checksum=report["report_checksum"])
        with self.assertRaises(IndustrialEvaluationError):
            build_report(gold, corpus, captures[:-1], snapshot, pins, capture_scope=scope)
        changed = deepcopy(report)
        changed["capture_scope"]["split"] = "all"
        changed["report_checksum"] = digest({key: value for key, value in changed.items() if key != "report_checksum"})
        with self.assertRaisesRegex(IndustrialEvaluationError, "omits"):
            validate_report(changed, gold, corpus)

    def test_cli_dev_summary_does_not_display_holdout_and_failed_quality_exits_nonzero(self):
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        report = build_report(gold, corpus, captures, snapshot, pins)
        spec = importlib.util.spec_from_file_location("industrial_eval_cli_test", ROOT / "scripts/evaluate_industrial_retrieval.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            report_path, assessment_path = Path(directory) / "capture.json", Path(directory) / "assessment.json"
            write_immutable_json(report_path, report)
            output = io.StringIO()
            with patch.object(module, "build_corpus", return_value=corpus), patch.object(module, "load_gold", return_value=gold), redirect_stdout(output):
                code = module.main(["--validate-report", str(report_path), "--selected-variant", "scoped_ranked",
                                    "--split", "dev", "--assessment-output", str(assessment_path)])
            summary = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(summary["quality_acceptance"]["status"], "FAILED")
            self.assertEqual(summary["official_pdf_integration"]["status"], "NOT_DISPLAYED")
            self.assertTrue(all(set(item) == {"dev"} for item in summary["variants"].values()))
            self.assertEqual(read_json(assessment_path), summary["quality_acceptance"])

    def test_acceptance_checks_contract_thresholds_and_keeps_holdout_hidden(self):
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        evaluation = evaluate_captures(gold, corpus, captures, snapshot, pins)
        contract = load_industrial_contract(ROOT / "contracts/industrial_knowledge.v1.json")
        # Holdout is deliberately inaccessible to the dev-only assessor.
        evaluation["variants"]["scoped_ranked"].pop("holdout")
        evaluation["variants"]["scoped_ranked"].pop("all")
        failed = assess_acceptance(evaluation, contract, selected_variant="scoped_ranked", split="dev", report_checksum="a" * 64)
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(failed["holdout_acceptance_assessed"])
        self.assertEqual({item["metric"]: item["target"] for item in failed["checks"] if item["metric"] in {"recall_at_5", "mrr", "ndcg_at_5"}},
                         {"recall_at_5": .85, "mrr": .8, "ndcg_at_5": .8})
        total = evaluation["variants"]["scoped_ranked"]["dev"]
        total.update(recall_at_5=.85, mrr=.8, ndcg_at_5=.8)
        passed = assess_acceptance(evaluation, contract, selected_variant="scoped_ranked", split="dev", report_checksum="a" * 64)
        self.assertEqual(passed["status"], "PASSED")
        self.assertEqual(passed["graph_bound_validation"], "NOT_RUN_I4")
        total["current_acl_exposure_count"] = 1
        self.assertEqual(assess_acceptance(evaluation, contract, selected_variant="scoped_ranked", split="dev", report_checksum="a" * 64)["status"], "FAILED")

    def test_worker_deadline_includes_blocked_stdin_and_preserves_remaining_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "nonreader.py"
            worker.write_text("import time\ntime.sleep(5)\n")
            requests = [{"case_id": f"case-{index}", "variant": "legacy_default", "query_checksum": "a" * 64,
                         "query_vector_checksum": "b" * 64, "padding": "x" * (256 * 1024)} for index in range(2)]
            started = time.monotonic()
            rows = run_retrieval_worker(requests, source_root=ROOT, worker_path=worker, deadline_seconds=.1)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual([row["case_id"] for row in rows], ["case-0", "case-1"])
            self.assertEqual([row["error"] for row in rows], ["WorkerDeadlineExceeded"] * 2)

    def test_worker_writes_complete_large_requests_and_reads_partial_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "reader.py"
            worker.write_text("import json, sys\nfor line in sys.stdin:\n"
                " value = json.loads(line)\n"
                " assert len(value['padding']) == 256 * 1024\n"
                " result = dict(case_id=value['case_id'], variant=value['variant'], chunks=[], error=None)\n"
                " encoded = json.dumps(result) + '\\n'\n"
                " for offset in range(0, len(encoded), 7):\n"
                "  sys.stdout.write(encoded[offset:offset+7]); sys.stdout.flush()\n")
            requests = [{"case_id": f"case-{index}", "variant": "legacy_default", "query_checksum": "a" * 64,
                         "query_vector_checksum": "b" * 64, "padding": "x" * (256 * 1024)} for index in range(2)]
            rows = run_retrieval_worker(requests, source_root=ROOT, worker_path=worker, deadline_seconds=3)
            self.assertEqual([row["error"] for row in rows], [None, None])

    def test_publication_activation_pin_detects_aba_and_inactive_publication(self):
        row = {"tenant_id": "tenant", "corpus_revision": 1, "generation_id": "generation",
               "generation_state": "ACTIVE", "generation_corpus_revision": 1,
               "generation_tenant_id": "tenant", "publication_id": "publication", "publication_tenant_id": "tenant",
               "publication_status": "ACTIVE", "knowledge_activation_generation": 2}
        driver = SimpleNamespace(execute_query=lambda *args, **kwargs: ([row], None, None))
        first = capture_index_pins(driver, "neo4j", ["tenant"])
        row["knowledge_activation_generation"] = 4
        self.assertNotEqual(first, capture_index_pins(driver, "neo4j", ["tenant"]))
        row["publication_status"] = "RETIRED"
        with self.assertRaises(IndustrialEvaluationError):
            capture_index_pins(driver, "neo4j", ["tenant"])
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        pins["indexes"][0].update(publication_id="publication", knowledge_activation_generation=4)
        for capture in captures:
            if capture["variant"] != "legacy_default":
                capture["trace"].update(knowledge_publication_id="publication", knowledge_activation_generation=4)
        evaluate_captures(gold, corpus, captures, snapshot, pins)
        captures[-1]["trace"]["knowledge_activation_generation"] = 2
        with self.assertRaisesRegex(IndustrialEvaluationError, "publication activation"):
            evaluate_captures(gold, corpus, captures, snapshot, pins)

    def test_pdf_evidence_uses_independent_original_edition_page_and_exact_ranges(self):
        corpus, _, _, snapshot, _, _, _ = fixture()
        parts = ["Possible causes: one condition.\n", "Footnote: applicability must be checked.\n"]
        text = "".join(parts)
        normalized = content_checksum(text)
        original = content_checksum(b"bounded-original-fixture")
        uri = "industrial://industrial-schneider-demo/fixture/official-pdf"
        did = document_id(corpus.tenant_id, uri)
        vid = version_id(did, normalized, original)
        policy = "industrial-acl:industrial-schneider-demo:public"
        metadata = {"schema_version": "industrial-source-provenance-v1", "tenant_id": corpus.tenant_id,
            "document_id": did, "version_id": vid, "original_checksum": original, "normalized_checksum": normalized,
            "access_policy_id": policy, "access_policy_version": 1, "access_groups": ["public"],
            "family": "canalis-kt", "contract_family": "CANALIS_KT", "asset_keys": [],
            "source_kind": "OFFICIAL_PUBLICATION", "source_key": "official-pdf-fixture",
            "source": {"source_id": "official-pdf-fixture", "embedded_revision": "fixture-edition"},
            "artifact_checksum": "d" * 64, "parser_version": "fixture-pdf-v1", "selected_pages": [1, 2],
            "source_locations": []}
        required = []
        offset = 0
        for ordinal, part in enumerate(parts):
            end = offset + len(part)
            metadata["source_locations"].append({"char_start": offset, "char_end": end, "page_number": ordinal + 1,
                                                  "kind": "page", "bbox": [0.0, 0.0, 100.0, 100.0]})
            snapshot["chunks"].append({"chunk_id": chunk_id(vid, "fixture-pdf-splitter-v1", ordinal, offset, end, content_checksum(part)),
                "document_id": did, "version_id": vid, "tenant_id": corpus.tenant_id,
                "access_policy_id": policy, "access_policy_version": 1, "access_groups": ["public"],
                "ordinal": ordinal, "text": part, "checksum": content_checksum(part), "char_start": offset,
                "char_end": end, "page_number": ordinal + 1, "section": None, "splitter_version": "fixture-pdf-splitter-v1"})
            required.append({"ordinal": ordinal, "char_start": offset, "char_end": end,
                             "page_number": ordinal + 1, "text_checksum": content_checksum(part)})
            offset = end
        encoded = canonical_json(metadata)
        snapshot["documents"].append({"document": {"document_id": did, "tenant_id": corpus.tenant_id,
                "canonical_uri": uri, "source_name": "OFFICIAL_PUBLICATION", "title": "PDF fixture", "access_policy_id": policy,
                "access_policy_version": 1, "access_groups": ["public"]},
            "version": {"version_id": vid, "document_id": did, "tenant_id": corpus.tenant_id, "checksum": normalized,
                "original_checksum": original, "normalized_text": text, "published_at": NOW.isoformat(), "version_number": 1,
                "industrial_provenance_json": encoded, "industrial_provenance_checksum": content_checksum(encoded),
                **{f"industrial_{key}": metadata[key] for key in ("family", "contract_family", "asset_keys", "source_kind", "source_key")}},
            "snapshot": {"snapshot_id": "pdf-fixture-snapshot", "document_id": did, "version_id": vid,
                         "tenant_id": corpus.tenant_id, "build_state": "PUBLISHED"}})
        raw = {"case_id": "pdf-case", "question": "Which conditions and footnotes are required?", "source_id": "official-pdf-fixture",
            "family": "canalis-kt", "source_kind": "OFFICIAL_PUBLICATION", "embedded_revision": "fixture-edition",
            "original_checksum": original, "normalized_checksum": normalized, "artifact_checksum": "d" * 64,
            "parser_version": "fixture-pdf-v1", "splitter_signature": "fixture-pdf-splitter-v1", "physical_pages": [1, 2],
            "required_chunks": required, "required_evidence_sets": [[0, 1]], "answerability": "SUPPORTED"}
        manifest = {"schema_version": "industrial-pdf-integration-gold-v1", "version": "pdf-fixture-v1",
                    "included_in_primary_gold_metrics": False, "cases": [raw]}
        manifest["checksum"] = digest(manifest)
        gold, bindings = pdf_evaluation_inputs(manifest, corpus, snapshot)
        self.assertEqual(len(bindings), 2)
        self.assertEqual(len(gold.cases[0].required_evidence_sets[0]), 2)
        self.assertEqual(gold.cases[0].user_scope.source_kinds, ("OFFICIAL_PUBLICATION",))
        for mutate in (
            lambda item: item.update(original_checksum="e" * 64),
            lambda item: item.update(embedded_revision="other-edition"),
            lambda item: item["required_chunks"][0].update(page_number=2),
            lambda item: item["required_chunks"][0].update(char_end=2),
        ):
            changed = deepcopy(manifest)
            mutate(changed["cases"][0])
            changed["checksum"] = digest({key: value for key, value in changed.items() if key != "checksum"})
            with self.assertRaises(IndustrialEvaluationError):
                pdf_evaluation_inputs(changed, corpus, snapshot)

    def test_standard_ranking_and_alternative_completeness_are_distinct(self):
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        result = evaluate_captures(gold, corpus, captures, snapshot, pins)
        actual = result["variants"]["scoped_default"]
        self.assertEqual(actual["all"]["case_count"], 3)
        self.assertEqual(actual["all"]["positive_target_count"], 2)
        self.assertEqual(actual["all"]["recall_at_5"], 0.75)
        self.assertEqual(actual["all"]["mrr"], 0.75)
        self.assertAlmostEqual(actual["all"]["ndcg_at_5"], (1 / (7 + 1 / math.log2(3)) + 1 / math.log2(3)) / 2)
        self.assertEqual(actual["all"]["complete_evidence_set_rate"], 1.0)
        self.assertEqual(actual["all"]["current_acl_exposure_count"], 0)
        self.assertIsNone(actual["cases"][2]["metrics"])
        self.assertEqual(actual["holdout"]["answerability_counts"]["INSUFFICIENT"], 1)
        self.assertFalse(result["runtime_answer_and_refusal_evaluated"])
        self.assertEqual(result["official_pdf_integration"]["status"], "NOT_RUN")

    def test_omitted_negative_different_vectors_and_code_pins_fail(self):
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        for mutate in (
            lambda rows, pin: rows.pop(),
            lambda rows, pin: rows[0].update(query_vector_checksum="c" * 64),
            lambda rows, pin: rows[0].update(implementation_checksum="c" * 64),
            lambda rows, pin: pin.update(gold_checksum="c" * 64),
        ):
            rows, pin = deepcopy(captures), deepcopy(pins)
            mutate(rows, pin)
            with self.assertRaises(IndustrialEvaluationError):
                evaluate_captures(gold, corpus, rows, snapshot, pin)

    def test_full_trace_acl_and_user_scope_are_checked_beyond_gold_sentinels(self):
        corpus, gold, captures, snapshot, pins, _, field = fixture()
        captures[2]["trace"]["vector_recall"] = [{"chunk_id": field, "rank": 1, "score": 0.9}]
        result = evaluate_captures(gold, corpus, captures, snapshot, pins)
        self.assertEqual(result["variants"]["legacy_default"]["all"]["current_acl_exposure_count"], 1)
        self.assertEqual(result["variants"]["legacy_default"]["all"]["forbidden_sentinel_exposure_count"], 1)
        gold.cases[0].user_scope = SimpleNamespace(family="evopact-hvx-up24", asset_keys=("unknown-asset",),
                                                  include_family_references=False, published_at_lte="2020-01-01T00:00:00+00:00")
        result = evaluate_captures(gold, corpus, captures, snapshot, pins)
        reasons = {row["reason"] for row in result["variants"]["scoped_default"]["cases"][0]["scope_exclusions_violated"]}
        self.assertEqual(reasons, {"product_family", "primary_asset", "publication_cutoff"})

    def test_current_source_text_acl_and_provenance_facets_fail_closed(self):
        corpus, _, _, snapshot, _, _, _ = fixture()
        for mutate in (
            lambda value: value["chunks"][0].update(text="invented sample"),
            lambda value: value["chunks"][0].update(access_groups=["public"]),
            lambda value: value["documents"][0]["version"].update(industrial_family="evopact-hvx-up24"),
            lambda value: value["chunks"][0].update(char_start=False),
        ):
            changed = deepcopy(snapshot)
            mutate(changed)
            with self.assertRaises(IndustrialEvaluationError):
                validate_source_snapshot(changed, corpus)

    def test_citations_and_report_metrics_are_recomputed_from_exact_source(self):
        corpus, gold, captures, snapshot, pins, _, _ = fixture()
        captures[0]["chunks"][0]["text"] = "altered"
        captures[0]["trace"]["context_chars"] = 7
        result = evaluate_captures(gold, corpus, captures, snapshot, pins)
        self.assertEqual(result["variants"]["legacy_default"]["all"]["citation_error_count"], 1)
        report = build_report(gold, corpus, captures, snapshot, pins)
        self.assertEqual(validate_report(report, gold, corpus), report["evaluation"])
        report["evaluation"]["variants"]["legacy_default"]["all"]["citation_error_count"] = 0
        report["report_checksum"] = digest({key: value for key, value in report.items() if key != "report_checksum"})
        with self.assertRaisesRegex(IndustrialEvaluationError, "metrics differ"):
            validate_report(report, gold, corpus)
        with self.assertRaisesRegex(IndustrialEvaluationError, "independently supplied"):
            validate_report(report, gold, corpus, expected_pins={})

    def test_vector_cache_requires_exact_model_query_checksum_dimensions_and_values(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            calls = []
            provider = lambda query: calls.append(query) or [1, 0, 0, 0]
            first, created = cached_query_vector(cache, PROFILE, "query", provider=provider)
            self.assertTrue(created)
            self.assertEqual(cached_query_vector(cache, PROFILE, "query"), (first, False))
            self.assertEqual(calls, ["query"])
            with self.assertRaisesRegex(IndustrialEvaluationError, "missing"):
                cached_query_vector(cache, replace(PROFILE, model="other-model"), "query")
            path = cache / (digest(query_cache_identity(PROFILE, "query")) + ".json")
            payload = read_json(path)
            payload["vector"][0] = True
            payload["vector_checksum"] = digest(payload["vector"])
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(IndustrialEvaluationError, "invalid values"):
                cached_query_vector(cache, PROFILE, "query")

    def test_baseline_preparation_excludes_gold_labels_and_industrial_scope(self):
        _, gold, _, _, _, _, _ = fixture()
        requests, traces = prepare_requests(gold.cases, PROFILE, {case.case_id: [1, 0, 0, 0] for case in gold.cases}, variant="legacy_default")
        encoded = json.dumps(requests)
        for forbidden in ("relevance", "answerability", "required_evidence", "forbidden_sections", "asset_keys", "family"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(traces[gold.cases[0].case_id]["scope_capability"], "UNSUPPORTED_BY_BASELINE")
        self.assertEqual(requests[0]["limits"]["anchor_k"], 3)
        self.assertEqual(limits_for("scoped_ranked").anchor_k, 5)
        self.assertEqual(limits_for("scoped_ranked").adjacent_window, 1)

    def test_bounded_json_immutable_writes_and_worker_module_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_immutable_json(path, {"a": 1})
            write_immutable_json(path, {"a": 1})
            with self.assertRaises(IndustrialEvaluationError):
                write_immutable_json(path, {"a": 2})
            with self.assertRaises(IndustrialEvaluationError):
                read_json(path, max_bytes=2)
            path.write_text('{"a":1,"a":2}')
            with self.assertRaises(IndustrialEvaluationError):
                read_json(path)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/industrial_legacy_retrieval_worker.py"),
            "--source-root", str(ROOT), "--check-import"], capture_output=True, text=True, timeout=15, check=True)
        output = json.loads(result.stdout)
        self.assertFalse(output["industrial_modules_imported"])
        self.assertEqual(output["implementation_checksum"], content_checksum((ROOT / "src/graphrag_prod/retrieval/engine.py").read_bytes()))


if __name__ == "__main__":
    unittest.main()
