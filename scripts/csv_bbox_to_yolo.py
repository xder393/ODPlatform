#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""把 Kaggle 风格的 bbox CSV 转成 YOLO 原始格式(raw).

适配的 CSV 格式: image,xmin,ymin,xmax,ymax  (像素坐标, 单类, 一个框一行)
输入: 一个 zip 或文件夹 (内含图片目录 + 那个 csv)
输出: data/raw/<name>/images/*.jpg + data/raw/<name>/annotations/*.txt
      坐标按每张图真实尺寸归一化; 只保留有框的图.

用法:
  python scripts/csv_bbox_to_yolo.py /path/to/archive.zip \
      --csv "data/train_solution_bounding_boxes (1).csv" \
      --images-dir "data/training_images" \
      --name det_demo --class object

转换完直接:
  odp-transform --dataset det_demo --format yolo --classes object
"""
from __future__ import annotations

import argparse
import csv as csv_mod
import io
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read_images_dir(src: Path, images_rel: str) -> dict[str, bytes]:
    """返回 {相对文件名: 图片字节}."""
    if src.is_file() and src.suffix.lower() == ".zip":
        z = zipfile.ZipFile(src)
        return {
            n.split("/")[-1]: z.read(n)
            for n in z.namelist()
            if n.startswith(images_rel.strip("/") + "/") and n.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
        }
    if src.is_dir():
        d = src / images_rel
        if not d.is_dir():
            raise FileNotFoundError(f"找不到图片目录: {d}")
        return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}
    raise ValueError(f"只接受文件夹或 zip: {src}")


def _read_csv(src: Path, csv_rel: str) -> list[dict[str, str]]:
    if src.is_file() and src.suffix.lower() == ".zip":
        z = zipfile.ZipFile(src)
        txt = z.read(csv_rel).decode("utf-8", "ignore")
    else:
        txt = (src / csv_rel).read_text(encoding="utf-8", errors="ignore")
    return list(csv_mod.DictReader(txt.splitlines()))


def convert(src: Path, csv_rel: str, images_rel: str, name: str, class_name: str, out_root: Path) -> dict[str, Any]:
    from PIL import Image

    rows = _read_csv(src, csv_rel)
    images = _read_images_dir(src, images_rel)

    # 按图聚合框
    boxes: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    for r in rows:
        boxes[r["image"]].append(
            tuple(float(r[k]) for k in ("xmin", "ymin", "xmax", "ymax"))
        )

    out = out_root / name
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "annotations").mkdir(parents=True, exist_ok=True)

    n_img = n_box = skipped = 0
    for img_name, boxlist in sorted(boxes.items()):
        raw = images.get(img_name)
        if raw is None:
            skipped += 1
            continue
        w, h = Image.open(io.BytesIO(raw)).size
        (out / "images" / img_name).write_bytes(raw)

        lines = []
        for xmin, ymin, xmax, ymax in boxlist:
            cx = (xmin + xmax) / 2 / w
            cy = (ymin + ymax) / 2 / h
            bw = (xmax - xmin) / w
            bh = (ymax - ymin) / h
            lines.append(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        (out / "annotations" / f"{img_name.rsplit('.', 1)[0]}.txt").write_text("\n".join(lines))
        n_img += 1
        n_box += len(boxlist)

    return {"name": name, "class": class_name, "images": n_img,
            "boxes": n_box, "skipped": skipped, "path": str(out)}


def main() -> int:
    from odp_platform.common.paths import RAW_DATA_DIR
    parser = argparse.ArgumentParser(description="Kaggle bbox CSV → YOLO raw 转换")
    parser.add_argument("source", type=Path, help="zip 或文件夹")
    parser.add_argument("--csv", required=True, help="CSV 在 zip/文件夹内的相对路径")
    parser.add_argument("--images-dir", default="images", help="图片目录相对路径")
    parser.add_argument("--name", required=True, help="数据集名(= data/raw 下的目录名)")
    parser.add_argument("--class", dest="class_name", default="object", help="类别名(单类)")
    args = parser.parse_args()

    result = convert(args.source, args.csv, args.images_dir,
                     args.name, args.class_name, RAW_DATA_DIR)
    print(f"转换完成: {result['images']} 张图 / {result['boxes']} 个框 "
          f"(跳过 {result['skipped']} 张找不到的图)")
    print(f"输出: {result['path']}")
    print(f"下一步: odp-transform --dataset {result['name']} "
          f"--format yolo --classes {result['class']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "platform" / "src"))
    sys.exit(main())
