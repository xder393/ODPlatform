-- Forward fix for databases that already recorded an earlier 0003 containing
-- a full unique index. Retain all historical failures/superseded revisions;
-- only a reusable active document must be unique.
DROP INDEX IF EXISTS knowledge_documents_content_identity_uq;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_content_identity_uq
    ON knowledge_documents (organization_id, source_name, content_sha256)
    WHERE status = 'INDEXED';
