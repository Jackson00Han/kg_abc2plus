"""Full GraphRAG pipeline: hybrid retrieval + graph context + LLM.

Combines semantic vector search, BM25 fulltext search, Cypher-based graph
enrichment, and an LLM to generate grounded natural-language answers.

This is the final step in the progression:
  02: vector search only           -> raw chunks
  03: vector + graph traversal     -> chunks with entity context
  04: hybrid + graph traversal     -> more robust matching
  05: hybrid + graph + LLM (here)  -> grounded natural-language answers

Run: uv run python src/05_graphrag_qa.py
"""

from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.generation.prompts import RagTemplate
from neo4j_graphrag.retrievers import HybridCypherRetriever

from config import (
    FULLTEXT_INDEX_NAME,
    NEO4J_DATABASE,
    VECTOR_INDEX_NAME,
    get_driver,
    get_embedder,
    get_llm,
)
from shared import RETRIEVAL_QUERY, formatter


# Keep retrieval behavior explicit instead of relying on library defaults.
# Linear fusion rewards chunks found by both search strategies. Vector search
# receives more weight because natural-language questions often use words that
# differ from the source text, while BM25 preserves exact names and numbers.
RETRIEVER_CONFIG = {
    "top_k": 3,
    "effective_search_ratio": 2,
    "ranker": "linear",
    "alpha": 0.7,
}

FALLBACK_RESPONSE = "I don't have enough context to answer this question."

# Extracted graph entities are useful navigation hints, but the source chunk is
# the evidence. This prompt prevents an incorrectly extracted entity from being
# treated as an independently verified fact and gives the model an explicit
# insufficient-evidence behavior.
ANSWER_PROMPT = RagTemplate(
    system_instructions=(
        "Answer the question using only the supplied context. "
        "Treat chunk text as evidence and related entities only as navigation hints. "
        "Do not invent facts or sources."
    ),
    template="""Context:
{context}

Examples:
{examples}

Instructions:
- Base every factual claim on the chunk text above.
- Do not treat a name in "Related entities" as proof unless the chunk text supports it.
- If the context is insufficient, answer exactly: "{fallback_response}"
- End the answer with the source names that appear in the context.

Question:
{query_text}

Answer:
""".replace("{fallback_response}", FALLBACK_RESPONSE),
)

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

        # Upgrade step 04 with answer generation: retrieve candidates using
        # vector similarity and BM25, fuse their scores, then enrich the final
        # chunks with entities and document provenance via RETRIEVAL_QUERY.
        retriever = HybridCypherRetriever(
            driver=driver,
            vector_index_name=VECTOR_INDEX_NAME,
            fulltext_index_name=FULLTEXT_INDEX_NAME,
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
        rag = GraphRAG(
            retriever=retriever,
            llm=llm,
            prompt_template=ANSWER_PROMPT,
        )

        for question in QUESTIONS:
            print(f"\n{'='*60}")
            print(f"Question: {question}")
            print("=" * 60)

            result = rag.search(
                query_text=question,
                retriever_config=RETRIEVER_CONFIG,
                # return_context=True includes the retriever results in the
                # response, so you can see exactly what the LLM was given.
                return_context=True,
                # This handles an empty index/result set. The prompt separately
                # instructs the LLM to refuse when returned context is insufficient.
                response_fallback=FALLBACK_RESPONSE,
            )

            print(f"\nAnswer:\n{result.answer}")

            # Print the complete retrieved context, not a short preview, so the
            # generated answer can be checked against exactly what the LLM saw.
            if result.retriever_result:
                print(f"\n--- Retrieved {len(result.retriever_result.items)} chunks ---")
                for i, item in enumerate(result.retriever_result.items):
                    score = (item.metadata or {}).get("score")
                    score_text = (
                        f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
                    )
                    print(f"\nChunk {i+1} (score: {score_text})")
                    print(item.content)


if __name__ == "__main__":
    main()
