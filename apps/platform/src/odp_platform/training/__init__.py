#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : __init__.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : training 子系统对外公共 API — 只暴露训练专属符号
"""ODPlatform ``training/`` 子系统对外面板.

跟 D5 / D4 / D3 同款风格 — 外部调用只 import 顶层包, 不碰内部模块路径.

★ training/ 只放训练专属(TrainService 编排 + archive 归档),
跨任务通用工具(model_path / dataset_path / log_rename / config_log / result /
plot_style)全部放 ``odp_platform.common.*``. 这一层只暴露真正训练相关的符号:

* ``TrainService``  — 训练流程编排
* ``TrainResult``   — 训练结果 dataclass
* ``TrainMetrics``  — 指标 dataclass (转再导出, 让用户 `from odp_platform.training` 也能拿)
* ``train_yolo``    — 便捷函数

下游子系统(D7 ValService / D8 InferService)需要 model_path / dataset_path 等
工具时, 应该直接 ``from odp_platform.common.xxx import ...`` — 不要绕道
training. 训练子系统不是这些工具的发行渠道.
"""
from __future__ import annotations

# ---- 核心(2): 训练流程 ----
from .service import TrainResult, TrainService, train_yolo

# ---- 指标 dataclass: 转再导出 ----
# TrainMetrics 实际定义在 common.result(因为 D7 ValMetrics 同款复用), 但
# `from odp_platform.training import TrainMetrics` 这个 import 路径足够直观,
# 保留下来让用户的肌肉记忆不破.
from odp_platform.common.result import TrainMetrics

__all__ = [
    "TrainService",
    "TrainResult",
    "TrainMetrics",
    "train_yolo",
]
