"""odp-transform 命令——D3 端到端 CLI。"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from odp_platform.common.constants import AnnotationFormat, Task
from odp_platform.common.logging_utils import get_logger
from odp_platform.common.paths import LOGGING_DIR
from odp_platform.data_pipeline import (
    DatasetPipeline, list_capabilities,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odp-transform",
        description=(
            "ODPlatform 数据集转换 + 划分工具。\n"
            "把 data/raw/<dataset>/{images,annotations}/ 转换、划分,\n"
            "并生成 ultralytics 训练用的 <dataset>.yaml。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_capability_matrix_text(),
    )
    parser.add_argument("--dataset", "-d", required=True,
                        help="数据集名 (= data/raw/ 下的子目录名)")
    parser.add_argument("--format", "-f", required=True,
                        choices=list(AnnotationFormat.all()),
                        help="原始标注格式")
    parser.add_argument("--task", "-t", default=Task.DETECT,
                        choices=list(Task.all()),
                        help="任务类型 (默认 detect)")
    parser.add_argument("--train-rate", type=float, default=0.8)
    parser.add_argument("--val-rate", type=float, default=0.1,
                        help="(test 自动 = 1 - train - val)")
    parser.add_argument("--classes", nargs="+", default=None,
                        help="类别白名单 (YOLO 格式必传)")
    parser.add_argument("--random-state", type=int, default=42,
                        help="随机种子 (会写入 yaml 的 odp_meta)")
    parser.add_argument("--coco-cls91to80", action="store_true",
                        help="(COCO 专属) 91 类映射到 80 类")
    return parser


def _capability_matrix_text() -> str:
    """生成 --help 末尾的能力矩阵——从 registry 实时读。"""
    try:
        caps = list_capabilities()
    except Exception:
        return ""
    lines = ["格式能力矩阵:"]
    for fmt, tasks in caps.items():
        lines.append(f"  {fmt:<15} -> {tasks}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # CLI 入口装配 odp_platform 根 logger——之后所有 getLogger(__name__) 冒泡继承
    get_logger(base_path=LOGGING_DIR, log_type="transform")

    try:
        result = DatasetPipeline(
            dataset_name=args.dataset,
            annotation_format=args.format,
            task=args.task,
            train_rate=args.train_rate,
            val_rate=args.val_rate,
            classes=args.classes,
            coco_cls91to80=args.coco_cls91to80,
            random_state=args.random_state,
        ).run()
        logger.info(f"成功完成: {result}")
        return 0

    except (ValueError, NotImplementedError) as e:
        logger.error(f"参数/能力错误: {e}")
        return 1
    except FileNotFoundError as e:
        logger.error(f"数据集问题: {e}")
        return 2
    except Exception as e:
        logger.exception(f"未预期异常: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
