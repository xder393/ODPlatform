"""TrainService 单元测试.

★ 关键: service 通过 import 别名调用 D5/D4/ultralytics, 要 patch
`odp_platform.training.service.xxx` 而不是 `odp_platform.runtime_config.xxx`
(patch 的是 service 模块**内部已经持有**的引用).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from odp_platform.training import TrainResult, TrainService


def _make_config_mock(task="detect"):
    cfg = MagicMock()
    cfg.task = task
    cfg.model = "yolo11n.pt"
    cfg.data = "rsod.yaml"
    cfg.to_ultralytics_kwargs.return_value = {
        "data": "/abs/rsod.yaml", "model": "yolo11n.pt",
        "epochs": 100, "batch": 16, "imgsz": 640,
    }
    cfg.to_audit_snapshot.return_value = {"task": task, "model": "yolo11n.pt"}
    cfg.__class__.model_fields = {"epochs": ..., "batch": ..., "imgsz": ...}
    return cfg


def test_train_success_full_flow(tmp_path):
    """成功路径全流程 — 验证 8 阶段都被调到."""
    save_dir = tmp_path / "runs" / "detect_train" / "train1" ; save_dir.mkdir(parents=True)
    (save_dir / "weights").mkdir() ; (save_dir / "weights" / "best.pt").touch()
    (save_dir / "weights" / "last.pt").touch()

    cfg = _make_config_mock()
    merger = MagicMock()
    merger.to_audit_log.return_value = {"fields": {}}

    fake_results = MagicMock()
    fake_results.save_dir = save_dir
    fake_results.task = "detect"
    fake_results.fitness = 0.5
    fake_results.speed = {"preprocess": 1, "inference": 10, "loss": 0, "postprocess": 0}
    fake_results.results_dict = {"fitness": 0.5}
    fake_results.maps = MagicMock()
    fake_results.maps.size = 0
    fake_results.names = {}

    with patch("odp_platform.training.service.build_train_config", return_value=(cfg, merger)), \
         patch("odp_platform.training.service.validate_dataset", return_value=MagicMock(exit_code=0, results=[])), \
         patch("odp_platform.training.service.render_to_logger"), \
         patch("odp_platform.training.service.YOLO") as yolo_cls, \
         patch("odp_platform.training.service.log_device_info"), \
         patch("odp_platform.training.service.archive_checkpoints", return_value={}), \
         patch("odp_platform.training.service.rename_log_to_save_dir"):
        yolo_cls.return_value.train.return_value = fake_results
        result = TrainService().train(cli_args={"epochs": 1})

    assert isinstance(result, TrainResult)
    assert result.success is True
    assert result.output_dir == save_dir


def test_train_validation_fail_returns_failure(tmp_path):
    """D4 校验报 ERROR — service 不抛, 装进 result.error."""
    cfg = _make_config_mock()
    merger = MagicMock()
    bad_report = MagicMock()
    bad_report.exit_code = 2     # ERROR 级
    bad_report.results = [MagicMock(severity="ERROR")]

    with patch("odp_platform.training.service.build_train_config", return_value=(cfg, merger)), \
         patch("odp_platform.training.service.validate_dataset", return_value=bad_report), \
         patch("odp_platform.training.service.render_to_logger"), \
         patch("odp_platform.training.service.log_device_info"):
        result = TrainService().train(cli_args={})

    assert result.success is False
    assert "数据集校验失败" in result.error


def test_train_yolo_runtime_error_caught(tmp_path):
    """ultralytics 训练时抛 RuntimeError — service 不传染, 装进 result.error."""
    cfg = _make_config_mock()
    merger = MagicMock()

    with patch("odp_platform.training.service.build_train_config", return_value=(cfg, merger)), \
         patch("odp_platform.training.service.validate_dataset", return_value=MagicMock(exit_code=0, results=[])), \
         patch("odp_platform.training.service.render_to_logger"), \
         patch("odp_platform.training.service.YOLO") as yolo_cls, \
         patch("odp_platform.training.service.log_device_info"):
        yolo_cls.return_value.train.side_effect = RuntimeError("CUDA OOM")
        result = TrainService().train(cli_args={})

    assert result.success is False
    assert "CUDA OOM" in result.error
