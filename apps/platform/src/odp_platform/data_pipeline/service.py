"""data_pipeline 调度层——5 行接住所有格式。"""
from __future__ import annotations

from pathlib import Path
from typing import List

from odp_platform.data_pipeline.registry import (
    ConvertOptions, get_converter,
)


def convert_data_to_yolo(
    input_dir: Path,
    output_labels_dir: Path,
    annotation_format: str,
    options: ConvertOptions,
) -> List[str]:
    """统一入口: 根据 format 分发到具体 converter。

    Args:
        input_dir:         原始标注目录
        output_labels_dir: YOLO txt 输出目录
        annotation_format: 格式名 (见 AnnotationFormat)
        options:           ConvertOptions

    Returns:
        类别名列表 (顺序 = yolo class_id 顺序)

    Raises:
        ValueError: 格式未注册 或 该 converter 不支持请求的 task
    """
    entry = get_converter(annotation_format)

    if not entry.supports(options.task):
        raise ValueError(
            f"格式 {annotation_format!r} 不支持 task={options.task!r}。"
            f"支持: {entry.supported_tasks}"
        )

    return entry.func(input_dir, output_labels_dir, options)
