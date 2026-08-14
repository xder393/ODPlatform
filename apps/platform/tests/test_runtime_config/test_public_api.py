"""__init__.py 公共 API + build_* 流水线."""
from __future__ import annotations

import inspect

from argparse import Namespace
import pytest

import odp_platform.runtime_config as rc


class TestPublicAPISurface:
    """公开 API 表面契约."""

    def test_all_symbols_importable(self):
        for name in rc.__all__:
            assert hasattr(rc, name), f"__all__ 声明了 {name} 但模块没有"
            obj = getattr(rc, name)
            assert obj is not None

    def test_all_symbols_have_docstring(self):
        """API 必须自我文档化."""
        missing = []
        for name in rc.__all__:
            obj = getattr(rc, name)
            if not (inspect.getdoc(obj) or "").strip():
                missing.append(name)
        assert not missing, f"以下公开符号缺 docstring: {missing}"

    def test_no_private_in_all(self):
        for name in rc.__all__:
            assert not name.startswith("_"), \
                f"私有符号 {name} 不应该在 __all__"

    def test_reexports_not_copies(self):
        """公开符号是 re-export, 不是 copy."""
        from odp_platform.runtime_config.train  import YOLOTrainConfig as _TC
        from odp_platform.runtime_config.merger import ConfigSource    as _CS
        assert rc.YOLOTrainConfig is _TC
        assert rc.ConfigSource    is _CS


class TestBuildTrainConfig:
    def test_full_pipeline(self, minimal_train_yaml):
        config, merger = rc.build_train_config(
            yaml_path=minimal_train_yaml.name,
            yaml_dir=minimal_train_yaml.parent,
            cli_args=Namespace(epochs=300, batch=64),
        )
        assert isinstance(config, rc.YOLOTrainConfig)
        assert config.epochs == 300
        assert config.batch  == 64
        assert config.lr0    == 0.005

    def test_returns_merger_with_traceability(self, minimal_train_yaml):
        config, merger = rc.build_train_config(
            yaml_path=minimal_train_yaml.name,
            yaml_dir=minimal_train_yaml.parent,
            cli_args=Namespace(epochs=300),
        )
        assert isinstance(merger, rc.ConfigMerger)
        meta = merger.get_metadata("epochs")
        assert meta.source == rc.ConfigSource.CLI
        assert "300(CLI) ← 200(YAML) ← 100(DEFAULT)" == meta.chain_str()

    def test_yaml_path_none_skips_yaml(self):
        config, merger = rc.build_train_config(
            yaml_path=None,
            cli_args=Namespace(model="x.pt", data="y.yaml", epochs=50),
        )
        assert config.epochs == 50
        yaml_keys = [
            k for k, m in merger._metadata.items()
            if m.source == rc.ConfigSource.YAML
        ]
        assert yaml_keys == []

    def test_track_sources_false(self):
        config, merger = rc.build_train_config(
            yaml_path=None,
            cli_args=Namespace(model="x.pt", data="y.yaml", epochs=50),
            track_sources=False,
        )
        assert config.epochs == 50
        assert merger.get_metadata("epochs") is None


class TestBuildValConfig:
    def test_full_pipeline(self, write_yaml):
        f = write_yaml("split: test\nconf: 0.5\n", name="val.yaml")
        config, merger = rc.build_val_config(
            yaml_path=f.name,
            yaml_dir=f.parent,
            cli_args=Namespace(iou=0.7),
        )
        assert isinstance(config, rc.YOLOValConfig)
        assert config.split == "test"
        assert config.conf  == 0.5
        assert config.iou   == 0.7
