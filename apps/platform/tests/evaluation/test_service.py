"""ValService 单元测试.

★ 关键: service 通过 import 别名调用 D5/D4/ultralytics, 要 patch
`odp_platform.evaluation.service.xxx` 而不是 `odp_platform.runtime_config.xxx`
(patch 的是 service 模块**内部已经持有**的引用).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from odp_platform.evaluation import ValResult, ValService


def _make_config_mock(task="detect"):
    cfg = MagicMock()
    cfg.task = task
    cfg.model = "yolo11n.pt"
    cfg.data = "rsod.yaml"
    cfg.split = "val"
    cfg.to_ultralytics_kwargs.return_value = {
        "data": "/abs/rsod.yaml", "split": "val", "imgsz": 640, "conf": 0.001,
    }
    cfg.to_audit_snapshot.return_value = {"task": task, "model": "yolo11n.pt"}
    cfg.__class__.model_fields = {"split": ..., "imgsz": ..., "conf": ...}
    return cfg


def _make_val_metrics_mock(save_dir: Path) -> MagicMock:
    """仿真 ultralytics model.val() 返回的 Metrics 对象."""
    m = MagicMock()
    m.save_dir = save_dir
    m.speed = {"preprocess": 1, "inference": 10, "loss": 0, "postprocess": 0}
    m.results_dict = {"metrics/mAP50(B)": 0.9, "metrics/mAP50-95(B)": 0.8}
    m.fitness = 0.8
    m.names = {}
    m.maps = MagicMock()
    m.maps.size = 0
    return m


def test_val_success_full_flow(tmp_path):
    """成功路径全流程 — 验证关键阶段都被调到, 且 task 显式覆盖."""
    save_dir = tmp_path / "runs" / "detect_val" / "val1"
    save_dir.mkdir(parents=True)

    cfg = _make_config_mock()
    merger = MagicMock()
    merger.to_audit_log.return_value = {"fields": {}}

    val_metrics = _make_val_metrics_mock(save_dir)

    with patch("odp_platform.evaluation.service.build_val_config", return_value=(cfg, merger)), \
         patch("odp_platform.evaluation.service.validate_dataset", return_value=MagicMock(exit_code=0, results=[])), \
         patch("odp_platform.evaluation.service.render_to_logger"), \
         patch("odp_platform.evaluation.service.YOLO") as yolo_cls, \
         patch("odp_platform.evaluation.service.log_device_info"), \
         patch("odp_platform.evaluation.service.rename_log_to_save_dir"):
        yolo_cls.return_value.val.return_value = val_metrics
        result = ValService().val(cli_args={"split": "test"})

    assert isinstance(result, ValResult)
    assert result.success is True
    assert result.output_dir == save_dir
    # task 显式覆盖后, metrics 里应带上 mAP 指标
    assert "metrics/mAP50(B)" in result.metrics


def test_val_validation_fail_returns_failure(tmp_path):
    """D4 校验报 ERROR — service 不抛, 装进 result.error."""
    cfg = _make_config_mock()
    merger = MagicMock()
    bad_report = MagicMock()
    bad_report.exit_code = 2     # ERROR 级
    bad_report.results = [MagicMock(severity="ERROR")]

    with patch("odp_platform.evaluation.service.build_val_config", return_value=(cfg, merger)), \
         patch("odp_platform.evaluation.service.validate_dataset", return_value=bad_report), \
         patch("odp_platform.evaluation.service.render_to_logger"), \
         patch("odp_platform.evaluation.service.log_device_info"):
        result = ValService().val(cli_args={})

    assert result.success is False
    assert "数据集校验失败" in result.error


def test_val_missing_ultralytics_returns_failure(tmp_path):
    """ultralytics 未装 — service 不抛, 返回明确错误."""
    cfg = _make_config_mock()
    merger = MagicMock()

    with patch("odp_platform.evaluation.service.build_val_config", return_value=(cfg, merger)), \
         patch("odp_platform.evaluation.service.validate_dataset", return_value=MagicMock(exit_code=0, results=[])), \
         patch("odp_platform.evaluation.service.render_to_logger"), \
         patch("odp_platform.evaluation.service.log_device_info"), \
         patch("odp_platform.evaluation.service.YOLO", None):
        result = ValService().val(cli_args={})

    assert result.success is False
    assert "ultralytics 未安装" in result.error
