"""Contract parity tests for deterministic and ONNX vision adapters."""

from hashlib import sha256

import pytest

from odp_api.adapters.vision.mock import DeterministicMockVisionAdapter
from odp_api.adapters.vision.onnx import OnnxVisionAdapter, VisionConfigurationError
from odp_api.modules.tasks.commands import InferenceExecutionContract
from odp_api.ports.vision import FrameInput

CONTRACT = InferenceExecutionContract(
    model_release="tiny-detector-v1",
    model_sha256="a" * 64,
    onnxruntime_version="1.20.0",
    execution_provider="CPUExecutionProvider",
    actual_input_shape=(1, 3, 640, 640),
    preprocessing_version="letterbox-rgb-v1",
    postprocessing_version="nms-v1",
    confidence_threshold=0.5,
    iou_threshold=0.45,
    nms_mode="post_nms_xyxy",
    nms_in_model=False,
    class_map_version="classes-v1",
)


def test_deterministic_mock_emits_complete_execution_contract():
    result = DeterministicMockVisionAdapter().inspect(
        FrameInput(fixture_name="scratch", content=b"jpeg"), CONTRACT
    )

    assert result.execution.model_sha256 == CONTRACT.model_sha256
    assert result.execution.actual_input_shape == (1, 3, 640, 640)
    assert result.execution.preprocessing_version == "letterbox-rgb-v1"
    assert result.execution.postprocessing_version == "nms-v1"
    assert result.detections[0].defect_type == "scratch"


def test_onnx_adapter_verifies_model_before_session_and_nms_is_deterministic(tmp_path, monkeypatch):
    model = tmp_path / "tiny.onnx"
    model.write_bytes(b"model")
    model_hash = sha256(b"model").hexdigest()

    class Input:
        name = "images"

    class Session:
        def get_inputs(self):
            return [Input()]

        def run(self, _outputs, _feeds):
            return [
                [
                    [0, 0, 10, 10, 0.9, 0],
                    [0, 0, 10, 10, 0.9, 0],
                    [20, 20, 30, 30, 0.8, 0],
                ]
            ]

    monkeypatch.setattr("odp_api.adapters.vision.onnx._preprocess", lambda _content: object())
    adapter = OnnxVisionAdapter(
        model,
        model_sha256=model_hash,
        session_factory=lambda _path, _providers: Session(),
    )
    contract = InferenceExecutionContract(
        model_release="tiny",
        model_sha256=model_hash,
        onnxruntime_version="test",
        execution_provider="CPUExecutionProvider",
        actual_input_shape=(1, 3, 640, 640),
        preprocessing_version="letterbox-rgb-v1",
        postprocessing_version="nms-v1",
        confidence_threshold=0.5,
        iou_threshold=0.5,
        nms_mode="post_nms_xyxy",
        nms_in_model=True,
        class_map_version="classes-v1",
    )
    result = adapter.inspect(FrameInput("tiny", b"frame"), contract)
    assert [item.xyxy for item in result.detections] == [(0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 30.0, 30.0)]

    with pytest.raises(VisionConfigurationError):
        OnnxVisionAdapter(model, model_sha256="b" * 64)
