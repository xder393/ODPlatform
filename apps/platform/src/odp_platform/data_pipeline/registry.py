"""data_pipeline 注册表 + 统一参数包 + 能力声明。

设计要点:
    - 一个 dict (_REGISTRY) 存"format -> ConverterEntry"映射
    - @register 装饰器在 converter 文件被 import 时自动登记
    - core/ 下的 converter 模块由 pkgutil 自动发现, 新增格式无需改注册表
    - ConvertOptions 是所有 converter 共用的参数包
    - ConverterEntry 同时携带"实现函数 + 能力声明"
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from odp_platform.common.constants import Task

logger = logging.getLogger(__name__)


# ============================================================
# 统一参数包: 所有 converter 函数签名一致
# ============================================================
@dataclass
class ConvertOptions:
    """所有 converter 共用的参数包。

    每个 converter 按需取用——不需要的字段忽略即可。
    新增字段时, 老 converter 自动兼容(因为不读它就行)。
    """
    task: str = Task.DETECT
    """任务类型: detect / segment / ..."""

    classes: Optional[List[str]] = None
    """类别白名单。
    - 对 pascal_voc / coco: 一般为 None, converter 自己探测
    - 对 yolo: 必须传, 因为 yolo 格式不含类别名信息
    """

    coco_cls91to80: bool = False
    """COCO 专属: 是否做 91 类 → 80 类映射"""


# ============================================================
# 注册表条目
# ============================================================
ConverterFunc = Callable[[Path, Path, ConvertOptions], List[str]]
"""converter 函数签名:
    (input_dir, output_labels_dir, options) -> List[str](类别名)
"""


@dataclass(frozen=True)
class ConverterEntry:
    """注册表里一条记录: 函数 + 它的能力声明。"""
    func: ConverterFunc
    supported_tasks: Tuple[str, ...]

    def supports(self, task: str) -> bool:
        return task in self.supported_tasks


# ============================================================
# 注册表本体 (模块级单例)
# ============================================================
_REGISTRY: Dict[str, ConverterEntry] = {}


def register(
    format_name: str,
    supported_tasks: Tuple[str, ...] = (Task.DETECT,),
) -> Callable[[ConverterFunc], ConverterFunc]:
    """装饰器: 把一个 converter 函数注册到 _REGISTRY。

    Usage:
        @register("pascal_voc", supported_tasks=(Task.DETECT,))
        def convert_voc(input_dir, output_labels_dir, options):
            ...
    """
    def decorator(func: ConverterFunc) -> ConverterFunc:
        if format_name in _REGISTRY:
            logger.warning(f"格式 {format_name} 被重复注册, 后者覆盖前者")
        _REGISTRY[format_name] = ConverterEntry(
            func=func,
            supported_tasks=tuple(supported_tasks),
        )
        logger.debug(
            f"注册 converter: format={format_name}, "
            f"tasks={supported_tasks}"
        )
        return func
    return decorator


# ============================================================
# 查询 API (供 service.py / CLI / 测试使用)
# ============================================================
def get_converter(format_name: str) -> ConverterEntry:
    """按 format 名取出 ConverterEntry。

    Raises:
        ValueError: 未注册的格式
    """
    _lazy_init()
    if format_name not in _REGISTRY:
        raise ValueError(
            f"未注册的格式: {format_name!r}。"
            f"已注册: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[format_name]


def list_capabilities() -> Dict[str, Tuple[str, ...]]:
    """返回当前所有已注册格式 → 支持的 task 列表。
    用于 CLI --help 显示能力矩阵。"""
    _lazy_init()
    return {fmt: entry.supported_tasks for fmt, entry in _REGISTRY.items()}


# ============================================================
# 延迟初始化: 自动发现 core/ 下所有 converter
# ============================================================
_LAZY_INITIALIZED = False


def _lazy_init() -> None:
    """扫描 core/ 包, 触发其中所有模块的 @register 装饰器执行。

    为什么需要"延迟":
        如果 registry.py 顶部直接 import core/* 模块,
        而那些模块又 `from data_pipeline.registry import register`,
        就形成循环 import。把 import 推迟到首次查询时, 循环就解开了。

    为什么"自动发现"而不是手写 import:
        手写 `from .core import pascal_voc, coco, yolo` 看起来直白,
        但每加一种格式都要回这里改一行——装饰器的"开闭原则"打了折扣。
        用 pkgutil.iter_modules 扫描包目录, 新增 core/<format>.py 后
        什么都不用改, 文件存在即自动注册。
        (Flask blueprints / Django apps / pytest 插件都是这个模式)

    为什么 _LAZY_INITIALIZED 要放在最后置 True:
        如果放在 import 之前置 True (你可能这么写过),
        import 中途抛异常时, 标志位已经是 True 但 _REGISTRY 是空的,
        后续所有 get_converter / list_capabilities 都会静默返回空,
        而且不会再重试。放最后的好处: 异常如实抛出, 修好环境后
        下一次调用会重新尝试, 一旦成功就 latch 住不再重复扫描。
    """
    global _LAZY_INITIALIZED
    if _LAZY_INITIALIZED:
        return

    from odp_platform.data_pipeline import core

    for module_info in pkgutil.iter_modules(core.__path__):
        # 跳过以 _ 开头的私有/工具模块 (如 _helpers.py)
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{core.__name__}.{module_info.name}")

    _LAZY_INITIALIZED = True   # ★ 必须放在 import 全部成功之后
