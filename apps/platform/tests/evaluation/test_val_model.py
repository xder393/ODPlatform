"""odp-val CLI 表面测试 — 只测 argparse, 不跑真实评估."""
from __future__ import annotations

from odp_platform.cli.val_model import build_parser


def test_parser_prog_and_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert parser.prog == "odp-val"
    assert args.yaml is None
    assert args.pre_validate is True
    assert args.rename_log is True


def test_parser_overrides():
    parser = build_parser()
    args = parser.parse_args([
        "--model", "best.pt", "--data", "rsod.yaml", "--split", "test",
        "--conf", "0.25", "--max-det", "100", "--no-pre-validate",
    ])
    assert args.model == "best.pt"
    assert args.data == "rsod.yaml"
    assert args.split == "test"
    assert args.conf == 0.25
    assert args.max_det == 100
    assert args.pre_validate is False


def test_parser_split_choices_rejected():
    parser = build_parser()
    import pytest
    with pytest.raises(SystemExit):
        parser.parse_args(["--split", "nope"])
