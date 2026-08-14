# apps/platform/src/odp_platform/data_validation/checks/yaml_schema.py
"""yaml_schema check — 验证数据集 yaml 文件的字段完整性和一致性。

阶段 4 重构: 不再自己 open / parse yaml, 改成消费 snapshot.yaml_data /
yaml_load_error / nc / class_names。代码从 ~150 行缩到 ~80 行 —— 收益来自
"只判断不做 IO"。
"""
from __future__ import annotations

from typing import List

from odp_platform.data_validation.registry import (
    check, CheckContext, CheckResult, CheckSeverity,
)


@check("yaml_schema")
def validate_yaml_schema(ctx: CheckContext) -> CheckResult:
    snap = ctx.snapshot

    # ---------- 前置错: yaml 加载阶段就出问题了 ----------
    if snap.yaml_load_error is not None:
        return CheckResult(
            name="yaml_schema",
            severity=CheckSeverity.ERROR,
            summary=snap.yaml_load_error,
            details={
                "reason":          "yaml_load_error",
                "yaml_path":       str(snap.yaml_path),
                "yaml_load_error": snap.yaml_load_error,
            },
        )

    # ---------- 业务错: 字段一致性 (收集所有, 一次报齐) ----------
    problems: List[str] = []

    nc = snap.nc
    if nc is None or nc <= 0:
        problems.append(f"nc 缺失或不是正整数: {snap.yaml_data.get('nc')!r}")

    names = snap.class_names
    if not names:
        # _normalize_names 返回空 tuple 表示"非法或缺失"
        raw = snap.yaml_data.get("names")
        problems.append(f"names 缺失或不是合法的 list[str] / dict[int,str]: {type(raw).__name__}")

    if nc is not None and nc > 0 and names and len(names) != nc:
        problems.append(f"nc ({nc}) 跟 names 长度 ({len(names)}) 不一致")

    if problems:
        return CheckResult(
            name="yaml_schema",
            severity=CheckSeverity.ERROR,
            summary=f"yaml 字段不一致: {len(problems)} 处问题",
            details={
                "reason":      "field_inconsistency",
                "problems":    problems,
                "nc":          nc,
                "names_count": len(names) if names else 0,
            },
        )

    # ---------- 全部通过 ----------
    return CheckResult(
        name="yaml_schema",
        severity=CheckSeverity.PASS,
        summary=f"yaml 字段一致 (nc={nc}, names_count={len(names)})",
        details={
            "nc":          nc,
            "names_count": len(names),
        },
    )
