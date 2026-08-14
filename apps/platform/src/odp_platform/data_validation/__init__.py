# apps/platform/src/odp_platform/data_validation/__init__.py
"""data_validation 子系统的对外公共 API。

外部调用方应该只 import 这里, 不直接 import 内部子模块。

公开符号:
    - CheckContext, CheckResult, CheckSeverity:  数据契约
    - check, get_all_checks, get_check:          注册表 API
    - run_all_checks:                            调度 (聚合执行)
    - DatasetSnapshot, SplitStats, build_snapshot: 一次扫描
    - ValidationReport:                          聚合产出 (纯数据)
    - render_to_logger:                          展示层 (纯展示)
    - validate_dataset:                          端到端函数
"""
from odp_platform.data_validation.registry import (
    CheckContext,
    CheckResult,
    CheckSeverity,
    check,
    get_all_checks,
    get_check,
    list_check_names,
)
from odp_platform.data_validation.service import run_all_checks, validate_dataset
from odp_platform.data_validation.snapshot import (
    DatasetSnapshot,
    SplitStats,
    build_snapshot,
)
from odp_platform.data_validation.report import ValidationReport
from odp_platform.data_validation.render import render_to_logger

__all__ = [
    "CheckContext",
    "CheckResult",
    "CheckSeverity",
    "check",
    "get_all_checks",
    "get_check",
    "list_check_names",
    "run_all_checks",
    "DatasetSnapshot",
    "SplitStats",
    "build_snapshot",
    "ValidationReport",
    "render_to_logger",
    "validate_dataset",
]
