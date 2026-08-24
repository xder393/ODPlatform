import pytest

from odp_api import seed_knowledge


def test_knowledge_seed_cli_requires_explicit_postgres_dsn(monkeypatch) -> None:
    monkeypatch.delenv("ODP_POSTGRES_URL", raising=False)
    monkeypatch.setenv("ODP_RETRIEVAL_BACKEND", "pgvector")

    with pytest.raises(SystemExit, match="ODP_POSTGRES_URL"):
        seed_knowledge.main()
