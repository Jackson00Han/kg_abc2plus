// Immutable source/conversion audit, separate from compiled T-Box responses.
CREATE CONSTRAINT tbox_source_file_id_unique IF NOT EXISTS
FOR (source:TBoxSourceFile) REQUIRE source.source_file_id IS UNIQUE;

CREATE CONSTRAINT tbox_source_file_identity_unique IF NOT EXISTS
FOR (source:TBoxSourceFile) REQUIRE
    (source.tenant_id, source.tbox_id, source.sha256, source.normalization_checksum) IS UNIQUE;
