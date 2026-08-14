#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : __init__.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : evaluation 子系统对外公共 API — 只暴露评估专属符号
"""ODPlatform ``evaluation/`` 子系统对外面板.

跟 D6 training 同款风格 — 外部调用只 import 顶层包, 不碰内部模块路径.

评估指标复用了 ``odp_platform.common.result.TrainMetrics`` (train/val 指标
结构一致), 这里转再导出, 让 ``from odp_platform.evaluation import TrainMetrics``
这个直观路径也能拿到. 跨任务通用工具(model_path / dataset_path / config_log /
log_rename)不在这里重导出 — 需要的模块直接 ``from odp_platform.common.xxx``.
"""
from __future__ import annotations

from .service import ValResult, ValService, val_yolo

from odp_platform.common.result import TrainMetrics

__all__ = [
    "ValService",
    "ValResult",
    "TrainMetrics",
    "val_yolo",
]
