#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : init_project.py
# @Project   : ODPlatform
# @Function  : 项目自动初始化——目录创建 + 数据集状态检查

import logging
from pathlib import Path
from typing import List

from odp_platform.common.paths import (
    ROOT_DIR,
    LOGGING_DIR,
    RAW_DATA_DIR,
    get_dirs_to_initialize,
)
from odp_platform.common.logging_utils import get_logger
from odp_platform.common.performance_utils import time_it
from odp_platform.common.system_utils import log_device_info
from odp_platform.common.string_utils import format_table_row, format_table_separator

LINE_WIDTH: int = 60

# 业务模块标准写法
logger = logging.getLogger(__name__)


def _check_raw_data_status() -> List[str]:
    """检查 raw 数据目录的状态"""
    raw_status: List[str] = []
    rel_raw = RAW_DATA_DIR.relative_to(ROOT_DIR)

    if not RAW_DATA_DIR.exists():
        logger.warning(
            f"原始数据集根目录不存在: {RAW_DATA_DIR}\n"
            f"请在该目录下创建以「数据集名称」命名的文件夹。"
        )
        raw_status.append(f"{rel_raw} 不存在 → 请创建并放入数据集")
    elif not any(RAW_DATA_DIR.iterdir()):
        logger.warning(
            f"原始数据集根目录为空: {RAW_DATA_DIR}\n"
            f"预期结构:\n"
            f"  {rel_raw}/<数据集名>/\n"
            f"  ├── images/\n"
            f"  └── annotations/"
        )
        raw_status.append(f"{rel_raw} 为空 → 请放入至少一个数据集")
    else:
        sub_dirs = [p for p in RAW_DATA_DIR.iterdir() if p.is_dir()]
        logger.info(f"原始数据集根目录就绪,检测到 {len(sub_dirs)} 个数据集文件夹")
        raw_status.append(f"{rel_raw} 就绪(包含 {len(sub_dirs)} 个数据集)")
        for sub in sorted(sub_dirs):
            raw_status.append(f"  • {sub.name}")
    return raw_status


@time_it(iterations=1, name="项目初始化", logger_instance=logger)
def initialize_project() -> None:
    """初始化项目核心目录 + 检查原始数据状态。

    本函数是 CLI 入口——负责装配根 logger 的 handler。
    装完之后, 整个进程里所有 getLogger(__name__) 都通过冒泡机制使用这套 handler。
    """
    # CLI 入口装配 handler (整个进程只装一次, get_logger 内部有幂等保护)
    get_logger(
        base_path=LOGGING_DIR,
        log_type="init_project",
        temp_log=False,
    )

    logger.info("开始初始化项目核心目录".center(LINE_WIDTH, '='))
    logger.info(f"项目核心目录为: {ROOT_DIR}")

    # 1. 打印环境快照——log_device_info 默认会用 system_utils 自己的 logger,
    #    通过冒泡同样到根 logger 的 handler, 一切自然
    log_device_info()

    # 2. 创建项目核心目录
    logger.info("检查并创建项目核心目录".center(LINE_WIDTH, '='))

    created: List[Path] = []
    existed: List[Path] = []

    for d in get_dirs_to_initialize():
        rel = d.relative_to(ROOT_DIR)
        if not d.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
                logger.info(f"成功创建: {rel}")
                created.append(d)
            except OSError as e:
                logger.error(f"创建失败 {rel}: {e}")
                raise SystemExit(1) from e
        else:
            logger.info(f"目录已存在: {rel}")
            existed.append(d)

    logger.info("项目核心目录检查创建完毕".center(LINE_WIDTH, '='))

    # 3. 检查原始数据目录状态
    logger.info("开始检查原始数据目录".center(LINE_WIDTH, '='))
    raw_status = _check_raw_data_status()

    # 4. 输出汇总信息
    logger.info("项目初始化汇总".center(LINE_WIDTH, '='))

    widths = [30, 12]
    aligns = ['left', 'right']
    logger.info(format_table_row(['目录', '状态'], widths, aligns))
    logger.info(format_table_separator(widths))
    for d in created:
        logger.info(format_table_row(
            [str(d.relative_to(ROOT_DIR)), '新建'], widths, aligns
        ))
    for d in existed:
        logger.info(format_table_row(
            [str(d.relative_to(ROOT_DIR)), '已存在'], widths, aligns
        ))

    if not created and not existed:
        logger.info("(本次没有任何目录变化)")

    logger.info("原始数据目录状态:")
    for status in raw_status:
        logger.info(f"  - {status}")

    logger.info("项目初始化完毕".center(LINE_WIDTH, '='))
    logger.info("下一步:把数据集放到 data/raw/ 下,然后运行数据转换脚本")
    logger.info("=" * LINE_WIDTH)


if __name__ == "__main__":
    initialize_project()
