"""Vector search + Cypher graph traversal for enriched context.

Demonstrates VectorCypherRetriever: finds relevant chunks by vector
similarity, then traverses the graph to pull in structured entity
information (companies, products, risk factors) that pure vector
search cannot surface.

Compare the output with step 02 — the same queries now return entity
names and types alongside the chunk text.

Run: uv run python src/03_vector_cypher_retriever.py
"""

from neo4j_graphrag.retrievers import VectorCypherRetriever

from config import NEO4J_DATABASE, VECTOR_INDEX_NAME, get_driver, get_embedder
from shared import RETRIEVAL_QUERY, formatter

QUERIES = [
    "What products does Apple sell?",
    "What are the key risk factors?",
    "How did Apple perform financially in 2024?",
]


def main():
    with get_driver() as driver:
        embedder = get_embedder()

        # VectorCypherRetriever adds a graph traversal step after vector search:
        #   1. Embed the query and find top_k similar chunks (same as VectorRetriever)
        #   2. For each matched chunk, run RETRIEVAL_QUERY to traverse the graph
        #   3. The Cypher query walks from chunks to their parent Document and
        #      to any entities extracted from that chunk (Product, RiskFactor, etc.)
        #   4. The result_formatter shapes each Neo4j record into a RetrieverResultItem
        #
        # This is the key GraphRAG pattern: vector search for relevance,
        # graph traversal for structured context.
        retriever = VectorCypherRetriever(
            driver=driver,
            index_name=VECTOR_INDEX_NAME,
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
                # content now includes chunk text + "Related entities: ..." + "Source: ..."
                # thanks to the formatter in shared.py
                print(item.content[:800])


if __name__ == "__main__":
    main()
