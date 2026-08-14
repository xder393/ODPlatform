"""测试 yaml_schema check 的边界情况。"""
from pathlib import Path

from odp_platform.data_validation import CheckContext, build_snapshot
from odp_platform.data_validation.checks.yaml_schema import validate_yaml_schema


def _run(yaml_path: Path):
    snap = build_snapshot(yaml_path)
    return validate_yaml_schema(CheckContext(yaml_path=yaml_path, snapshot=snap))


def test_yaml_pass(tmp_path):
    p = tmp_path / "ok.yaml"
    p.write_text("nc: 3\nnames: [a, b, c]\ntrain: images/train\n")
    r = _run(p)
    assert r.severity == "PASS"
    assert r.details["nc"] == 3


def test_yaml_not_exists(tmp_path):
    r = _run(tmp_path / "missing.yaml")
    assert r.severity == "ERROR"
    assert "yaml_load_error" in r.details["reason"]


def test_yaml_nc_names_mismatch(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("nc: 5\nnames: [a, b, c]\n")
    r = _run(p)
    assert r.severity == "ERROR"
    assert any("不一致" in prob for prob in r.details["problems"])


def test_yaml_names_dict_form(tmp_path):
    """D3 yaml_writer 出的 dict 形式 names 应被接受。"""
    p = tmp_path / "dict_names.yaml"
    p.write_text("nc: 2\nnames:\n  0: cat\n  1: dog\n")
    r = _run(p)
    assert r.severity == "PASS"


def test_yaml_top_level_not_dict(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    r = _run(p)
    assert r.severity == "ERROR"
