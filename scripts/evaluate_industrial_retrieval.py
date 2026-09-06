#!/usr/bin/env python3
"""Validate frozen industrial gold/reports or explicitly capture live retrieval."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv

from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.contract import load_industrial_contract
from graphrag_prod.industrial.evaluation import (
    BASELINE_COMMIT, RERANK_USAGE_FIELDS, RERANK_VARIANT, SUPPORTED_VARIANTS, VARIANTS, IndustrialEvaluationError, assess_acceptance, build_report, cached_query_vector,
    capture_index_pins, capture_source_snapshot, json_safe, limits_for, prepare_requests,
    pdf_evaluation_inputs, read_json, require_legacy_checkout, run_retrieval_worker, source_code_pin,
    select_capture_gold, validate_report, validate_source_snapshot, write_immutable_json,
)
from graphrag_prod.industrial.gold import load_gold, load_pdf_gold
from graphrag_prod.industrial.provenance import digest
from graphrag_prod.ingestion.pipeline import EmbeddingProfile


ROOT = Path(__file__).resolve().parents[1]


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise IndustrialEvaluationError(f"required environment setting is missing: {name}")
    return value


def _outside_repository(path: Path, name: str) -> Path:
    result = path.resolve()
    if result.is_relative_to(ROOT):
        raise IndustrialEvaluationError(f"{name} must be outside the repository")
    return result


def _progress(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _live(args: argparse.Namespace, gold, corpus) -> dict:
    import neo4j
    from openai import OpenAI

    if args.output is None or args.vector_cache is None:
        raise IndustrialEvaluationError("--live requires external --output and --vector-cache paths")
    output = _outside_repository(args.output, "live output")
    cache = _outside_repository(args.vector_cache, "query vector cache")
    if output.exists():
        raise IndustrialEvaluationError("live report already exists; choose a new immutable run path")
    capture_scope = {"split": args.capture_split, "variants": [RERANK_VARIANT]} if args.rerank else None
    captured_gold = select_capture_gold(gold, capture_scope) if capture_scope else gold
    variants = (RERANK_VARIANT,) if args.rerank else VARIANTS
    rerank_cache = None
    reranker_config = None
    if args.rerank:
        if args.rerank_cache is None:
            raise IndustrialEvaluationError("reranking requires an explicit external rerank cache")
        rerank_cache = _outside_repository(args.rerank_cache, "rerank cache")
        from graphrag_prod.retrieval.rerank_provider import provider_configuration
        from graphrag_prod.retrieval.rerank_provider import DEFAULT_PROFILE
        reranker_config = provider_configuration(profile=args.rerank_profile or DEFAULT_PROFILE)
    baseline_root = args.baseline_root.resolve()
    legacy_pin = require_legacy_checkout(baseline_root)
    current_pin = source_code_pin(ROOT)
    load_dotenv(ROOT / ".env")
    uri = _required("PLAYGROUND_NEO4J_URI")
    endpoint = urlsplit(uri)
    if endpoint.hostname is None or not ipaddress.ip_address(endpoint.hostname).is_loopback or endpoint.username or endpoint.password:
        raise IndustrialEvaluationError("industrial evaluation requires an explicit loopback Neo4j endpoint")
    base_url = _required("OPENAI_BASE_URL")
    provider_url = urlsplit(base_url)
    if provider_url.scheme != "https" or provider_url.hostname not in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
    } or provider_url.username or provider_url.password or provider_url.query or provider_url.fragment:
        raise IndustrialEvaluationError("live query embedding requires the configured official DashScope HTTPS endpoint")
    profile = EmbeddingProfile("dashscope-openai-compatible", _required("EMBEDDING_MODEL"), "api-v1",
                               int(_required("EMBEDDING_DIMENSIONS")), "provider-default")
    database = os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j")
    if database != "neo4j":
        raise IndustrialEvaluationError("local evaluation requires the configured development neo4j database")
    tenants = sorted({case.principal.tenant_id for case in captured_gold.cases})
    query_usage = {"provider_calls": 0, "cache_hits": 0, "provider_reported_total_tokens": 0,
                   "calls_without_reported_tokens": 0, "estimated_cost_usd": None}
    with neo4j.GraphDatabase.driver(uri, auth=(_required("PLAYGROUND_NEO4J_USER"), _required("PLAYGROUND_NEO4J_PASSWORD")),
            max_connection_pool_size=4, connection_acquisition_timeout=10.0) as driver:
        driver.verify_connectivity()
        indexes = capture_index_pins(driver, database, tenants)
        if any(row["embedding_space_id"] != profile.embedding_space_id or row["dimensions"] != profile.dimensions for row in indexes):
            raise IndustrialEvaluationError("configured query embeddings differ from active tenant vector spaces")
        index_rows, _, _ = driver.execute_query(
            "SHOW FULLTEXT INDEXES YIELD name,state,labelsOrTypes,properties,options "
            "WHERE name='graphrag_chunk_text_v2' RETURN name,state,labelsOrTypes,properties,options", database_=database)
        fulltext = [json_safe(dict(row)) for row in index_rows]
        if len(fulltext) != 1 or fulltext[0]["state"] != "ONLINE":
            raise IndustrialEvaluationError("current fulltext index is not ready")
        snapshot = capture_source_snapshot(driver, database, tenants)
        validate_source_snapshot(snapshot, corpus)
        write_immutable_json(output.with_name(output.stem + ".sources.json"), snapshot)
        if capture_index_pins(driver, database, tenants) != indexes:
            raise IndustrialEvaluationError("corpus changed during source snapshot")
        pdf_gold = None
        pdf_suite = {"status": "NOT_RUN", "reason": "--include-pdf was not requested; no integration pass is claimed."}
        if args.include_pdf:
            from graphrag_prod.industrial.loading import read_normalized_artifact
            from graphrag_prod.industrial.sources import load_source_catalog
            manifest = load_pdf_gold()
            required_files = {item["suggested_artifact_filename"] for item in manifest["cases"]}
            required_files.update(item["source_id"] + ".pdf" for item in manifest["cases"])
            if args.pdf_cache is None or not all((args.pdf_cache / name).is_file() for name in required_files):
                pdf_suite = {"status": "NOT_RUN", "reason": "Required external normalized excerpts or full original PDFs are missing."}
            else:
                pdf_cache = _outside_repository(args.pdf_cache, "PDF cache")
                catalog = load_source_catalog(ROOT / "datasets/industrial-v1/sources.json")
                verified = {}
                for name in sorted({item["suggested_artifact_filename"] for item in manifest["cases"]}):
                    artifact = read_normalized_artifact(pdf_cache / name, catalog, pdf_cache)
                    matching = [item for item in manifest["cases"] if item["source_id"] == artifact["source"]["source_id"]]
                    if not matching or any(item["artifact_checksum"] != artifact["artifact_checksum"] for item in matching):
                        raise IndustrialEvaluationError("PDF cache artifact differs from frozen PDF integration pin")
                    verified[artifact["source"]["source_id"]] = artifact["original_checksum"]
                pdf_gold, _ = pdf_evaluation_inputs(manifest, corpus, snapshot)
                pdf_suite = {"status": "RAN", "manifest": manifest, "original_cache_verification": verified,
                             "captures": [], "query_vectors": {}}
        vectors = {}
        with OpenAI(api_key=_required("OPENAI_API_KEY"), base_url=base_url, timeout=30.0, max_retries=1) as client:
            def embed(query: str):
                query_usage["provider_calls"] += 1
                response = client.embeddings.create(input=[query], model=profile.model,
                    dimensions=profile.dimensions, encoding_format="float")
                if len(response.data) != 1 or response.data[0].index != 0:
                    raise IndustrialEvaluationError("query embedding provider returned an unexpected count")
                tokens = getattr(getattr(response, "usage", None), "total_tokens", None)
                if type(tokens) is int and tokens >= 0:
                    query_usage["provider_reported_total_tokens"] += tokens
                else:
                    query_usage["calls_without_reported_tokens"] += 1
                return response.data[0].embedding
            for case in (*captured_gold.cases, *(pdf_gold.cases if pdf_gold else ())):
                vector, created = cached_query_vector(cache, profile, case.question, provider=embed)
                vectors[case.case_id] = vector
                query_usage["cache_hits"] += int(not created)
                _progress({"phase": "query_embedding", "case_id": case.case_id, "cache_hit": not created})
        captures = []
        suites = [("core", captured_gold.cases)] + ([("pdf", pdf_gold.cases)] if pdf_gold else [])
        for suite_name, cases in suites:
            for variant in variants:
                if capture_index_pins(driver, database, tenants) != indexes:
                    raise IndustrialEvaluationError("corpus changed before comparison variant")
                requests, scope_traces = prepare_requests(cases, profile, vectors, variant=variant, driver=driver,
                                                        database=database, reranker_configuration=reranker_config)
                # Store independently inspectable inputs without relevance/answer labels.
                request_path = output.with_name(output.stem + f".{suite_name}.{variant}.requests.json")
                write_immutable_json(request_path, {"variant": variant, "requests": requests})
                implementation = legacy_pin if variant == "legacy_default" else current_pin
                rows = run_retrieval_worker(requests, source_root=baseline_root if variant == "legacy_default" else ROOT,
                    worker_path=ROOT / "scripts" / ("industrial_current_retrieval_worker.py" if args.rerank else "industrial_legacy_retrieval_worker.py"),
                    progress=_progress, allow_provider_credentials=args.rerank,
                    worker_arguments=("--live-rerank", "--rerank-cache", str(rerank_cache),
                                      "--rerank-profile", reranker_config["profile"]) if args.rerank else ())
                for row in rows:
                    if row["error"] is None and row["implementation_checksum"] != implementation["source_files"]["src/graphrag_prod/retrieval/engine.py"]:
                        raise IndustrialEvaluationError("worker imported a different retrieval implementation")
                    row["scope_trace"] = scope_traces[row["case_id"]]
                write_immutable_json(output.with_name(output.stem + f".{suite_name}.{variant}.captures.json"), {
                    "source_snapshot_checksum": digest(snapshot), "indexes": indexes,
                    "source_checksum": implementation["source_checksum"], "captures": rows})
                if suite_name == "core":
                    captures.extend(rows)
                else:
                    pdf_suite["captures"].extend(rows)
        if pdf_gold:
            pdf_suite["query_vectors"] = {case.case_id: digest(list(vectors[case.case_id])) for case in pdf_gold.cases}
        if capture_index_pins(driver, database, tenants) != indexes:
            raise IndustrialEvaluationError("source generation changed during evaluation")
        final_snapshot = capture_source_snapshot(driver, database, tenants)
        if digest(final_snapshot) != digest(snapshot):
            raise IndustrialEvaluationError("source content or ACL changed during evaluation")
        final_fulltext, _, _ = driver.execute_query(
            "SHOW FULLTEXT INDEXES YIELD name,state,labelsOrTypes,properties,options "
            "WHERE name='graphrag_chunk_text_v2' RETURN name,state,labelsOrTypes,properties,options", database_=database)
        if [json_safe(dict(row)) for row in final_fulltext] != fulltext:
            raise IndustrialEvaluationError("fulltext index configuration changed during evaluation")
    if source_code_pin(ROOT) != current_pin or require_legacy_checkout(baseline_root) != legacy_pin:
        raise IndustrialEvaluationError("retrieval source code changed during evaluation")
    if build_corpus().manifest_checksum != corpus.manifest_checksum or load_gold(args.gold).checksum != gold.checksum:
        raise IndustrialEvaluationError("source corpus or gold annotations changed during evaluation")
    pins = {"corpus_version": corpus.version, "corpus_checksum": corpus.manifest_checksum,
        "gold_version": gold.version, "gold_checksum": gold.checksum,
        "embedding_profile": json_safe(profile), "embedding_space_id": profile.embedding_space_id,
        "provider_host": provider_url.hostname, "query_vectors": {case.case_id: digest(list(vectors[case.case_id])) for case in captured_gold.cases},
        "indexes": indexes, "fulltext_indexes": fulltext, "current_source": current_pin, "legacy_source": legacy_pin,
        "source_snapshot_checksum": digest(snapshot),
        "configurations": {variant: json_safe(limits_for(variant)) for variant in variants},
        "dependencies": {name: importlib.metadata.version(name) for name in ("neo4j", "openai", "pdfplumber")},
        "uv_lock_checksum": content_checksum((ROOT / "uv.lock").read_bytes()),
        "source_catalog_checksum": content_checksum((ROOT / "datasets/industrial-v1/sources.json").read_bytes()),
        "captured_at": datetime.now(UTC).isoformat(), "query_embedding_usage": query_usage,
        "source_embedding_usage": {"provider_calls": None, "provider_reported_total_tokens": None, "estimated_cost_usd": None,
            "status": "NOT_CAPTURED_BY_I2_LOADER", "note": "Source Chunk count is not a provider-call or billing-token measurement."}}
    if args.rerank:
        all_rows = [*captures, *pdf_suite.get("captures", [])]
        pins["reranker"] = reranker_config
        pins["rerank_usage"] = {name: sum(row.get("rerank_usage", {}).get(name, 0) for row in all_rows) for name in RERANK_USAGE_FIELDS}
        pins["rerank_usage"]["estimated_cost_usd"] = None
    report = build_report(gold, corpus, captures, snapshot, pins, pdf_suite=pdf_suite, capture_scope=capture_scope)
    write_immutable_json(output.with_name(output.stem + ".pins.json"), pins)
    write_immutable_json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--live", action="store_true", help="Explicitly call the configured embedding provider and local read-only DB")
    parser.add_argument("--output", type=Path, help="New external immutable live report")
    parser.add_argument("--vector-cache", type=Path)
    parser.add_argument("--baseline-root", type=Path, default=Path("/tmp/graphrag-industrial-i2-baseline"))
    parser.add_argument("--include-pdf", action="store_true", help="Run the separately pinned three-case PDF context suite")
    parser.add_argument("--pdf-cache", type=Path, help="External directory holding normalized artifacts and {source_id}.pdf originals")
    parser.add_argument("--validate-report", type=Path, help="Recompute metrics/source checks without DB or provider calls")
    parser.add_argument("--expected-pins", type=Path, help="Optional independently retained run pins for offline validation")
    parser.add_argument("--rerank", action="store_true", help="Capture only the declared scoped rerank variant in a v2 report")
    parser.add_argument("--rerank-cache", type=Path, help="Explicit external cache for complete ordered rerank inputs/responses")
    parser.add_argument("--rerank-profile", help="Versioned provider input profile; omitted uses the declared default")
    parser.add_argument("--capture-split", choices=("dev", "all"), default="all", help="v2 dev captures the entire 36-case dev split only; all retains all72")
    parser.add_argument("--selected-variant", choices=SUPPORTED_VARIANTS, help="Assess the selected configuration against the industrial contract")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="all", help="Only display and assess this split; dev never prints holdout or PDF predictions")
    parser.add_argument("--assessment-output", type=Path, help="Optional new external immutable acceptance assessment")
    args = parser.parse_args(argv)
    if args.live and args.validate_report:
        raise IndustrialEvaluationError("live capture and offline report validation are separate modes")
    if (args.rerank or args.rerank_cache or args.rerank_profile or args.capture_split != "all") and not args.live:
        raise IndustrialEvaluationError("rerank capture options require explicit --live")
    if args.capture_split != "all" and not args.rerank:
        raise IndustrialEvaluationError("historical comparison always retains all cases; dev-only capture requires v2 rerank")
    if (args.rerank_cache or args.rerank_profile) and not args.rerank:
        raise IndustrialEvaluationError("rerank cache/profile requires an explicit rerank capture")
    if args.capture_split == "dev" and (args.include_pdf or args.split != "dev"):
        raise IndustrialEvaluationError("dev-only capture requires --split dev and excludes the PDF integration suite")
    if (args.selected_variant or args.assessment_output) and not (args.live or args.validate_report):
        raise IndustrialEvaluationError("acceptance requires a live capture or an offline report")
    if args.assessment_output and not args.selected_variant:
        raise IndustrialEvaluationError("acceptance output requires an explicitly selected variant")
    corpus = build_corpus()
    gold = load_gold(args.gold)
    if args.live:
        report = _live(args, gold, corpus)
        evaluation = report["evaluation"]
    elif args.validate_report:
        report = read_json(args.validate_report)
        evaluation = validate_report(report, gold, corpus,
            expected_pins=read_json(args.expected_pins) if args.expected_pins else None)
    else:
        print(json.dumps({"mode": "gold-validation", "cases": len(gold.cases), "gold_checksum": gold.checksum,
            "corpus_checksum": corpus.manifest_checksum, "provider_calls": 0, "db_calls": 0,
            "baseline_commit": BASELINE_COMMIT}, ensure_ascii=False, sort_keys=True))
        return 0
    if evaluation.get("capture_scope", {}).get("split") == "dev" and args.split != "dev":
        raise IndustrialEvaluationError("dev-only capture must be displayed and assessed with --split dev")
    acceptance = {"status": "NOT_ASSESSED", "reason": "No configuration selected; successful capture is not a quality pass."}
    if args.selected_variant:
        acceptance = assess_acceptance(evaluation, load_industrial_contract(ROOT / "contracts/industrial_knowledge.v1.json"),
            selected_variant=args.selected_variant, split=args.split, report_checksum=report["report_checksum"])
        if args.assessment_output:
            write_immutable_json(_outside_repository(args.assessment_output, "acceptance output"), acceptance)
    summary = {"mode": "live-capture" if args.live else "offline-report-validation", "displayed_split": args.split,
        "report_checksum": report["report_checksum"], "runtime_answer_and_refusal_evaluated": False,
        "capture_completeness": evaluation.get("capture_completeness", "COMPLETE_CORE_GOLD"),
        "quality_acceptance": acceptance,
        "variants": {name: {split: value[split] for split in (("all", "dev", "holdout") if args.split == "all" else (args.split,))}
                     for name, value in evaluation["variants"].items()},
        "official_pdf_integration": ({"status": "NOT_DISPLAYED", "reason": "PDF predictions are separate from dev selection."}
                                     if args.split == "dev" else evaluation["official_pdf_integration"]),
        "query_embedding_usage": report["pins"].get("query_embedding_usage"),
        "rerank_usage": report["pins"].get("rerank_usage"),
        "source_embedding_usage": report["pins"].get("source_embedding_usage")}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    # Baseline missing product scope is an observed comparison result. Current
    # scope/security/citation failures and runtime failures must fail the run.
    checked_variants = list(evaluation["variants"].items())
    for variant, metrics in checked_variants:
        total = metrics[args.split]
        if any(total[key] for key in ("current_acl_exposure_count", "citation_error_count", "forbidden_sentinel_exposure_count", "runtime_error_count")):
            return 1
        if variant != "legacy_default" and total["scope_violation_count"]:
            return 1
    if args.split != "dev" and evaluation["official_pdf_integration"]["status"] == "RAN":
        for variant, metrics in evaluation["official_pdf_integration"]["variants"].items():
            total = metrics["all"]
            if any(total[key] for key in ("current_acl_exposure_count", "citation_error_count", "forbidden_sentinel_exposure_count", "runtime_error_count")):
                return 1
            if variant != "legacy_default" and total["scope_violation_count"]:
                return 1
    if acceptance["status"] == "FAILED":
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Industrial retrieval evaluation failed ({type(error).__name__}); inspect bounded local inputs and run pins.", file=sys.stderr)
        raise SystemExit(2) from None
