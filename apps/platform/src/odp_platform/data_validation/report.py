# apps/platform/src/odp_platform/data_validation/report.py
"""ValidationReport — 一次验证的完整产出 (纯数据)。

设计原则: 只装数据 + 派生属性 + to_dict。
所有"怎么展示给人看"的逻辑归 render.py — 这是 D4 撞墙③ 的解药。

派生属性 (永不存值):
    - overall_severity:   全部结果里最严重的级别
    - counts_by_severity: 各级别计数
    - exit_code:          0/1/2 — Unix 退出码语义
    - failed_results:     非 PASS 的 results 子集
    - report_path:        run_dir/report.json (run_dir 未设则 None)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from odp_platform.data_validation.registry import CheckResult, CheckSeverity
from odp_platform.data_validation.snapshot import DatasetSnapshot


@dataclass
class ValidationReport:
    """一次验证的完整产出。

    Args:
        run_id:            形如 '20260516_184523' 的时间戳 ID
        yaml_path:         验证的 yaml 文件路径
        snapshot:          一次扫描产物 (含 stats_per_split / class_names 等)
        results:           各 check 的 CheckResult 列表 (按注册顺序)
        duration_seconds:  整体耗时
        started_at_iso:    起始时间 ISO 8601 字符串 (UTC)
        run_dir:           本次运行的产出目录 (None = 不写盘模式)
    """
    run_id:           str
    yaml_path:        Path
    snapshot:         DatasetSnapshot
    results:          List[CheckResult]
    duration_seconds: float
    started_at_iso:   str
    run_dir:          Optional[Path] = None

    # ---------- 派生属性 (从 results 算, 不存值) ----------

    @property
    def overall_severity(self) -> str:
        """全部结果里最严重的 severity。

        无结果时返回 PASS — 'nothing went wrong' 的合理默认。
        """
        if not self.results:
            return CheckSeverity.PASS
        return max(
            self.results,
            key=lambda r: CheckSeverity.rank(r.severity),
        ).severity

    @property
    def counts_by_severity(self) -> Dict[str, int]:
        """{ERROR: 0, WARNING: 1, INFO: 2, PASS: 1} 之类。"""
        counts: Dict[str, int] = {}
        for r in self.results:
            counts[r.severity] = counts.get(r.severity, 0) + 1
        return counts

    @property
    def exit_code(self) -> int:
        """Unix 退出码:
            0 — PASS / 只有 INFO
            1 — 有 WARNING
            2 — 有 ERROR
        """
        sev = self.overall_severity
        if sev == CheckSeverity.ERROR:
            return 2
        if sev == CheckSeverity.WARNING:
            return 1
        return 0

    @property
    def failed_results(self) -> List[CheckResult]:
        """非 PASS / 非 INFO 的结果 (即 WARNING + ERROR)。"""
        return [r for r in self.results if not r.passed]

    @property
    def report_path(self) -> Optional[Path]:
        """JSON 报告路径 — run_dir 未设时返回 None。"""
        return (self.run_dir / "report.json") if self.run_dir else None

    # ---------- JSON 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        """供 JSON 报告序列化。结构稳定 — 监控 / 趋势分析可以读这份。"""
        return {
            "run_id":           self.run_id,
            "yaml_path":        str(self.yaml_path),
            "task_type":        self.snapshot.task_type,
            "started_at":       self.started_at_iso,
            "duration_seconds": round(self.duration_seconds, 3),
            "overall_severity": self.overall_severity,
            "exit_code":        self.exit_code,
            "counts":           self.counts_by_severity,
            "dataset_summary": {
                "nc":          self.snapshot.nc,
                "class_names": list(self.snapshot.class_names),
                "stats_per_split": {
                    split: {
                        "image_count":     stat.image_count,
                        "annotated_count": stat.annotated_count,
                        "total_instances": stat.total_instances,
                    }
                    for split, stat in self.snapshot.stats_per_split.items()
                },
            },
            "results": [
                {
                    "name":     r.name,
                    "severity": r.severity,
                    "summary":  r.summary,
                    "details":  r.details,
                }
                for r in self.results
            ],
        }
