"""Runtime composition contracts for independently deployed P1 processes."""

from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from odp_api.modules.tasks.commands import InferenceExecutionContract, LeaseClaim
from odp_api.processes.inference_worker import LoadedArtifact

PROCESS_MODULES = (
    "frame_ingestor",
    "outbox_relay",
    "inference_worker",
    "recovery_scheduler",
    "artifact_reconciler",
    "stream_retention",
)


@pytest.mark.parametrize("module_name", PROCESS_MODULES)
def test_each_process_exposes_a_composition_entrypoint(module_name: str):
    module = import_module(f"odp_api.processes.{module_name}")

    assert callable(getattr(module, "main", None))
    builder = getattr(module, "_build_process", None)
    assert callable(builder)
    assert len(signature(builder).parameters) == 1


def test_runtime_adapters_keep_database_and_provider_boundaries_explicit():
    runtime = import_module("odp_api.processes.runtime")

    assert callable(runtime.build_database)
    assert callable(runtime.build_storage)
    assert callable(runtime.build_redis)
    assert callable(runtime.build_worker)
    assert callable(runtime.build_ingestor)


def test_compose_model_fixture_exists_and_matches_the_pinned_sha256():
    root = Path(__file__).parents[4]
    model = root / "apps/web-backend/tests/fixtures/models/tiny-detector.onnx"
    assert model.is_file()
    digest = sha256(model.read_bytes()).hexdigest()
    compose = (root / "deploy/compose.yaml").read_text()
    assert f"ODP_MODEL_SHA256: ${{ODP_MODEL_SHA256:-{digest}}}" in compose


def test_source_factory_rejects_unknown_source_type():
    runtime = import_module("odp_api.processes.runtime")

    with pytest.raises(ValueError, match="unsupported inspection source type"):
        runtime.source_from_session(
            type(
                "Session",
                (),
                {
                    "source_type": "USB",
                    "sanitized_uri": "/dev/video0",
                    "camera_id": None,
                    "session_id": None,
                },
            )()
        )


@pytest.mark.parametrize(
    ("source_type", "uri", "expected"),
    [
        ("RECORDED", "/tmp/demo.mp4", "RecordedVideoSource"),
        ("RTSP", "rtsp://camera.example/line-1", "RtspSource"),
        ("LOCAL_CAMERA", "0", "LocalCameraSource"),
    ],
)
def test_source_factory_resolves_validated_source_types(source_type, uri, expected):
    runtime = import_module("odp_api.processes.runtime")
    source = runtime.source_from_session(
        SimpleNamespace(
            source_type=source_type,
            sanitized_uri=uri,
            camera_id=uuid4(),
            session_id=uuid4(),
        )
    )
    assert type(source).__name__ == expected


@pytest.mark.anyio
async def test_vision_adapter_preserves_reproducible_result_contract():
    runtime = import_module("odp_api.processes.runtime")
    execution = InferenceExecutionContract(
        model_release="fixture",
        model_sha256="a" * 64,
        onnxruntime_version="test",
        execution_provider="CPUExecutionProvider",
        actual_input_shape=(1, 3, 640, 640),
        preprocessing_version="letterbox-rgb-v1",
        postprocessing_version="nms-v1",
        confidence_threshold=0.8,
        iou_threshold=0.45,
        nms_mode="post_nms_xyxy",
        nms_in_model=True,
        class_map_version="classes-v1",
    )

    class FakeVision:
        def inspect(self, frame, contract=None):
            from odp_api.ports.vision import VisionResult

            assert frame.content == b"jpeg"
            return VisionResult(execution, "a" * 64, ())

    adapter = runtime.VisionInferenceAdapter(FakeVision())
    claim = LeaseClaim(uuid4(), uuid4(), uuid4(), uuid4(), 1, 1, "worker", datetime.now(UTC))
    result = await adapter.infer(LoadedArtifact(b"jpeg", "a" * 64), claim)
    assert result.execution_contract == execution
    assert result.frame_sha256 == "a" * 64
