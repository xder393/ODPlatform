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
            {"name": r.name, "severity": r.severity, "summary": r.summary,
             "details": r.details}
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


# ====================================================================
# 数据集信息检测 / 预览 / 实验列表 (供新 UI 用)
# ====================================================================

def detect_dataset_info(dataset_name: str) -> dict[str, Any]:
    """扫描 data/raw/<name>, 返回 {name, exists, format, images, annotations, classes}."""
    import json as _json
    import xml.etree.ElementTree as ET
    from pathlib import Path

    from odp_platform.common.constants import IMAGE_EXTENSIONS
    from odp_platform.common.paths import RAW_DATA_DIR

    root = RAW_DATA_DIR / dataset_name
    if not root.is_dir():
        return {"name": dataset_name, "exists": False,
                "error": f"目录不存在: {root}"}

    img_exts = {e.lower() for e in IMAGE_EXTENSIONS}
    images_dir = root / "images"
    ann_dir = root / "annotations"

    images = sorted(
        p for p in images_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in img_exts
    ) if images_dir.is_dir() else []
    anns = sorted(p for p in ann_dir.rglob("*") if p.is_file()) if ann_dir.is_dir() else []

    # 格式检测: 按标注扩展名
    exts = {p.suffix.lower() for p in anns}
    if ".xml" in exts:
        fmt = "pascal_voc"
    elif ".json" in exts:
        fmt = "coco"
    elif ".txt" in exts:
        fmt = "yolo"
    else:
        fmt = "unknown"

    # 类别提取
    classes: list[str] = []
    if fmt == "pascal_voc":
        for f in sorted(ann_dir.glob("*.xml"))[:100]:
            try:
                for el in ET.parse(f).getroot().iter("name"):
                    n = (el.text or "").strip()
                    if n and n not in classes:
                        classes.append(n)
            except Exception:
                pass
    elif fmt == "coco":
        for f in sorted(ann_dir.glob("*.json"))[:1]:
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                cats = sorted(data.get("categories", []), key=lambda c: c.get("id", 0))
                classes = [c["name"] for c in cats]
            except Exception:
                pass

    return {"name": dataset_name, "exists": True, "format": fmt,
            "images": len(images), "annotations": len(anns), "classes": classes}


def sample_image_paths(dataset_name: str, limit: int = 6) -> list[str]:
    """取数据集的几张样本图 (绝对路径), 给预览用."""
    from pathlib import Path

    from odp_platform.common.constants import IMAGE_EXTENSIONS
    from odp_platform.common.paths import RAW_DATA_DIR

    img_exts = {e.lower() for e in IMAGE_EXTENSIONS}
    images_dir = RAW_DATA_DIR / dataset_name / "images"
    if not images_dir.is_dir():
        return []
    out = []
    for p in sorted(images_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in img_exts:
            out.append(str(p))
            if len(out) >= limit:
                break
    return out


def draw_boxes_pil(image_path: str, label_path: str, class_names: list[str] | None = None):
    """在图上画 YOLO 标注框, 返回 PIL Image. 没有标注时原样返回."""
    from pathlib import Path

    from PIL import Image, ImageDraw

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    label_file = Path(label_path)
    if not label_file.exists():
        return img
    for line in label_file.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            cid = int(parts[0])
            cx, cy, bw, bh = (float(x) for x in parts[1:5])
        except ValueError:
            continue
        x0 = (cx - bw / 2) * w
        y0 = (cy - bh / 2) * h
        x1 = (cx + bw / 2) * w
        y1 = (cy + bh / 2) * h
        d.rectangle([x0, y0, x1, y1], outline=(255, 59, 48), width=max(2, w // 300))
        if class_names and 0 <= cid < len(class_names):
            d.text((x0, max(0, y0 - 12)), str(class_names[cid]), fill=(255, 59, 48))
    return img


# ====================================================================
# 重任务 → 子进程参数 (可取消)
# ====================================================================

def train_args(model: str, data: str, epochs: int, imgsz: int, device: str) -> list[str]:
    import sys
    return [sys.executable, "-m", "odp_platform.cli.train_model",
            "--data", data, "--model", model, "--epochs", str(epochs),
            "--imgsz", str(imgsz), "--device", device, "--workers", "0"]


def val_args(model: str, data: str, split: str, imgsz: int, device: str) -> list[str]:
    import sys
    return [sys.executable, "-m", "odp_platform.cli.val_model",
            "--data", data, "--model", model, "--split", split,
            "--imgsz", str(imgsz), "--device", device]


def infer_args(model: str, source: str, device: str, save: bool = True) -> list[str]:
    import sys
    args = [sys.executable, "-m", "odp_platform.cli.infer",
            "--model", model, "--source", source, "--device", device]
    if save:
        args.append("--save")
    return args


# ====================================================================
# 训练实验列表 (读 runs/**/odp_audit.json)
# ====================================================================

def _f(value: Any, nd: int = 4):
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return None


def list_experiments() -> list[dict[str, Any]]:
    """扫描 runs/**/odp_audit.json, 按时间倒序返回实验列表."""
    import json as _json
    from pathlib import Path

    from odp_platform.common.paths import RUNS_DIR

    exps: list[dict[str, Any]] = []
    audits = sorted(
        Path(RUNS_DIR).rglob("odp_audit.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for audit in audits:
        try:
            data = _json.loads(audit.read_text(encoding="utf-8"))
        except Exception:
            continue
        cfg = data.get("config", {}).get("values", {})
        metrics = data.get("metrics", {})
        overall = metrics.get("overall", {})
        exps.append({
            "name": audit.parent.name,                       # train / train2 / val / ...
            "group": audit.parent.parent.name,               # detect_train / detect_val / ...
            "task": metrics.get("task", cfg.get("task", "detect")),
            "model": cfg.get("model", "?"),
            "data": cfg.get("data", "?"),
            "time": metrics.get("timestamp", ""),
            "mAP50": _f(overall.get("metrics/mAP50(B)")),
            "mAP50_95": _f(overall.get("metrics/mAP50-95(B)")),
            "precision": _f(overall.get("metrics/precision(B)")),
            "recall": _f(overall.get("metrics/recall(B)")),
            "output_dir": str(audit.parent),
            "log_path": data.get("result_summary", {}).get("log_path"),
            "results_csv": str(audit.parent / "results.csv"),
            "confusion_png": str(audit.parent / "confusion_matrix.png"),
        })
    return exps


def ensure_config(kind: str) -> str:
    """确保 configs/runtime/<kind>.yaml 存在, 不存在则生成. 返回 yaml 绝对路径."""
    from pathlib import Path

    from odp_platform.common.paths import RUNTIME_CONFIGS_DIR
    from odp_platform.runtime_config import (
        ConfigGenerator, YOLOTrainConfig, YOLOValConfig, YOLOInferConfig,
    )
    mapping = {"train": YOLOTrainConfig, "val": YOLOValConfig, "infer": YOLOInferConfig}
    out = RUNTIME_CONFIGS_DIR / f"{kind}.yaml"
    if not out.exists():
        ConfigGenerator().generate(mapping[kind], out)
    return str(Path(out))


def detect_images(model: str, source: str, conf: float = 0.25) -> dict[str, Any]:
    """检测目标: 直接调 ultralytics predict 出框并保存, 返回统计.

    (D8 推理子系统的 frame_source/visualization 前置模块未落地, 这里走
    ultralytics 原生 predict 作为可用的检测路径.)
    """
    from ultralytics import YOLO

    m = YOLO(model)
    results = m.predict(source=source, conf=conf, save=True, verbose=False)
    total = sum(r.boxes.shape[0] if r.boxes is not None else 0 for r in results)
    save_dir = getattr(results[0], "save_dir", None) if results else None
    return {
        "kind": "detect",
        "success": True,
        "images": len(results),
        "detections": total,
        "save_dir": str(save_dir) if save_dir else None,
        "source": source,
    }
