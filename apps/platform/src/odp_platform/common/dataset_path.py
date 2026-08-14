#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : dataset_path.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 解析数据集 yaml 路径 — 绝对路径 / 仅文件名 fallback 到 DATASET_CONFIGS_DIR
"""数据集 yaml 路径解析.

用户在 train.yaml 或 CLI 里通常写 `data: rsod.yaml` 这种**仅文件名**形式,
期望从项目的 DATASET_CONFIGS_DIR (apps/platform/configs/datasets/) 加载.
本模块负责把这种简写解析成实际绝对路径, 解析不到时返回原值
(让 D4 validate_dataset / ultralytics 自己报"找不到").

策略跟 resolve_model_path 同款 3 分支, 但**没有 search_dirs 参数** —
数据集配置目录只有一个 SSoT (DATASET_CONFIGS_DIR), D6/D7/D8 用法一致.
"""
from __future__ import annotations

import logging
from pathlib import Path

from odp_platform.common.paths import DATASET_CONFIGS_DIR

logger = logging.getLogger(__name__)


def resolve_dataset_path(data: str | Path) -> Path:
    """把数据集 yaml 名/路径解析成实际 Path."""
    data_path = Path(data)

    # 分支 1: 绝对路径
    if data_path.is_absolute():
        return data_path

    # 分支 2: 仅文件名 → 查 DATASET_CONFIGS_DIR
    config_candidate = DATASET_CONFIGS_DIR / data_path.name
    if config_candidate.exists():
        logger.info(f"从数据集配置目录加载: {config_candidate}")
        return config_candidate

    # 分支 3: 都没命中 → 让下游报错
    logger.warning(
        f"数据集 yaml 未在 DATASET_CONFIGS_DIR 找到: {data_path.name}\n"
        f"  DATASET_CONFIGS_DIR: {DATASET_CONFIGS_DIR}\n"
        f"  D4 / ultralytics 接下来会按 '{data_path}' 原样解析, 可能报'找不到文件'."
    )
    return data_path
