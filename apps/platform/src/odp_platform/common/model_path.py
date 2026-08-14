#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : model_path.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 解析 YOLO 模型路径 — 绝对路径 / 仅文件名 fallback 到 search_dirs 列表
"""模型路径解析.

策略(3 个分支, 从具体到 fallback):
  1. 绝对路径 → 直接用
  2. 仅文件名 → 在 search_dirs 里依次找, 命中即用
  3. 都没命中 → 返回原值, 让 ultralytics 走自己的下载/搜索逻辑
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from odp_platform.common.paths import PRETRAINED_MODELS_DIR

logger = logging.getLogger(__name__)


def resolve_model_path(
    model: str | Path,
    *,
    search_dirs: Sequence[Path] | None = None,
) -> Path:
    """把 YOLO 模型名/路径解析成实际 Path."""
    model_path = Path(model)

    # 分支 1: 绝对路径
    if model_path.is_absolute():
        return model_path

    # 分支 2: 仅文件名 → 按顺序查 search_dirs
    dirs: Sequence[Path] = search_dirs if search_dirs is not None else [PRETRAINED_MODELS_DIR]
    for d in dirs:
        candidate = d / model_path.name
        if candidate.exists():
            logger.info(f"模型已定位: {candidate} (来自 {d})")
            return candidate

    # 分支 3: fallback — 让 ultralytics 自己处理
    logger.warning(
        f"模型文件未在任何搜索目录命中: {model_path.name}\n"
        f"  搜索过的目录: {[str(d) for d in dirs]}\n"
        f"  ultralytics 将尝试自动下载或从其他位置加载."
    )
    return model_path
