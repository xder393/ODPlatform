# -*- coding:utf-8 -*-
"""服务层 → 普通 dict 的薄封装.

每个函数返回普通 dict (不是 odp_platform 内部 dataclass), 让 GUI 层跟内部
类型解耦. 重依赖(torch/ultralytics)的 import 都在函数内, GUI 启动不碰它们.

约定: 每个返回 dict 都带 "kind" 字段, 结果面板按 kind 渲染.
"""
from __future__ import annotations

from typing import Any


def transform_dataset(
    dataset_name: str,
    fmt: str,
    train_rate: float,
    val_rate: float,
) -> dict[str, Any]:
    """格式转换 + 划分 (D3)."""
    from odp_platform.data_pipeline import DatasetPipeline
    result = DatasetPipeline(
        dataset_name=dataset_name,
        annotation_format=fmt,
        train_rate=train_rate,
        val_rate=val_rate,
    ).run()
    return {
        "kind": "transform",
        "counts": result["counts"],
        "yaml": result["yaml"],
    }


def validate_dataset_checked(dataset_name: str) -> dict[str, Any]:
    """数据质检 (D4)."""
    from odp_platform.common.paths import dataset_yaml_path
    from odp_platform.data_validation import validate_dataset
    report = validate_dataset(dataset_yaml_path(dataset_name), task_type="detect")
    return {
        "kind": "validate",
        "overall_severity": report.overall_severity,
        "exit_code": report.exit_code,
        "results": [
            {"name": r.name, "severity": r.severity, "summary": r.summary}
            for r in report.results
        ],
        "dataset_summary": {
            "nc": report.snapshot.nc,
            "classes": list(report.snapshot.class_names),
            "total_images": report.snapshot.total_images,
        },
        "stats_per_split": {
            split: {
                "image_count": stat.image_count,
                "annotated_count": stat.annotated_count,
                "total_instances": stat.total_instances,
            }
            for split, stat in report.snapshot.stats_per_split.items()
        },
        "report_path": str(report.report_path) if report.report_path else None,
    }


def generate_config(kind: str) -> dict[str, Any]:
    """生成运行配置模板 (D5)."""
    from odp_platform.common.paths import RUNTIME_CONFIGS_DIR
    from odp_platform.runtime_config import (
        ConfigGenerator, YOLOTrainConfig, YOLOValConfig, YOLOInferConfig,
    )
    mapping = {
        "train": YOLOTrainConfig,
        "val": YOLOValConfig,
        "infer": YOLOInferConfig,
    }
    out = RUNTIME_CONFIGS_DIR / f"{kind}.yaml"
    generated = ConfigGenerator().generate(mapping[kind], out)
    return {"kind": "gen_config", "generated": generated, "path": str(out)}


def run_train(model: str, data: str, epochs: int) -> dict[str, Any]:
    """训练 (D6)."""
    from odp_platform.training import TrainService
    result = TrainService().train(
        yaml_path=None,
        cli_args={"model": model, "data": data, "epochs": epochs},
    )
    return {
        "kind": "train",
        "success": result.success,
        "error": result.error,
        "output_dir": str(result.output_dir),
        "metrics": result.metrics,
        "train_time": result.train_time,
    }


def run_val(model: str, data: str, split: str) -> dict[str, Any]:
    """评估 (D7)."""
    from odp_platform.evaluation import ValService
    result = ValService().val(
        yaml_path=None,
        cli_args={"model": model, "data": data, "split": split},
    )
    return {
        "kind": "val",
        "success": result.success,
        "error": result.error,
        "output_dir": str(result.output_dir),
        "metrics": result.metrics,
        "val_time": result.val_time,
    }


def run_infer(model: str, source: str) -> dict[str, Any]:
    """推理 (D8)."""
    from odp_platform.inference import InferService
    result = InferService().predict(
        yaml_path=None,
        cli_args={"model": model, "source": source},
    )
    return {
        "kind": "infer",
        "success": result.success,
        "error": result.error,
        "output_dir": str(result.output_dir) if result.output_dir else None,
        "infer_time": result.infer_time,
        "saved": result.saved,
        "stats": result.stats,
    }
