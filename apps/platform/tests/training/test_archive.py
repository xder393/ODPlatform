"""archive_checkpoints 行为契约测试."""
from __future__ import annotations

from odp_platform.training.archive import archive_checkpoints


def test_archive_copies_both(fake_train_dir, tmp_path):
    ckpt_dir = tmp_path / "ckpt"
    result = archive_checkpoints(
        fake_train_dir, "yolo11n.pt", checkpoint_dir=ckpt_dir
    )
    assert "best" in result and "last" in result
    assert result["best"].exists()
    assert "train3" in result["best"].name           # train_dir 名进文件名
    assert "yolo11n" in result["best"].name          # model_stem 进文件名
    assert "best" in result["best"].name


def test_archive_missing_train_dir_returns_empty(tmp_path):
    """train_dir 不存在 — 返回 {}, warning, 不抛."""
    result = archive_checkpoints(
        tmp_path / "nonexistent", "yolo11n.pt", checkpoint_dir=tmp_path / "ckpt"
    )
    assert result == {}


def test_archive_no_best_no_last(tmp_path):
    """train_dir 存在但 weights/ 是空的 — 跳过, 不抛."""
    empty_train = tmp_path / "train1" / "weights" ; empty_train.mkdir(parents=True)
    result = archive_checkpoints(
        tmp_path / "train1", "yolo11n.pt", checkpoint_dir=tmp_path / "ckpt"
    )
    assert result == {}
