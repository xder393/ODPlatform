"""tests/training/ 共用 fixture.

fake_train_dir 仿真 ultralytics save_dir(含 weights/best.pt / last.pt),
给 archive 测试用.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_train_dir(tmp_path: Path) -> Path:
    """构造一个仿真的 ultralytics save_dir."""
    train_dir = tmp_path / "runs" / "detect_train" / "train3"
    (train_dir / "weights").mkdir(parents=True)
    (train_dir / "weights" / "best.pt").write_bytes(b"fake-best-weights")
    (train_dir / "weights" / "last.pt").write_bytes(b"fake-last-weights")
    return train_dir
