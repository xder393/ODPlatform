"""Offline ONNX smoke test; skipped only when optional runtime assets are absent."""

from hashlib import sha256
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")

import cv2
import numpy as np

from odp_api.adapters.vision.onnx import OnnxVisionAdapter
from odp_api.ports.vision import FrameInput

MODEL = Path(__file__).parents[1] / "fixtures/models/tiny-detector.onnx"


@pytest.mark.skipif(not MODEL.is_file(), reason="fixture model has not been built")
def test_tiny_onnx_detector_runs_cpu():
    adapter = OnnxVisionAdapter(
        MODEL,
        model_sha256=sha256(MODEL.read_bytes()).hexdigest(),
        class_map={0: "scratch"},
    )
    ok, encoded = cv2.imencode(".jpg", np.zeros((640, 640, 3), dtype=np.uint8))
    assert ok
    frame = FrameInput("tiny", bytes(encoded.tobytes()))
    result = adapter.inspect(frame)
    assert result.execution.execution_provider == "CPUExecutionProvider"
    assert result.detections[0].class_name == "scratch"
