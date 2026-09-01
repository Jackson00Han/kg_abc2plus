// BM25 recall. Authorization and active-version predicates remain in every
// query that consumes this global candidate index.
CREATE FULLTEXT INDEX graphrag_chunk_text_v1 IF NOT EXISTS
FOR (node:Chunk) ON EACH [node.text];

CREATE INDEX chunk_retrieval_scope_lookup IF NOT EXISTS
FOR (node:Chunk) ON (node.tenant_id, node.document_id, node.version_id);
