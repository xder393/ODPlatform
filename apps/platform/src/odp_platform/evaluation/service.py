#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : service.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : ValService — 编排 D5 配置 + D4 校验 + D2 系统 + ultralytics 验证(评估)
"""验证(评估)服务编排器.

★ 跟 D6 TrainService 同款纪律 — 不重新发明 D5 / D4 / D2 已有的轮子:
  - 不写 YAMLLoader / CLILoader / ConfigMerger 调用 (走 build_val_config)
  - 不读 data.yaml 数样本 (走 validate_dataset)
  - 不配 logging handler (CLI 入口装)

跟 D6 TrainService 的差别:
  - 评估不产生权重, 所以没有 archive 阶段
  - 复用 common.result.TrainMetrics 存指标 (train/val 指标结构一致, 见 result.py)

验证方式:
  grep "YAMLLoader\\|CLILoader\\|ConfigMerger\\|build_snapshot" service.py
  → 应该没有任何输出.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ultralytics 是可选重依赖 — 未安装时本模块 import 不崩, 评估阶段再明确报错
try:
    from ultralytics import YOLO
except ImportError:  # pragma: no cover - 只在未安装 ultralytics 的环境触发
    YOLO = None  # type: ignore[assignment]

from odp_platform.common.config_log import log_effective_config, log_override_chains
from odp_platform.common.dataset_path import resolve_dataset_path
from odp_platform.common.log_rename import rename_log_to_save_dir
from odp_platform.common.model_path import resolve_model_path
from odp_platform.common.paths import RUNS_DIR
from odp_platform.common.result import TrainMetrics, log_train_metrics
from odp_platform.common.system_utils import log_device_info
from odp_platform.data_validation import render_to_logger, validate_dataset
from odp_platform.runtime_config import build_val_config

logger = logging.getLogger(__name__)


def _find_project_log_path() -> Path | None:
    """从 D2 'odp_platform' 根 logger 找 FileHandler 的实际文件路径.

    只读检查, 不操作 handler. 给 audit JSON 用.
    """
    root = logging.getLogger("odp_platform")
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            return Path(h.baseFilename)
    return None


@dataclass(frozen=True)
class ValResult:
    """评估结果一次性快照."""
    success:    bool
    output_dir: Path
    metrics:    dict[str, float] = field(default_factory=dict)
    val_time:   float | None = None
    error:      str | None = None
    audit_path: Path | None = None
    log_path:   Path | None = None


class ValService:
    """YOLO 验证(评估)流程编排."""

    def __init__(self) -> None:
        """__init__ 不接任何参数 — 配置都通过 val() 传."""
        pass

    def val(
        self,
        yaml_path: str | Path | None = None,
        cli_args: dict[str, Any] | None = None,
        *,
        pre_validate: bool = True,
        rename_log: bool = True,
    ) -> ValResult:
        """跑一次完整评估."""
        start = datetime.now()
        output_dir: Path | None = None

        try:
            # ============================================================
            # 阶段 1: 配置加载 (★ D5 接口承诺兑现, 一行)
            # ============================================================
            config, merger = build_val_config(
                yaml_path=yaml_path,
                cli_args=cli_args,
            )

            # ============================================================
            # 阶段 2: 上下文日志 (D2 系统快照 + D5 字段溯源)
            # ============================================================
            logger.info("=" * 60)
            logger.info(f"开始 YOLO 评估 (task={config.task})".center(60))
            logger.info("=" * 60)

            raw_model = config.model or "yolo11n.pt"
            raw_data = config.data
            logger.info(f"任务类型:    {config.task}")
            logger.info(f"数据集(声明): {raw_data}")
            data_path = resolve_dataset_path(raw_data)
            logger.info(f"数据集(解析): {data_path}")
            logger.info(f"模型(声明):  {raw_model}")
            model_path = resolve_model_path(raw_model)
            logger.info(f"模型(解析):  {model_path}")
            logger.info(f"评估划分:    {config.split}")

            log_device_info()
            log_effective_config(config, merger, logger=logger)
            log_override_chains(config, merger, logger=logger)

            # ============================================================
            # 阶段 3: 数据集预校验 (D4, 可关 — 评估指标来自坏数据等于白算)
            # ============================================================
            if pre_validate:
                logger.info("=" * 60)
                logger.info("数据集预校验 (D4)".center(60))
                logger.info("=" * 60)
                report = validate_dataset(data_path, task_type=config.task)
                render_to_logger(report, logger=logger)
                if report.exit_code >= 2:
                    error_count = len([
                        r for r in report.results
                        if getattr(r, "severity", None) == "ERROR"
                    ])
                    raise RuntimeError(
                        f"数据集校验失败 ({error_count} 个 ERROR 级问题). "
                        f"请用 `odp-validate --dataset {data_path.stem} "
                        f"--task {config.task}` 修复后再评估. "
                        f"如要跳过校验跑评估(不推荐), 加 --no-pre-validate."
                    )

            # ============================================================
            # 阶段 4: 加载模型
            # ============================================================
            if YOLO is None:
                raise RuntimeError(
                    "ultralytics 未安装 — 请先 `pip install ultralytics` 再评估."
                )
            model = YOLO(str(model_path))

            # ============================================================
            # 阶段 5: 执行评估 (ultralytics model.val)
            # ============================================================
            yolo_kwargs = config.to_ultralytics_kwargs()
            # 用解析后的绝对路径覆盖 — 防 ultralytics 拿 'rsod.yaml' 这种
            # 相对名在 cwd 找不到
            yolo_kwargs["data"] = str(data_path)
            # 输出根: 跟 D6 train / D8 infer 在 RUNS_DIR 下一级并列
            # (runs/detect_train / runs/detect_val / runs/detect_infer)
            yolo_kwargs.setdefault("project", str(RUNS_DIR / f"{config.task}_val"))

            logger.info("=" * 60)
            logger.info("启动评估".center(60))
            logger.info("=" * 60)
            logger.info(f"输出目录(project): {yolo_kwargs['project']}")

            val_metrics = model.val(**yolo_kwargs)
            # model.val() 返回 Metrics 对象, save_dir 可能在 metrics 上,
            # 也可能在 model.validator 上 — 依次兜底
            output_dir = Path(
                getattr(val_metrics, "save_dir", None)
                or getattr(getattr(model, "validator", None), "save_dir", None)
                or "unknown"
            )

            # ============================================================
            # 阶段 6: 结果指标
            # ============================================================
            logger.info("=" * 60)
            logger.info("评估完成".center(60))
            logger.info("=" * 60)
            # ★ model.val() 返回的 Metrics 不带 .task, 用 config.task 显式覆盖
            metrics = TrainMetrics.from_yolo_results(val_metrics, task=config.task)
            log_train_metrics(metrics, logger=logger, title="评估结果")

            # ============================================================
            # 阶段 7: 整理输出 (rename_log)
            # ============================================================
            if rename_log:
                rename_log_to_save_dir(output_dir, Path(raw_model).stem)

            # ============================================================
            # 阶段 8: 审计快照
            # ============================================================
            audit_path = output_dir / "odp_audit.json"
            log_path = _find_project_log_path()
            try:
                audit_payload = {
                    "config":  config.to_audit_snapshot(),
                    "merger":  merger.to_audit_log(),
                    "metrics": metrics.to_dict(),
                    "result_summary": {
                        "val_time_sec": (datetime.now() - start).total_seconds(),
                        "log_path": str(log_path) if log_path else None,
                    },
                }
                audit_path.write_text(
                    json.dumps(audit_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"审计快照: {audit_path}")
            except OSError as e:
                logger.warning(f"写审计快照失败(不影响评估结果): {e}")
                audit_path = None

            # ============================================================
            # 收尾 — ValResult
            # ============================================================
            val_time = (datetime.now() - start).total_seconds()

            logger.info("=" * 60)
            logger.info(f"评估总耗时: {val_time:.2f} 秒")
            logger.info(f"输出目录:   {output_dir}")
            if log_path:
                logger.info(f"本次日志:   {log_path}")
            logger.info("=" * 60)

            return ValResult(
                success=True,
                output_dir=output_dir,
                metrics=metrics.overall,
                val_time=val_time,
                audit_path=audit_path,
                log_path=log_path,
            )

        # =====================================================================
        # 顶层异常拦截 — 永不抛, 打包成 ValResult.error
        # =====================================================================
        except Exception as e:
            logger.error(f"评估失败: {e}", exc_info=True)
            val_time = (datetime.now() - start).total_seconds()
            return ValResult(
                success=False,
                output_dir=output_dir or Path("unknown"),
                metrics={},
                val_time=val_time,
                error=str(e),
                log_path=_find_project_log_path(),
            )


def val_yolo(
    yaml_path: str | Path | None = None,
    cli_args: dict[str, Any] | None = None,
    *,
    pre_validate: bool = True,
    rename_log: bool = True,
) -> ValResult:
    """一行启动评估 — 风格跟 D6 train_yolo 一致."""
    return ValService().val(
        yaml_path=yaml_path,
        cli_args=cli_args,
        pre_validate=pre_validate,
        rename_log=rename_log,
    )
