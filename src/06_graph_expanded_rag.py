"""Graph-expanded RAG with established retrieval and ranking methods.

This step extends the progression without changing the knowledge graph:

  1. Retrieve independent vector and BM25 rankings.
  2. Fuse them with Reciprocal Rank Fusion (RRF, k=60) to select seed chunks.
  3. Expand from every seed through all shared entities.
  4. Rank shared-entity chunks with the Resource Allocation (RA) index.
  5. Re-score the expanded candidate pool with cosine similarity.
  6. Fuse the candidate vector ranking and BM25 ranking with RRF.
  7. Attach adjacent chunks as an unscored context window.
  8. Give the bounded, source-labelled context to the LLM.

No entity type is excluded and no custom weighted relevance formula is used.

References:
  - Cormack, Clarke, Buettcher (SIGIR 2009), Reciprocal Rank Fusion.
  - Zhou, Lu, Zhang (EPJ B 2009), Resource Allocation index.

Run the full QA demo:
    uv run python src/06_graph_expanded_rag.py

Inspect retrieval without calling the LLM:
    uv run python src/06_graph_expanded_rag.py --retrieval-only

Run one question:
    uv run python src/06_graph_expanded_rag.py --question "your question"
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

import neo4j
from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.generation.prompts import RagTemplate
from neo4j_graphrag.retrievers.base import Retriever
from neo4j_graphrag.types import RawSearchResult, RetrieverResult, RetrieverResultItem

from config import (
    FULLTEXT_INDEX_NAME,
    NEO4J_DATABASE,
    VECTOR_INDEX_NAME,
    get_driver,
    get_embedder,
    get_llm,
)


# RRF's original SIGIR 2009 paper used k=60. RRF combines ranks rather than
# incomparable raw cosine, BM25, and graph scores.
RRF_K = 60

# These are candidate/context budgets, not relevance weights.
SEARCH_CONFIG = {
    "top_k": 5,
    # The sample graph has only eight chunks. A window of three keeps initial
    # retrieval selective so graph expansion can introduce genuinely new
    # candidates instead of starting with the entire corpus.
    "retrieval_window": 3,
    "seed_top_k": 3,
    "graph_window": 10,
    "anchor_top_k": 3,
}

FALLBACK_RESPONSE = "I don't have enough context to answer this question."

ANSWER_PROMPT = RagTemplate(
    system_instructions=(
        "Answer the question using only the supplied context. "
        "Treat chunk text as evidence and related entities as navigation hints. "
        "Do not invent facts or sources."
    ),
    template="""Context:
{context}

Examples:
{examples}

Instructions:
- Base every factual claim on the chunk text above.
- Cite supporting chunks inline using their labels, for example [Chunk 6].
- Do not treat a name in "Related entities" as proof unless chunk text supports it.
- Adjacent-context chunks are evidence even though they have no retrieval rank.
- If the context is insufficient, answer exactly: "{fallback_response}"
- End with the source names that appear in the context.

Question:
{query_text}

Answer:
""".replace("{fallback_response}", FALLBACK_RESPONSE),
)


QUESTIONS = [
    (
        "What products and services does Apple offer, and what risks could affect "
        "its ability to manufacture and deliver them?"
    ),
    (
        "Summarize Apple's fiscal year 2024 financial performance, including "
        "shareholder returns and year-end cash."
    ),
    "What is the capital of France?",
]


VECTOR_QUERY = """
CALL db.index.vector.queryNodes(
    $vector_index_name, $limit, $query_vector
)
YIELD node, score
RETURN
    elementId(node) AS chunk_id,
    node.index AS chunk_index,
    score
ORDER BY score DESC, chunk_id
"""


FULLTEXT_QUERY = """
CALL db.index.fulltext.queryNodes(
    $fulltext_index_name, $query_text, {limit: $limit}
)
YIELD node, score
RETURN
    elementId(node) AS chunk_id,
    node.index AS chunk_index,
    score
ORDER BY score DESC, chunk_id
"""


CANDIDATE_VECTOR_QUERY = """
UNWIND $candidate_ids AS candidate_id
MATCH (chunk:Chunk)
WHERE elementId(chunk) = candidate_id
  AND chunk.embedding IS NOT NULL
WITH chunk,
     vector.similarity.cosine(chunk.embedding, $query_vector) AS score
RETURN
    elementId(chunk) AS chunk_id,
    chunk.index AS chunk_index,
    score
ORDER BY score DESC, chunk_id
"""


# Resource Allocation between a seed Chunk and candidate Chunk:
#
#   RA(seed, candidate) = sum(1 / degree(entity))
#
# Every entity connected to both chunks contributes. Highly connected entities
# contribute less automatically; entity labels are never filtered or weighted.
RESOURCE_ALLOCATION_QUERY = """
MATCH (seed:Chunk)
WHERE elementId(seed) = $seed_id
MATCH (entity)-[:FROM_CHUNK]->(seed)
MATCH (entity)-[:FROM_CHUNK]->(candidate:Chunk)
WHERE candidate <> seed
MATCH (entity)-[:FROM_CHUNK]->(linked:Chunk)
WITH candidate, entity, count(DISTINCT linked) AS entity_degree
WITH candidate,
     sum(1.0 / toFloat(entity_degree)) AS ra_score,
     collect({
         entity: coalesce(entity.name, entity.ticker, elementId(entity)),
         label: [label IN labels(entity)
                 WHERE NOT label IN ['__KGBuilder__', '__Entity__']][0],
         degree: entity_degree
     }) AS evidence
RETURN
    elementId(candidate) AS chunk_id,
    candidate.index AS chunk_index,
    ra_score,
    evidence
ORDER BY ra_score DESC, chunk_id
LIMIT $limit
"""


ADJACENT_QUERY = """
UNWIND range(0, size($anchor_ids) - 1) AS anchor_position
WITH anchor_position, $anchor_ids[anchor_position] AS anchor_id
MATCH (anchor:Chunk)-[:FROM_DOCUMENT]->(doc:Document)
WHERE elementId(anchor) = anchor_id
MATCH (neighbor:Chunk)-[:FROM_DOCUMENT]->(doc)
WHERE abs(toInteger(neighbor.index) - toInteger(anchor.index)) = 1
RETURN
    anchor_id,
    anchor.index AS anchor_index,
    elementId(neighbor) AS chunk_id,
    neighbor.index AS chunk_index
ORDER BY anchor_position, chunk_index
"""


CONTEXT_QUERY = """
UNWIND range(0, size($selected) - 1) AS position
WITH position, $selected[position] AS selection
MATCH (chunk:Chunk)
WHERE elementId(chunk) = selection.chunk_id
OPTIONAL MATCH (chunk)-[:FROM_DOCUMENT]->(doc:Document)
OPTIONAL MATCH (entity)-[:FROM_CHUNK]->(chunk)
WITH position, selection, chunk, doc,
     collect(DISTINCT CASE
         WHEN entity IS NULL THEN NULL
         ELSE coalesce(
             [label IN labels(entity)
              WHERE NOT label IN ['__KGBuilder__', '__Entity__']][0],
             'Entity'
         ) + ': ' + coalesce(entity.name, entity.ticker, elementId(entity))
     END) AS raw_entities
RETURN
    chunk.text AS text,
    chunk.index AS chunk_index,
    elementId(chunk) AS chunk_id,
    selection.role AS role,
    selection.reasons AS reasons,
    selection.rrf_score AS score,
    doc.source AS source,
    doc.filing_type AS filing_type,
    [entity IN raw_entities WHERE entity IS NOT NULL] AS entities
ORDER BY position
"""


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]],
    *,
    rank_constant: int = RRF_K,
) -> tuple[list[str], dict[str, float], dict[str, dict[str, int]]]:
    """Fuse named rankings using the original RRF scoring rule."""
    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")

    scores: defaultdict[str, float] = defaultdict(float)
    rank_positions: defaultdict[str, dict[str, int]] = defaultdict(dict)

    for ranking_name, chunk_ids in rankings.items():
        seen: set[str] = set()
        for rank, chunk_id in enumerate(chunk_ids, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1.0 / (rank_constant + rank)
            rank_positions[chunk_id][ranking_name] = rank

    ordered_ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return ordered_ids, dict(scores), dict(rank_positions)


def format_context_record(record: neo4j.Record) -> RetrieverResultItem:
    """Format source-labelled context for the LLM and debug output."""
    chunk_index = record.get("chunk_index")
    role = record.get("role", "ranked")
    reasons = record.get("reasons", [])
    entities = record.get("entities", [])
    source = record.get("source")
    filing_type = record.get("filing_type")

    parts = [f"[Chunk {chunk_index} | {role}]", record.get("text", "")]
    if reasons:
        parts.append(f"Retrieval reasons: {'; '.join(reasons)}")
    if entities:
        parts.append(f"Related entities: {', '.join(entities)}")
    if source:
        provenance = source
        if filing_type:
            provenance = f"{provenance} ({filing_type})"
        parts.append(f"Source: {provenance}")

    return RetrieverResultItem(
        content="\n".join(parts),
        metadata={
            "score": record.get("score"),
            "chunk_id": record.get("chunk_id"),
            "chunk_index": chunk_index,
            "role": role,
            "reasons": reasons,
            "source": source,
        },
    )


class GraphExpandedRetriever(Retriever):
    """RRF seed retrieval followed by RA expansion and hybrid re-ranking."""

    def __init__(
        self,
        driver: neo4j.Driver,
        embedder: Any,
        vector_index_name: str,
        fulltext_index_name: str,
        neo4j_database: str,
    ) -> None:
        super().__init__(driver, neo4j_database)
        self.embedder = embedder
        self.vector_index_name = vector_index_name
        self.fulltext_index_name = fulltext_index_name
        self.result_formatter = format_context_record

    def _execute(self, query: str, **parameters: Any) -> list[neo4j.Record]:
        records, _, _ = self.driver.execute_query(
            query,
            parameters,
            database_=self.neo4j_database,
            routing_=neo4j.RoutingControl.READ,
        )
        return records

    @staticmethod
    def _ranking_ids(records: list[neo4j.Record]) -> list[str]:
        return [record["chunk_id"] for record in records]

    def get_search_results(
        self,
        query_text: str,
        top_k: int = 5,
        retrieval_window: int = 3,
        seed_top_k: int = 3,
        graph_window: int = 10,
        anchor_top_k: int = 3,
    ) -> RawSearchResult:
        """Return graph-expanded context using RRF and Resource Allocation."""
        for name, value in {
            "top_k": top_k,
            "retrieval_window": retrieval_window,
            "seed_top_k": seed_top_k,
            "graph_window": graph_window,
            "anchor_top_k": anchor_top_k,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        query_vector = self.embedder.embed_query(query_text)

        vector_records = self._execute(
            VECTOR_QUERY,
            vector_index_name=self.vector_index_name,
            limit=retrieval_window,
            query_vector=query_vector,
        )
        fulltext_records = self._execute(
            FULLTEXT_QUERY,
            fulltext_index_name=self.fulltext_index_name,
            limit=retrieval_window,
            query_text=query_text,
        )

        vector_ids = self._ranking_ids(vector_records)
        fulltext_ids = self._ranking_ids(fulltext_records)
        initial_rankings = {"vector": vector_ids, "bm25": fulltext_ids}
        seed_order, seed_scores, seed_positions = reciprocal_rank_fusion(
            initial_rankings
        )
        seed_ids = seed_order[:seed_top_k]

        chunk_indexes: dict[str, int] = {}
        for record in [*vector_records, *fulltext_records]:
            chunk_indexes[record["chunk_id"]] = record["chunk_index"]

        ra_records_by_seed: dict[str, list[neo4j.Record]] = {}
        graph_reasons: defaultdict[str, list[str]] = defaultdict(list)
        candidate_ids = set(vector_ids) | set(fulltext_ids)
        for seed_id in seed_ids:
            ra_records = self._execute(
                RESOURCE_ALLOCATION_QUERY,
                seed_id=seed_id,
                limit=graph_window,
            )
            ra_records_by_seed[seed_id] = ra_records
            seed_index = chunk_indexes.get(seed_id, seed_id)
            for rank, record in enumerate(ra_records, start=1):
                chunk_indexes[record["chunk_id"]] = record["chunk_index"]
                candidate_ids.add(record["chunk_id"])
                graph_reasons[record["chunk_id"]].append(
                    f"RA from Chunk {seed_index} rank {rank}"
                )

        # RA is used for graph candidate generation, not as a third RRF channel.
        # Treating every seed's RA list as an equal retrieval system can promote
        # a weak shared-entity path above two strong lexical/semantic matches.
        # Re-score the complete candidate pool semantically, then apply the
        # standard two-channel vector + BM25 RRF used by production hybrid search.
        candidate_vector_records = self._execute(
            CANDIDATE_VECTOR_QUERY,
            candidate_ids=sorted(candidate_ids),
            query_vector=query_vector,
        )
        candidate_vector_ids = self._ranking_ids(candidate_vector_records)
        for record in candidate_vector_records:
            chunk_indexes[record["chunk_id"]] = record["chunk_index"]

        final_rankings = {
            "vector": candidate_vector_ids,
            "bm25": fulltext_ids,
        }
        final_order, final_scores, final_positions = reciprocal_rank_fusion(
            final_rankings
        )
        anchor_ids = final_order[: min(anchor_top_k, top_k)]

        selections: list[dict[str, Any]] = []
        selection_by_id: dict[str, dict[str, Any]] = {}

        def add_selection(
            chunk_id: str,
            role: str,
            reasons: list[str],
        ) -> None:
            if chunk_id in selection_by_id:
                existing_reasons = selection_by_id[chunk_id]["reasons"]
                for reason in reasons:
                    if reason not in existing_reasons:
                        existing_reasons.append(reason)
                return
            selection = {
                "chunk_id": chunk_id,
                "role": role,
                "reasons": reasons,
                "rrf_score": final_scores.get(chunk_id),
            }
            selections.append(selection)
            selection_by_id[chunk_id] = selection

        for chunk_id in anchor_ids:
            reasons = [
                f"{name} rank {rank}"
                for name, rank in sorted(final_positions[chunk_id].items())
            ]
            reasons.extend(graph_reasons.get(chunk_id, []))
            add_selection(chunk_id, "RRF anchor", reasons)

        if anchor_ids and len(selections) < top_k:
            adjacent_records = self._execute(
                ADJACENT_QUERY,
                anchor_ids=anchor_ids,
            )
            for record in adjacent_records:
                anchor_index = record["anchor_index"]
                neighbor_id = record["chunk_id"]
                chunk_indexes[neighbor_id] = record["chunk_index"]
                add_selection(
                    neighbor_id,
                    "adjacent context",
                    [f"adjacent to Chunk {anchor_index}"],
                )
                if len(selections) >= top_k:
                    break

        # If overlapping windows produced fewer than top_k unique chunks, fill
        # the remaining context with the next items from the final RRF ranking.
        if len(selections) < top_k:
            for chunk_id in final_order:
                reasons = [
                    f"{name} rank {rank}"
                    for name, rank in sorted(final_positions[chunk_id].items())
                ]
                reasons.extend(graph_reasons.get(chunk_id, []))
                add_selection(chunk_id, "RRF candidate", reasons)
                if len(selections) >= top_k:
                    break

        context_records = self._execute(CONTEXT_QUERY, selected=selections)

        def serialise_records(
            records: list[neo4j.Record], score_key: str
        ) -> list[dict[str, Any]]:
            return [
                {
                    "chunk_index": record["chunk_index"],
                    "chunk_id": record["chunk_id"],
                    "score": record[score_key],
                }
                for record in records
            ]

        ra_trace: dict[str, list[dict[str, Any]]] = {}
        for seed_id, records in ra_records_by_seed.items():
            seed_label = str(chunk_indexes.get(seed_id, seed_id))
            ra_trace[seed_label] = [
                {
                    "chunk_index": record["chunk_index"],
                    "chunk_id": record["chunk_id"],
                    "ra_score": record["ra_score"],
                    "evidence": record["evidence"],
                }
                for record in records
            ]

        trace = {
            "method": "RRF(k=60) + Resource Allocation",
            "initial_vector_ranking": serialise_records(vector_records, "score"),
            "bm25_ranking": serialise_records(fulltext_records, "score"),
            "seed_ranking": [
                {
                    "chunk_index": chunk_indexes.get(chunk_id),
                    "chunk_id": chunk_id,
                    "rrf_score": seed_scores[chunk_id],
                    "ranks": seed_positions[chunk_id],
                }
                for chunk_id in seed_order
            ],
            "ra_rankings": ra_trace,
            "candidate_vector_ranking": serialise_records(
                candidate_vector_records, "score"
            ),
            "final_ranking": [
                {
                    "chunk_index": chunk_indexes.get(chunk_id),
                    "chunk_id": chunk_id,
                    "rrf_score": final_scores[chunk_id],
                    "ranks": final_positions[chunk_id],
                }
                for chunk_id in final_order
            ],
            "selected_context": selections,
        }
        return RawSearchResult(records=context_records, metadata={"trace": trace})


def print_ranking(
    title: str,
    rows: list[dict[str, Any]],
    score_key: str,
) -> None:
    print(f"\n--- {title} ---")
    if not rows:
        print("  (no results)")
        return
    for rank, row in enumerate(rows, start=1):
        score = row.get(score_key)
        score_text = f"{score:.6f}" if isinstance(score, (int, float)) else "N/A"
        print(f"  {rank}. Chunk {row.get('chunk_index')}  score={score_text}")


def print_retrieval_result(result: RetrieverResult) -> None:
    trace = (result.metadata or {}).get("trace", {})
    print_ranking(
        "Initial vector ranking",
        trace.get("initial_vector_ranking", []),
        "score",
    )
    print_ranking("BM25 ranking", trace.get("bm25_ranking", []), "score")
    print_ranking("Initial RRF seeds", trace.get("seed_ranking", []), "rrf_score")

    for seed_index, rows in trace.get("ra_rankings", {}).items():
        print_ranking(
            f"Resource Allocation from seed Chunk {seed_index}",
            rows,
            "ra_score",
        )
        for row in rows:
            evidence = ", ".join(
                f"{item.get('label')}: {item.get('entity')} "
                f"(degree={item.get('degree')})"
                for item in row.get("evidence", [])
            )
            if evidence:
                print(f"       Chunk {row.get('chunk_index')}: {evidence}")

    print_ranking(
        "Vector re-ranking of expanded candidates",
        trace.get("candidate_vector_ranking", []),
        "score",
    )
    print_ranking("Final RRF ranking", trace.get("final_ranking", []), "rrf_score")
    print(f"\n--- Final context ({len(result.items)} chunks) ---")
    for item in result.items:
        score = (item.metadata or {}).get("score")
        score_text = f"{score:.6f}" if isinstance(score, (int, float)) else "unscored"
        chunk_index = (item.metadata or {}).get("chunk_index")
        role = (item.metadata or {}).get("role")
        print(f"\nChunk {chunk_index} | {role} | RRF={score_text}")
        print(item.content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Run retrieval and graph expansion without calling the LLM.",
    )
    parser.add_argument(
        "--question",
        action="append",
        help="Question to run. Repeat the flag for multiple questions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    questions = args.question or QUESTIONS

    with get_driver() as driver:
        retriever = GraphExpandedRetriever(
            driver=driver,
            embedder=get_embedder(),
            vector_index_name=VECTOR_INDEX_NAME,
            fulltext_index_name=FULLTEXT_INDEX_NAME,
            neo4j_database=NEO4J_DATABASE,
        )

        rag = None
        if not args.retrieval_only:
            rag = GraphRAG(
                retriever=retriever,
                llm=get_llm(),
                prompt_template=ANSWER_PROMPT,
            )

        for question in questions:
            print(f"\n{'=' * 72}")
            print(f"Question: {question}")
            print("=" * 72)

            if args.retrieval_only:
                retrieval_result = retriever.search(
                    query_text=question,
                    **SEARCH_CONFIG,
                )
                print_retrieval_result(retrieval_result)
                continue

            assert rag is not None
            result = rag.search(
                query_text=question,
                retriever_config=SEARCH_CONFIG,
                return_context=True,
                response_fallback=FALLBACK_RESPONSE,
            )
            print(f"\nAnswer:\n{result.answer}")
            if result.retriever_result:
                print_retrieval_result(result.retriever_result)


if __name__ == "__main__":
    main()
