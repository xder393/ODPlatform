# Task 8 report: versioned tenant-scoped knowledge ingestion

## Delivered

- Added `KnowledgeDocument` versions with `INDEXED`, `SUPERSEDED`, and `FAILED` states. A successful replacement marks the previous indexed version for the same organization and source name as `SUPERSEDED`; failed uploads are recorded without indexing partial chunks.
- Added validated PDF and DOCX extraction. PDF uses `pypdf` when supplied by a deployment, with a deterministic standard-library fallback for uncompressed text PDFs. DOCX validation requires a valid OOXML package and extracts paragraph text from `word/document.xml`.
- Added parent paragraphs and overlapping child chunks. Every child records its document version context, organization, parent ID, source page, and source paragraph.
- Added the `RAGRetrievalPort` and a deterministic `PgVectorRetrievalAdapter` test adapter. Index rows validate organization consistency; `search` requires an organization UUID and `RetrievalFilters`, applies organization before scoring, and only returns documents matching the requested status, line, and product constraints.
- Hybrid retrieval combines deterministic vector cosine similarity and BM25 lexical relevance after independently normalizing both scores. The production SQL contract includes a bound `WHERE organization_id = %(organization_id)s` predicate.

## Tests

- RED: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/knowledge/test_ingest.py -v` initially failed during collection because the retrieval adapter and ingestion modules did not exist.
- GREEN: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/knowledge/test_ingest.py -v` — 5 passed.
- Regression: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v` — 54 passed, with one pre-existing Starlette deprecation warning.
- Syntax: `apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api` — exit 0.
- Static checks: `uvx ruff check --select F,I,UP` for the Task 8 source and tests — clean.
- Whitespace: `git diff --check` — clean.

## Migration note

No PostgreSQL migration was runnable: the repository has no Alembic configuration or migrations directory, and Alembic is not installed in the backend environment. The port plus deterministic adapter preserves the required schema/query boundary until a PostgreSQL migration stack is introduced; its SQL contract explicitly binds the mandatory organization scope.

## Scope

This task intentionally does not implement LLM generation, advice responses, or routes; those belong to Task 9.
