"""Deterministic CPU ONNX vision adapter with explicit preprocessing metadata."""

from __future__ import annotations

import hashlib
import importlib
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from odp_api.modules.tasks.commands import Detection, InferenceExecutionContract
from odp_api.ports.vision import FrameInput, VisionResult, default_mock_contract


class VisionConfigurationError(RuntimeError):
    """The model bytes, provider, output mode, or contract is incompatible."""


class OnnxVisionAdapter:
    """Run one verified model with CPU-only deterministic postprocessing."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        model_sha256: str,
        class_map: dict[int, str] | None = None,
        output_mode: str = "post_nms_xyxy",
        providers: Sequence[str] = ("CPUExecutionProvider",),
        input_size: tuple[int, int] = (640, 640),
        session_factory: Callable[[str, Sequence[str]], Any] | None = None,
    ) -> None:
        path = Path(model_path)
        if output_mode not in {"post_nms_xyxy", "yolo_raw"}:
            raise ValueError("unsupported ONNX output mode")
        if not path.is_file():
            raise VisionConfigurationError("model file does not exist")
        actual_hash = _file_sha256(path)
        if actual_hash.lower() != model_sha256.lower():
            raise VisionConfigurationError("model SHA-256 does not match configured release")
        if tuple(input_size) != (640, 640):
            raise VisionConfigurationError("only the frozen 640x640 input is supported")
        if not providers or "CPUExecutionProvider" not in providers:
            raise VisionConfigurationError("CPUExecutionProvider is required")
        self._path = path
        self._model_sha256 = actual_hash
        self._class_map = dict(class_map or {0: "scratch"})
        self._output_mode = output_mode
        self._providers = tuple(providers)
        if session_factory is None:
            try:
                runtime = importlib.import_module("onnxruntime")
            except ImportError as error:  # pragma: no cover - runtime image only
                raise VisionConfigurationError("onnxruntime is required") from error
            session_factory = lambda value, selected: runtime.InferenceSession(
                value, providers=list(selected)
            )
        self._session = session_factory(str(path), self._providers)
        inputs = self._session.get_inputs()
        if len(inputs) != 1:
            raise VisionConfigurationError("ONNX model must expose one input")
        self._input_name = inputs[0].name
        self._runtime_version = _runtime_version()

    def inspect(
        self,
        frame: FrameInput,
        contract: InferenceExecutionContract | None = None,
    ) -> VisionResult:
        execution = contract or self._default_contract()
        self._validate_contract(execution)
        started = time.perf_counter()
        tensor = _preprocess(frame.content)
        preprocess_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        outputs = self._session.run(None, {self._input_name: tensor})
        inference_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        rows = _rows_from_outputs(outputs)
        if self._output_mode == "yolo_raw":
            rows = _decode_yolo_rows(rows, execution.confidence_threshold, len(self._class_map))
        detections = _detections_from_rows(
            _nms(rows, execution.iou_threshold),
            self._class_map,
            execution.confidence_threshold,
        )
        postprocess_ms = (time.perf_counter() - started) * 1000
        return VisionResult(
            execution=execution,
            frame_sha256=hashlib.sha256(frame.content).hexdigest(),
            detections=tuple(detections),
            stage_durations=(
                ("preprocess_ms", preprocess_ms),
                ("inference_ms", inference_ms),
                ("postprocess_ms", postprocess_ms),
            ),
        )

    def _default_contract(self) -> InferenceExecutionContract:
        base = default_mock_contract()
        return InferenceExecutionContract(
            model_release=self._path.stem,
            model_sha256=self._model_sha256,
            onnxruntime_version=self._runtime_version,
            execution_provider="CPUExecutionProvider",
            actual_input_shape=(1, 3, 640, 640),
            preprocessing_version="letterbox-rgb-v1",
            postprocessing_version="nms-v1",
            confidence_threshold=base.confidence_threshold,
            iou_threshold=base.iou_threshold,
            nms_mode=self._output_mode,
            nms_in_model=self._output_mode == "post_nms_xyxy",
            class_map_version="classes-v1",
        )

    def _validate_contract(self, contract: InferenceExecutionContract) -> None:
        if contract.model_sha256.lower() != self._model_sha256.lower():
            raise VisionConfigurationError("execution contract model hash mismatch")
        if contract.actual_input_shape != (1, 3, 640, 640):
            raise VisionConfigurationError("execution contract input shape mismatch")
        if contract.execution_provider not in self._providers:
            raise VisionConfigurationError("execution contract provider is unavailable")
        if contract.preprocessing_version != "letterbox-rgb-v1":
            raise VisionConfigurationError("unsupported preprocessing version")
        if contract.postprocessing_version != "nms-v1":
            raise VisionConfigurationError("unsupported postprocessing version")
        if contract.nms_mode != self._output_mode:
            raise VisionConfigurationError("execution contract output mode mismatch")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_version() -> str:
    try:
        return str(importlib.import_module("onnxruntime").__version__)
    except ImportError:
        return "onnxruntime-unavailable"


def _preprocess(content: bytes):
    try:
        import cv2
        import numpy as np
    except ImportError as error:  # pragma: no cover - runtime image only
        raise VisionConfigurationError("opencv and numpy are required") from error
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("input frame is not a decodable image")
    height, width = image.shape[:2]
    scale = min(640 / width, 640 / height)
    resized = cv2.resize(image, (round(width * scale), round(height * scale)))
    canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
    top, left = (640 - resized.shape[0]) // 2, (640 - resized.shape[1]) // 2
    canvas[top : top + resized.shape[0], left : left + resized.shape[1]] = resized
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    return (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]


def _rows_from_outputs(outputs: Sequence[Any]) -> list[tuple[float, float, float, float, float, int]]:
    if not outputs:
        return []
    rows = outputs[0]
    shape = getattr(rows, "shape", None)
    if shape is not None and len(shape) > 2:
        rows = rows.reshape((-1, shape[-1]))
    else:
        while (
            isinstance(rows, (list, tuple))
            and len(rows) == 1
            and isinstance(rows[0], (list, tuple))
            and rows[0]
            and isinstance(rows[0][0], (list, tuple))
        ):
            rows = rows[0]
    normalized = []
    for row in rows:
        if len(row) < 6:
            raise VisionConfigurationError("ONNX detection output must contain six values")
        normalized.append(tuple(float(value) for value in row[:5]) + (int(row[5]),))
    return normalized


def _decode_yolo_rows(rows, threshold: float, class_count: int):
    decoded = []
    for row in rows:
        if len(row) == 6:
            decoded.append(row)
            continue
        if len(row) < 5 + class_count:
            raise VisionConfigurationError("YOLO output width is incompatible with class map")
        cx, cy, width, height, objectness = row[:5]
        scores = row[5 : 5 + class_count]
        class_id = max(range(class_count), key=lambda index: (scores[index], -index))
        confidence = objectness * scores[class_id]
        if confidence >= threshold:
            decoded.append(
                (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2, confidence, class_id)
            )
    return decoded


def _nms(rows, iou_threshold: float):
    ordered = sorted(enumerate(rows), key=lambda item: (-item[1][4], item[0]))
    selected = []
    for original_index, row in ordered:
        if all(
            row[5] != previous[5] or _iou(row, previous) <= iou_threshold
            for _, previous in selected
        ):
            selected.append((original_index, row))
    return [row for _, row in selected]


def _iou(left, right) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_left = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    area_right = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = area_left + area_right - intersection
    return intersection / union if union else 0.0


def _detections_from_rows(rows, class_map: dict[int, str], threshold: float):
    detections = []
    for x1, y1, x2, y2, confidence, class_id in rows:
        if confidence < threshold or class_id not in class_map:
            continue
        name = class_map[class_id]
        detections.append(
            Detection(
                class_id=class_id,
                class_name=name,
                defect_type=name,
                severity="MEDIUM",
                confidence=confidence,
                xyxy=(x1, y1, x2, y2),
                spatial_zone="GLOBAL",
            )
        )
    return detections


__all__ = ["OnnxVisionAdapter", "VisionConfigurationError"]
