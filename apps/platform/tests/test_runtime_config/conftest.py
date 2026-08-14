"""runtime_config 测试共享 fixture."""
from __future__ import annotations

from pathlib import Path
from typing  import Callable

import pytest


@pytest.fixture
def write_yaml(tmp_path: Path) -> Callable[..., Path]:
    """生成临时 yaml 文件.

    用法:
        def test_xxx(write_yaml):
            f = write_yaml("epochs: 100\nbatch: 16", name="train.yaml")
            ...
    """
    def _make(content: str, name: str = "test.yaml") -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path
    return _make


@pytest.fixture
def yaml_dir(tmp_path: Path) -> Path:
    """提供一个空的 yaml 目录."""
    d = tmp_path / "configs"
    d.mkdir()
    return d


@pytest.fixture
def minimal_train_yaml(write_yaml) -> Path:
    """一份最小的 train yaml — 测合并 / 加载场景常用."""
    return write_yaml("""
model: yolo11n.pt
data: rsod.yaml
epochs: 200
batch: 32
lr0: 0.005
""", name="train.yaml")
