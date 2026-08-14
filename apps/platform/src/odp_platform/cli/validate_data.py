# apps/platform/src/odp_platform/cli/validate_data.py
"""odp-validate CLI — D4 子系统的端到端入口。

用法:
    odp-validate --dataset NAME [--task detect|segment]
    odp-validate --yaml /path/to/yaml [--task detect|segment]

退出码:
    0  PASS or only INFO
    1  WARNING present
    2  ERROR present
    3  Ctrl-C or unexpected exception (CLI bug, not data issue)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from odp_platform.common.logging_utils import get_logger
from odp_platform.common.paths import LOGGING_DIR, dataset_yaml_path
from odp_platform.data_validation import (
    render_to_logger,
    validate_dataset,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odp-validate",
        description="YOLO 数据集质量验证 (data_validation 子系统的 CLI 入口)",
    )

    # 数据集来源 — 互斥, 必须有且只能有一个
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dataset",
        help="数据集名 (= configs/datasets/<name>.yaml 的 stem; 日常使用)",
    )
    source.add_argument(
        "--yaml",
        type=Path,
        help="直接指定 yaml 完整路径 (调试 / 临时数据集用)",
    )

    parser.add_argument(
        "--task",
        choices=("detect", "segment"),
        default=None,
        help="任务类型 (None = 读 yaml.task 字段, 仍读不到则 detect)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="不写 JSON 报告 (只跑验证, 看日志即可)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="DEBUG 级日志输出 (控制台和文件同步开 DEBUG)",
    )

    return parser


def main() -> int:
    args = _build_parser().parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logger = get_logger(
        base_path=LOGGING_DIR,
        log_type="validate",
        log_level=log_level,
    )

    try:
        # ---- 解析 yaml_path ----
        if args.dataset:
            yaml_path = dataset_yaml_path(args.dataset)
        else:
            yaml_path = args.yaml.resolve()

        # ---- 跑端到端 ----
        report = validate_dataset(
            yaml_path=yaml_path,
            task_type=args.task,
            write_report=not args.no_report,
        )

        # ---- 渲染日志 ----
        render_to_logger(report, logger, report_path=report.report_path)

        return report.exit_code

    except KeyboardInterrupt:
        logger.warning("用户中断 (Ctrl-C)")
        return 3
    except Exception:
        logger.exception("未预期异常 — CLI bug, 不是数据问题")
        return 3


if __name__ == "__main__":
    sys.exit(main())
