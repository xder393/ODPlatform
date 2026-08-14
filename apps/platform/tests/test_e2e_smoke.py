"""端到端 smoke: 走完整个 odp-transform 流程, 检验 yaml 内容。"""
from pathlib import Path

import pytest
import yaml

from odp_platform.cli.transform_data import main as cli_main


@pytest.fixture
def toy_voc_dataset(tmp_path, monkeypatch):
    """造一个最小可跑的 VOC 数据集 + 把输出路径重定向到 tmp_path。

    重定向分两类:
      - paths 模块的 RAW_DATA_DIR / DATASET_CONFIGS_DIR 被 raw_dataset_root /
        dataset_yaml_path 这两个函数在【运行时】读取, 直接 monkeypatch 模块属性即可;
      - TRAIN/VAL/TEST_*_DIR 被 orchestrator 在【import 时】以值的形式绑定,
        所以这里 monkeypatch orchestrator 模块里的引用, 才能真正避免写真实 data/。
    """
    from PIL import Image

    from odp_platform.common import paths as paths_mod
    from odp_platform.data_pipeline import orchestrator as orch_mod

    monkeypatch.setattr(paths_mod, "RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(paths_mod, "DATASET_CONFIGS_DIR", tmp_path / "configs" / "datasets")

    monkeypatch.setattr(orch_mod, "TRAIN_IMAGES_DIR", tmp_path / "train" / "images")
    monkeypatch.setattr(orch_mod, "TRAIN_LABELS_DIR", tmp_path / "train" / "labels")
    monkeypatch.setattr(orch_mod, "VAL_IMAGES_DIR",   tmp_path / "val"   / "images")
    monkeypatch.setattr(orch_mod, "VAL_LABELS_DIR",   tmp_path / "val"   / "labels")
    monkeypatch.setattr(orch_mod, "TEST_IMAGES_DIR",  tmp_path / "test"  / "images")
    monkeypatch.setattr(orch_mod, "TEST_LABELS_DIR",  tmp_path / "test"  / "labels")

    # 造数据集
    root = tmp_path / "raw" / "toy"
    (root / "images").mkdir(parents=True)
    (root / "annotations").mkdir(parents=True)
    for i in range(10):
        Image.new("RGB", (640, 480), color=(i * 20, 0, 0)).save(
            root / "images" / f"img_{i}.jpg"
        )
        (root / "annotations" / f"img_{i}.xml").write_text(f"""<annotation>
  <size><width>640</width><height>480</height></size>
  <object>
    <name>cat</name>
    <bndbox><xmin>100</xmin><ymin>100</ymin><xmax>300</xmax><ymax>300</ymax></bndbox>
  </object>
</annotation>""")

    return tmp_path


def test_e2e_smoke_voc(toy_voc_dataset):
    """跑一次 odp-transform, 检验 exit code + yaml 内容。"""
    exit_code = cli_main([
        "--dataset", "toy",
        "--format",  "pascal_voc",
        "--random-state", "42",
    ])
    assert exit_code == 0

    yaml_path = toy_voc_dataset / "configs" / "datasets" / "toy.yaml"
    assert yaml_path.exists()

    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert "names" in doc
    assert 0 in doc["names"]
    assert doc["names"][0] == "cat"

    assert "odp_meta" in doc
    assert doc["odp_meta"]["source_format"] == "pascal_voc"
    assert doc["odp_meta"]["split"]["random_state"] == 42
    assert doc["odp_meta"]["split"]["counts"]["total"] == 10


def test_e2e_unknown_format_exits():
    """argparse 在 choices 不匹配时会 SystemExit(2) (argparse 自己定的退出码);
    这条主要是确保 CLI 不崩, 让 argparse 的标准错误信息正常呈现给用户。"""
    with pytest.raises(SystemExit):
        cli_main(["--dataset", "x", "--format", "not_a_format"])
