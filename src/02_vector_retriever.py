"""Basic vector similarity search over chunk embeddings.

Demonstrates VectorRetriever: given a natural language query, find the
most similar chunks by cosine distance. No graph traversal — pure
semantic search over the embedded text.

Run: uv run python src/02_vector_retriever.py
"""

from neo4j_graphrag.retrievers import VectorRetriever

from config import NEO4J_DATABASE, VECTOR_INDEX_NAME, get_driver, get_embedder

QUERIES = [
    "What products does Apple sell?",
    "What are the key risk factors?",
    "How did Apple perform financially in 2024?",
]


def main():
    with get_driver() as driver:
        embedder = get_embedder()

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
                score = item.metadata.get("score")
                score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
                print(f"\n--- Result {i+1} (score: {score_str}) ---")
                print(item.content[:300])


if __name__ == "__main__":
    main()
