"""ConfigMerger + ConfigMetadata 链表溯源."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from odp_platform.runtime_config.train  import YOLOTrainConfig
from odp_platform.runtime_config.merger import (
    ConfigMerger, ConfigSource, ConfigMetadata,
)


# ============================================================
# 优先级: CLI > YAML > DEFAULT
# ============================================================

class TestPriority:
    def test_cli_overrides_yaml(self):
        m = ConfigMerger()
        c = m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 200}),
            (ConfigSource.CLI,  {"epochs": 300}),
        ])
        assert c.epochs == 300

    def test_yaml_overrides_default(self):
        m = ConfigMerger()
        c = m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": 50})])
        assert c.epochs == 50

    def test_default_falls_through(self):
        m = ConfigMerger()
        c = m.merge(YOLOTrainConfig)
        assert c.epochs == 100       # BaseConfig default

    def test_partial_overrides(self):
        m = ConfigMerger()
        c = m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 200, "batch": 32, "lr0": 0.005}),
            (ConfigSource.CLI,  {"epochs": 300}),
        ])
        assert c.epochs == 300        # CLI
        assert c.batch  == 32         # YAML
        assert c.lr0    == 0.005      # YAML
        assert c.workers == 8         # DEFAULT


# ============================================================
# 链表溯源
# ============================================================

class TestChain:
    def test_three_layer_chain(self):
        m = ConfigMerger()
        m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 200}),
            (ConfigSource.CLI,  {"epochs": 300}),
        ])
        meta = m.get_metadata("epochs")
        chain = meta.chain()
        assert len(chain) == 3, "3 层链: CLI ← YAML ← DEFAULT"
        # 顺序: 当前 → 上一版 → 上上版
        assert chain[0].value == 300 and chain[0].source == ConfigSource.CLI
        assert chain[1].value == 200 and chain[1].source == ConfigSource.YAML
        assert chain[2].value == 100 and chain[2].source == ConfigSource.DEFAULT

    def test_two_layer_chain(self):
        m = ConfigMerger()
        m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": 200})])
        chain = m.get_metadata("epochs").chain()
        assert len(chain) == 2
        assert chain[0].source == ConfigSource.YAML
        assert chain[1].source == ConfigSource.DEFAULT

    def test_one_layer_chain_for_untouched_default(self):
        m = ConfigMerger()
        m.merge(YOLOTrainConfig)
        chain = m.get_metadata("epochs").chain()
        assert len(chain) == 1
        assert chain[0].source == ConfigSource.DEFAULT

    def test_chain_str_format(self):
        m = ConfigMerger()
        m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"lr0": 0.005}),
            (ConfigSource.CLI,  {"lr0": 0.001}),
        ])
        meta = m.get_metadata("lr0")
        assert meta.chain_str() == "0.001(CLI) ← 0.005(YAML) ← 0.01(DEFAULT)"


# ============================================================
# 报告
# ============================================================

class TestReports:
    def test_source_report_groups_by_source(self):
        m = ConfigMerger()
        m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 200, "batch": 32}),
            (ConfigSource.CLI,  {"lr0": 0.001}),
        ])
        report = m.get_source_report()
        assert "CLI (1 项)"  in report
        assert "YAML (2 项)" in report
        assert "DEFAULT"     in report

    def test_conflict_report_shows_one_step_override(self):
        """conflict_report 显示"最近一次覆盖"(一步); 完整链走 get_metadata(...).chain_str()."""
        m = ConfigMerger()
        m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 200, "lr0": 0.005}),
            (ConfigSource.CLI,  {"epochs": 300}),
        ])
        report = m.get_conflict_report()
        # epochs 只显示最近一次覆盖: YAML → CLI
        assert "epochs: 200 (YAML) → 300 (CLI)" in report
        # lr0 只被 YAML 覆盖过一次: DEFAULT → YAML
        assert "lr0: 0.01 (DEFAULT) → 0.005 (YAML)" in report


# ============================================================
# ValidationError 增强
# ============================================================

class TestValidationErrorEnhancement:
    def test_msg_contains_source_chain(self):
        """ValidationError.errors() msg 含 [来源: chain]."""
        m = ConfigMerger()
        with pytest.raises(ValidationError) as exc_info:
            m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": -5})])
        errs = exc_info.value.errors()
        epochs_err = next(e for e in errs if e["loc"] == ("epochs",))
        assert "来源" in epochs_err["msg"]
        assert "-5(YAML)" in epochs_err["msg"]
        assert "100(DEFAULT)" in epochs_err["msg"]

    def test_track_sources_false_no_enhancement(self):
        """track_sources=False 时不增强错误信息."""
        m = ConfigMerger(track_sources=False)
        with pytest.raises(ValidationError) as exc_info:
            m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": -5})])
        errs = exc_info.value.errors()
        epochs_err = next(e for e in errs if e["loc"] == ("epochs",))
        assert "来源" not in epochs_err["msg"]


# ============================================================
# 边界
# ============================================================

class TestEdgeCases:
    def test_none_does_not_override(self):
        """None 不参与覆盖 (Loader 已过滤, Merger 再防御)."""
        m = ConfigMerger()
        c = m.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, {"epochs": 50}),
            (ConfigSource.CLI,  {"epochs": None}),
        ])
        assert c.epochs == 50

    def test_merger_reusable(self):
        """同一个 merger 实例可以多次 merge, 状态不串台."""
        m = ConfigMerger()

        c1 = m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": 100})])
        assert m.get_metadata("epochs").value == 100

        c2 = m.merge(YOLOTrainConfig, sources=[(ConfigSource.CLI, {"epochs": 200})])
        # 第二次合并应该重置, 第一次的痕迹不该残留
        assert m.get_metadata("epochs").value  == 200
        assert m.get_metadata("epochs").source == ConfigSource.CLI
        # 第二次 chain 应该是 200(CLI) ← 100(DEFAULT), 不含 100(YAML)
        chain = m.get_metadata("epochs").chain()
        assert all(meta.source != ConfigSource.YAML for meta in chain)

    def test_track_sources_disabled(self):
        m = ConfigMerger(track_sources=False)
        c = m.merge(YOLOTrainConfig, sources=[(ConfigSource.YAML, {"epochs": 77})])
        assert c.epochs == 77
        assert m.get_metadata("epochs") is None
        assert "未启用" in m.get_source_report()
