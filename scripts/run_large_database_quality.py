#!/usr/bin/env python3
"""Run the adjudicated development cases beside the Stage 9 load corpus.

This check deliberately adds ``dev-corpus-v1`` to an already populated
production-reference database.  It catches global-index crowding and proves
that tenant-specific vector generations, authorization filters, citations, and
grounded answers retain their Stage 8 quality at representative graph size.
The output includes all 49 raw case observations, but generated prose and
citation metadata are retained only as commitments to committed synthetic
gold, so protected text cannot enter the evidence artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import Principal
from graphrag_prod.evaluation import load_gold_dataset
from graphrag_prod.evaluation.answers import evaluate_answer_results
from graphrag_prod.evaluation.quality_evidence import (
    build_quality_case_evidence,
    canonical_quality_digest,
    evaluate_quality_case_evidence,
)
from graphrag_prod.evaluation.production_config import (
    resolve_production_answer_retrieval_limits,
)
from graphrag_prod.evaluation.reference_predictions import (
    REFERENCE_PREDICTION_PROVIDER,
    REFERENCE_PREDICTION_SHA256,
    REFERENCE_PREDICTION_VERSION,
    load_reference_predictions,
    prediction_payload,
)
from graphrag_prod.generation import (
    AnswerModelRequest,
    AnswerStatus,
    GenerationRequest,
    GroundedGenerationService,
)
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager, Neo4jIngestionService
from graphrag_prod.retrieval import Neo4jRetrievalEngine, RetrievalLimits, RetrievalRequest
from graphrag_prod.retrieval.metrics import evaluate_retrieval_results
from scripts.load_production_corpus import canonical_graph_state
from tests.fixtures.dev_corpus import DevCorpusFixture, load_dev_corpus_fixture


ROOT = Path(__file__).resolve().parents[1]
GOLD_MANIFEST = ROOT / "evaluation" / "gold-v1" / "manifest.json"
REFERENCE_PREDICTIONS = (
    ROOT / "evaluation" / "reference-answer-predictions.v1.json"
)
PRODUCTION_CONFIGURATION = (
    ROOT / "evaluation" / "production-reference-config.v1.json"
)


class _RecordedReferenceAnswerModel:
    """Render a separately recorded prediction against the real prompt sources."""

    def __init__(self, predictions_by_query: dict[str, dict[str, Any]]) -> None:
        self.predictions_by_query = predictions_by_query

    def generate(self, request: AnswerModelRequest) -> object:
        return prediction_payload(request, self.predictions_by_query)


def _settings() -> tuple[str, str, str, str]:
    names = (
        "TEST_NEO4J_URI",
        "TEST_NEO4J_USER",
        "TEST_NEO4J_PASSWORD",
        "TEST_NEO4J_DATABASE",
    )
    values = tuple(os.getenv(name, "") for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
    if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
        raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
    host = urlparse(values[0]).hostname
    if host is None or not ipaddress.ip_address(host).is_loopback:
        raise RuntimeError("Stage 9 accepts only a loopback disposable Neo4j URI")
    return values  # type: ignore[return-value]


def _request(
    fixture: DevCorpusFixture,
    question: dict[str, Any],
    *,
    limits: RetrievalLimits,
):
    principal = question["principal"]
    return RetrievalRequest(
        query_text=question["query"],
        query_vector=fixture.query_vector(question),
        principal=Principal(
            principal["principal_id"],
            principal["tenant_id"],
            frozenset(principal["groups"]),
        ),
        query_embedding_space_id=fixture.build.manifest["embedding_profile"][
            "embedding_space_id"
        ],
        limits=limits,
    )


def _trace_resources(result: Any) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    for stage_name in (
        "vector_recall",
        "bm25_recall",
        "seed_ranking",
        "graph_expansion",
        "candidate_vector_ranking",
        "final_ranking",
    ):
        resources.extend(
            {"id": hit.chunk_id, "kind": "chunk", "stage": stage_name}
            for hit in getattr(result.trace, stage_name)
        )
    resources.extend(
        {"id": item, "kind": "chunk", "stage": "selected_context"}
        for item in result.trace.selected_chunk_ids
    )
    return resources


def _database_chunk_sets(
    driver: neo4j.Driver,
    database: str,
    principal: Principal,
) -> tuple[set[str], set[str]]:
    """Return every stored Chunk ID and the principal's active authorized IDs."""

    all_records, _, _ = driver.execute_query(
        "MATCH (chunk:Chunk) RETURN DISTINCT chunk.chunk_id AS chunk_id",
        database_=database,
    )
    allowed_records, _, _ = driver.execute_query(
        """
        MATCH (document:Document {tenant_id: $tenant_id})
              -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                  tenant_id: $tenant_id, build_state: 'PUBLISHED'
              })-[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
        MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
            tenant_id: $tenant_id
        })
        MATCH (snapshot)-[:OF_VERSION]->(version)
        WHERE any(group IN document.access_groups WHERE group IN $groups)
          AND any(group IN chunk.access_groups WHERE group IN $groups)
          AND chunk.document_id = document.document_id
          AND chunk.version_id = version.version_id
          AND chunk.access_policy_id = document.access_policy_id
          AND chunk.access_policy_version = document.access_policy_version
        RETURN DISTINCT chunk.chunk_id AS chunk_id
        """,
        tenant_id=principal.tenant_id,
        groups=sorted(principal.groups),
        database_=database,
    )
    all_ids = {str(record["chunk_id"]) for record in all_records}
    allowed_ids = {str(record["chunk_id"]) for record in allowed_records}
    if not all_ids or not allowed_ids or not allowed_ids <= all_ids:
        raise RuntimeError("database authorization inventory is inconsistent")
    return all_ids, allowed_ids


def _source_authorized_chunk_ids(
    fixture: DevCorpusFixture,
    principal: Principal,
) -> set[str]:
    """Derive the ACL oracle from committed source records, never the graph."""

    allowed: set[str] = set()
    for chunk in fixture.build.chunks:
        document = fixture.documents_by_id.get(str(chunk["document_id"]))
        if document is None:
            raise RuntimeError("development Chunk has no committed Document")
        chunk_groups = set(str(value) for value in chunk["access_groups"])
        document_groups = set(
            str(value) for value in document["access_groups"]
        )
        if (
            chunk["tenant_id"] == principal.tenant_id
            and document["tenant_id"] == principal.tenant_id
            and chunk["access_policy_id"] == document["access_policy_id"]
            and chunk["access_policy_version"]
            == document["access_policy_version"]
            and bool(chunk_groups & principal.groups)
            and bool(document_groups & principal.groups)
        ):
            allowed.add(str(chunk["chunk_id"]))
    if not allowed:
        raise RuntimeError("committed source authorization oracle is empty")
    return allowed


def _load_quality_gold(fixture: DevCorpusFixture):
    """Load the pinned gold and reject drift from the executable fixture."""

    gold = load_gold_dataset(GOLD_MANIFEST)
    projected_questions = tuple(
        {
            key: value
            for key, value in question.items()
            if key != "required_evidence_groups"
        }
        for question in gold.questions
    )
    if projected_questions != fixture.build.questions:
        raise RuntimeError("development questions drifted from gold-v1")
    if gold.answers != fixture.build.answers:
        raise RuntimeError("development answers drifted from gold-v1")
    return gold


def run(output: Path, config_path: Path = PRODUCTION_CONFIGURATION) -> None:
    uri, user, password, database = _settings()
    driver = neo4j.GraphDatabase.driver(
        uri,
        auth=(user, password),
        max_connection_pool_size=32,
        connection_acquisition_timeout=5,
    )
    try:
        driver.verify_connectivity()
        fixture = load_dev_corpus_fixture()
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes)
        answer_retrieval_limits = resolve_production_answer_retrieval_limits(config)
        production_configuration_sha256 = hashlib.sha256(config_bytes).hexdigest()
        quality_retrieval_limits = RetrievalLimits(
            top_k=5,
            anchor_k=5,
            minimum_vector_score=0.75,
        )
        gold = _load_quality_gold(fixture)
        gold_manifest = gold.manifest
        questions = gold.questions
        answers = gold.answers
        required_case_ids = gold_manifest["coverage"]["required_case_ids"]
        observed_case_ids = sorted(
            question["id"] for question in questions
        )
        if observed_case_ids != required_case_ids:
            raise RuntimeError("large-database case IDs drifted from gold-v1")
        predictions_by_query = load_reference_predictions(REFERENCE_PREDICTIONS)
        question_identity = {
            str(question["query"]): str(question["id"])
            for question in questions
        }
        prediction_identity = {
            query: str(prediction["id"])
            for query, prediction in predictions_by_query.items()
        }
        if prediction_identity != question_identity:
            raise RuntimeError(
                "recorded answer predictions do not match the 49 quality cases"
            )
        service = Neo4jIngestionService(
            driver,
            database,
            worker_id="stage9-large-database-quality",
        )
        for plan in fixture.plans:
            result = service.ingest(plan)
            if result.active_snapshot_id != plan.snapshot.snapshot_id:
                raise RuntimeError(
                    f"development corpus did not activate: {plan.document_id}"
                )

        manager = Neo4jEmbeddingIndexManager(driver, database)
        tenant_plans: dict[str, list[Any]] = {}
        for plan in fixture.plans:
            tenant_plans.setdefault(plan.tenant_id, []).append(plan)
        active_generations: dict[str, str] = {}
        for tenant_id, plans in sorted(tenant_plans.items()):
            profile = plans[0].bundles[0].all_embeddings[0]
            generation = manager.prepare(
                tenant_id=tenant_id,
                embedding_profile=profile,
                generation_version=1,
            )
            if not manager.coverage(generation.generation_id).complete:
                raise RuntimeError(f"incomplete quality generation: {tenant_id}")
            active = manager.activate(
                generation.generation_id,
                expected_active_generation_id=None,
            )
            active_generations[tenant_id] = active.generation_id
        driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=database,
        )

        engine = Neo4jRetrievalEngine(driver, database)
        actual_retrieval: list[dict[str, object]] = []
        retrieval_by_id: dict[str, Any] = {}
        allowed_by_id: dict[str, set[str]] = {}
        unauthorized_count = 0
        for question in questions:
            request = _request(
                fixture,
                question,
                limits=quality_retrieval_limits,
            )
            result = engine.retrieve(request)
            accepted_final = [
                hit
                for hit in result.trace.final_ranking
                if len(hit.ranks) >= result.trace.limits.minimum_rrf_channels
            ]
            visible = _trace_resources(result)
            visible_ids = {item["id"] for item in visible}
            all_ids, allowed_ids = _database_chunk_sets(
                driver,
                database,
                request.principal,
            )
            expected_allowed_ids = _source_authorized_chunk_ids(
                fixture,
                request.principal,
            )
            if allowed_ids != expected_allowed_ids:
                raise RuntimeError(
                    "database authorization differs from committed source ACLs: "
                    f"{question['id']}"
                )
            if not set(fixture.chunks_by_id) <= all_ids:
                raise RuntimeError("development corpus is incomplete in the database")
            forbidden_ids = all_ids - expected_allowed_ids
            if not set(question["forbidden_chunk_ids"]) <= forbidden_ids:
                raise RuntimeError(
                    f"gold forbidden IDs are not forbidden in database: {question['id']}"
                )
            exposures = visible_ids - allowed_ids
            unauthorized_count += len(exposures)
            allowed_by_id[question["id"]] = expected_allowed_ids
            actual_retrieval.append(
                {
                    "id": question["id"],
                    "ranking": [hit.chunk_id for hit in accepted_final],
                    "visible_resources": visible,
                }
            )
            retrieval_by_id[question["id"]] = result

        retrieval_metrics = evaluate_retrieval_results(
            questions,
            actual_retrieval,
        )
        if unauthorized_count != retrieval_metrics.unauthorized_exposure_count:
            raise RuntimeError("authorization exposure accounting diverged")

        answers_by_id = {item["id"]: item for item in answers}
        generation_service = GroundedGenerationService(
            _RecordedReferenceAnswerModel(predictions_by_query)
        )
        actual_answers: list[dict[str, object]] = []
        for question in questions:
            gold = answers_by_id[question["id"]]
            retrieval = engine.retrieve(
                _request(
                    fixture,
                    question,
                    limits=answer_retrieval_limits,
                )
            )
            answer_visible_ids = {
                item["id"] for item in _trace_resources(retrieval)
            }
            answer_exposures = answer_visible_ids - allowed_by_id[question["id"]]
            if answer_exposures:
                raise RuntimeError(
                    f"answer retrieval exposed forbidden Chunks: {question['id']}"
                )
            answer = generation_service.generate(
                GenerationRequest(question["query"], retrieval.chunks)
            )
            if answer.failure_code is not None:
                raise RuntimeError(
                    f"grounded answer failed closed unexpectedly: {question['id']}"
                )
            if answer.status.value != gold["expected_status"]:
                raise RuntimeError(f"grounded answer status changed: {question['id']}")
            if answer.status is AnswerStatus.ANSWERED and not answer.citations:
                raise RuntimeError(f"answered case has no citations: {question['id']}")
            record = answer.as_dict()
            record["id"] = question["id"]
            actual_answers.append(record)
        answer_metrics = evaluate_answer_results(
            answers,
            actual_answers,
        )
        case_evidence = build_quality_case_evidence(
            actual_retrieval,
            actual_answers,
        )
        _, recomputed_retrieval, recomputed_answers = evaluate_quality_case_evidence(
            case_evidence
        )
        if recomputed_retrieval != asdict(retrieval_metrics):
            raise RuntimeError("raw retrieval evidence does not reproduce its metrics")
        if recomputed_answers != asdict(answer_metrics):
            raise RuntimeError("raw answer evidence does not reproduce its metrics")

        failures: list[str] = []
        checks = (
            ("retrieval recall_at_5", retrieval_metrics.recall_at_5 >= 0.90),
            ("retrieval mrr", retrieval_metrics.mrr >= 0.80),
            ("retrieval ndcg_at_5", retrieval_metrics.ndcg_at_5 >= 0.85),
            (
                "retrieval unauthorized exposure",
                retrieval_metrics.unauthorized_exposure_count == 0,
            ),
            (
                "answer supported claim rate",
                answer_metrics.supported_claim_rate >= 0.95,
            ),
            ("answer citation precision", answer_metrics.citation_precision >= 0.95),
            ("answer citation coverage", answer_metrics.citation_coverage >= 0.95),
            ("answer numerical fidelity", answer_metrics.numerical_fidelity == 1.0),
            ("answer refusal f1", answer_metrics.refusal_f1 >= 0.90),
            ("answer correctness", answer_metrics.answer_correctness == 1.0),
            (
                "answer generation failures",
                answer_metrics.generation_failure_count == 0,
            ),
            (
                "answer forbidden exposure",
                answer_metrics.forbidden_answer_exposure_count == 0,
            ),
        )
        failures.extend(name for name, passed in checks if not passed)
        if failures:
            raise RuntimeError("large-database quality gates failed: " + "; ".join(failures))

        observation = {
            "active_generation_ids": dict(sorted(active_generations.items())),
            "answer_retrieval_limits": asdict(answer_retrieval_limits),
            "answer_metrics": asdict(answer_metrics),
            "case_count": len(questions),
            "case_evidence": case_evidence,
            "case_ids": observed_case_ids,
            "case_set_sha256": (
                "sha256:" + gold_manifest["coverage"]["case_set_sha256"]
            ),
            "corpus_version": fixture.build.manifest["version"],
            "failures": failures,
            "gold_projection_sha256": canonical_quality_digest(case_evidence),
            "graph_state_sha256": "sha256:"
            + canonical_graph_state(driver, database)["sha256"],
            "passed": not failures,
            "prediction_provider": REFERENCE_PREDICTION_PROVIDER,
            "prediction_sha256": "sha256:" + REFERENCE_PREDICTION_SHA256,
            "prediction_version": REFERENCE_PREDICTION_VERSION,
            "production_configuration_sha256": (
                "sha256:" + production_configuration_sha256
            ),
            "retrieval_metrics": asdict(retrieval_metrics),
            "schema_version": "production-large-database-quality-v1",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    finally:
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=PRODUCTION_CONFIGURATION,
    )
    args = parser.parse_args()
    run(args.output, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
