#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : log_rename.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 训练结束后, 把 D2 'odp_platform' 根 logger 的日志文件名跟 ultralytics save_dir 对齐
"""日志文件重命名.

ultralytics 训练完才知道 save_dir 的实际名字 (train / train2 / train3 ...),
而日志在训练**开始之前**就要建立, 名字只能是占位.

本模块在训练结束后, 把已经在写的日志文件 rename 成跟 save_dir 对齐的名字,
**同时**把对应的 FileHandler 重定向到新文件, 让训练结束之后的日志(归档/审计/
最终统计)依然能写进同一份日志.

★ 设计纪律:
  - 操作对象是 D2 'odp_platform' **named root logger**, 不是 unnamed root.
  - 调用方不需要持有 FileHandler — 本模块自己去 named root 上找.

命名格式:
  原文件名(D2 get_logger 产出): train_<timestamp>.log
  新文件名:                     <save_dir.name>_<timestamp>_<model_stem>.log
  例: train3_20260524-001234-567_yolo11n.log
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

# 跟 D2 logging_utils.ROOT_LOGGER_NAME 对齐 (硬编码避免循环依赖)
ROOT_LOGGER_NAME: str = "odp_platform"

logger = logging.getLogger(__name__)

# 匹配 D2 时间戳格式: 20260524-001234 后可能跟 -<微秒前几位>
_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6}(?:-\d+)?)")


def rename_log_to_save_dir(
    save_dir: Path,
    model_stem: str,
) -> Path | None:
    """把 'odp_platform' 根 logger 的 FileHandler 改名跟 save_dir 对齐.

    Args:
        save_dir:   ultralytics 实际 save_dir (e.g. runs/detect_train/train3)
        model_stem: 模型 stem (e.g. 'yolo11n', 用于新文件名)

    Returns:
        新文件 Path. 失败时返回 None (失败原因通过 logger.warning 输出).

    永不抛异常 — 改名失败靠 logger.warning 表达, 不影响训练结果本身.
    """
    root = logging.getLogger(ROOT_LOGGER_NAME)

    # 1. 在 named root 上找 FileHandler
    file_handler = next(
        (h for h in root.handlers if isinstance(h, logging.FileHandler)),
        None,
    )
    if file_handler is None:
        logger.warning(
            f"'{ROOT_LOGGER_NAME}' 根 logger 上没有 FileHandler, "
            f"跳过日志改名 (CLI 入口可能没调 get_logger?)"
        )
        return None

    old_path = Path(file_handler.baseFilename)

    # 2. 从原文件名提取时间戳
    match = _TIMESTAMP_RE.search(old_path.stem)
    if match:
        timestamp = match.group(1)
    else:
        timestamp = "unknown-time"
        logger.warning(f"原日志文件名缺时间戳, 用占位符: {old_path.name}")

    new_name = f"{save_dir.name}_{timestamp}_{model_stem}.log"
    new_path = old_path.parent / new_name

    if new_path == old_path:
        return old_path     # 已经对齐, 不重复操作

    # 3. 保存旧 handler 配置给新 handler 复用
    formatter = file_handler.formatter
    level = file_handler.level
    encoding = getattr(file_handler, "encoding", None) or "utf-8"

    # 4. 关闭旧 handler 释放文件句柄(Windows 必须先关才能 rename)
    file_handler.close()
    root.removeHandler(file_handler)

    # 5. 物理 rename
    if not old_path.exists():
        logger.warning(f"旧日志文件不存在, 无法改名: {old_path}")
        return None

    try:
        old_path.rename(new_path)
    except OSError as e:
        logger.warning(f"日志 rename 失败 ({e}), 尝试恢复旧 handler 继续写...")
        # 失败回滚: 重新挂回旧文件, 确保后续日志不丢
        try:
            restored = logging.FileHandler(old_path, encoding=encoding)
            if formatter:
                restored.setFormatter(formatter)
            restored.setLevel(level)
            root.addHandler(restored)
        except OSError as e2:
            logger.error(f"回滚 handler 也失败 ({e2}) — 后续日志可能丢失")
        return None

    # 6. 新 handler 指向新文件
    try:
        new_handler = logging.FileHandler(new_path, encoding=encoding)
        if formatter:
            new_handler.setFormatter(formatter)
        new_handler.setLevel(level)
        root.addHandler(new_handler)
    except OSError as e:
        logger.error(
            f"创建新 FileHandler 失败 ({e}) — 文件已改名, 但后续日志写不进新文件"
        )
        return new_path

    logger.info(f"日志文件已重命名: {new_path.name}")
    return new_path
