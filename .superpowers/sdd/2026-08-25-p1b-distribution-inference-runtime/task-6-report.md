# Task 6 — Deterministic Mock and ONNX adapters

## Scope delivered

- Expanded the vision port to carry the complete immutable execution
  contract, typed detections, frame digest, and stage durations while keeping
  the P0 fixture metadata compatibility view.
- Added `DeterministicMockVisionAdapter` as the stable offline adapter and
  preserved the existing `MockVisionAdapter` entry point.
- Added a lazy ONNX adapter with model-file SHA verification before session
  creation, CPU provider enforcement, frozen 640×640 RGB letterbox
  preprocessing, `post_nms_xyxy` and `yolo_raw` modes, class-aware deterministic
  NMS, and explicit model/configuration errors.
- Added the offline fixture builder script and an environment-gated ONNX
  smoke test.
- Declared runtime/dev dependencies for ONNX Runtime and ONNX in addition to
  the Task 4/5 media dependencies.

## Verification evidence

- Vision contract + fake-session ONNX tests: 2 passed.
- Full backend: 394 passed, 30 skipped, 1 warning.
- Ruff on source/tests/scripts/migrations: clean.
- `compileall`: clean.
- `git diff --check`: clean.

## Environment gate

The current environment has no `onnxruntime`, `onnx`, OpenCV, or NumPy wheels,
and cannot resolve PyPI. Therefore the real ONNX smoke and fixture generation
remain explicit dependency-gated skips; the committed fixture binary and
`uv.lock` refresh must be produced when network access is available. No model
or lock hashes were fabricated.
