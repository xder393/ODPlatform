"""tests/common/ 共用 fixture.

mock_det_results / mock_segment_results 服务 test_result.py — 仿真 ultralytics
DetMetrics / SegmentMetrics 对象的 attribute 形状, 不依赖真实 ultralytics 安装.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture
def mock_det_results():
    """仿真 ultralytics DetMetrics 对象."""
    mock = MagicMock()
    mock.task = "detect"
    mock.save_dir = Path("/tmp/runs/detect_train/train3")
    mock.fitness = 0.5805
    mock.speed = {
        "preprocess": 1.234,
        "inference": 12.345,
        "loss": 0.123,
        "postprocess": 0.567,
    }
    mock.results_dict = {
        "metrics/precision(B)": 0.7234,
        "metrics/recall(B)": 0.6543,
        "metrics/mAP50(B)": 0.6912,
        "metrics/mAP50-95(B)": 0.4321,
        "fitness": 0.5805,
    }
    mock.maps = np.array([0.4521, 0.3812, 0.2103])
    mock.names = {0: "person", 1: "car", 2: "bicycle"}
    return mock


@pytest.fixture
def mock_segment_results():
    """仿真 ultralytics SegmentMetrics — 比 det 多 4 个 mask 指标."""
    mock = MagicMock()
    mock.task = "segment"
    mock.save_dir = Path("/tmp/runs/segment_train/train1")
    mock.fitness = 0.6123
    mock.speed = {
        "preprocess": 1.0, "inference": 15.0, "loss": 0.2, "postprocess": 0.8,
    }
    mock.results_dict = {
        "metrics/precision(B)": 0.72, "metrics/recall(B)": 0.65,
        "metrics/mAP50(B)": 0.70,     "metrics/mAP50-95(B)": 0.45,
        "metrics/precision(M)": 0.68, "metrics/recall(M)": 0.62,
        "metrics/mAP50(M)": 0.66,     "metrics/mAP50-95(M)": 0.42,
        "fitness": 0.6123,
    }
    mock.maps = np.array([0.55, 0.48])
    mock.names = {0: "person", 1: "vehicle"}
    return mock
