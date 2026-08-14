#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""ODPlatform 数据集 yaml 审计工具 —— 回查一次实验用的什么数据划分。

用法:
    python scripts/audit_dataset_yaml.py apps/platform/configs/datasets/<dataset>.yaml

打印 dataset / source_format / task / 划分比例 / 实际样本数 / random_state / 生成时间。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def audit_yaml(yaml_path: Path) -> None:
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    meta = doc.get("odp_meta")
    if not meta:
        print(f"⚠️  {yaml_path} 缺少 odp_meta 块(可能是外部生成的 yaml)")
        return

    split = meta.get("split", {})
    counts = split.get("counts", {})

    print(f"数据集名   : {meta.get('dataset')}")
    print(f"源格式     : {meta.get('source_format')}")
    print(f"任务类型   : {meta.get('task')}")
    print(f"生成时间   : {meta.get('created_at')}")
    print(f"schema 版本: {meta.get('schema_version')}")
    print("-" * 40)
    print(f"划分比例   : train={split.get('train_rate')} / "
          f"val={split.get('val_rate')} / test={split.get('test_rate')}")
    print(f"实际样本数 : train={counts.get('train')} / "
          f"val={counts.get('val')} / test={counts.get('test')} "
          f"(total={counts.get('total')})")
    print(f"random_state: {split.get('random_state')}")
    print("-" * 40)
    print(f"类别数     : {len(doc.get('names', {}))}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="回查 ODPlatform 数据集 yaml 的划分元数据 (odp_meta)。"
    )
    parser.add_argument("yaml_path", type=Path, help="数据集 yaml 文件路径")
    args = parser.parse_args()

    if not args.yaml_path.exists():
        print(f"❌ 文件不存在: {args.yaml_path}", file=sys.stderr)
        return 2

    audit_yaml(args.yaml_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
