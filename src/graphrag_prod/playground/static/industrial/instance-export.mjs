/** Validate the complete server snapshot before creating a download. */
export function instanceExportFile(payload, {documentId = '', publicationId} = {}) {
  if (payload?.format !== 'graphrag-abox-export-v1'
    || payload.truncated !== false
    || !Array.isArray(payload.items)
    || !Number.isSafeInteger(payload.total_record_count)
    || !Number.isSafeInteger(payload.matching_record_count)
    || payload.items.length !== payload.matching_record_count
    || payload.total_record_count < payload.matching_record_count
    || (!documentId && payload.total_record_count !== payload.matching_record_count)
    || (payload.document_id || '') !== documentId
    || payload.scope !== (documentId ? 'document' : 'publication')
    || !payload.tenant_id || !payload.publication_id || !payload.ontology_version_id
    || !payload.manifest_hash || !payload.exported_at
    || !Number.isSafeInteger(payload.publication_generation) || payload.publication_generation < 1
    || (publicationId && payload.publication_id !== publicationId)
    || payload.items.some(item => !item.record_id || !item.revision_id || !item.evidence
      || (documentId && item.evidence.document_id !== documentId))
    || new Set(payload.items.map(item => item.revision_id)).size !== payload.items.length) {
    throw new Error('导出数据不完整或版本、筛选已变化，请刷新后重试。');
  }
  const safeId = payload.publication_id.replace(/[^a-zA-Z0-9_-]/g, '_');
  return {
    name: `knowledge-instances-v${payload.publication_generation}-${safeId}${documentId ? '-filtered' : ''}.json`,
    text: `${JSON.stringify(payload, null, 2)}\n`,
  };
}
