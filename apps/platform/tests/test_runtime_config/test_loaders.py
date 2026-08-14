"""YAMLLoader + CLILoader + _drop_none 行为契约."""
from __future__ import annotations

from argparse import Namespace
from pathlib  import Path

import pytest

from odp_platform.runtime_config.loaders import (
    YAMLLoader, CLILoader, load_all_sources, _drop_none,
)


# ============================================================
# _drop_none — 经典 falsy 坑测试
# ============================================================

class TestDropNone:
    def test_drops_none(self):
        result = _drop_none({"a": None, "b": 1})
        assert result == {"b": 1}

    def test_keeps_false(self):
        """★ 经典坑: 不能写成 `if v`, 那样会过滤 False."""
        result = _drop_none({"flag": False})
        assert result == {"flag": False}

    def test_keeps_zero(self):
        result = _drop_none({"count": 0, "ratio": 0.0})
        assert result == {"count": 0, "ratio": 0.0}

    def test_keeps_empty_string(self):
        result = _drop_none({"name": ""})
        assert result == {"name": ""}

    def test_keeps_empty_list(self):
        result = _drop_none({"items": []})
        assert result == {"items": []}


# ============================================================
# YAMLLoader 正常路径
# ============================================================

class TestYAMLLoaderHappy:
    def test_load_normal_yaml(self, minimal_train_yaml):
        loader = YAMLLoader()
        cfg = loader.load(minimal_train_yaml)
        assert cfg["epochs"]  == 200
        assert cfg["batch"]   == 32
        assert cfg["lr0"]     == 0.005
        assert cfg["model"]   == "yolo11n.pt"

    def test_load_empty_file_returns_dict(self, write_yaml):
        loader = YAMLLoader()
        f = write_yaml("", name="empty.yaml")
        assert loader.load(f) == {}

    def test_load_all_comments_returns_dict(self, write_yaml):
        loader = YAMLLoader()
        f = write_yaml("# only comment\n# nothing else\n", name="comments.yaml")
        assert loader.load(f) == {}

    def test_load_filters_yaml_null(self, write_yaml):
        """yaml 的 null → 过滤; false / 0 → 保留."""
        f = write_yaml(
            "device: null\namp: false\nfraction: 0.0\n",
            name="null.yaml",
        )
        cfg = YAMLLoader().load(f)
        assert "device" not in cfg
        assert cfg["amp"] is False
        assert cfg["fraction"] == 0.0


# ============================================================
# YAMLLoader fail-fast 路径 — ★ 撞墙③
# ============================================================

class TestYAMLLoaderFailFast:
    def test_file_not_exists_raises_with_repair_command(self, tmp_path):
        """文件不存在 → FileNotFoundError + 'odp-gen-config' 修复指引."""
        loader = YAMLLoader()
        target = tmp_path / "not_exist.yaml"
        with pytest.raises(FileNotFoundError) as exc_info:
            loader.load(target)
        msg = str(exc_info.value)
        assert "请先生成默认配置模板" in msg
        assert "odp-gen-config" in msg
        assert "not_exist" in msg, "修复指引应含模板名"

    def test_yaml_format_error(self, write_yaml):
        """yaml 格式错误 → ValueError, 含原始错误链."""
        f = write_yaml("epochs: 100\n\tbatch: 16\n", name="bad.yaml")
        loader = YAMLLoader()
        with pytest.raises(ValueError) as exc_info:
            loader.load(f)
        msg = str(exc_info.value)
        assert "YAML 格式错误" in msg
        assert "检查缩进" in msg
        assert exc_info.value.__cause__ is not None, "应保留 exception chain"

    def test_top_level_not_dict(self, write_yaml):
        f = write_yaml("- 1\n- 2\n- 3\n", name="list.yaml")
        with pytest.raises(ValueError) as exc_info:
            YAMLLoader().load(f)
        assert "顶层必须是字典" in str(exc_info.value)


# ============================================================
# YAMLLoader 路径解析
# ============================================================

class TestYAMLLoaderPathResolution:
    def test_absolute_path_ignores_config_dir(self, write_yaml, tmp_path):
        """绝对路径无视 config_dir."""
        f = write_yaml("epochs: 50", name="train.yaml")
        loader = YAMLLoader(config_dir="/should/be/ignored")
        assert loader._resolve_path(f).resolve() == f.resolve()

    def test_relative_path_uses_config_dir(self, write_yaml, tmp_path):
        f = write_yaml("epochs: 50", name="train.yaml")
        loader = YAMLLoader(config_dir=tmp_path)
        cfg = loader.load("train.yaml")     # 文件名 → 从 tmp_path 找
        assert cfg["epochs"] == 50

    def test_no_config_dir_uses_cwd(self, tmp_path, monkeypatch):
        """没有 config_dir → 基于当前目录."""
        f = tmp_path / "x.yaml"
        f.write_text("epochs: 7", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        loader = YAMLLoader()
        cfg = loader.load("x.yaml")
        assert cfg["epochs"] == 7


# ============================================================
# CLILoader
# ============================================================

class TestCLILoader:
    def test_namespace_basic(self):
        args = Namespace(model="x.pt", epochs=200, batch=32)
        cfg = CLILoader().load(args)
        assert cfg == {"model": "x.pt", "epochs": 200, "batch": 32}

    def test_default_excludes_control_fields(self):
        args = Namespace(
            model="x.pt", epochs=10,
            help=False, config="train.yaml",
            cfg="abc", yaml_path="def",
            debug=True, version="1.0",
        )
        cfg = CLILoader().load(args)
        for control_field in ["help", "config", "cfg", "yaml_path", "debug", "version"]:
            assert control_field not in cfg, f"{control_field} 应被默认排除"
        assert cfg == {"model": "x.pt", "epochs": 10}

    def test_excludes_private_fields(self):
        args = Namespace(model="x.pt", _internal="hide", _debug=True)
        cfg = CLILoader().load(args)
        assert cfg == {"model": "x.pt"}

    def test_filter_none_default(self):
        args = Namespace(model="x.pt", lr0=None, batch=16)
        cfg = CLILoader().load(args)
        assert "lr0" not in cfg
        assert cfg["batch"] == 16

    def test_filter_none_disabled(self):
        args = Namespace(model="x.pt", device=None)
        cfg = CLILoader().load(args, filter_none=False)
        assert "device" in cfg and cfg["device"] is None

    def test_custom_exclude(self):
        args = Namespace(model="x.pt", my_flag=True)
        cfg = CLILoader(exclude=["my_flag"]).load(args)
        assert "my_flag" not in cfg

    def test_name_mapping(self):
        """CLI 名 (learning_rate) → Pydantic 字段名 (lr0)."""
        args = Namespace(learning_rate=0.001, model="x.pt")
        cfg = CLILoader(mapping={"learning_rate": "lr0"}).load(args)
        assert cfg == {"lr0": 0.001, "model": "x.pt"}

    def test_args_none_returns_empty(self):
        assert CLILoader().load(None) == {}

    def test_dict_input(self):
        cfg = CLILoader().load({"epochs": 50})
        assert cfg == {"epochs": 50}

    def test_wrong_type_raises(self):
        with pytest.raises(TypeError):
            CLILoader().load("not a Namespace")


# ============================================================
# load_all_sources
# ============================================================

class TestLoadAllSources:
    def test_loads_both_sources(self, minimal_train_yaml):
        sources = load_all_sources(
            yaml_path=minimal_train_yaml.name,
            yaml_dir=minimal_train_yaml.parent,
            cli_args=Namespace(epochs=999, batch=64),
        )
        assert sources["yaml"]["epochs"] == 200
        assert sources["cli"]["epochs"]  == 999

    def test_no_yaml_path_skips_yaml(self):
        sources = load_all_sources(
            yaml_path=None,
            cli_args=Namespace(epochs=10),
        )
        assert sources["yaml"] == {}
        assert sources["cli"]["epochs"] == 10
