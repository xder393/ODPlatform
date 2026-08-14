# apps/platform/src/odp_platform/data_validation/checks/pair_existence.py
"""pair_existence check — 验证每张图都有对应的 .txt 标签文件。

Severity 按缺失比例分级:
    missing_ratio == 0.0           → PASS
    missing_ratio <  WARN_RATIO    → INFO    (个别遗漏, 可容忍)
    missing_ratio <  ERROR_RATIO   → WARNING (显著遗漏, 影响精度)
    missing_ratio >= ERROR_RATIO   → ERROR   (流程级问题, 不允许训练)

注: 空 .txt 文件算"存在"且合法 (YOLO 接受空标签表示无目标图像)。
    这里只判定"文件是否存在", 不判定"是否为空"。
"""
from __future__ import annotations

from typing import Any, Dict, List

from odp_platform.common.constants import (
    PAIR_MISSING_ERROR_RATIO,
    PAIR_MISSING_WARN_RATIO,
)
from odp_platform.data_validation.registry import (
    check, CheckContext, CheckResult, CheckSeverity,
)


# 详情里展示的"前 N 个缺失文件"上限 — 避免 details 爆炸
DETAILS_PREVIEW_LIMIT = 20


@check("pair_existence")
def validate_pair_existence(ctx: CheckContext) -> CheckResult:
    snap = ctx.snapshot

    if not snap.images_per_split:
        return CheckResult(
            name="pair_existence",
            severity=CheckSeverity.INFO,
            summary="无任何 split 可检查 (snapshot 为空)",
            details={"reason": "empty_snapshot"},
        )

    # 收集每个 split 的孤儿图像 (有图无标签)
    orphan_per_split: Dict[str, List[str]] = {}
    total_images  = 0
    total_missing = 0

    for split, images in snap.images_per_split.items():
        labels = snap.labels_per_split.get(split, ())
        missing_in_split: List[str] = []
        # build_snapshot 保证两者长度一致 + 顺序一致 (按图像名构造标签路径)
        for img, lbl in zip(images, labels):
            total_images += 1
            if not lbl.exists():
                total_missing += 1
                missing_in_split.append(str(img))
        if missing_in_split:
            orphan_per_split[split] = missing_in_split

    missing_ratio = total_missing / max(total_images, 1)

    # ---------- 按比例分级 severity (从最严到最松写) ----------
    if total_missing == 0:
        severity = CheckSeverity.PASS
        summary  = f"全部 {total_images} 张图像都有对应标签"
    elif missing_ratio >= PAIR_MISSING_ERROR_RATIO:
        severity = CheckSeverity.ERROR
        summary = (
            f"缺标签比例 {missing_ratio:.1%} ≥ {PAIR_MISSING_ERROR_RATIO:.0%} "
            f"({total_missing}/{total_images} 张图无标签)"
        )
    elif missing_ratio >= PAIR_MISSING_WARN_RATIO:
        severity = CheckSeverity.WARNING
        summary = (
            f"缺标签比例 {missing_ratio:.1%} ≥ {PAIR_MISSING_WARN_RATIO:.0%} "
            f"({total_missing}/{total_images} 张图无标签)"
        )
    else:
        severity = CheckSeverity.INFO
        summary = (
            f"少量标签缺失 ({total_missing}/{total_images} = {missing_ratio:.2%})"
        )

    # ---------- 构造 details (含 preview 截断防止报告爆炸) ----------
    details: Dict[str, Any] = {
        "total_images":  total_images,
        "total_missing": total_missing,
        "missing_ratio": round(missing_ratio, 4),
        "thresholds": {
            "error_at": PAIR_MISSING_ERROR_RATIO,
            "warn_at":  PAIR_MISSING_WARN_RATIO,
        },
        "missing_per_split": {
            split: len(orphans) for split, orphans in orphan_per_split.items()
        },
    }

    # 详细清单截断 — 前 N 条供日志展开, 完整列表通过 JSON 报告查看
    if orphan_per_split:
        details["missing_examples"] = {
            split: orphans[:DETAILS_PREVIEW_LIMIT]
            for split, orphans in orphan_per_split.items()
        }

    return CheckResult(
        name="pair_existence",
        severity=severity,
        summary=summary,
        details=details,
    )
