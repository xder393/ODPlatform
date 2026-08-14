"""TrainMetrics + log_train_metrics 行为契约测试."""
from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from odp_platform.common.result import TrainMetrics, log_train_metrics


def test_train_metrics_from_det_results(mock_det_results):
    metrics = TrainMetrics.from_yolo_results(mock_det_results)
    assert metrics.task == "detect"
    assert metrics.overall["fitness"] == pytest.approx(0.5805)
    assert metrics.overall["metrics/mAP50(B)"] == pytest.approx(0.6912)
    assert "person" in metrics.class_map_50_95


def test_train_metrics_speed_total_excludes_nan(mock_det_results):
    """speed_ms['total'] = sum(非 nan), 4 项有效 = 一切 OK."""
    mock_det_results.speed["loss"] = None    # 让 loss 变 nan
    metrics = TrainMetrics.from_yolo_results(mock_det_results)
    # total 应当只包括 preprocess + inference + postprocess
    expected = 1.234 + 12.345 + 0.567
    assert metrics.speed_ms["total"] == pytest.approx(expected, abs=1e-3)


def test_to_dict_nan_converts_to_none():
    """to_dict 时 NaN → None, 让 JSON 能序列化."""
    metrics = TrainMetrics(
        task="detect", save_dir=Path("/tmp"),
        timestamp="2026-05-24T10:00:00",
        speed_ms={"preprocess": math.nan},
        overall={"fitness": 0.5},
    )
    d = metrics.to_dict()
    assert d["speed_ms"]["preprocess"] is None    # NaN → None
    assert d["overall"]["fitness"] == 0.5


def test_log_train_metrics_unknown_task_falls_back(caplog):
    """task='unknown' 时打 results_dict 全量, 不崩."""
    caplog.set_level(logging.INFO)

    metrics = TrainMetrics(
        task="unknown", save_dir=Path("/tmp"),
        timestamp="2026-05-24T10:00:00",
        speed_ms={}, overall={"fitness": 0.5, "metric_x": 0.7},
    )
    log_train_metrics(metrics)
    assert "task='unknown' 不在" in caplog.text   # 兜底分支被走到
    assert "metric_x" in caplog.text              # 全量打印


def test_segment_task_logs_8_metrics(mock_segment_results, caplog):
    """segment 任务 8 个指标全部 log."""
    caplog.set_level(logging.INFO)

    metrics = TrainMetrics.from_yolo_results(mock_segment_results)
    log_train_metrics(metrics)
    for k in ("mAP50(B)", "mAP50(M)", "Precision(M)", "Recall(M)"):
        assert k in caplog.text
