#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : service.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : TrainService — 编排 D5 配置 + D4 校验 + D2 系统 + ultralytics 训练
"""训练服务编排器.

★ 核心纪律: 不重新发明 D5 / D4 / D2 已有的轮子. 这个 service 内部:
  - 不写 YAMLLoader / CLILoader / ConfigMerger 调用 (走 build_train_config)
  - 不读 data.yaml 数样本 (走 validate_dataset)
  - 不配 logging handler / 不感知 FileHandler 细节
    (handler 由 D2 logging_utils.get_logger() 在 CLI 入口装好)

验证方式:
  grep "YAMLLoader\\|CLILoader\\|ConfigMerger\\|build_snapshot" service.py
  → 应该没有任何输出. 子系统边界清晰的硬指标.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ultralytics 是可选重依赖 — 未安装时本模块 import 不崩, 训练阶段再明确报错
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
from odp_platform.runtime_config import build_train_config

from .archive import archive_checkpoints

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
class TrainResult:
    """训练结果一次性快照."""
    success:     bool
    output_dir:  Path
    best_weight: Path | None = None
    last_weight: Path | None = None
    metrics:     dict[str, float] = field(default_factory=dict)
    train_time:  float | None = None
    error:       str | None = None
    audit_path:  Path | None = None
    log_path:    Path | None = None


class TrainService:
    """YOLO 训练流程编排."""

    def __init__(self) -> None:
        """__init__ 不接任何参数 — 配置都通过 train() 传."""
        pass

    def train(
        self,
        yaml_path: str | Path | None = None,
        cli_args: dict[str, Any] | None = None,
        *,
        pre_validate: bool = True,
        archive: bool = True,
        rename_log: bool = True,
    ) -> TrainResult:
        """跑一次完整训练."""
        start = datetime.now()
        output_dir: Path | None = None

        try:
            # ============================================================
            # 阶段 1: 配置加载 (★ D5 接口承诺兑现, 一行)
            # ============================================================
            config, merger = build_train_config(
                yaml_path=yaml_path,
                cli_args=cli_args,
            )

            # ============================================================
            # 阶段 2: 上下文日志 (D2 系统快照 + D5 字段溯源)
            # ============================================================
            logger.info("=" * 60)
            logger.info(f"开始 YOLO 训练 (task={config.task})".center(60))
            logger.info("=" * 60)

            # 立即展示核心标识 — 即使后面崩, 用户也看到"在训啥"
            raw_model = config.model or "yolo11n.pt"
            raw_data = config.data
            logger.info(f"任务类型:    {config.task}")
            logger.info(f"数据集(声明): {raw_data}")
            data_path = resolve_dataset_path(raw_data)
            logger.info(f"数据集(解析): {data_path}")
            logger.info(f"模型(声明):  {raw_model}")
            model_path = resolve_model_path(raw_model)
            logger.info(f"模型(解析):  {model_path}")

            # D2 系统快照
            log_device_info()

            # D5 字段溯源 (两段: 当前值/来源 + 完整链)
            log_effective_config(config, merger, logger=logger)
            log_override_chains(config, merger, logger=logger)

            # ============================================================
            # 阶段 3: 数据集预校验 (D4, 可关)
            # ============================================================
            if pre_validate:
                logger.info("=" * 60)
                logger.info("数据集预校验 (D4)".center(60))
                logger.info("=" * 60)
                report = validate_dataset(data_path, task_type=config.task)
                render_to_logger(report, logger=logger)
                # exit_code: 0=PASS/INFO 1=WARNING 2=ERROR
                if report.exit_code >= 2:
                    error_count = len([
                        r for r in report.results
                        if getattr(r, "severity", None) == "ERROR"
                    ])
                    raise RuntimeError(
                        f"数据集校验失败 ({error_count} 个 ERROR 级问题). "
                        f"请用 `odp-validate --dataset {data_path.stem} "
                        f"--task {config.task}` 修复后再训练. "
                        f"如要跳过校验跑训练(不推荐), 加 --no-pre-validate."
                    )

            # ============================================================
            # 阶段 4: 加载模型
            # ============================================================
            if YOLO is None:
                raise RuntimeError(
                    "ultralytics 未安装 — 请先 `pip install ultralytics` 再训练."
                )
            model = YOLO(str(model_path))

            # ============================================================
            # 阶段 5: 执行训练 (ultralytics)
            # ============================================================
            yolo_kwargs = config.to_ultralytics_kwargs()
            # 用解析后的绝对路径覆盖 — 防 ultralytics 拿 'rsod.yaml' 这种
            # 相对名在 cwd 找不到
            yolo_kwargs["data"] = str(data_path)
            # 用户没指定 project 时, 走 RUNS_DIR/<task>_train/ 作为输出根.
            # 扁平化命名 <task>_<mode>: 最终路径形如 runs/detect_train/train,
            # runs/detect_train/train2. D7 评估走 runs/detect_val/, D8 推理走
            # runs/detect_infer/ — 它们在 RUNS_DIR 下一级并列, 不互相穿过.
            # 不做 runs/train/detect/ 这种嵌套(detect 会变成"穿过层", 视觉很丑).
            yolo_kwargs.setdefault("project", str(RUNS_DIR / f"{config.task}_train"))

            logger.info("=" * 60)
            logger.info("启动训练".center(60))
            logger.info("=" * 60)
            logger.info(f"输出目录(project): {yolo_kwargs['project']}")

            yolo_results = model.train(**yolo_kwargs)
            output_dir = Path(yolo_results.save_dir)

            # ============================================================
            # 阶段 6: 结果指标
            # ============================================================
            logger.info("=" * 60)
            logger.info("训练完成".center(60))
            logger.info("=" * 60)
            # ★ ultralytics 8.x 的 model.train() 返回 Metrics 对象, 不带 .task,
            #   用 config.task 显式覆盖, 保证 _METRIC_FIELDS_BY_TASK 选对字段表
            metrics = TrainMetrics.from_yolo_results(
                yolo_results,
                model_trainer=getattr(model, "trainer", None),
                task=config.task,
            )
            log_train_metrics(metrics, logger=logger)

            # ============================================================
            # 阶段 7: 整理输出 (rename_log 先, archive 后)
            # ============================================================
            model_stem = Path(raw_model).stem

            # 7a. 改日志名跟 save_dir 对齐(归档动作也能进新文件名的日志)
            if rename_log:
                rename_log_to_save_dir(output_dir, model_stem)

            # 7b. 归档权重
            archived: dict[str, Path] = {}
            if archive:
                archived = archive_checkpoints(
                    train_dir=output_dir,
                    model_filename=raw_model,
                )

            # ============================================================
            # 阶段 8: 审计快照 (★ 给未来 experiment_db 留落点)
            # ============================================================
            audit_path = output_dir / "odp_audit.json"
            log_path = _find_project_log_path()
            try:
                audit_payload = {
                    "config":  config.to_audit_snapshot(),
                    "merger":  merger.to_audit_log(),
                    "metrics": metrics.to_dict(),
                    "result_summary": {
                        "best_archive": str(archived.get("best", "")) or None,
                        "last_archive": str(archived.get("last", "")) or None,
                        "train_time_sec": (datetime.now() - start).total_seconds(),
                        "log_path": str(log_path) if log_path else None,
                    },
                }
                audit_path.write_text(
                    json.dumps(audit_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"审计快照: {audit_path}")
            except OSError as e:
                logger.warning(f"写审计快照失败(不影响训练结果): {e}")
                audit_path = None

            # ============================================================
            # 收尾 — TrainResult
            # ============================================================
            train_time = (datetime.now() - start).total_seconds()
            best_weight = archived.get("best") or (output_dir / "weights" / "best.pt")
            last_weight = archived.get("last") or (output_dir / "weights" / "last.pt")

            logger.info("=" * 60)
            logger.info(f"训练总耗时: {train_time:.2f} 秒")
            logger.info(f"输出目录:   {output_dir}")
            logger.info(f"最佳权重:   {best_weight}")
            if log_path:
                logger.info(f"本次日志:   {log_path}")
            logger.info("=" * 60)

            return TrainResult(
                success=True,
                output_dir=output_dir,
                best_weight=best_weight if best_weight.exists() else None,
                last_weight=last_weight if last_weight.exists() else None,
                metrics=metrics.overall,
                train_time=train_time,
                audit_path=audit_path,
                log_path=log_path,
            )

        # =====================================================================
        # 顶层异常拦截 — 永不抛, 打包成 TrainResult.error
        # =====================================================================
        except Exception as e:
            logger.error(f"训练失败: {e}", exc_info=True)
            train_time = (datetime.now() - start).total_seconds()
            return TrainResult(
                success=False,
                output_dir=output_dir or Path("unknown"),
                metrics={},
                train_time=train_time,
                error=str(e),
                log_path=_find_project_log_path(),  # 失败也带日志路径, 方便排查
            )


def train_yolo(
    yaml_path: str | Path | None = None,
    cli_args: dict[str, Any] | None = None,
    *,
    pre_validate: bool = True,
    archive: bool = True,
    rename_log: bool = True,
) -> TrainResult:
    """一行启动训练 — 风格跟 D5 build_train_config 一致."""
    service = TrainService()
    return service.train(
        yaml_path=yaml_path,
        cli_args=cli_args,
        pre_validate=pre_validate,
        archive=archive,
        rename_log=rename_log,
    )
