"""PASCAL VOC XML → YOLO txt converter。"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

from odp_platform.common.constants import AnnotationFormat, Task
from odp_platform.data_pipeline.registry import ConvertOptions, register

logger = logging.getLogger(__name__)


@register(AnnotationFormat.PASCAL_VOC, supported_tasks=(Task.DETECT,))
def convert_voc(
    input_dir: Path,
    output_labels_dir: Path,
    options: ConvertOptions,
) -> List[str]:
    """读 input_dir 下所有 .xml, 写 YOLO txt 到 output_labels_dir。

    Returns:
        类别名列表 (按 XML 中首次出现顺序)
    """
    xml_files = sorted(input_dir.glob("*.xml"))
    if not xml_files:
        raise FileNotFoundError(f"在 {input_dir} 下未找到任何 XML")

    output_labels_dir.mkdir(parents=True, exist_ok=True)
    # ★ 注意: None vs [] 的语义区分 (见 2.1.1 节)
    #   - None  → 用户没指定, converter 自动发现类别
    #   - []    → 用户说"白名单为空", 所有 object 都不在白名单 → 写空 txt
    #   不能写成 `not options.classes`, 那会把 [] 也当成 None
    auto_discover = options.classes is None
    classes: List[str] = list(options.classes) if options.classes is not None else []

    n_ok = n_skip = 0
    for xml_path in xml_files:
        ok = _convert_one(
            xml_path, output_labels_dir, classes, auto_discover,
        )
        if ok:
            n_ok += 1
        else:
            n_skip += 1

    logger.info(
        f"VOC 转换完成: {n_ok} 成功, {n_skip} 跳过, 类别 {len(classes)} 种"
    )
    return classes


def _convert_one(
    xml_path: Path,
    output_labels_dir: Path,
    classes: List[str],
    auto_discover: bool,
) -> bool:
    """转一个 XML, 写一个 txt。返回 True 表示成功。"""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        logger.warning(f"{xml_path.name} XML 损坏: {e}, 跳过")
        return False

    root = tree.getroot()

    size = root.find("size")
    if size is None:
        logger.warning(f"{xml_path.name} 缺少 <size>, 跳过")
        return False
    W = float(size.findtext("width", "0"))
    H = float(size.findtext("height", "0"))
    if W <= 0 or H <= 0:
        logger.warning(f"{xml_path.name} 尺寸非法 (W={W}, H={H}), 跳过")
        return False

    lines: List[str] = []
    for obj in root.findall("object"):
        name = obj.findtext("name")
        if name is None:
            continue

        if name not in classes:
            if auto_discover:
                classes.append(name)
            else:
                logger.debug(f"{xml_path.name}: 类别 {name!r} 不在白名单, 跳过该 object")
                continue
        cls_id = classes.index(name)

        bbox = obj.find("bndbox")
        if bbox is None:
            continue
        try:
            xmin = float(bbox.findtext("xmin"))
            ymin = float(bbox.findtext("ymin"))
            xmax = float(bbox.findtext("xmax"))
            ymax = float(bbox.findtext("ymax"))
        except (TypeError, ValueError):
            logger.debug(f"{xml_path.name}: bndbox 数值非法, 跳过该 object")
            continue

        cx = (xmin + xmax) / 2 / W
        cy = (ymin + ymax) / 2 / H
        w  = (xmax - xmin) / W
        h  = (ymax - ymin) / H

        # 边界裁剪
        cx = max(0.0, min(1.0, cx))
        cy = max(0.0, min(1.0, cy))
        w  = max(0.0, min(1.0, w))
        h  = max(0.0, min(1.0, h))

        lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

    out_txt = output_labels_dir / (xml_path.stem + ".txt")
    out_txt.write_text("\n".join(lines), encoding="utf-8")
    return True
