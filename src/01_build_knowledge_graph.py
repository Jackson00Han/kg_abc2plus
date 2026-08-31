"""Build a knowledge graph from the Apple 10-K excerpt using SimpleKGPipeline.

This script:
1. Reads the sample filing text
2. Defines a schema (node types, relationship types, patterns)
3. Runs SimpleKGPipeline to chunk the text, generate embeddings,
   extract entities/relationships via the LLM, and write everything to Neo4j
4. Runs entity resolution to merge duplicate entities (enabled by default)
5. Creates vector and fulltext indexes on Chunk nodes for retrieval

Run: uv run python src/01_build_knowledge_graph.py
"""

import asyncio

from neo4j_graphrag.components.text_splitters.fixed_size_splitter import (
    FixedSizeSplitter,
)
from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline
from neo4j_graphrag.indexes import create_fulltext_index, create_vector_index

from config import (
    DATA_DIR,
    EMBEDDING_DIMENSIONS,
    FULLTEXT_INDEX_NAME,
    NEO4J_DATABASE,
    VECTOR_INDEX_NAME,
    get_driver,
    get_embedder,
    get_llm,
)

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------
# The schema tells the LLM what entities and relationships to look for
# during extraction. Without a schema, the LLM will invent its own types,
# leading to inconsistent and noisy graphs. With a schema, you get a clean,
# typed graph that maps to your domain.
#
# Each node type can have:
#   - label: the Neo4j node label (required)
#   - description: helps the LLM understand what qualifies as this type
#   - properties: typed attributes the LLM should try to extract
#
# Each relationship type has a label and optional description.
#
# Patterns define which node types can connect through which relationships.
# The LLM uses these as constraints during extraction.
# ---------------------------------------------------------------------------
NODE_TYPES = [
    {"label": "Company", "properties": [{"name": "ticker", "type": "STRING"}]},
    {
        "label": "Product",
        "description": "A product or service offered by a company",
        "properties": [{"name": "name", "type": "STRING"}],
    },
    {
        "label": "RiskFactor",
        "description": "A business risk faced by a company",
        "properties": [{"name": "name", "type": "STRING"}],
    },
]

RELATIONSHIP_TYPES = [
    {"label": "OFFERS", "description": "Company offers a product or service"},
    {"label": "FACES_RISK", "description": "Company faces a business risk"},
]

# Patterns constrain which relationships can exist between which node types.
# Only (Company)-[:OFFERS]->(Product) and (Company)-[:FACES_RISK]->(RiskFactor)
# are valid — the LLM won't create relationships outside these patterns.
PATTERNS = [
    ("Company", "OFFERS", "Product"),
    ("Company", "FACES_RISK", "RiskFactor"),
]


def verify_apoc(driver) -> None:
    """Fail fast when the APOC procedures required by the pipeline are absent."""
    required = {"apoc.merge.relationship", "apoc.refactor.mergeNodes"}
    records, _, _ = driver.execute_query(
        """
        SHOW PROCEDURES YIELD name
        WHERE name IN $required
        RETURN name
        """,
        required=sorted(required),
        database_=NEO4J_DATABASE,
    )
    missing = required - {record["name"] for record in records}
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(
            f"Neo4j APOC Core is missing required procedures: {names}. "
            "Install the APOC plugin and restart Neo4j before running this script."
        )


async def build_graph():
    text = (DATA_DIR / "apple_10k_excerpt.txt").read_text()
    print(f"Loaded {len(text)} characters from apple_10k_excerpt.txt")

    llm = get_llm()
    embedder = get_embedder()

    with get_driver() as driver:
        verify_apoc(driver)

        # Clear existing data so the demo is idempotent.
        # In production you'd use incremental updates instead.
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted")
            deleted = result.single()["deleted"]
            if deleted > 0:
                print(f"Cleared {deleted} existing nodes")

        print("\nBuilding knowledge graph with SimpleKGPipeline...")
        print("  - Splitting text into chunks")
        print("  - Generating embeddings for each chunk")
        print("  - Extracting entities and relationships via LLM")
        print("  - Writing to Neo4j")
        print("  - Resolving duplicate entities\n")

        # Use smaller chunks (500 chars, 100 overlap) so different queries
        # match different sections of the filing. The default chunk size
        # (4000 chars) would put the entire ~3000-char filing into a single
        # chunk, making the retrieval comparison in later steps meaningless.
        text_splitter = FixedSizeSplitter(chunk_size=500, chunk_overlap=100)

        # SimpleKGPipeline orchestrates the full pipeline in one call:
        #   1. Split text into chunks (using our text_splitter)
        #   2. Embed each chunk (using the embedder)
        #   3. Extract entities and relationships from each chunk (using the LLM)
        #   4. Write everything to Neo4j (Chunk, Document, entity nodes + relationships)
        #   5. Run entity resolution to merge duplicates (e.g. "Apple" and "Apple Inc.")
        #
        # The resulting graph has two layers:
        #   - Lexical layer: Document -> Chunk nodes (with embeddings and text)
        #   - Semantic layer: Company, Product, RiskFactor nodes with OFFERS/FACES_RISK edges
        kg_builder = SimpleKGPipeline(
            llm=llm,
            driver=driver,
            embedder=embedder,
            text_splitter=text_splitter,
            schema={
                "node_types": NODE_TYPES,
                "relationship_types": RELATIONSHIP_TYPES,
                "patterns": PATTERNS,
            },
            from_file=False,
            neo4j_database=NEO4J_DATABASE,
            # Entity resolution merges nodes with the same label and name
            # property, preventing duplicates like "Apple" and "Apple Inc."
            perform_entity_resolution=True,
        )

        result = await kg_builder.run_async(
            text=text,
            # Metadata is stored as properties on the Document node,
            # making it available during retrieval for provenance tracking.
            document_metadata={"source": "SEC EDGAR", "filing_type": "10-K"},
        )
        print(f"Pipeline result: {result}\n")

        # --- Index creation ---
        # SimpleKGPipeline stores embeddings on Chunk nodes but does NOT
        # create indexes automatically. We need to create them manually
        # so the retriever scripts can perform vector and fulltext search.

        # Vector index: enables cosine similarity search over chunk embeddings
        print(f"Creating vector index '{VECTOR_INDEX_NAME}'...")
        create_vector_index(
            driver,
            name=VECTOR_INDEX_NAME,
            label="Chunk",
            embedding_property="embedding",
            dimensions=EMBEDDING_DIMENSIONS,
            similarity_fn="cosine",
        )
        print("Vector index created.")

        # Fulltext index: enables keyword search over chunk text.
        # Used by HybridCypherRetriever in step 04 to combine keyword
        # matching with vector similarity.
        print(f"Creating fulltext index '{FULLTEXT_INDEX_NAME}'...")
        create_fulltext_index(
            driver,
            name=FULLTEXT_INDEX_NAME,
            label="Chunk",
            node_properties=["text"],
        )
        print("Fulltext index created.\n")

        # --- Print a summary of what was built ---
        # Filter out __KGBuilder__ (internal label added to all pipeline-created nodes)
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run("""
                MATCH (n)
                UNWIND labels(n) AS label
                WITH label WHERE label <> '__KGBuilder__'
                RETURN label, count(*) AS count
                ORDER BY label
            """)
            print("=== Nodes ===")
            for record in result:
                print(f"  {record['label']}: {record['count']}")

            result = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) AS type, count(*) AS count
                ORDER BY type
            """)
            print("\n=== Relationships ===")
            for record in result:
                print(f"  {record['type']}: {record['count']}")

    # Clean up the async OpenAI client used by SimpleKGPipeline
    await llm.async_client.close()


if __name__ == "__main__":
    asyncio.run(build_graph())
