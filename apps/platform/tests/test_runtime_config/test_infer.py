"""YOLOInferConfig 行为契约."""
from __future__ import annotations

import warnings
import pytest
from pydantic import ValidationError

from odp_platform.common.constants     import Task
from odp_platform.runtime_config.infer import YOLOInferConfig


# ============================================================
# source 字段行为
# ============================================================

class TestInferSource:
    """source — 推理特有, 跟 train/val 的 data 字段并列"""

    def test_source_default_none(self):
        assert YOLOInferConfig().source is None

    def test_source_str(self):
        c = YOLOInferConfig(source="image.jpg")
        assert c.source == "image.jpg"

    def test_source_int_camera_index(self):
        """摄像头索引 int 应标准化为 str."""
        c = YOLOInferConfig(source=0)
        assert c.source == "0"

    def test_source_rejects_invalid_type(self):
        with pytest.raises((ValidationError, TypeError)):
            YOLOInferConfig(source=[1, 2, 3])

    def test_source_in_ultralytics_kwargs(self):
        """source 必须传给 ultralytics."""
        c = YOLOInferConfig(source="x.jpg")
        kw = c.to_ultralytics_kwargs()
        assert kw.get("source") == "x.jpg"


# ============================================================
# conf 默认值与 val 不同 (语义差异)
# ============================================================

def test_infer_conf_default_differs_from_val():
    """infer 默认 conf=0.25, 不同于 val 的 0.001."""
    assert YOLOInferConfig().conf == 0.25


def test_infer_conf_range():
    """conf 必须在 [0, 1] 范围."""
    with pytest.raises(ValidationError):
        YOLOInferConfig(conf=1.5)
    with pytest.raises(ValidationError):
        YOLOInferConfig(conf=-0.1)


# ============================================================
# FRAMEWORK_ONLY_FIELDS 同 val
# ============================================================

def test_framework_only_fields_match_val():
    assert YOLOInferConfig.FRAMEWORK_ONLY_FIELDS == (
        {"verbose", "experiment_name", "task"}
    )


def test_task_not_in_ultralytics_kwargs():
    c = YOLOInferConfig(source="x.jpg", task=Task.DETECT)
    kw = c.to_ultralytics_kwargs()
    assert "task" not in kw
    assert "experiment_name" not in kw


# ============================================================
# task SSoT (跟 train/val 同款规则)
# ============================================================

@pytest.mark.parametrize("invalid_task", [
    "detect_v2",
    "Detection",
    "predict",       # 推理时容易写的错值, 但还是要拒
    "infer",         # 同上
])
def test_infer_task_rejects_invalid(invalid_task):
    with pytest.raises(ValidationError) as exc_info:
        YOLOInferConfig(task=invalid_task)
    # 错误信息必须指引 experiment_name 字段
    assert "experiment_name" in str(exc_info.value)


# ============================================================
# 跨字段警告 (4 个场景全 warn 不 raise)
# ============================================================

class TestInferCrossFieldWarnings:
    def test_save_conf_without_save_txt_warns(self):
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            YOLOInferConfig(save_conf=True, save_txt=False)
            assert any(
                "save_conf=True 但 save_txt=False" in str(w.message)
                for w in ws
            )

    def test_save_conf_with_save_txt_silent(self):
        """save_conf=True 且 save_txt=True 不应该警告."""
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            YOLOInferConfig(save_conf=True, save_txt=True)
            assert not any(
                "save_conf=True 但 save_txt=False" in str(w.message)
                for w in ws
            )

    def test_stream_buffer_without_stream_warns(self):
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            YOLOInferConfig(stream_buffer=True, stream=False)
            assert any(
                "stream_buffer=True 但 stream=False" in str(w.message)
                for w in ws
            )

    def test_retina_masks_in_detect_task_warns(self):
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            YOLOInferConfig(retina_masks=True, task=Task.DETECT)
            assert any(
                "retina_masks=True 仅对 segment" in str(w.message)
                for w in ws
            )

    def test_retina_masks_in_segment_task_silent(self):
        """segment 任务下 retina_masks=True 不应该警告."""
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            YOLOInferConfig(retina_masks=True, task=Task.SEGMENT)
            assert not any(
                "retina_masks=True 仅对 segment" in str(w.message)
                for w in ws
            )


# ============================================================
# 字段分组 (★ 反射出 yaml 模板的基础)
# ============================================================

def test_infer_field_groups():
    """infer 字段分组完整."""
    c = YOLOInferConfig()
    groups = c.get_field_groups()
    expected = {
        "核心参数", "推理控制", "视频流", "输出控制", "任务特定",
        # BaseConfig 继承的组
        "输入配置", "设备配置", "输出配置", "基础设置",
    }
    assert expected <= set(groups.keys()), \
        f"缺失分组: {expected - set(groups.keys())}"


# ============================================================
# audit_snapshot 也得支持 (实验复现关键)
# ============================================================

def test_infer_audit_snapshot_roundtrip():
    c = YOLOInferConfig(source="video.mp4", conf=0.5)
    snapshot = c.to_audit_snapshot()
    assert snapshot["config_class"] == "YOLOInferConfig"
    restored = YOLOInferConfig.from_audit_snapshot(snapshot)
    assert restored.source == "video.mp4"
    assert restored.conf == 0.5
