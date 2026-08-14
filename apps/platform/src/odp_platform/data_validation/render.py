# apps/platform/src/odp_platform/data_validation/render.py
"""render.py — D4 报告的展示层 (纯展示, 不动数据)。

唯一公开 API: render_to_logger(report, logger, report_path=None)

输出结构 (五段式):
    1. 报告头 (run_id / yaml_path / task / 耗时)
    2. 数据集摘要 (类别 / 各 split 的 3 个数字)
    3. 检查项一览 (每个 check 一行)
    4. 失败详情 (仅当有非 PASS check 时出现, 按 check 类型展开 details)
    5. 报告尾 (JSON 报告路径)

未来扩展:
    - render_to_html(report) → str         给非工程师看的报告 (D4.x)
    - render_to_markdown(report) → str     给 CI / Slack 摘要 (D4.x)
    所有 renderer 消费同一份 ValidationReport, 互不影响。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from odp_platform.data_validation.registry import CheckResult
from odp_platform.data_validation.report import ValidationReport
from odp_platform.data_validation.snapshot import DatasetSnapshot


H1_LINE = "=" * 72
H2_LINE = "-" * 72


# ============================================================
# 公开 API
# ============================================================

def render_to_logger(
    report:      ValidationReport,
    logger:      logging.Logger,
    report_path: Optional[Path] = None,
) -> None:
    """把 ValidationReport 渲染成三段式日志输出。

    无副作用之外的事 — 不写盘, 不 raise, 只调用 logger.info 把内容打出来。
    """
    _render_header(report, logger)
    _render_dataset_summary(report.snapshot, logger)
    _render_check_overview(report.results, logger)

    if report.failed_results:
        _render_failure_details(report.failed_results, logger)

    _render_footer(report_path, logger)


# ============================================================
# 段 1: 报告头
# ============================================================

def _render_header(report: ValidationReport, logger: logging.Logger) -> None:
    logger.info(H1_LINE)
    logger.info("                       YOLO 数据集验证报告")
    logger.info(H1_LINE)
    logger.info(f"  run_id   {report.run_id}")
    logger.info(f"  yaml     {report.yaml_path}")
    logger.info(
        f"  task     {report.snapshot.task_type:8s}  "
        f"耗时  {report.duration_seconds:.2f}s  "
        f"severity  {report.overall_severity}"
    )


# ============================================================
# 段 2: 数据集摘要
# ============================================================

def _render_dataset_summary(snapshot: DatasetSnapshot, logger: logging.Logger) -> None:
    logger.info(H2_LINE)
    logger.info("  ▸ 数据集摘要")

    if snapshot.class_names:
        names_str = ", ".join(snapshot.class_names)
        logger.info(f"    类别:  {names_str}  (nc={snapshot.nc})")
    else:
        logger.info("    类别:  (未取到 — yaml_schema 应已报错)")

    if not snapshot.stats_per_split:
        logger.info("    (无任何 split 可统计)")
        return

    for split, stat in snapshot.stats_per_split.items():
        logger.info(
            f"    {split:6s}:  "
            f"{stat.image_count:>6,} 张  /  "
            f"{stat.annotated_count:>6,} 标注  /  "
            f"{stat.total_instances:>6,} 实例"
        )


# ============================================================
# 段 3: 检查项一览
# ============================================================

def _render_check_overview(results: list, logger: logging.Logger) -> None:
    logger.info(H2_LINE)
    logger.info("  ▸ 检查项一览")
    for r in results:
        logger.info(f"    [{r.severity:7s}]  {r.name:18s}  {r.summary}")


# ============================================================
# 段 4: 失败详情 (well-known keys 模式)
# ============================================================

def _render_failure_details(failed: list, logger: logging.Logger) -> None:
    logger.info(H2_LINE)
    logger.info("  ▸ 失败详情")
    for r in failed:
        _render_one_check_details(r, logger)


def _render_one_check_details(r: CheckResult, logger: logging.Logger) -> None:
    """按 check name 选择性展开 details 里的 well-known 字段。

    展示层局部 if/elif —— 这里的增长很慢 (只在新 check 想自定义展开时
    才动这里), 跟"调度层的 if/elif"性质完全不同。 见讲义阶段 8.5 设计点。
    """
    logger.info("")
    logger.info(f"    >> {r.name}  [{r.severity}]")
    det = r.details

    if r.name == "yaml_schema" and "problems" in det:
        for p in det["problems"]:
            logger.info(f"        - {p}")

    elif r.name == "pair_existence":
        mps = det.get("missing_per_split", {})
        if mps:
            parts = ", ".join(f"{s}={n}" for s, n in mps.items())
            logger.info(f"        各 split 缺失:  {parts}")
        ex = det.get("missing_examples", {})
        for split, paths in ex.items():
            logger.info(f"        示例 ({split}, 前 {min(5, len(paths))} 条):")
            for p in paths[:5]:
                logger.info(f"          {p}")

    elif r.name == "label_format":
        kinds = det.get("error_kinds", {})
        if kinds:
            parts = ", ".join(f"{k}={v}" for k, v in kinds.items())
            logger.info(f"        错误类型:  {parts}")
        for e in det.get("errors_preview", [])[:5]:
            logger.info(
                f"        - {Path(e['label']).name}:{e['line_no']}  "
                f"{e['kind']}  {e['detail']}"
            )

    elif r.name == "split_uniqueness" and det.get("overlaps"):
        for o in det["overlaps"]:
            logger.info(
                f"        {o['split_a']} ↔ {o['split_b']}:  "
                f"{o['count']} 张重复"
            )
            for stem in o["preview"][:5]:
                logger.info(f"          {stem}")

    # 通用 fallback: 任何未匹配 well-known 分支的 check, 至少展示 reason
    else:
        if "reason" in det:
            logger.info(f"        reason: {det['reason']}")


# ============================================================
# 段 5: 报告尾
# ============================================================

def _render_footer(report_path: Optional[Path], logger: logging.Logger) -> None:
    logger.info(H2_LINE)
    if report_path is not None:
        logger.info(f"  详细报告:  {report_path}")
    logger.info(H1_LINE)
