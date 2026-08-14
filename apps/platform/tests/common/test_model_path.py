"""resolve_model_path 行为契约测试."""
from __future__ import annotations

import pytest
from pathlib import Path

from odp_platform.common.model_path import resolve_model_path

# ──── 默认行为(不传 search_dirs) ────────────────────────────────


def test_absolute_path_returned_as_is(tmp_path):
    """绝对路径直接用, 不查 search_dirs."""
    abs_path = tmp_path / "anywhere" / "yolo11n.pt"
    result = resolve_model_path(abs_path)
    assert result == abs_path


def test_filename_falls_back_to_pretrained(tmp_path, monkeypatch):
    """仅文件名默认查 PRETRAINED_MODELS_DIR."""
    fake_pretrained = tmp_path / "pretrained"
    fake_pretrained.mkdir()
    (fake_pretrained / "yolo11n.pt").write_bytes(b"")
    monkeypatch.setattr(
        "odp_platform.common.model_path.PRETRAINED_MODELS_DIR", fake_pretrained
    )
    result = resolve_model_path("yolo11n.pt")
    assert result == fake_pretrained / "yolo11n.pt"


def test_not_found_returns_original(caplog):
    """找不到时返回原值, 不 raise(让 ultralytics 自己处理)."""
    result = resolve_model_path("definitely-not-exists.pt")
    assert str(result) == "definitely-not-exists.pt"
    assert "未在任何搜索目录命中" in caplog.text


# ──── search_dirs 升级 (D7/D8 接口) ──────────────────────────────


def test_search_dirs_first_dir_hit(tmp_path):
    """search_dirs 第 1 个目录命中, 不查后面."""
    dir1 = tmp_path / "a" ; dir1.mkdir() ; (dir1 / "x.pt").write_bytes(b"")
    dir2 = tmp_path / "b" ; dir2.mkdir() ; (dir2 / "x.pt").write_bytes(b"")
    result = resolve_model_path("x.pt", search_dirs=[dir1, dir2])
    assert result == dir1 / "x.pt"           # 第 1 个就命中


def test_search_dirs_fallback_to_second(tmp_path):
    """search_dirs 第 1 个没有, fallback 到第 2 个."""
    dir1 = tmp_path / "ckpt" ; dir1.mkdir()
    dir2 = tmp_path / "pretrained" ; dir2.mkdir()
    (dir2 / "yolo11n.pt").write_bytes(b"")   # 只在第 2 个目录
    result = resolve_model_path(
        "yolo11n.pt", search_dirs=[dir1, dir2]
    )
    assert result == dir2 / "yolo11n.pt"


def test_search_dirs_none_equals_default(tmp_path, monkeypatch):
    """search_dirs=None 等价于不传, 走默认 [PRETRAINED_MODELS_DIR]."""
    fake_pre = tmp_path / "pre" ; fake_pre.mkdir()
    (fake_pre / "y.pt").write_bytes(b"")
    monkeypatch.setattr(
        "odp_platform.common.model_path.PRETRAINED_MODELS_DIR", fake_pre
    )
    explicit_none = resolve_model_path("y.pt", search_dirs=None)
    omitted       = resolve_model_path("y.pt")
    assert explicit_none == omitted == fake_pre / "y.pt"
