// BM25 v2 ranks only candidates already partitioned by tenant, active
// publication, and access group.  The v1 index remains for migration safety
// but production retrieval never queries it.
CREATE FULLTEXT INDEX graphrag_chunk_text_v2 IF NOT EXISTS
FOR (node:Chunk) ON EACH [node.text, node.retrieval_scope];
