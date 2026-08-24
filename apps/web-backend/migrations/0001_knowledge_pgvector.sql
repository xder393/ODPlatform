-- Task 8 standalone migration SQL. Apply through the deployment migration runner
-- when Alembic (or the chosen runner) is introduced for this backend.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE knowledge_documents (
    document_id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    source_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    media_type TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('INDEXED', 'SUPERSEDED', 'FAILED')),
    indexed_at TIMESTAMPTZ NOT NULL,
    applicable_line_id UUID,
    product_category TEXT,
    failure_reason TEXT,
    UNIQUE (organization_id, source_name, version)
);
CREATE INDEX knowledge_documents_tenant_status_idx
    ON knowledge_documents (organization_id, status, source_name, version DESC);

CREATE TABLE knowledge_parent_chunks (
    parent_chunk_id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES knowledge_documents(document_id),
    organization_id UUID NOT NULL,
    text TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    paragraph_number INTEGER NOT NULL CHECK (paragraph_number > 0)
);
CREATE INDEX knowledge_parent_chunks_tenant_document_idx
    ON knowledge_parent_chunks (organization_id, document_id);

CREATE TABLE knowledge_chunk_index (
    chunk_id UUID PRIMARY KEY,
    parent_chunk_id UUID NOT NULL REFERENCES knowledge_parent_chunks(parent_chunk_id),
    document_id UUID NOT NULL REFERENCES knowledge_documents(document_id),
    organization_id UUID NOT NULL,
    text TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    paragraph_number INTEGER NOT NULL CHECK (paragraph_number > 0),
    child_index INTEGER NOT NULL CHECK (child_index >= 0),
    embedding vector(64) NOT NULL
);
CREATE INDEX knowledge_chunk_index_tenant_document_idx
    ON knowledge_chunk_index (organization_id, document_id);
CREATE INDEX knowledge_chunk_index_embedding_hnsw_idx
    ON knowledge_chunk_index USING hnsw (embedding vector_cosine_ops);
CREATE INDEX knowledge_chunk_index_lexical_idx
    ON knowledge_chunk_index USING gin (to_tsvector('simple', text));
