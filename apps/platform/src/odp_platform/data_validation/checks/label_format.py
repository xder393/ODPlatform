# apps/platform/src/odp_platform/data_validation/checks/label_format.py
"""label_format check — 验证每行 .txt 的格式合法性。

支持两种 task (从 snapshot.task_type 读):
    detect:  每行 'cls cx cy w h'                 (5 字段)
    segment: 每行 'cls x1 y1 x2 y2 ... xN yN'    (1+2N 字段, N>=3)

判定:
    - 全通过 → PASS
    - 任何一行错 → ERROR (不分级 — label 格式是结构性问题)

details 收集:
    - error_kinds:    错误类型计数 (字段数 / 类别越界 / 坐标越界 / 多边形点数不足)
    - errors_preview: 前 N 条具体错误样例 (label 文件名 + 行号 + 错误类型)
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from odp_platform.common.constants import Task
from odp_platform.data_validation.registry import (
    check, CheckContext, CheckResult, CheckSeverity,
)


ERRORS_PREVIEW_LIMIT = 20

# 错误类型常量 (机器可读, 用于 details["error_kinds"] 计数)
KIND_FIELD_COUNT_MISMATCH = "field_count_mismatch"
KIND_PARSE_ERROR          = "parse_error"
KIND_CLASS_ID_OUT_OF_RANGE = "class_id_out_of_range"
KIND_COORD_OUT_OF_RANGE   = "coord_out_of_range"
KIND_POLYGON_TOO_FEW      = "polygon_too_few_points"


@check("label_format")
def validate_label_format(ctx: CheckContext) -> CheckResult:
    snap = ctx.snapshot

    if snap.nc is None or snap.nc <= 0:
        # 没有合法 nc, 没法验证 class_id 越界 — 留给 yaml_schema 报错, 我们 INFO 跳过
        return CheckResult(
            name="label_format",
            severity=CheckSeverity.INFO,
            summary="缺少合法 nc, 跳过 label_format (yaml_schema 应已报告)",
            details={"reason": "nc_unavailable"},
        )

    task_type = snap.task_type
    errors: List[Dict[str, Any]] = []
    error_kinds: Counter = Counter()
    total_lines = 0

    for split, labels in snap.labels_per_split.items():
        for lbl in labels:
            if not lbl.exists():
                continue   # pair_existence 报
            try:
                content = lbl.read_text(encoding="utf-8")
            except OSError:
                continue   # IO 错也跳过 — best effort
            for line_no, line in enumerate(content.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue   # 空行允许
                total_lines += 1
                err = _validate_one_line(line, task_type, snap.nc)
                if err is not None:
                    kind, detail = err
                    error_kinds[kind] += 1
                    if len(errors) < ERRORS_PREVIEW_LIMIT:
                        errors.append({
                            "label":   str(lbl),
                            "line_no": line_no,
                            "kind":    kind,
                            "detail":  detail,
                        })

    if not error_kinds:
        return CheckResult(
            name="label_format",
            severity=CheckSeverity.PASS,
            summary=f"全部 {total_lines} 行标签格式正确 (task={task_type})",
            details={
                "task_type":   task_type,
                "total_lines": total_lines,
            },
        )

    total_errors = sum(error_kinds.values())
    return CheckResult(
        name="label_format",
        severity=CheckSeverity.ERROR,
        summary=f"{total_errors}/{total_lines} 行标签格式错误 (task={task_type})",
        details={
            "task_type":       task_type,
            "total_lines":     total_lines,
            "total_errors":    total_errors,
            "error_kinds":     dict(error_kinds),
            "errors_preview":  errors,
        },
    )


# ============================================================
# 私有: 验证单行
# ============================================================

def _validate_one_line(line: str, task_type: str, nc: int):
    """验证单行 .txt, 返回 (kind, detail) 或 None (通过)。"""
    parts = line.split()

    # ---------- detect ----------
    if task_type == Task.DETECT:
        if len(parts) != 5:
            return KIND_FIELD_COUNT_MISMATCH, f"detect 任务要求 5 字段, 实际 {len(parts)}"
        try:
            cls_id = int(parts[0])
            coords = [float(x) for x in parts[1:5]]
        except ValueError as e:
            return KIND_PARSE_ERROR, f"字段类型错: {e}"
        if not (0 <= cls_id < nc):
            return KIND_CLASS_ID_OUT_OF_RANGE, f"cls_id={cls_id} 不在 [0,{nc}) 内"
        if not all(0.0 <= c <= 1.0 for c in coords):
            bad = [round(c, 4) for c in coords if not (0.0 <= c <= 1.0)]
            return KIND_COORD_OUT_OF_RANGE, f"坐标越界 [0,1]: {bad}"
        return None

    # ---------- segment ----------
    if task_type == Task.SEGMENT:
        # 至少: cls + 3 对 (x,y) = 1 + 6 = 7 字段
        if len(parts) < 7 or (len(parts) - 1) % 2 != 0:
            if len(parts) < 7:
                return KIND_POLYGON_TOO_FEW, f"segment 任务多边形至少 3 个点 (7 字段), 实际 {len(parts)}"
            return KIND_FIELD_COUNT_MISMATCH, f"segment 任务字段数应为 1+2N, 实际 {len(parts)} (N 非整)"
        try:
            cls_id = int(parts[0])
            coords = [float(x) for x in parts[1:]]
        except ValueError as e:
            return KIND_PARSE_ERROR, f"字段类型错: {e}"
        if not (0 <= cls_id < nc):
            return KIND_CLASS_ID_OUT_OF_RANGE, f"cls_id={cls_id} 不在 [0,{nc}) 内"
        if not all(0.0 <= c <= 1.0 for c in coords):
            bad_count = sum(1 for c in coords if not (0.0 <= c <= 1.0))
            return KIND_COORD_OUT_OF_RANGE, f"{bad_count}/{len(coords)} 个坐标越界 [0,1]"
        return None

    # 未知 task_type — build_snapshot 已经兜底过了, 这里再防一层
    return KIND_PARSE_ERROR, f"未知 task_type: {task_type}"
