#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : archive.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 训练完归档 best/last.pt 到 CHECKPOINTS_DIR
"""权重归档.

ultralytics 训练完产出 `<train_dir>/weights/best.pt` 和 `last.pt`,
本模块把它们复制一份到 ODPlatform 的 CHECKPOINTS_DIR (权重归档 SSoT),
重命名带上时间戳和训练目录后缀, 防止跨实验覆盖.

命名格式: `<train_dir_name>-<timestamp>-<model_stem>-<best|last>.pt`
例: `train3-20260523-103045-yolo11n-best.pt`

归档逻辑跟 ultralytics 的输出完全解耦:
  - 原文件保留在 train_dir/weights/ (供 ultralytics resume/val 用)
  - 归档文件供 D7 ValService / D8 InferService 通过 CHECKPOINTS_DIR 引用
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from odp_platform.common.paths import CHECKPOINTS_DIR

logger = logging.getLogger(__name__)


def archive_checkpoints(
    train_dir: Path,
    model_filename: str | Path,
    *,
    checkpoint_dir: Path | None = None,
) -> dict[str, Path]:
    """归档 best.pt 和 last.pt 到 CHECKPOINTS_DIR.

    Args:
        train_dir:       ultralytics 训练输出目录 (e.g. runs/detect_train/train3)
        model_filename:  模型名 (用来生成归档文件名, e.g. 'yolo11n.pt' → 'yolo11n')
        checkpoint_dir:  归档目录 (默认 CHECKPOINTS_DIR, 测试时可注入临时目录)

    Returns:
        {'best': PosixPath('.../train3-20260523-103045-yolo11n-best.pt'),
         'last': PosixPath('.../train3-20260523-103045-yolo11n-last.pt')}
        某个文件不存在 / 复制失败时, 该 key 不在返回字典里(best-effort).

    永不抛异常 — train_dir 不存在 / 权限错误 / 磁盘满都靠 logger.warning 表达,
    返回空字典让调用方决定下一步.
    """
    checkpoint_dir = checkpoint_dir or CHECKPOINTS_DIR
    results: dict[str, Path] = {}

    # 防御 1: train_dir 必须存在且是目录
    if not train_dir.is_dir():
        logger.warning(f"训练目录不存在或不是目录, 跳过归档: {train_dir}")
        return results

    # 防御 2: 归档目录 mkdir(idempotent)
    try:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"创建归档目录失败, 跳过归档: {e}")
        return results

    # 准备命名组件
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_model_name = Path(model_filename).stem      # 'yolo11n.pt' → 'yolo11n'
    train_suffix = train_dir.name                    # 'train3'

    # 逐个复制 best / last
    for model_type in ("best", "last"):
        src_path = train_dir / "weights" / f"{model_type}.pt"
        if not src_path.exists():
            logger.warning(f"未找到权重文件, 跳过: {src_path}")
            continue

        dest_name = f"{train_suffix}-{timestamp}-{base_model_name}-{model_type}.pt"
        dest_path = checkpoint_dir / dest_name

        try:
            shutil.copy2(src_path, dest_path)        # copy2 保留 mtime/permissions
            logger.info(f"权重已归档: {dest_path.name}")
            results[model_type] = dest_path
        except (OSError, shutil.Error) as e:
            logger.warning(f"归档 {model_type}.pt 失败: {e}")

    return results
