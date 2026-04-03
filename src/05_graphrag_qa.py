"""Full GraphRAG pipeline: retriever + LLM for question answering.

Demonstrates GraphRAG: combines VectorCypherRetriever with an LLM to
generate answers grounded in knowledge graph context. Shows the complete
flow from question to graph-enriched retrieval to generated answer.

This is the final step in the progression:
  02: vector search only           -> raw chunks
  03: vector + graph traversal     -> chunks with entity context
  04: hybrid + graph traversal     -> more robust matching
  05: hybrid + graph + LLM (here)  -> natural language answers

Run: uv run python src/05_graphrag_qa.py
"""

from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.retrievers import VectorCypherRetriever

from config import NEO4J_DATABASE, VECTOR_INDEX_NAME, get_driver, get_embedder, get_llm
from shared import RETRIEVAL_QUERY, formatter

# Questions that exercise different parts of the knowledge graph.
# Each question should pull context from different chunks and entities.
QUESTIONS = [
    "What products and services does Apple offer?",
    "What are the main risk factors Apple faces and why are they significant?",
    "Summarize Apple's financial performance in fiscal year 2024.",
    "What is Apple's fastest growing business segment?",
]


def main():
    with get_driver() as driver:
        embedder = get_embedder()
        llm = get_llm()

        # Same retriever as step 03 — vector search + Cypher graph traversal.
        # The retriever finds relevant chunks and enriches them with entity
        # context from the knowledge graph.
        retriever = VectorCypherRetriever(
            driver=driver,
            index_name=VECTOR_INDEX_NAME,
            retrieval_query=RETRIEVAL_QUERY,
            result_formatter=formatter,
            embedder=embedder,
            neo4j_database=NEO4J_DATABASE,
        )

        # GraphRAG composes the retriever and LLM into a single search call:
        #   1. The retriever finds relevant, graph-enriched context
        #   2. That context is formatted into a prompt for the LLM
        #   3. The LLM generates an answer grounded in the retrieved context
        #
        # The LLM sees the chunk text, related entity names, and source
        # metadata — everything the formatter in shared.py produces.
        rag = GraphRAG(retriever=retriever, llm=llm)

        for question in QUESTIONS:
            print(f"\n{'='*60}")
            print(f"Question: {question}")
            print("=" * 60)

            result = rag.search(
                query_text=question,
                # return_context=True includes the retriever results in the
                # response, so you can see exactly what the LLM was given.
                return_context=True,
                # Fallback message if the retriever finds no relevant context.
                response_fallback="I don't have enough context to answer this question.",
            )

            print(f"\nAnswer:\n{result.answer}")

            # Show which chunks were retrieved to produce this answer.
            # This makes the pipeline transparent — you can verify the LLM
            # is grounding its answer in the right source material.
            if result.retriever_result:
                print(f"\n--- Retrieved {len(result.retriever_result.items)} chunks ---")
                for i, item in enumerate(result.retriever_result.items):
                    print(f"  Chunk {i+1}: {item.content[:100]}...")


if __name__ == "__main__":
    main()
