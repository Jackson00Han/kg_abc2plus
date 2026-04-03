"""Shared retrieval query and formatter for vector-cypher retrieval.

Used by 03_vector_cypher_retriever.py, 04_hybrid_cypher_retriever.py,
and 05_graphrag_qa.py.
"""

import neo4j
from neo4j_graphrag.types import RetrieverResultItem

# After vector search finds matching Chunks, this Cypher query traverses
# the graph to collect structured context: the parent document and any
# entities extracted from that chunk.
#
# SimpleKGPipeline creates:
#   (entity)-[:FROM_CHUNK]->(chunk)   entity was extracted from this chunk
#   (chunk)-[:FROM_DOCUMENT]->(doc)   chunk belongs to this document
#
# We filter out internal labels (__KGBuilder__, __Entity__) to show only
# the domain-specific entity types (Product, RiskFactor, Company, etc.).
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
    """Format a neo4j Record into a RetrieverResultItem with entity context."""
    text = record.get("text", "")
    entities = record.get("entities", [])
    source = record.get("source", "")

    parts = [text]
    if entities:
        parts.append(f"Related entities: {', '.join(entities)}")
    if source:
        parts.append(f"Source: {source}")

    return RetrieverResultItem(
        content="\n".join(parts),
        metadata={"score": record.get("score", 0)},
    )
