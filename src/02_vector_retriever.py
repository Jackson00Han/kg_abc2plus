"""Basic vector similarity search over chunk embeddings.

Demonstrates VectorRetriever: given a natural language query, find the
most similar chunks by cosine distance. No graph traversal — pure
semantic search over the embedded text.

This is the baseline retrieval strategy. Compare the output here with
step 03 (which adds graph context) to see what graph enrichment provides.

Run: uv run python src/02_vector_retriever.py
"""

from neo4j_graphrag.retrievers import VectorRetriever

from config import NEO4J_DATABASE, VECTOR_INDEX_NAME, get_driver, get_embedder

# Sample queries that target different sections of the Apple 10-K filing.
# Each should match different chunks, demonstrating that vector search
# routes queries to the most semantically relevant text.
QUERIES = [
    "What products does Apple sell?",
    "What are the key risk factors?",
    "How did Apple perform financially in 2024?",
]


def main():
    with get_driver() as driver:
        embedder = get_embedder()

        # VectorRetriever performs a simple nearest-neighbor search:
        #   1. Embed the query text using the same embedder that created chunk embeddings
        #   2. Find the top_k chunks with highest cosine similarity in the vector index
        #   3. Return the chunk content and similarity score
        #
        # No Cypher, no graph traversal — just embedding similarity.
        retriever = VectorRetriever(
            driver=driver,
            index_name=VECTOR_INDEX_NAME,
            embedder=embedder,
            neo4j_database=NEO4J_DATABASE,
        )

        for query in QUERIES:
            print(f"\n{'='*60}")
            print(f"Query: {query}")
            print("=" * 60)

            results = retriever.search(query_text=query, top_k=3)

            for i, item in enumerate(results.items):
                # Score is cosine similarity (0-1). Higher = more similar.
                score = item.metadata.get("score")
                score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
                print(f"\n--- Result {i+1} (score: {score_str}) ---")
                # content is the raw chunk text — no entity context attached
                print(item.content[:300])


if __name__ == "__main__":
    main()
