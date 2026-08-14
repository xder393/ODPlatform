#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : val_model.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : odp-val CLI 入口 — argparse + 装日志 handler + 调 ValService
"""odp-val CLI 入口.

★ 职责边界 (跟 odp-train 完全对齐):
  - 解析 argparse (把 CLI 字段变成 dict, 交给 D5 build_val_config 合并)
  - 装文件日志 handler (业务模块只发声纪律的兑现位)
  - 调 ValService.val(...) 跑评估
  - 把退出码翻译给操作系统 (0/1/130)

CLI 不做的事:
  - 不合并配置(那是 D5 的事)
  - 不校验数据集(那是 D4 的事, 由 service 自动调)
  - 不动 ultralytics(那是 service 的事)
"""
from __future__ import annotations

import argparse
import logging
import sys

from odp_platform.common.logging_utils import get_logger
from odp_platform.common.paths import LOGGING_DIR

from odp_platform.evaluation import ValService


# ============================================================================
# argparse
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    """构造 argparse parser. 拆出来让测试可以独立验证 CLI 表面."""
    parser = argparse.ArgumentParser(
        prog="odp-val",
        description="YOLO 评估 — 调 D5 配置 + D4 校验 + ultralytics model.val",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  odp-val                                              # 默认 val.yaml
  odp-val --model runs/detect_train/train/weights/best.pt --split test
  odp-val --yaml my_val.yaml --conf 0.25 --imgsz 1280
  odp-val --device 0 --batch 64
  odp-val --no-pre-validate                            # 跳过 D4 校验

half / plots / save_json 等开关默认走 val.yaml, 用 `odp-gen-config val`
生成模板后按需编辑.
        """,
    )

    # ---- 配置文件 ----
    parser.add_argument(
        "--yaml", type=str, default=None,
        help="YAML 配置文件路径(默认走 RUNTIME_CONFIGS_DIR/val.yaml)",
    )

    # ---- 评估参数(覆盖 yaml) ----
    parser.add_argument("--model", type=str, help="模型路径 / 文件名(默认走 yaml)")
    parser.add_argument("--data",  type=str, help="数据集 yaml(默认走 yaml)")
    parser.add_argument("--split", type=str, choices=["train", "val", "test"],
                        help="评估的数据集划分(默认 val)")
    parser.add_argument("--conf", type=float, help="置信度阈值")
    parser.add_argument("--iou",  type=float, help="NMS IoU 阈值")
    parser.add_argument("--imgsz", type=int, help="输入图像尺寸")
    parser.add_argument("--batch", type=int, help="batch size")
    parser.add_argument("--device", type=str, help="评估设备(0/cpu/0,1)")
    parser.add_argument("--max-det", type=int, help="每张图最大检测数")
    parser.add_argument("--project", type=str, help="输出根目录")
    parser.add_argument("--name", type=str, help="运行名(yolo 用)")
    parser.add_argument("--experiment-name", dest="experiment_name", type=str,
                        help="实验名(ODP 用, 进 runs/<task>_val/<experiment_name>/)")

    # ---- D7 开关(service 层的 keyword-only 参数) ----
    parser.add_argument(
        "--no-pre-validate", dest="pre_validate", action="store_false", default=True,
        help="跳过评估前 D4 数据集校验(不推荐 — fail-fast 原则)",
    )
    parser.add_argument(
        "--no-rename-log", dest="rename_log", action="store_false", default=True,
        help="不把日志文件名改成 <save_dir>_<ts>_<model>.log 形式",
    )

    # ---- 可选辅助 ----
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )

    return parser


# ============================================================================
# 日志 handler 装载
# ============================================================================

def _setup_logging(log_level: str) -> None:
    """调 D2 的 get_logger 给 'odp_platform' 根 logger 装上 console + file handler."""
    get_logger(
        base_path=LOGGING_DIR,
        log_type="val",
        log_level=getattr(logging, log_level),
        temp_log=False,
    )


# ============================================================================
# main 入口
# ============================================================================

def main() -> int:
    """odp-val 主入口. 返回退出码 0/1/130."""
    parser = build_parser()
    args = parser.parse_args()

    # 1. 装日志 handler (走 D2 get_logger, 唯一一次)
    _setup_logging(args.log_level)
    log = logging.getLogger("odp_platform.cli.val_model")

    # 2. argparse.Namespace → dict, 过滤 None(让 D5 走默认值) + 拆出非配置字段
    NON_CONFIG_KEYS = {"yaml", "pre_validate", "rename_log", "log_level"}
    cli_args = {
        k: v for k, v in vars(args).items()
        if v is not None and k not in NON_CONFIG_KEYS
    }

    # 3. 调 service
    log.info(f"启动 odp-val, CLI 字段: {list(cli_args.keys())}")
    try:
        service = ValService()
        result = service.val(
            yaml_path=args.yaml,
            cli_args=cli_args,
            pre_validate=args.pre_validate,
            rename_log=args.rename_log,
        )
    except KeyboardInterrupt:
        log.warning("用户中断 (Ctrl+C)")
        return 130
    except Exception as e:        # service 本应 not raise, 兜底
        log.error(f"未预期异常: {e}", exc_info=True)
        return 1

    # 4. 退出码
    if result.success:
        log.info(f"✓ 评估成功. 用时 {result.val_time:.2f}s, 输出 {result.output_dir}")
        return 0
    else:
        log.error(f"✗ 评估失败: {result.error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
