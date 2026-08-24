CREATE EXTENSION IF NOT EXISTS pgcrypto;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64);
UPDATE knowledge_documents
SET content_sha256 = encode(digest(document_id::text || ':' || source_name, 'sha256'), 'hex')
WHERE content_sha256 IS NULL;
ALTER TABLE knowledge_documents ALTER COLUMN content_sha256 SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_content_identity_uq
    ON knowledge_documents (organization_id, source_name, content_sha256);
