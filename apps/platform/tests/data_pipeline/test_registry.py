"""检查注册表机制本身——格式都登记上了, 能力声明对得上。"""
import pytest

from odp_platform.common.constants import AnnotationFormat, Task
from odp_platform.data_pipeline import (
    ConvertOptions, convert_data_to_yolo, list_capabilities,
)
from odp_platform.data_pipeline.registry import get_converter


def test_all_formats_registered():
    caps = list_capabilities()
    for fmt in AnnotationFormat.all():
        assert fmt in caps, f"格式 {fmt} 未注册"


def test_pascal_voc_supports_detect_only():
    entry = get_converter("pascal_voc")
    assert entry.supports(Task.DETECT)
    assert not entry.supports(Task.SEGMENT)


def test_coco_supports_both():
    entry = get_converter("coco")
    assert entry.supports(Task.DETECT)
    assert entry.supports(Task.SEGMENT)


def test_yolo_supports_detect_and_segment():
    entry = get_converter("yolo")
    assert entry.supports(Task.DETECT)
    assert entry.supports(Task.SEGMENT)


def test_unknown_format_raises():
    with pytest.raises(ValueError, match="未注册"):
        get_converter("not_a_real_format")


def test_task_capability_check_at_service_layer(tmp_path):
    """service 层应该拦截不支持的 task。"""
    with pytest.raises(ValueError, match="不支持 task"):
        convert_data_to_yolo(
            input_dir=tmp_path,
            output_labels_dir=tmp_path / "out",
            annotation_format="pascal_voc",
            options=ConvertOptions(task=Task.SEGMENT),
        )
