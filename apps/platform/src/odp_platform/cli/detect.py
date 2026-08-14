#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : detect.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 简单目标检测 CLI — 出框保存, 打印结果 JSON
"""odp-detect — 简单目标检测.

跟 D8 inference 的完整流水线不同, 这里直接走 ultralytics 原生 predict,
作为"出框保存 + 统计数量"的轻量检测入口. 输出一行 ODP_RESULT JSON 供 GUI 解析.

用法:
  python -m odp_platform.cli.detect --model <权重> --source <图/文件夹> --conf 0.25
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main() -> int:
    # 压掉 ultralytics 的 "Results saved to ..." 等输出, 只留我们的 JSON 行
    logging.getLogger("ultralytics").setLevel(logging.ERROR)

    from ultralytics import YOLO

    from odp_platform.common.model_path import resolve_model_path

    parser = argparse.ArgumentParser(prog="odp-detect", description="简单目标检测, 出框保存")
    parser.add_argument("--model", required=True, help="模型权重路径 / 文件名")
    parser.add_argument("--source", required=True, help="图片 / 视频 / 文件夹")
    parser.add_argument("--conf", type=float, default=0.25, help="置信度阈值")
    args = parser.parse_args()

    try:
        # resolve_model_path 会先在 models/pretrained 等目录找, 不会去下载
        model = YOLO(str(resolve_model_path(args.model)))
        results = model.predict(source=args.source, conf=args.conf, save=True, verbose=False)
        total = sum(r.boxes.shape[0] if r.boxes is not None else 0 for r in results)
        save_dir = getattr(results[0], "save_dir", None) if results else None
        print("ODP_RESULT " + json.dumps({
            "images": len(results),
            "detections": total,
            "save_dir": str(save_dir) if save_dir else None,
        }, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001 - CLI 兜底
        print(f"检测失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
