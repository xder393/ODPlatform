#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : infer.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : odp-infer CLI 入口 — argparse + 装日志 handler + 调 infer_yolo
"""ODPlatform odp-infer CLI 入口.

职责 (跟 odp-train 完全同构):
  1. 装 logging handler (D2 get_logger, console+file 两份)
  2. argparse → cli_args dict
  3. 调 infer_yolo() 跑
  4. 退出码: 0 成功, 1 失败 (CI 友好)

★ 不传任何接缝参数 → service 走默认 → 100% 等价老式 CLI 行为.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from odp_platform.common.logging_utils import get_logger
from odp_platform.inference import infer_yolo


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odp-infer",
        description="ODPlatform YOLO 推理 CLI",
    )
    # ---- 输入输出 ----
    p.add_argument("--source", type=str, default=None,
                   help="输入源: 摄像头号(0/1) / 视频文件 / 图片或目录 / RTSP. 不传则用 infer.yaml.")
    p.add_argument("--model", type=str, default=None,
                   help="模型文件名 (best.pt / yolo11n.pt). 不传则用 infer.yaml.")
    p.add_argument("--yaml", type=str, default=None,
                   help="D5 infer.yaml 路径. 不传走默认.")
    p.add_argument("--pipeline-yaml", type=str, default=None,
                   help="帧源+美化 yaml 路径. 不传走默认 configs/runtime/infer_pipeline.yaml.")
    p.add_argument("--name", type=str, default=None, dest="experiment_name",
                   help="输出子目录名 (重复自增: predict / predict2 / ...).")

    # ---- 推理参数 ----
    p.add_argument("--conf", type=float, default=None, help="置信度阈值 (0~1).")
    p.add_argument("--iou",  type=float, default=None, help="NMS IoU 阈值 (0~1).")
    p.add_argument("--imgsz", type=int, default=None, help="推理输入尺寸.")
    p.add_argument("--max-det", type=int, default=None, dest="max_det", help="单图最大检测数.")
    p.add_argument("--classes", type=int, nargs="+", default=None, help="只保留这些类别 ID.")
    p.add_argument("--device", type=str, default=None, help="cpu / 0 / 0,1 / mps.")
    p.add_argument("--batch", type=int, default=None, help="批大小 (视频/图片夹).")

    # ---- 显示 / 存盘 ----
    g_show = p.add_mutually_exclusive_group()
    g_show.add_argument("--show", action="store_true", default=None, help="弹窗显示画面.")
    g_show.add_argument("--no-show", dest="show", action="store_false", help="关掉弹窗.")
    g_save = p.add_mutually_exclusive_group()
    g_save.add_argument("--save", action="store_true", default=None, help="结果存盘.")
    g_save.add_argument("--no-save", dest="save", action="store_false", help="不存盘.")
    p.add_argument("--no-viz", action="store_true", help="关掉美化, 用 YOLO 原生 plot().")
    p.add_argument("--no-hud", action="store_true", help="画面不叠 HUD 信息面板.")

    # ---- 其他 ----
    p.add_argument("--warmup", type=int, default=0,
                   help="启动丢弃前 N 帧 (摄像头帧率不稳).")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="日志级别.")
    return p


def _ns_to_cli_args(ns: argparse.Namespace) -> dict:
    """argparse Namespace → cli_args dict (D5 merger 吃 dict).

    只放非 None 的字段, 让 D5 / pydantic 的 default 兜底.
    `--no-viz` / `--no-hud` / `--warmup` / `--yaml` / `--pipeline-yaml` / `--log-level`
    不进 cli_args (这些是 CLI 自己的行为开关, 不是 D5 配置字段).
    """
    keys = ("source", "model", "experiment_name",
            "conf", "iou", "imgsz", "max_det", "classes", "device", "batch",
            "show", "save")
    return {k: v for k in keys if (v := getattr(ns, k, None)) is not None}


def main(argv: list[str] | None = None) -> int:
    ns = _build_parser().parse_args(argv)

    # ★ 纪律 B: 整个进程里【唯一】装 handler 的地方
    get_logger(
        base_path=Path("logs"),
        log_type="infer",
        log_level=getattr(logging, ns.log_level),
    )

    cli_args = _ns_to_cli_args(ns)
    result = infer_yolo(
        yaml_path=ns.yaml,
        pipeline_yaml=ns.pipeline_yaml,
        cli_args=cli_args,
        beautify=(not ns.no_viz),
        show_info=(not ns.no_hud),
        warmup_frames=ns.warmup,
        # ★ 不传 output_sink / hooks / cancel_token → service 走默认 → 等价老式 CLI
    )

    if result.success:
        return 0
    sys.stderr.write(f"\n推理失败: {result.error}\n")
    if result.log_path:
        sys.stderr.write(f"详细日志见: {result.log_path}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
