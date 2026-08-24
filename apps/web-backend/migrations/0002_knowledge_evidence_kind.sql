-- Explicit evidence classification is required for confidence-tiered retrieval.
ALTER TABLE knowledge_documents
    ADD COLUMN evidence_kind TEXT
    CHECK (evidence_kind IN ('CURRENT_SPECIFICATION', 'HISTORICAL_CASE'));

CREATE INDEX knowledge_documents_evidence_scope_idx
    ON knowledge_documents (
        organization_id,
        status,
        evidence_kind,
        applicable_line_id,
        product_category
    );
