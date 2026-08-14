"""resolve_dataset_path 行为契约测试."""
from __future__ import annotations

import pytest

from odp_platform.common.dataset_path import resolve_dataset_path


def test_absolute_path_returned_as_is(tmp_path):
    yaml = tmp_path / "rsod.yaml" ; yaml.write_text("path: ...")
    assert resolve_dataset_path(yaml) == yaml


def test_filename_falls_back_to_dataset_configs_dir(tmp_path, monkeypatch):
    fake_dir = tmp_path / "datasets" ; fake_dir.mkdir()
    (fake_dir / "rsod.yaml").write_text("path: ...")
    monkeypatch.setattr(
        "odp_platform.common.dataset_path.DATASET_CONFIGS_DIR", fake_dir
    )
    assert resolve_dataset_path("rsod.yaml") == fake_dir / "rsod.yaml"


def test_not_found_returns_original_with_warning(caplog):
    result = resolve_dataset_path("nonexistent.yaml")
    assert str(result) == "nonexistent.yaml"
    assert "未在 DATASET_CONFIGS_DIR 找到" in caplog.text
