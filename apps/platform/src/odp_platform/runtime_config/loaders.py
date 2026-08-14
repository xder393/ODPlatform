#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : loaders.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : runtime_config 子系统——配置加载器
"""配置加载器: 从不同来源加载配置(不负责验证和合并).

- YAMLLoader: 加载 YAML 配置文件, 文件不存在 → fail-fast + 修复指引
- CLILoader:  加载命令行参数, 过滤控制字段 + 支持参数名映射

★ 设计原则: Loader 只把外部数据装进 dict, 不验证字段值, 不合并.
   - 字段值验证 → Pydantic (阶段 2-3 已立)
   - 多源合并   → ConfigMerger (阶段 5)
"""
from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import yaml

logger = logging.getLogger(__name__)


# ============================================================
# 工具函数
# ============================================================

def _drop_none(d: Mapping[str, Any]) -> Dict[str, Any]:
    """过滤 None 值, 保留 False / 0 / '' 等显式值.

    YAML 的 null 等同于 "没写" — 在后续合并阶段, None 不该参与覆盖,
    应该让下层(默认值)有机会跑出来.

    但 False / 0 / '' 是用户显式填的 "假", 必须保留 —
    不要写成 `{k: v for k, v in d.items() if v}`(经典 falsy 误伤).
    """
    return {k: v for k, v in d.items() if v is not None}


# ============================================================
# YAMLLoader
# ============================================================

class YAMLLoader:
    """加载 YAML 配置文件 → dict.

    ★ 4 件事:
        1. 路径解析: 绝对 / 相对 / 文件名 都接受
        2. 编码: 默认 UTF-8, 失败 fallback 系统默认
        3. 解析: 失败 fail-fast (raise + 原 exception chain)
        4. 不存在: fail-fast + 修复指引

    使用示例:
        from odp_platform.common.paths import RUNTIME_CONFIGS_DIR
        loader = YAMLLoader(config_dir=RUNTIME_CONFIGS_DIR)
        cfg = loader.load("train.yaml")          # 文件名(从 config_dir 找)
        cfg = loader.load("/abs/path.yaml")      # 绝对路径
    """

    def __init__(self, config_dir: Optional[Union[str, Path]] = None):
        """
        Args:
            config_dir: 配置文件目录, 用于解析相对路径.
                       None 时相对路径基于当前目录.
        """
        self.config_dir = Path(config_dir) if config_dir else None

    def load(self, filename: Union[str, Path]) -> Dict[str, Any]:
        """加载 YAML 配置文件.

        Args:
            filename: 文件名或路径(相对 / 绝对)

        Returns:
            配置字典. 空文件返回 {} (合法, 等同 "全用默认值").

        Raises:
            FileNotFoundError: 文件不存在(带修复指引)
            ValueError:        YAML 格式错误 / 顶层不是 dict
        """
        # 1. 解析路径
        filepath = self._resolve_path(filename)

        # 2. 文件不存在 → fail-fast + 修复指引(★ 撞墙③)
        if not filepath.exists():
            raise FileNotFoundError(
                f"YAML 配置文件不存在: {filepath}\n\n"
                f"请先生成默认配置模板:\n"
                f"  odp-gen-config {filepath.stem}\n\n"
                f"生成后编辑该文件再重新运行."
            )

        # 3. 读文件(默认 UTF-8, 失败 fallback)
        try:
            content = filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning(f"UTF-8 解码失败, 尝试系统默认编码: {filepath}")
            content = filepath.read_text()

        # 4. 空文件 → 返回 {}, 等同 "全用默认值"
        if not content.strip():
            logger.debug(f"YAML 文件为空: {filepath}")
            return {}

        # 5. 解析 YAML — 失败 fail-fast, 保留 exception chain
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise ValueError(
                f"YAML 格式错误: {filepath}\n"
                f"原始错误: {e}\n"
                f"提示: 检查缩进、引号匹配、冒号后是否有空格."
            ) from e

        # 6. 顶层结构检查
        if data is None:
            return {}     # YAML 显式 null, 等同空文件

        if not isinstance(data, dict):
            raise ValueError(
                f"YAML 顶层必须是字典, 当前是 {type(data).__name__}: {filepath}\n"
                f"内容预览: {str(data)[:100]}"
            )

        # 7. 过滤 None 值(保留 False / 0 / '')
        return _drop_none(data)

    def _resolve_path(self, filename: Union[str, Path]) -> Path:
        """解析文件路径(★ 绝对路径必须先判, 详见 4.2 细节①)"""
        path = Path(filename)

        if path.is_absolute():
            return path

        if self.config_dir:
            return (self.config_dir / path).resolve()

        return path.resolve()


# ============================================================
# CLILoader
# ============================================================

class CLILoader:
    """加载命令行参数 → dict.

    功能:
        - 接受 argparse.Namespace 或 dict
        - 排除控制字段(help / config / cfg / yaml_path / debug / version)
        - 排除私有字段(_xxx)
        - 过滤 None 值(默认; 可关)
        - 支持参数名映射(CLI 名 → Pydantic 字段名)

    使用示例:
        loader = CLILoader()
        cfg = loader.load(args)                  # args 是 argparse.Namespace

        # 自定义排除 + 映射:
        loader = CLILoader(
            exclude=["my_flag"],
            mapping={"learning_rate": "lr0"},
        )
    """

    # 默认排除的控制字段(不该进配置 dict)
    DEFAULT_EXCLUDE: set[str] = {
        "help",
        "config", "cfg", "yaml_path",       # yaml 路径是 loader 的输入
        "debug",
        "version",
    }

    def __init__(
        self,
        exclude: Optional[List[str]] = None,
        mapping: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            exclude: 额外排除的字段列表(并入 DEFAULT_EXCLUDE)
            mapping: 参数名映射, {CLI 名: Pydantic 字段名}
        """
        self.exclude = self.DEFAULT_EXCLUDE | set(exclude or [])
        self.mapping = mapping or {}

    def load(
        self,
        args: Optional[Union[Namespace, Dict[str, Any]]] = None,
        filter_none: bool = True,
    ) -> Dict[str, Any]:
        """加载命令行参数.

        Args:
            args:        argparse.Namespace 或 dict.
                         None 表示没传 CLI, 返回 {}.
            filter_none: 是否过滤 None 值(默认 True, 99% 场景)

        Returns:
            配置字典.

        Raises:
            TypeError: args 不是 Namespace 或 dict
        """
        if args is None:
            return {}

        # 转字典
        if isinstance(args, Namespace):
            raw = vars(args)
        elif isinstance(args, dict):
            raw = args
        else:
            raise TypeError(
                f"args 必须是 argparse.Namespace 或 dict, "
                f"当前是 {type(args).__name__}"
            )

        # 过滤 + 映射
        result: Dict[str, Any] = {}
        for key, value in raw.items():
            # 排除控制字段 + 私有字段
            if key in self.exclude or key.startswith("_"):
                continue

            # 过滤 None
            if filter_none and value is None:
                continue

            # 参数名映射
            mapped_key = self.mapping.get(key, key)
            result[mapped_key] = value

        return result


# ============================================================
# 便捷函数: 一次性加载所有源
# ============================================================

def load_all_sources(
    yaml_path: Optional[Union[str, Path]] = None,
    yaml_dir: Optional[Union[str, Path]] = None,
    cli_args: Optional[Union[Namespace, Dict[str, Any]]] = None,
    cli_exclude: Optional[List[str]] = None,
    cli_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """一次性加载所有配置源 → {'yaml': ..., 'cli': ...}

    给上层(service 层 / Merger)的便捷入口, 不做合并.

    Args:
        yaml_path:    YAML 文件名或路径(None 跳过加载 yaml)
        yaml_dir:     YAML 文件目录(用于解析相对路径)
        cli_args:     命令行参数
        cli_exclude:  CLI 额外排除字段
        cli_mapping:  CLI 参数名映射

    Returns:
        {"yaml": {...}, "cli": {...}}

    使用示例:
        sources = load_all_sources(
            yaml_path="train.yaml",
            yaml_dir=RUNTIME_CONFIGS_DIR,
            cli_args=args,
        )
        merger.merge(YOLOTrainConfig, sources=[
            (ConfigSource.YAML, sources["yaml"]),
            (ConfigSource.CLI,  sources["cli"]),
        ])
    """
    yaml_config: Dict[str, Any] = {}
    if yaml_path:
        loader = YAMLLoader(config_dir=yaml_dir)
        yaml_config = loader.load(yaml_path)

    cli_loader = CLILoader(exclude=cli_exclude, mapping=cli_mapping)
    cli_config = cli_loader.load(cli_args)

    return {"yaml": yaml_config, "cli": cli_config}
