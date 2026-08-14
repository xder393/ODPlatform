"""生成 ultralytics 训练用 yaml, 加 odp_meta 块持久化划分元数据。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List

import yaml

from odp_platform.data_pipeline.split.manifest import SplitManifest

logger = logging.getLogger(__name__)


def write_dataset_yaml(
    yaml_path: Path,
    *,
    dataset_root: Path,
    classes: List[str],
    manifest: SplitManifest,
    dataset_name: str,
    source_format: str,
    task: str,
) -> None:
    """写一份 ultralytics 兼容的 yaml, 含 odp_meta 块。

    Args:
        yaml_path:     输出 yaml 路径
        dataset_root:  ultralytics 的 path 字段值 (训练数据根)
        classes:       类别名列表 (顺序 = yolo class_id)
        manifest:      划分结果 + 元数据
        dataset_name:  数据集名 (写入 odp_meta)
        source_format: 原始格式名 (写入 odp_meta)
        task:          任务类型 (写入 odp_meta)
    """
    doc = {
        # ============================================================
        # ultralytics 标准字段
        # ============================================================
        "path":  str(dataset_root.resolve()),
        "train": "train/images",
        "val":   "val/images",
        "test":  "test/images",
        "names": {i: name for i, name in enumerate(classes)},

        # ============================================================
        # odp_meta: 非标准, 仅 ODPlatform 自己读
        # 用途: 实验追溯 / 划分复现 / 数据沿袭
        # ============================================================
        "odp_meta": {
            "dataset":       dataset_name,
            "source_format": source_format,
            "task":          task,
            "created_at":    datetime.now().isoformat(timespec="seconds"),
            "split": {
                "train_rate":   round(manifest.train_rate, 6),
                "val_rate":     round(manifest.val_rate, 6),
                "test_rate":    round(manifest.test_rate, 6),
                "random_state": manifest.random_state,
                "counts":       manifest.summary(),
            },
            "schema_version": 1,
        },
    }

    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)

    logger.info(f"已写入 dataset yaml: {yaml_path}")
