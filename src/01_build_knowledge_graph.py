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

from neo4j_graphrag.experimental.components.text_splitters.fixed_size_splitter import (
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

# Schema definition: what entities and relationships to extract
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

PATTERNS = [
    ("Company", "OFFERS", "Product"),
    ("Company", "FACES_RISK", "RiskFactor"),
]


async def build_graph():
    text = (DATA_DIR / "apple_10k_excerpt.txt").read_text()
    print(f"Loaded {len(text)} characters from apple_10k_excerpt.txt")

    llm = get_llm()
    embedder = get_embedder()

    with get_driver() as driver:
        # Clear existing data
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted")
            deleted = result.single()["deleted"]
            if deleted > 0:
                print(f"Cleared {deleted} existing nodes")

        # Build the knowledge graph
        print("\nBuilding knowledge graph with SimpleKGPipeline...")
        print("  - Splitting text into chunks")
        print("  - Generating embeddings for each chunk")
        print("  - Extracting entities and relationships via LLM")
        print("  - Writing to Neo4j")
        print("  - Resolving duplicate entities\n")

        # Smaller chunks so different queries match different sections of
        # the filing, making the retrieval comparison more visible.
        text_splitter = FixedSizeSplitter(chunk_size=500, chunk_overlap=100)

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
            from_pdf=False,
            neo4j_database=NEO4J_DATABASE,
            # Entity resolution merges nodes with the same label and name
            # property, preventing duplicates like "Apple" and "Apple Inc."
            perform_entity_resolution=True,
        )

        result = await kg_builder.run_async(
            text=text,
            document_metadata={"source": "SEC EDGAR", "filing_type": "10-K"},
        )
        print(f"Pipeline result: {result}\n")

        # Create indexes on Chunk nodes for retrieval
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

        print(f"Creating fulltext index '{FULLTEXT_INDEX_NAME}'...")
        create_fulltext_index(
            driver,
            name=FULLTEXT_INDEX_NAME,
            label="Chunk",
            node_properties=["text"],
        )
        print("Fulltext index created.\n")

        # Show what was built
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

    await llm.async_client.close()


if __name__ == "__main__":
    asyncio.run(build_graph())
