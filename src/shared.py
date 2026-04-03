"""Shared retrieval query and formatter for graph-enriched retrieval.

Used by 03_vector_cypher_retriever.py, 04_hybrid_cypher_retriever.py,
and 05_graphrag_qa.py. Extracting these into a shared module avoids
duplicating the Cypher query and formatting logic across scripts.
"""

import neo4j
from neo4j_graphrag.types import RetrieverResultItem

# ---------------------------------------------------------------------------
# Cypher retrieval query
# ---------------------------------------------------------------------------
# When a VectorCypherRetriever (or HybridCypherRetriever) finds matching
# Chunk nodes via vector/fulltext search, it then runs THIS Cypher query
# to pull in additional graph context.
#
# The query starts from the matched chunk (provided as `node` by the
# retriever) and traverses two relationship types that SimpleKGPipeline
# creates:
#
#   (entity)-[:FROM_CHUNK]->(chunk)   — entity was extracted from this chunk
#   (chunk)-[:FROM_DOCUMENT]->(doc)   — chunk belongs to this document
#
# For each chunk, we collect:
#   1. The chunk text itself
#   2. The similarity score from the vector/hybrid search
#   3. The source metadata from the parent Document node
#   4. A list of entity labels and names (e.g. "Product: iPhone")
#
# We filter out internal labels (__KGBuilder__, __Entity__) that the
# library adds to all nodes, keeping only domain-specific types like
# Product, RiskFactor, Company.
# ---------------------------------------------------------------------------
RETRIEVAL_QUERY = """
WITH node AS chunk, score
OPTIONAL MATCH (chunk)-[:FROM_DOCUMENT]->(doc:Document)
OPTIONAL MATCH (entity)-[:FROM_CHUNK]->(chunk)
WITH chunk, score, doc, entity,
     [l IN labels(entity) WHERE NOT l IN ['__KGBuilder__', '__Entity__']][0] AS entity_label
WITH chunk, score, doc,
     collect(DISTINCT CASE WHEN entity_label IS NOT NULL
         THEN entity_label + ': ' + entity.name END) AS entities
RETURN
    chunk.text AS text,
    score,
    doc.source AS source,
    [e IN entities WHERE e IS NOT NULL] AS entities
"""


def formatter(record: neo4j.Record) -> RetrieverResultItem:
    """Format a Neo4j record into a RetrieverResultItem with entity context.

    The retriever returns raw Neo4j records. This function shapes each
    record into the format that GraphRAG (or direct printing) expects:
    the chunk text, followed by any related entities and the document source.

    This is what the LLM sees as context when generating answers — so the
    format here directly affects answer quality.
    """
    text = record.get("text", "")
    entities = record.get("entities", [])
    source = record.get("source", "")

    # Build a multi-line string: chunk text first, then entity context
    parts = [text]
    if entities:
        parts.append(f"Related entities: {', '.join(entities)}")
    if source:
        parts.append(f"Source: {source}")

    return RetrieverResultItem(
        content="\n".join(parts),
        metadata={"score": record.get("score", 0)},
    )
