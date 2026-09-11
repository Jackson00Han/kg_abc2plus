// Published text is selected by immutable version membership, independently
// from ingestion's latest-version pointers. Keep v2 for migration safety.
CREATE FULLTEXT INDEX graphrag_chunk_text_v3 IF NOT EXISTS
FOR (node:Chunk) ON EACH [node.text, node.publication_scope];
