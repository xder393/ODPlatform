"""测试 D4 注册表机制本身 — 不测具体 check, 测框架。"""
from pathlib import Path

import pytest

from odp_platform.data_validation.registry import (
    CheckContext, CheckResult, CheckSeverity,
    _REGISTRY, check, get_all_checks, get_check,
)
from odp_platform.data_validation.service import run_all_checks
from odp_platform.data_validation.snapshot import build_snapshot


@pytest.fixture
def fresh_registry(monkeypatch):
    """每个测试都用干净的注册表, 不被全局 check 污染。"""
    monkeypatch.setattr(
        "odp_platform.data_validation.registry._REGISTRY", {},
    )
    monkeypatch.setattr(
        "odp_platform.data_validation.registry._INITIALIZED", True,
    )


def test_check_decorator_registers(fresh_registry):
    @check("test_a")
    def fn(ctx):
        return CheckResult(name="test_a", severity="PASS", summary="ok")
    assert "test_a" in [e.name for e in get_all_checks()]


def test_duplicate_registration_rejected(fresh_registry):
    @check("test_b")
    def fn1(ctx):
        return CheckResult(name="test_b", severity="PASS", summary="ok")
    with pytest.raises(ValueError, match="重复注册"):
        @check("test_b")
        def fn2(ctx):
            return CheckResult(name="test_b", severity="PASS", summary="ok")


def test_check_severity_rank_ordering():
    """ERROR > WARNING > INFO > PASS"""
    ranks = [CheckSeverity.rank(s) for s in ("ERROR", "WARNING", "INFO", "PASS")]
    assert ranks == [3, 2, 1, 0]


def test_aggregator_catches_check_exceptions(fresh_registry, tmp_path):
    """⭐ 灵魂测试: 任意 check 抛异常不能阻断其他 check。

    这条测试在测 D4 的设计哲学, 不是测代码细节 — 它在告诉未来的 refactor 者:
    改什么都行, 别改这条。
    """
    @check("test_raises")
    def raising(ctx):
        raise RuntimeError("intentional")

    @check("test_ok")
    def ok(ctx):
        return CheckResult(name="test_ok", severity="PASS", summary="ok")

    yaml_path = tmp_path / "dummy.yaml"
    yaml_path.write_text("nc: 1\nnames: [x]\n")
    snap = build_snapshot(yaml_path)
    ctx = CheckContext(yaml_path=yaml_path, snapshot=snap)

    results = run_all_checks(ctx)
    by_name = {r.name: r for r in results}
    assert by_name["test_raises"].severity == "ERROR"
    assert "intentional" in by_name["test_raises"].summary
    assert by_name["test_ok"].severity == "PASS"


def test_get_check_unknown_raises(fresh_registry):
    with pytest.raises(KeyError, match="未注册"):
        get_check("nonexistent")
