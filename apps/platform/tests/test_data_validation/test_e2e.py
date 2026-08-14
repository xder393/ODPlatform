"""测 validate_dataset 端到端 — 用 tmp_path 造 toy 数据集。

这条测试故意用【真文件】而不是 mock — 数据流复杂的系统,
mock 出来的测试覆盖率高但什么也没真测到。
"""
from pathlib import Path

from odp_platform.data_validation import validate_dataset


def _make_toy_dataset(root: Path, with_leak: bool = False):
    """造一个最小可用 YOLO 数据集结构。"""
    for split in ("train", "val"):
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (root / "images" / split / f"img_{split}_{i}.jpg").write_bytes(b"fake")
            (root / "labels" / split / f"img_{split}_{i}.txt").write_text(
                "0 0.5 0.5 0.1 0.1\n"
            )

    if with_leak:
        # 把 train 的一张图复制到 val (制造数据泄露)
        (root / "images" / "val" / "img_train_0.jpg").write_bytes(b"fake")
        (root / "labels" / "val" / "img_train_0.txt").write_text("0 0.5 0.5 0.1 0.1\n")

    yaml = root / "data.yaml"
    yaml.write_text(
        "nc: 2\n"
        "names: [cat, dog]\n"
        "path: .\n"
        "train: images/train\n"
        "val: images/val\n"
    )
    return yaml


def test_e2e_healthy_dataset(tmp_path):
    yaml = _make_toy_dataset(tmp_path)
    report = validate_dataset(yaml, task_type="detect", write_report=False)

    assert report.exit_code == 0
    assert report.overall_severity == "PASS"
    assert len(report.results) == 4
    assert all(r.severity == "PASS" for r in report.results)


def test_e2e_data_leak_detected(tmp_path):
    yaml = _make_toy_dataset(tmp_path, with_leak=True)
    report = validate_dataset(yaml, task_type="detect", write_report=False)

    assert report.exit_code == 2
    assert report.overall_severity == "ERROR"
    leak_result = next(r for r in report.results if r.name == "split_uniqueness")
    assert leak_result.severity == "ERROR"
    assert leak_result.details["total_duplicates"] == 1


def test_e2e_json_report_written(tmp_path):
    yaml = _make_toy_dataset(tmp_path)
    report = validate_dataset(
        yaml, task_type="detect",
        run_dir=tmp_path / "run_out",
        write_report=True,
    )

    assert report.report_path is not None
    assert report.report_path.exists()

    import json
    data = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert data["overall_severity"] == "PASS"
    assert data["exit_code"] == 0
    assert "dataset_summary" in data
    assert "results" in data
