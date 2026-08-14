#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : system_utils.py
# @Project   : ODPlatform
# @Function  : 环境快照——OS / Python / PyTorch / CPU / GPU / 内存

from __future__ import annotations

import os
import platform
import time
import logging
from typing import Optional

from odp_platform.common.string_utils import pad_to_width

# 业务模块标准写法
logger = logging.getLogger(__name__)


def _format_size(bytes_size) -> str:
    """简洁的字节单位格式化"""
    if not bytes_size or not isinstance(bytes_size, (int, float)):
        return "N/A"
    if bytes_size >= 1024 ** 3:
        return f"{bytes_size / (1024 ** 3):.2f} GB"
    if bytes_size >= 1024 ** 2:
        return f"{bytes_size / (1024 ** 2):.2f} MB"
    return f"{bytes_size / 1024:.2f} KB"


def get_basic_device_info() -> dict:
    """返回结构化的环境信息字典"""
    cpu_name = platform.processor() or platform.machine() or "Unknown CPU"
    cpu_cores = os.cpu_count() or "Unknown"

    # 内存(psutil 是软依赖)
    try:
        import psutil
        memory = psutil.virtual_memory()
        total_ram = _format_size(memory.total)
        available_ram = _format_size(memory.available)
        ram_usage = f"{memory.percent:.1f}%"
    except ImportError:
        total_ram = "Unknown (psutil 未安装)"
        available_ram = "Unknown"
        ram_usage = "Unknown"

    # PyTorch / Ultralytics(软依赖)
    try:
        import torch
        torch_version = torch.__version__
        cuda_available = torch.cuda.is_available()
        gpu_count = torch.cuda.device_count() if cuda_available else 0
        _torch = torch  # 缓存模块引用, 后面 GPU 详情段直接用, 不再重复 import
    except ImportError:
        torch_version = "Unknown (torch 未安装)"
        cuda_available = False
        gpu_count = 0
        _torch = None

    try:
        from ultralytics import __version__ as ultralytics_version
    except ImportError:
        ultralytics_version = "Unknown (ultralytics 未安装)"

    gpu_info = {
        "CUDA可用": cuda_available,
        "GPU数量": gpu_count,
    }
    if cuda_available and _torch is not None:
        for i in range(gpu_count):
            gpu_info[f"GPU_{i}"] = _torch.cuda.get_device_name(i)
            gpu_info[f"GPU_{i}_显存"] = _format_size(
                _torch.cuda.get_device_properties(i).total_memory
            )

    return {
        "系统信息": {
            "操作系统": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "主机名": platform.node(),
            "Python版本": platform.python_version(),
            "PyTorch版本": torch_version,
            "Ultralytics版本": ultralytics_version,
            "当前时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "CPU信息": {
            "CPU型号": cpu_name,
            "核心数": cpu_cores,
        },
        "内存信息": {
            "总内存": total_ram,
            "可用内存": available_ram,
            "使用率": ram_usage,
        },
        "GPU信息": gpu_info,
    }


def log_device_info(target_logger: Optional[logging.Logger] = None) -> dict:
    """把环境信息打印到 logger。

    Args:
        target_logger: 可选, 指定的 logger 实例。
                       None 时使用本模块的 __name__ logger(推荐——
                       业务方调用时, 通过冒泡机制走到根 logger 的 handler)。
    """
    log = target_logger if target_logger is not None else logger

    info = get_basic_device_info()

    log.info("🚀 运行环境概览".center(60))
    log.info("=" * 60)

    key_width = 16

    for category, details in info.items():
        log.info(f"{category}".center(60))
        log.info("-" * 60)
        for key, value in details.items():
            padded_key = pad_to_width(key, key_width)
            log.info(f"  {padded_key}: {value}")

    return info


if __name__ == "__main__":
    # 自测——这里用 basicConfig 是因为自测脚本里没走完整 CLI 入口,
    # 真实使用中, CLI 入口已经调过 get_logger(), 这里不需要做任何配置
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log_device_info()
