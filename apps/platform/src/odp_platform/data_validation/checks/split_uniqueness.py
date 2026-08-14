# apps/platform/src/odp_platform/data_validation/checks/split_uniqueness.py
"""split_uniqueness check — 验证 train / val / test 之间无图像名重复。

判重方式: 按图像 stem (文件名不含扩展名)。理由见讲义阶段 7.1。

Severity: 任何重复 → ERROR。数据泄露的破坏性跟数量无关 — 1 张图重复
就足以让评估指标失真, 是质变型错误, 不按比例分级。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, List

from odp_platform.data_validation.registry import (
    check, CheckContext, CheckResult, CheckSeverity,
)


OVERLAP_PREVIEW_LIMIT = 20


@check("split_uniqueness")
def validate_split_uniqueness(ctx: CheckContext) -> CheckResult:
    snap = ctx.snapshot

    if len(snap.images_per_split) < 2:
        # 只有一个或零个 split, 没法判重 — 不算错, 但也没什么可报告的
        return CheckResult(
            name="split_uniqueness",
            severity=CheckSeverity.PASS,
            summary=f"少于 2 个 split, 跳过判重 (当前 splits: {list(snap.splits)})",
            details={"reason": "fewer_than_2_splits"},
        )

    # 每个 split 收集 stem 集合
    stems_by_split: Dict[str, set] = {
        split: {img.stem for img in images}
        for split, images in snap.images_per_split.items()
    }

    # 两两求交集
    overlaps: List[Dict[str, Any]] = []
    for s1, s2 in combinations(stems_by_split.keys(), 2):
        common = stems_by_split[s1] & stems_by_split[s2]
        if common:
            stems_sorted = sorted(common)
            overlaps.append({
                "split_a": s1,
                "split_b": s2,
                "count":   len(common),
                "preview": stems_sorted[:OVERLAP_PREVIEW_LIMIT],
            })

    if not overlaps:
        return CheckResult(
            name="split_uniqueness",
            severity=CheckSeverity.PASS,
            summary=(
                f"{len(snap.splits)} 个 split "
                f"({' / '.join(snap.splits)}) 之间无图像名重复"
            ),
            details={
                "splits": list(snap.splits),
            },
        )

    total_dup = sum(o["count"] for o in overlaps)
    pairs_str = ", ".join(f"{o['split_a']}↔{o['split_b']}({o['count']})" for o in overlaps)
    return CheckResult(
        name="split_uniqueness",
        severity=CheckSeverity.ERROR,
        summary=f"split 间有 {total_dup} 张图像名重复 — 数据泄露! [{pairs_str}]",
        details={
            "reason":      "splits_overlap",
            "splits":      list(snap.splits),
            "total_duplicates": total_dup,
            "overlaps":    overlaps,
        },
    )
