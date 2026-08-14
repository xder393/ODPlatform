# apps/platform/src/odp_platform/data_validation/registry.py
"""data_validation 注册表 + 数据契约 (CheckResult / CheckSeverity / CheckContext)。

跟 D3 的 data_pipeline/registry.py 是【同一模式的两种用法】:
    - D3: 互斥分发 (一次调用一个 converter)
    - D4: 聚合执行 (一次调用全部 check, 收集结果)

公开 API:
    @check(name)                 装饰器, 自动注册一个 check 函数
    CheckResult                  统一返回类型
    CheckSeverity                严重程度 (四级)
    CheckContext                 check 函数的入参合同 (扩展位)
    get_all_checks()             返回全部注册的 check (供 service 调度)
    get_check(name)              按名查询单个 check (供测试)
    list_check_names()           返回注册的 check 名列表
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# 1. CheckSeverity — 严重程度 (四级一次到位)
# ============================================================

class CheckSeverity:
    """Check 结果的严重程度。

    跨级关系 (供 ValidationReport.overall_severity 比较):
        ERROR > WARNING > INFO > PASS

    四级而不是两级 (passed: bool) 的理由——见讲义阶段 2.2 选择 1。
    """
    ERROR   = "ERROR"     # 阻塞级 (CI 必须停, 训练绝不能继续)
    WARNING = "WARNING"   # 关注级 (能继续, 但需要人工 review)
    INFO    = "INFO"      # 告知级 (工程上"知道一下", 不阻断)
    PASS    = "PASS"      # 通过

    _ORDER = {"PASS": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

    @classmethod
    def rank(cls, level: str) -> int:
        """供聚合层比较 severity 用 — ERROR.rank() > PASS.rank()。"""
        return cls._ORDER.get(level, 0)


# ============================================================
# 2. CheckResult — 单个 check 的统一返回类型
# ============================================================

@dataclass
class CheckResult:
    """单个 check 的完整产出。

    Args:
        name:     check 名 (= 注册时的字符串, 跟 @check("name") 一致)
        severity: ERROR / WARNING / INFO / PASS (见 CheckSeverity)
        summary:  一句话总结, 供终端日志 / 报告头部使用 (给人看)
        details:  结构化详情字典 (给机器看 — JSON 报告 / 监控趋势)
    """
    name:     str
    severity: str
    summary:  str
    details:  Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """便于聚合层判断是否算"通过"。

        PASS / INFO 都算通过 — INFO 的语义是"知道一下但不阻断"。
        """
        return self.severity in (CheckSeverity.PASS, CheckSeverity.INFO)


# ============================================================
# 3. CheckContext — check 函数的入参合同 (扩展位)
# ============================================================

@dataclass
class CheckContext:
    """Check 函数的入参 — 所有 check 函数签名都是 (ctx: CheckContext) -> CheckResult。

    阶段 2 立的字段:
        yaml_path : 数据集 yaml 文件路径
    阶段 4 加的字段:
        snapshot  : DatasetSnapshot — 一次扫描产物, 供所有 check 共享消费

    未来扩展(D4.x / D5+):
        - previous_snapshot: 历史比较
        - config_overrides:  数据集级阈值覆盖
        - executor:          并发上下文
    永远不改 check 函数签名 —— 这是 CheckContext 这层包装的全部意义。
    """
    yaml_path: Path
    snapshot:  "DatasetSnapshot"   # 字符串前向引用, 避免循环 import


# ============================================================
# 4. 注册表条目
# ============================================================

@dataclass(frozen=True)
class CheckEntry:
    """注册表里的一条记录。frozen — 注册后不可改, 一锤子买卖。"""
    name: str
    func: Callable[[CheckContext], CheckResult]


# ============================================================
# 5. 模块级注册表 + @check 装饰器
# ============================================================

_REGISTRY: Dict[str, CheckEntry] = {}
_INITIALIZED: bool = False


def check(name: str) -> Callable:
    """注册一个 check 函数到全局注册表。

    用法:
        @check("yaml_schema")
        def validate_yaml_schema(ctx: CheckContext) -> CheckResult:
            ...

    设计:
        - 装饰器不包装函数 — 直接 return func。
          理由: 让被装饰的函数堆栈干净, 单元测试可以直接调用它而不经过装饰器,
          失败时的 traceback 也不会多一层"check_wrapper"的迷惑帧。

        - 重复注册立刻报 ValueError, 不是覆盖。
          理由: 重复注册要么是手误(两个 check 取了同名), 要么是 import 链异常
          导致同一个 check 被加载了两次 — 两种都是 bug, 应该立刻可见。

    Args:
        name: check 的字符串名 (会出现在 JSON 报告 / 日志里, 选稳定的)

    Returns:
        装饰器函数 — 注册副作用发生后, 原函数被原样返回。
    """
    def decorator(func: Callable[[CheckContext], CheckResult]) -> Callable:
        if name in _REGISTRY:
            raise ValueError(
                f"check '{name}' 重复注册 — 第二次出现在 {func.__module__}.{func.__name__}"
            )
        _REGISTRY[name] = CheckEntry(name=name, func=func)
        return func
    return decorator


# ============================================================
# 6. 自动 import — 加新 check 不改框架代码的物理基础
# ============================================================

def _ensure_initialized() -> None:
    """首次调用 get_all_checks() / get_check() 时, 自动 import checks/ 子包下
    所有非下划线开头的模块, 让其中的 @check 装饰器副作用发生。

    设计:
        - 用 pkgutil.iter_modules 自动扫盘, 不维护硬编码 import 列表。
          理由: 框架的 registry.py 维护一个手工 import 列表违反开闭 —
          加新 check 要改两处(checks/ 加文件 + registry.py 加 import)。
          自动 import 5 行解决, 加新 check 只动 checks/。

        - 跳过下划线开头的模块(_placeholder, _internal_helpers 等)。
          理由: 给框架留一个"内部辅助模块不被自动注册"的逃生口。

        - _INITIALIZED 标志防止重复 import (import 已经是 idempotent 的,
          但 pkgutil 扫盘有微小开销, flag 一下省掉)。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    import importlib
    import pkgutil
    from odp_platform.data_validation import checks

    for _, name, _ in pkgutil.iter_modules(checks.__path__):
        if name.startswith("_"):
            continue
        importlib.import_module(f"{checks.__name__}.{name}")


# ============================================================
# 7. 查询 API
# ============================================================

def get_all_checks() -> List[CheckEntry]:
    """返回全部注册的 check (按注册顺序)。供 service.run_all_checks 调度用。"""
    _ensure_initialized()
    return list(_REGISTRY.values())


def get_check(name: str) -> CheckEntry:
    """按名查询单个 check。供测试 / CLI 调试用。

    Raises:
        KeyError: 未注册的 check 名 — 立刻报错, 不返回 None 让调用方一脸懵
    """
    _ensure_initialized()
    if name not in _REGISTRY:
        raise KeyError(f"check '{name}' 未注册 — 已注册的: {list(_REGISTRY)}")
    return _REGISTRY[name]


def list_check_names() -> List[str]:
    """返回已注册 check 的名字列表。"""
    _ensure_initialized()
    return list(_REGISTRY.keys())
