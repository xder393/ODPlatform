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


def import_dataset(source: str) -> dict[str, Any]:
    """把拖进来的数据集(文件夹/zip)导入到 data/raw/<name>/.

    处理规则 (按文件扩展名归类, 平铺):
      - 图片 (jpg/png/bmp/... 见 IMAGE_EXTENSIONS) → data/raw/<name>/images/
      - 标注 (xml / json / txt)                        → data/raw/<name>/annotations/
    所以下面这些布局都能识别:
      - 平铺: img.jpg + img.xml 混在一起
      - 标准: images/ + annotations/
      - VOC:  JPEGImages/ + Annotations/
      - zip 包: 先解压再导入
    重名文件跳过 (幂等), 已在 data/raw/ 下则直接返回.
    """
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    from odp_platform.common.constants import IMAGE_EXTENSIONS
    from odp_platform.common.paths import RAW_DATA_DIR

    IMAGE_EXTS = {e.lower() for e in IMAGE_EXTENSIONS}
    ANN_EXTS = {".xml", ".json", ".txt"}

    src = Path(source)
    tmp_root = None
    try:
        # 1. zip 先解压到临时目录
        if src.is_file() and src.suffix.lower() == ".zip":
            tmp_root = Path(tempfile.mkdtemp(prefix="odp_import_"))
            with zipfile.ZipFile(src) as zf:
                zf.extractall(tmp_root)
            entries = [p for p in tmp_root.iterdir()]
            if len(entries) == 1 and entries[0].is_dir():
                src = entries[0]        # zip 里只有一个顶层目录, 用它
            else:
                src = tmp_root
        elif not src.is_dir():
            raise ValueError(f"只接受文件夹或 zip: {source}")

        name = src.name
        dest = RAW_DATA_DIR / name

        # 已经在本仓库 data/raw/ 下 → 无需重复导入
        if src.resolve() == dest.resolve():
            return {"kind": "import", "name": name, "images": 0,
                    "annotations": 0, "path": str(dest), "already_here": True}

        images_dir = dest / "images"
        ann_dir = dest / "annotations"
        images_dir.mkdir(parents=True, exist_ok=True)
        ann_dir.mkdir(parents=True, exist_ok=True)

        found_img = found_ann = 0
        copied_img = copied_ann = 0
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS:
                found_img += 1
                if not (images_dir / f.name).exists():
                    shutil.copy2(f, images_dir / f.name)
                    copied_img += 1
            elif ext in ANN_EXTS:
                found_ann += 1
                if not (ann_dir / f.name).exists():
                    shutil.copy2(f, ann_dir / f.name)
                    copied_ann += 1

        if found_img == 0 and found_ann == 0:
            raise ValueError(
                f"在 {src.name} 里没找到图片或标注 "
                f"(图片: {sorted(IMAGE_EXTS)}, 标注: xml/json/txt)"
            )

        # 什么都没复制 → 说明同名数据集之前已经导入过
        already_here = copied_img == 0 and copied_ann == 0
        return {"kind": "import", "name": name, "images": copied_img,
                "annotations": copied_ann, "path": str(dest),
                "already_here": already_here}
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)
