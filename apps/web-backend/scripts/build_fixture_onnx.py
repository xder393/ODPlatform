"""Build and validate the fixed-output detector used for pipeline tests.

This fixture always emits a scratch; it does not measure model accuracy.
"""

from pathlib import Path


def build(output: Path) -> None:
    import onnx
    from onnx import TensorProto, helper

    value = helper.make_tensor(
        "fixture_detection", TensorProto.FLOAT, [1, 1, 6],
        [64.0, 64.0, 576.0, 576.0, 0.95, 0.0],
    )
    graph = helper.make_graph(
        [helper.make_node("Constant", [], ["detections"], value=value)],
        "tiny-detector",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 640, 640])],
        [helper.make_tensor_value_info("detections", TensorProto.FLOAT, [1, 1, 6])],
    )
    model = helper.make_model(
        graph, producer_name="odp-platform", ir_version=8,
        opset_imports=[helper.make_opsetid("", 13)],
    )
    onnx.checker.check_model(model, full_check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(model.SerializeToString(deterministic=True))


if __name__ == "__main__":
    build(Path(__file__).parents[1] / "tests/fixtures/models/tiny-detector.onnx")
