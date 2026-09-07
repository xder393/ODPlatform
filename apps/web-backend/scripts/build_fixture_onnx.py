"""Build the tiny deterministic ONNX detector fixture used by CI."""

from pathlib import Path


def build(output: Path) -> None:
    try:
        import numpy as np
        import onnx
        from onnx import TensorProto, helper, numpy_helper
    except ImportError as error:  # pragma: no cover - build environment only
        raise SystemExit("install the backend dev dependencies before building the fixture") from error

    input_info = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 640, 640])
    output_info = helper.make_tensor_value_info("detections", TensorProto.FLOAT, [1, 1, 6])
    constant = helper.make_node(
        "Constant",
        inputs=[],
        outputs=["detections"],
        value=numpy_helper.from_array(
            np.asarray([[[64.0, 64.0, 576.0, 576.0, 0.95, 0.0]]], dtype=np.float32),
        ),
    )
    graph = helper.make_graph([constant], "tiny-detector", [input_info], [output_info])
    model = helper.make_model(graph, producer_name="odp-platform", opset_imports=[helper.make_opsetid("", 13)])
    output.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, output)


if __name__ == "__main__":
    build(Path(__file__).parents[1] / "tests/fixtures/models/tiny-detector.onnx")
