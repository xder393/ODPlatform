#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : logging_utils.py
# @Project   : ODPlatform
# @Function  : 高度可配置的日志工具,创建独立日志文件 + 彩色控制台输出
#
# 设计哲学:
#   - 业务模块: 顶部一行 logger = logging.getLogger(__name__), 不配 handler
#   - 本模块: 提供 get_logger(), 把 handler 挂到根 logger "odp_platform" 上
#   - CLI 入口: 调用一次 get_logger() 完成 handler 装配,
#               之后所有 getLogger(__name__) 通过冒泡机制自动继承

import logging
import sys
import platform
from datetime import datetime
from pathlib import Path
from typing import Optional
from colorlog import ColoredFormatter


# 项目根 logger 名 = 顶层 Python 包名
# 业务模块写 getLogger("odp_platform.xxx.yyy") 会通过冒泡找到这里挂的 handler
ROOT_LOGGER_NAME: str = "odp_platform"


def get_logger(
    base_path: Path,
    log_type: str = "general",
    model_name: Optional[str] = None,
    log_level: int = logging.INFO,
    temp_log: bool = False,
    encoding: str = "utf-8",
    logger_name: str = ROOT_LOGGER_NAME,
) -> logging.Logger:
    """
    配置项目【根 logger】, 挂上 console + file handler。

    设计要点:
        - 默认 logger_name = "odp_platform" (顶层包名),
          与业务模块的 getLogger(__name__) 形成 logger 树
        - 业务模块的日志会通过冒泡机制自动来到这里, 被本函数挂的 handler 处理
        - 重复调用幂等 (检查 handlers 是否已配置, 避免重复挂)

    Args:
        base_path: 日志根目录(通常是 paths.LOGGING_DIR)
        log_type: 日志类型,如 "init_project" / "train" / "val"
        model_name: 模型名(如 "yolo11n"),会拼到日志文件名里
        log_level: 日志级别(默认 INFO)
        temp_log: 是否标记为临时日志(文件名前缀变成 "temp")
        encoding: 文件编码(默认 utf-8)
        logger_name: 要配置的 logger 名 (默认是根 logger "odp_platform")

    Returns:
        配置好的 logging.Logger 实例
    """
    # ============================================================
    # 1. 获取/复用命名 Logger
    # ============================================================
    logger = logging.getLogger(logger_name)
    # 幂等保护: getLogger 是 singleton——同一个 logger_name 多次调用拿到同一对象。
    # 第一次调用配置好 handler 后, 后续调用直接返回, 避免重复挂多份 handler
    # (重复挂 handler 会导致同一条日志被打印 N 遍)。
    if logger.handlers:
        return logger

    logger.setLevel(log_level)
    logger.propagate = False  # 根 logger 不再向上冒泡(避免与 Python 默认 root logger 重复输出)

    # ============================================================
    # 2. 准备日志目录
    # ============================================================
    log_dir: Path = base_path / log_type
    log_dir.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # 3. 构造日志文件名: <prefix>_<timestamp>[_<model>].log
    # ============================================================
    timestamp: str = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
    prefix = "temp" if temp_log else log_type.replace("_", "-")

    filename_parts = [prefix, timestamp]
    if model_name:
        safe_model = "".join(
            c if c.isalnum() or c in "_-" else "_" for c in model_name
        )
        filename_parts.append(safe_model)
    log_file: Path = log_dir / ("_".join(filename_parts) + ".log")

    # ============================================================
    # 4. 文件 Handler(完整格式, 含 logger 名)
    # ============================================================
    # %(name)s 字段会输出 logger 名, 比如 "odp_platform.cli.init_project"
    file_formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)-8s - "
            "%(filename)s:%(lineno)d - %(funcName)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_file, encoding=encoding)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # ============================================================
    # 5. 控制台 Handler(彩色 + 紧凑格式)
    # ============================================================
    console_formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s%(reset)s "
        "%(log_color)s[%(levelname)-8s]%(reset)s "
        "%(cyan)s%(filename)-25s%(reset)s:"
        "%(blue)s%(lineno)-4d%(reset)s "
        "%(log_color)s│ %(message)s%(reset)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "white",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red,bg_white",
        },
        style='%'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # ============================================================
    # 6. 初始化信息
    # ============================================================
    logger.info('=' * 60)
    logger.info("日志系统初始化完成")
    logger.info(f"运行环境: {platform.system()} {platform.release()}")
    logger.info(f"阶段类型: {log_type}")
    logger.info(f"日志文件: {log_file}")
    logger.info(f"日志级别: {logging.getLevelName(log_level)}")
    logger.info(f"模型名称: {model_name or '无'}")
    logger.info('=' * 60)

    return logger


if __name__ == "__main__":
    # 模块自测——演示"基础设施装 handler + 业务模块发声"的标准用法
    from odp_platform.common.paths import LOGGING_DIR

    # 第一步: 装配根 logger 的 handler(这是 CLI 入口该做的事, 这里只是自测演示)
    get_logger(
        base_path=LOGGING_DIR,
        log_type="test",
        temp_log=True,
    )

    # 第二步: 业务代码就这样写——拿一个 __name__ logger, 直接发声
    test_logger = logging.getLogger(__name__)
    test_logger.debug("这是 DEBUG (默认 INFO 级别看不到)")
    test_logger.info("这是 INFO")
    test_logger.warning("这是 WARNING")
    test_logger.error("这是 ERROR")
