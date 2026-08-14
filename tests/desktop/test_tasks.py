"""桌面端任务层的单元测试 (纯函数, 不依赖 PySide6/torch).

通过 monkeypatch paths 模块, 不碰真实 data/ 和 runs/.
"""
import json
import os
from pathlib import Path

from odp_desktop import tasks


def _make_voc_dataset(root: Path, name: str = "demo") -> Path:
    d = root / name
    (d / "images").mkdir(parents=True)
    (d / "annotations").mkdir(parents=True)
    for i in range(4):
        (d / "images" / f"img_{i}.jpg").write_bytes(b"fake")
        (d / "annotations" / f"img_{i}.xml").write_text(
            "<annotation><object><name>helmet</name></object></annotation>")
    (d / "annotations" / "img_2.xml").write_text(
        "<annotation><object><name>person</name></object></annotation>")
    return d


def test_detect_dataset_info_voc(tmp_path, monkeypatch):
    from odp_platform.common import paths
    _make_voc_dataset(tmp_path / "raw")
    monkeypatch.setattr(paths, "RAW_DATA_DIR", tmp_path / "raw")

    info = tasks.detect_dataset_info("demo")
    assert info["exists"] is True
    assert info["format"] == "pascal_voc"
    assert info["images"] == 4
    assert info["annotations"] == 4
    assert set(info["classes"]) == {"helmet", "person"}


def test_detect_dataset_info_missing(tmp_path, monkeypatch):
    from odp_platform.common import paths
    monkeypatch.setattr(paths, "RAW_DATA_DIR", tmp_path / "raw")
    info = tasks.detect_dataset_info("nope")
    assert info["exists"] is False


def test_list_experiments_parses_audit(tmp_path, monkeypatch):
    from odp_platform.common import paths
    run_dir = tmp_path / "runs" / "detect_train" / "train7"
    run_dir.mkdir(parents=True)
    audit = {
        "config": {"values": {"model": "yolo11n.pt", "data": "demo.yaml"}},
        "merger": {},
        "metrics": {
            "task": "detect",
            "timestamp": "2026-08-15T00:00:00",
            "overall": {"metrics/mAP50(B)": 0.5, "metrics/mAP50-95(B)": 0.4,
                        "metrics/precision(B)": 0.6, "metrics/recall(B)": 0.7},
        },
        "result_summary": {"log_path": None},
    }
    (run_dir / "odp_audit.json").write_text(json.dumps(audit))
    monkeypatch.setattr(paths, "RUNS_DIR", tmp_path / "runs")

    exps = tasks.list_experiments()
    assert len(exps) == 1
    e = exps[0]
    assert e["name"] == "train7"
    assert e["mAP50"] == 0.5
    assert e["precision"] == 0.6
    assert e["model"] == "yolo11n.pt"


def test_ensure_config_creates_yaml(tmp_path, monkeypatch):
    from odp_platform.common import paths
    monkeypatch.setattr(paths, "RUNTIME_CONFIGS_DIR", tmp_path / "configs" / "runtime")
    p = tasks.ensure_config("train")
    assert Path(p).exists()
    assert Path(p).name == "train.yaml"


def test_latest_checkpoint_prefers_newest(tmp_path, monkeypatch):
    from odp_platform.common import paths
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    old = ckpt / "a-best.pt"
    new = ckpt / "b-best.pt"
    old.touch()
    new.touch()
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    monkeypatch.setattr(paths, "CHECKPOINTS_DIR", ckpt)
    assert Path(tasks.latest_checkpoint()).name == "b-best.pt"
