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

## Fix round 1

- Added `apps/web-backend/migrations/0001_knowledge_pgvector.sql`, which enables pgvector and creates tenant-scoped document, parent-chunk, and child-chunk tables. It includes the tenant/document index, HNSW cosine vector index, and lexical GIN index.
- Replaced the previous SQL documentation constant with `PgVectorPostgresAdapter`: it persists versions and chunks through a parameterized executor port and executes the hybrid retrieval SQL. The query applies the organization filter before both scoring paths, calculates raw cosine and BM25 values, independently normalizes each component, and returns their combined score.
- Added deterministic adapter-contract tests that invoke the production adapter with a recording executor. They prove bound query parameters contain the requested organization, status, line/product filters, query vector, and limit; they also prove all document/parent/child persistence operations carry the organization ID.
- Added required `pypdf>=5.0.0` backend dependency and removed the fallback parser. The two-page fixture is now a `pypdf`-written Flate-compressed PDF, and malformed parser input is recorded as a failed upload.
- Strengthened DOCX validation by parsing `[Content_Types].xml`, package-level `_rels/.rels`, and the WordprocessingML root/body. A ZIP lacking the required office-document relationship is rejected.

### Fix-round verification

```text
$ apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/knowledge/test_ingest.py -v
9 passed in 0.04s

$ apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
58 passed, 1 warning in 0.26s

$ apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

$ uvx ruff check --select F,I,UP <Task 8 changed source and tests>
All checks passed!

$ git diff --check
exit 0
```

`psql`, `postgres`, and `initdb` are unavailable in this worktree, so the pgvector migration cannot be executed locally. The executable contract tests cover its adapter boundary; a deployment PostgreSQL 16 + pgvector instance must apply the migration before wiring `PgVectorPostgresAdapter` into runtime composition.

## Fix round 2

- Added `PGVECTOR_EMBEDDING_DIMENSIONS = 64` as the adapter’s single embedding-dimension contract, synchronized with the pgvector migration. Query embeddings must have exactly 64 finite values before database execution; indexing precomputes and validates every child embedding before issuing supersession or insert statements.
- Updated production-adapter tests to use 64-dimensional vectors. The two-dimensional mismatch regression proves both search and indexing fail with no executor calls.
- DOCX package validation now accepts an `officeDocument` relationship only when `TargetMode` is absent or exactly `Internal`. The external-target spoof regression is rejected as a failed document.

### Fix-round verification

```text
$ apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/knowledge/test_ingest.py -v
11 passed in 0.03s

$ apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
60 passed, 1 warning in 0.27s

$ apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

$ uvx ruff check --select F,I,UP <Task 8 changed source and tests>
All checks passed!

$ git diff --check
exit 0
```
