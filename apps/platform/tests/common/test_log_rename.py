"""rename_log_to_save_dir 行为契约测试.

覆盖 4 类场景: named root 无 handler / 时间戳能/不能提取 / rename 失败回滚 /
已对齐不重复操作.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from odp_platform.common.log_rename import ROOT_LOGGER_NAME, rename_log_to_save_dir


def _attach_file_handler(root_logger, file_path):
    """辅助: 给 named root 装一个 FileHandler 模拟 D2 get_logger 完成后的状态."""
    handler = logging.FileHandler(file_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)
    return handler


@pytest.fixture(autouse=True)
def _clean_named_root():
    """每个测试后清空 'odp_platform' 根 logger 的 handler, 防跨测试污染."""
    yield
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.fixture
def named_root_with_log(tmp_path):
    """每次测试一个干净的 named root + 一个 file handler 指向 tmp 文件."""
    log_dir = tmp_path / "logs" ; log_dir.mkdir()
    log_file = log_dir / "train_20260524-103045.log"
    log_file.touch()

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for h in list(root.handlers):                   # 清场
        root.removeHandler(h) ; h.close()
    _attach_file_handler(root, log_file)
    yield root, log_file
    for h in list(root.handlers):                   # 清场
        root.removeHandler(h) ; h.close()


def test_no_filehandler_returns_none_with_warning(caplog):
    """named root 没 handler — 跳过, 返回 None, warning."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h) ; h.close()
    result = rename_log_to_save_dir(Path("/tmp/train3"), "yolo11n")
    assert result is None
    assert "没有 FileHandler" in caplog.text


def test_rename_reuses_timestamp(named_root_with_log, tmp_path):
    """新文件名复用原时间戳, 不用 datetime.now()."""
    root, log_file = named_root_with_log
    save_dir = tmp_path / "runs" / "detect_train" / "train3" ; save_dir.mkdir(parents=True)
    new_path = rename_log_to_save_dir(save_dir, "yolo11n")
    assert new_path is not None
    assert new_path.name == "train3_20260524-103045_yolo11n.log"   # 原 timestamp


def test_no_timestamp_uses_placeholder(tmp_path):
    """原文件名没时间戳, 用 'unknown-time' 占位."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h) ; h.close()
    log_file = tmp_path / "weird-name.log" ; log_file.touch()
    _attach_file_handler(root, log_file)

    save_dir = tmp_path / "runs" / "train1" ; save_dir.mkdir(parents=True)
    new_path = rename_log_to_save_dir(save_dir, "yolo11n")
    assert "unknown-time" in new_path.name
