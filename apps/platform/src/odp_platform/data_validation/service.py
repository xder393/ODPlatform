# apps/platform/src/odp_platform/data_validation/service.py
"""data_validation 调度层 — run_all_checks + validate_dataset。

聚合模式核心承诺: 任何 check 抛异常都不能阻断其他 check。
这条承诺通过 _safe_run_one 里的 try/except Exception 兑现 —— 整个 D4 子系统
里仅此一处用到 Exception 这种宽泛的捕获, 因为这里是"开闭原则下的扩展点"
(谁都能加 check), 调度层无法预知 check 会抛什么。

其他地方应该用具体的 except OSError / except yaml.YAMLError 等。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from odp_platform.common.paths import validation_run_dir
from odp_platform.common.performance_utils import time_it
from odp_platform.common.system_utils import log_device_info
from odp_platform.data_validation.registry import (
    CheckContext, CheckEntry, CheckResult, CheckSeverity, get_all_checks,
)
from odp_platform.data_validation.report import ValidationReport
from odp_platform.data_validation.snapshot import build_snapshot

logger = logging.getLogger(__name__)


@time_it(name="run_all_checks")
def run_all_checks(ctx: CheckContext) -> List[CheckResult]:
    """跑全部注册的 check, 收集结果。

    聚合模式承诺:
        任何 check 自身抛异常都被本函数接住, 包装成 ERROR 级 CheckResult,
        不阻断其他 check。 这是 D4 跟 D3 (互斥模式) 最大的区别 —— D3 service.convert
        失败立刻抛, D4 service.run_all_checks 失败也继续。

    Returns:
        List[CheckResult] — 按 check 注册顺序, 一条不漏
    """
    entries = get_all_checks()
    logger.info(f"开始执行 {len(entries)} 个 check")

    results: List[CheckResult] = []
    for entry in entries:
        result = _safe_run_one(entry, ctx)
        _log_check_result(result)
        results.append(result)

    _log_summary(results)
    return results


def _safe_run_one(entry: CheckEntry, ctx: CheckContext) -> CheckResult:
    """跑单个 check, 异常包装成 ERROR — 聚合模式承诺的兑现处。

    设计:
        - try/except Exception 是【唯一】合理使用宽泛捕获的地方。
          理由: check 是开闭原则下的扩展点, 谁都能加新 check, 调度层无法预知
          它会抛什么(KeyError / TypeError / 自定义异常 / ...)。 接住一切,
          包装成 ERROR 级 CheckResult, 让其他 check 继续。

        - exc_info=True: 把完整 traceback 写到日志, 方便事后排查 — 这条信息
          不会出现在 CheckResult.summary 里(避免污染用户看到的简短摘要),
          但保留在日志文件里, 调试时 grep 一下就有。
    """
    try:
        return entry.func(ctx)
    except Exception as e:
        logger.exception(f"check '{entry.name}' 抛异常, 已捕获为 ERROR 级结果")
        return CheckResult(
            name=entry.name,
            severity=CheckSeverity.ERROR,
            summary=f"check 内部异常: {type(e).__name__}: {e}",
            details={
                "exception_type": type(e).__name__,
                "exception_msg":  str(e),
            },
        )


def _log_check_result(r: CheckResult) -> None:
    """单个 check 跑完即时打一条日志, 让用户看到进度。

    Severity → log level 映射:
        ERROR   → logger.error
        WARNING → logger.warning
        INFO    → logger.info
        PASS    → logger.debug   (默认不显示, -v 才看)

    为什么 PASS 走 DEBUG:
        PASS 不是"事件", 是"非事件" — 健康数据集每次跑 4 行 [PASS] 刷屏,
        噪声盖过信号。 日志只记 WARN/ERROR/INFO 这种"有变化"的事件,
        PASS 信息走 ValidationReport 的最终摘要 (阶段 8 的 render.py),
        集中展示。
    """
    log_method = {
        CheckSeverity.ERROR:   logger.error,
        CheckSeverity.WARNING: logger.warning,
        CheckSeverity.INFO:    logger.info,
        CheckSeverity.PASS:    logger.debug,
    }.get(r.severity, logger.info)

    log_method(f"[{r.severity:7s}] {r.name}: {r.summary}")


def _log_summary(results: List[CheckResult]) -> None:
    """所有 check 跑完后, 打一行总览。"""
    counts = {}
    for r in results:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    parts = [f"{n} {s}" for s, n in counts.items()]
    logger.info(f"check 执行完毕: {' / '.join(parts)}")


def validate_dataset(
    yaml_path:    Path,
    task_type:    Optional[str] = None,
    run_id:       Optional[str] = None,
    run_dir:      Optional[Path] = None,
    write_report: bool = True,
) -> ValidationReport:
    """端到端验证: 构造 snapshot → 跑 check → 包装 report → 可选写盘。

    Args:
        yaml_path:    数据集 yaml 文件路径
        task_type:    'detect' / 'segment' / None (None → 读 yaml.task, 再不行 detect)
        run_id:       手动指定运行 ID; None 表示自动用时间戳
        run_dir:      手动指定运行目录; None 表示用 validation_run_dir(run_id)
        write_report: 是否写 JSON 报告到 run_dir/report.json

    Returns:
        ValidationReport (run_dir 字段已填, 调用方可以拿 .report_path 取 JSON 位置)
    """
    # ---- 解析 run_id / run_dir ----
    resolved_run_id  = run_id  or datetime.now().strftime("%Y%m%d_%H%M%S")
    resolved_run_dir = run_dir or (validation_run_dir(resolved_run_id) if write_report else None)

    if write_report and resolved_run_dir is not None:
        resolved_run_dir.mkdir(parents=True, exist_ok=True)

    # ---- 跑核心流程 ----
    t0 = time.perf_counter()
    started_iso = datetime.now(timezone.utc).isoformat()

    log_device_info(logger)   # 端到端入口打一次设备信息, 让"慢"可归因
    snapshot = build_snapshot(yaml_path=yaml_path, task_type=task_type)
    ctx      = CheckContext(yaml_path=yaml_path, snapshot=snapshot)
    results  = run_all_checks(ctx)

    duration = time.perf_counter() - t0

    # ---- 包装 ValidationReport ----
    report = ValidationReport(
        run_id=resolved_run_id,
        yaml_path=yaml_path,
        snapshot=snapshot,
        results=results,
        duration_seconds=duration,
        started_at_iso=started_iso,
        run_dir=resolved_run_dir,
    )

    # ---- 写 JSON 报告 ----
    if write_report and resolved_run_dir is not None:
        report_path = resolved_run_dir / "report.json"
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return report
