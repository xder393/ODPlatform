"""Explicit, idempotent bootstrap for the runtime knowledge database."""

from __future__ import annotations

import os

from odp_api.adapters.retrieval.embedding import hash_embedding
from odp_api.adapters.retrieval.pgvector import (
    PgVectorPostgresAdapter,
    psycopg_executor,
)
from odp_api.main import _ingest_seed_documents
from odp_api.seed import build_demo_seed
from odp_api.settings import Settings


def main() -> None:
    settings = Settings()
    if settings.retrieval_backend != "pgvector":
        raise SystemExit("ODP_RETRIEVAL_BACKEND must be pgvector for knowledge seeding.")
    postgres_url = os.environ.get("ODP_POSTGRES_URL", "").strip()
    if not postgres_url:
        raise SystemExit("ODP_POSTGRES_URL must be explicitly configured for knowledge seeding.")
    index = PgVectorPostgresAdapter(psycopg_executor(postgres_url), embed=hash_embedding)
    _ingest_seed_documents(index, build_demo_seed())
    print("Knowledge seed completed.")


if __name__ == "__main__":
    main()
