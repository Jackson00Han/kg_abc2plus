"""Hybrid search (vector + fulltext) with Cypher graph traversal.

Demonstrates HybridCypherRetriever: combines vector similarity with
fulltext keyword search, then traverses the graph for entity context.
This retrieves chunks that match both semantically and by keyword,
producing more robust results than either approach alone.

Run: uv run python src/04_hybrid_cypher_retriever.py
"""

from neo4j_graphrag.retrievers import HybridCypherRetriever

from config import (
    FULLTEXT_INDEX_NAME,
    NEO4J_DATABASE,
    VECTOR_INDEX_NAME,
    get_driver,
    get_embedder,
)
from shared import RETRIEVAL_QUERY, formatter

QUERIES = [
    "What products does Apple sell?",
    "What are the key risk factors?",
    "How did Apple perform financially in 2024?",
]


def main():
    with get_driver() as driver:
        embedder = get_embedder()

        retriever = HybridCypherRetriever(
            driver=driver,
            vector_index_name=VECTOR_INDEX_NAME,
            fulltext_index_name=FULLTEXT_INDEX_NAME,
            retrieval_query=RETRIEVAL_QUERY,
            result_formatter=formatter,
            embedder=embedder,
            neo4j_database=NEO4J_DATABASE,
        )

        for query in QUERIES:
            print(f"\n{'='*60}")
            print(f"Query: {query}")
            print("=" * 60)

            results = retriever.search(query_text=query, top_k=3)

            for i, item in enumerate(results.items):
                score = item.metadata.get("score")
                score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
                print(f"\n--- Result {i+1} (score: {score_str}) ---")
                print(item.content[:800])


if __name__ == "__main__":
    main()
