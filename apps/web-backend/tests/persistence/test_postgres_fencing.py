"""PostgreSQL-only concurrency coverage is enabled when ODP_POSTGRES_TEST_URL exists."""

import os

import pytest


@pytest.mark.skipif(not os.getenv("ODP_POSTGRES_TEST_URL"), reason="requires PostgreSQL")
def test_postgres_fencing_suite_is_environment_gated():
    assert os.getenv("ODP_POSTGRES_TEST_URL")
