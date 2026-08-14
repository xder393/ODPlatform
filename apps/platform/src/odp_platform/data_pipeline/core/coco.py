"""COCO JSON → YOLO txt converter, 走 ultralytics.convert_coco。

设计要点:
    - 不重造轮子: ultralytics 已有成熟实现, 调它
    - tempfile 中转: ultralytics 的输出结构跟我们不一样,
      先让它写到系统临时目录, 然后挑出我们要的 *.txt 平铺到
      output_labels_dir, 不污染 data/raw/ 也不污染仓库
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import List

from odp_platform.common.constants import AnnotationFormat, Task
from odp_platform.data_pipeline.registry import ConvertOptions, register

logger = logging.getLogger(__name__)


@register(
    AnnotationFormat.COCO,
    supported_tasks=(Task.DETECT, Task.SEGMENT),
)
def convert_coco(
    input_dir: Path,
    output_labels_dir: Path,
    options: ConvertOptions,
) -> List[str]:
    """把 input_dir 下的 COCO JSON 转 YOLO txt, 写到 output_labels_dir。

    Returns:
        类别名列表 (顺序与 COCO categories 的 'id' 升序一致)
    """
    # 第三方依赖延迟 import, 让模块导入更轻
    from ultralytics.data.converter import convert_coco as _ul_convert_coco

    # 1. 找 JSON
    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"在 {input_dir} 下未找到 COCO JSON")
    if len(json_files) > 1:
        logger.warning(
            f"找到多个 JSON, 仅使用第一个: {json_files[0].name}"
        )
    coco_json = json_files[0]

    # 2. 读 categories (用于返回类别名列表)
    classes = _read_classes(coco_json)
    logger.info(f"COCO 类别 {len(classes)} 种: {classes[:5]}...")

    # 3. ★ 关键: 用 tempfile 做中转, 不污染任何项目目录
    output_labels_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="odp_coco_"))
    logger.debug(f"COCO 临时中转目录: {tmp_root}")

    try:
        # ultralytics 把 coco_json 转成 yolo txt, 输出到 tmp_root 下
        _ul_convert_coco(
            labels_dir=str(input_dir),
            save_dir=str(tmp_root),
            use_segments=(options.task == Task.SEGMENT),
            cls91to80=options.coco_cls91to80,
        )

        # 从 tmp_root 找出所有 *.txt, 平铺复制到 output_labels_dir
        n_copied = _flatten_txts(tmp_root, output_labels_dir)
        logger.info(
            f"COCO 转换完成: {n_copied} 个 yolo txt 已写入 {output_labels_dir}"
        )

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_root, ignore_errors=True)

    return classes


def _read_classes(coco_json: Path) -> List[str]:
    """从 COCO JSON 的 categories 段读取类别名, 按 id 升序。"""
    data = json.loads(coco_json.read_text(encoding="utf-8"))
    cats = sorted(data.get("categories", []), key=lambda c: c["id"])
    return [c["name"] for c in cats]


def _flatten_txts(src_root: Path, dst_dir: Path) -> int:
    """递归找 src_root 下所有 *.txt, 平铺复制到 dst_dir。"""
    n = 0
    for txt in src_root.rglob("*.txt"):
        shutil.copy2(txt, dst_dir / txt.name)
        n += 1
    return n
