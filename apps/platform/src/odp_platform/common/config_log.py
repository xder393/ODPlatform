#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : config_log.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 按字段维度打印配置参数信息 / 配置覆盖情况
"""配置参数日志输出.

跟 D5 的 get_source_report (按来源分组) 互补 — 本模块按字段一行展示.
"""
from __future__ import annotations

import logging
from typing import Any

from odp_platform.common.string_utils import pad_to_width


def log_effective_config(
    config: Any,
    merger: Any,
    *,
    logger: logging.Logger | None = None,
    key_width: int = 20,
    section_width: int = 60,
) -> None:
    """打印"配置参数信息" — 每个字段当前生效值 + 来源, 一行一个."""
    log = logger or logging.getLogger(__name__)

    log.info("=" * section_width)
    log.info("配置参数信息".center(section_width))
    log.info("-" * section_width)

    for field_name in config.__class__.model_fields.keys():
        value = getattr(config, field_name, None)
        meta = _safe_get_metadata(merger, field_name)
        source_label = meta.source_label if meta is not None else "未知"
        log.info(
            f"{pad_to_width(field_name, key_width)}: {value}  "
            f"(来源: {source_label})"
        )


def log_override_chains(
    config: Any,
    merger: Any,
    *,
    logger: logging.Logger | None = None,
    key_width: int = 20,
    section_width: int = 60,
) -> None:
    """打印"配置覆盖情况" — 每个字段的完整来源链(DEFAULT → YAML → CLI 顺序).

    跟 D5 get_conflict_report 的差别:
        - get_conflict_report 只展示**被覆盖**的字段, 只显示最近一次覆盖
        - 本函数展示**所有**字段(覆盖与否都有, 看出"为什么这值是这值")
        - 本函数完整链不只一步
        - 顺序是 oldest→newest 跟用户阅读习惯一致
    """
    log = logger or logging.getLogger(__name__)

    log.info("-" * section_width)
    log.info("配置覆盖情况".center(section_width))
    log.info("-" * section_width)

    for field_name in config.__class__.model_fields.keys():
        meta = _safe_get_metadata(merger, field_name)
        if meta is None:
            value = getattr(config, field_name, None)
            log.info(f"{pad_to_width(field_name, key_width)}: {value}")
            continue

        # D5 chain() 是 newest-first, reverse 成 oldest-first
        chain = list(reversed(meta.chain()))
        chain_str = " <- ".join(f"{m.value}({m.source_label})" for m in chain)
        log.info(f"{pad_to_width(field_name, key_width)}: {chain_str}")


def _safe_get_metadata(merger: Any, field_name: str) -> Any:
    """get_metadata 的防御性封装.

    merger 是 D5 的 ConfigMerger, 但万一测试时传了 mock 没这个方法, 不要崩.
    """
    if not hasattr(merger, "get_metadata"):
        return None
    try:
        return merger.get_metadata(field_name)
    except Exception:
        return None
