# apps/platform/src/odp_platform/data_validation/snapshot.py
"""DatasetSnapshot — 一次扫描, 多次复用。

设计原则:
    - 装数据, 不做判断 (业务判断属于 check 函数)
    - frozen=True 防止 check 串改造成调试灾难
    - best-effort: yaml 解析失败 / split 目录不存在都不抛, 装进 snapshot
      让 check 自己拿去判定 — 调度层永远不该自己 fail
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from odp_platform.common.constants import IMAGE_EXTENSIONS, Task
from odp_platform.common.performance_utils import time_it

logger = logging.getLogger(__name__)


# ============================================================
# SplitStats — 轻量统计 (3 个数字, 供报告展示)
# ============================================================

@dataclass(frozen=True)
class SplitStats:
    """单个 split 的轻量统计 — 用于 ValidationReport 数据集摘要。

    刻意只装 3 个数字 (不分类别 / 不算 bbox 面积):
        够"看一眼知道数据集啥规模", 不越界做分布分析。
        细分统计如果需要, 加独立的 check, 不污染 snapshot。
    """
    image_count:     int    # 该 split 的图像总数
    annotated_count: int    # 有非空标签的图像数
    total_instances: int    # 全部 bbox 实例数 (label 文件总行数)


# ============================================================
# DatasetSnapshot — 不可变数据载体
# ============================================================

@dataclass(frozen=True)
class DatasetSnapshot:
    """对一个数据集做一次完整快照, 供所有 check 共享消费。

    所有路径已经解析为绝对路径; 调用方不需要再考虑相对路径 / cwd 问题。

    Args:
        yaml_path:        数据集 yaml 文件绝对路径
        yaml_data:        原始 yaml 解析结果 (dict, 供 yaml_schema 直接判定)
        yaml_load_error:  yaml 解析过程中的错误信息 (None = 解析成功)
                          注意: 解析失败时 yaml_data 是 {}, 但 snapshot 仍然构造
                          成功 — 让 yaml_schema check 拿着 yaml_load_error 报错,
                          而不是 build_snapshot 自己抛异常
        data_root:        数据集根目录 (yaml 里 'path' 字段, 或 yaml 同级目录兜底)
        nc:               类别数 (yaml.nc; 缺失或非法时 None)
        class_names:      类别名 (按 ID 升序的 tuple; 缺失时空 tuple)
        task_type:        'detect' / 'segment'
        images_per_split: {split_name: (图像 Path, ...)} 绝对路径, 已排序
        labels_per_split: {split_name: (预期标签 Path, ...)} 按 stem 一对一对应
                          注意: 此处只是"按图像名构造期望的 .txt 路径", 不保证
                          文件存在 — pair_existence check 才负责存在性
        stats_per_split:  {split_name: SplitStats} 每个 split 的 3 个数字
        scan_warnings:    扫描过程中的非致命警告 (split 目录不存在等)
    """
    yaml_path:        Path
    yaml_data:        Dict[str, Any]
    yaml_load_error:  Optional[str]
    data_root:        Path
    nc:               Optional[int]
    class_names:      Tuple[str, ...]
    task_type:        str
    images_per_split: Dict[str, Tuple[Path, ...]]
    labels_per_split: Dict[str, Tuple[Path, ...]]
    stats_per_split:  Dict[str, SplitStats]
    scan_warnings:    Tuple[str, ...] = field(default_factory=tuple)

    # ---------- 派生属性 (不存值) ----------

    @property
    def splits(self) -> Tuple[str, ...]:
        """返回快照里实际有图像的 split 名 (按 train/val/test 顺序)。"""
        order = ("train", "val", "test")
        return tuple(s for s in order if s in self.images_per_split)

    @property
    def total_images(self) -> int:
        return sum(len(imgs) for imgs in self.images_per_split.values())


# ============================================================
# 内部辅助 — IO / 解析 (失败不抛)
# ============================================================

def _load_yaml(yaml_path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """加载 yaml, 返回 (data, error_str)。失败时返回 ({}, err)。"""
    if not yaml_path.exists():
        return {}, f"yaml 文件不存在: {yaml_path}"
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}, f"yaml 顶层不是 dict: {type(data).__name__}"
        return data, None
    except yaml.YAMLError as e:
        return {}, f"yaml 解析失败: {e}"
    except OSError as e:
        return {}, f"yaml 读取失败: {e}"


def _resolve_data_root(yaml_path: Path, yaml_data: Dict[str, Any]) -> Path:
    """三种合法写法:
       1. yaml.path 是绝对路径 → 用它
       2. yaml.path 是相对路径 → 相对 yaml 文件所在目录
       3. yaml.path 缺失 → 用 yaml 文件所在目录
    """
    path_str = yaml_data.get("path")
    if not path_str:
        return yaml_path.parent.resolve()
    p = Path(path_str)
    return p.resolve() if p.is_absolute() else (yaml_path.parent / p).resolve()


def _resolve_split_dir(data_root: Path, split_field: Any) -> Optional[Path]:
    """yaml 里的 train/val/test 字段, 只支持字符串形式 (ultralytics 主流写法)。
    其他形式 (list / None) 返回 None, 让 yaml_schema 后续负责报错。
    """
    if not isinstance(split_field, str) or not split_field.strip():
        return None
    p = Path(split_field)
    return p.resolve() if p.is_absolute() else (data_root / p).resolve()


def _list_images(split_dir: Path) -> List[Path]:
    """返回 split_dir 下所有支持扩展名的图像 (绝对路径, 已排序)。"""
    if not split_dir.exists() or not split_dir.is_dir():
        return []
    images: List[Path] = []
    for ext in IMAGE_EXTENSIONS:
        images.extend(split_dir.glob(f"*{ext}"))
        images.extend(split_dir.glob(f"*{ext.upper()}"))   # Windows 大小写
    return sorted(set(images))


def _label_path_for_image(image_path: Path) -> Path:
    """YOLO 默认布局: images/<split>/foo.jpg → labels/<split>/foo.txt

    倒着找最后一个 'images' 替换为 'labels' (路径里可能多次出现 'images',
    取最后一个最稳妥)。
    """
    parts = list(image_path.parts)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "images":
            parts[i] = "labels"
            break
    return Path(*parts[:-1]) / (image_path.stem + ".txt")


def _normalize_names(names_raw: Any) -> Tuple[str, ...]:
    """把 yaml.names 归一化成"按 ID 升序的 tuple"。

    输入合法形式:
        - list[str]:      ['a','b','c']        → ('a','b','c')
        - dict[int,str]:  {0:'a',1:'b',2:'c'}  → ('a','b','c')

    非法或缺失返回空 tuple — 由 yaml_schema 负责报错。
    """
    if isinstance(names_raw, list):
        if all(isinstance(n, str) for n in names_raw):
            return tuple(names_raw)
        return ()
    if isinstance(names_raw, dict):
        if all(isinstance(k, int) for k in names_raw.keys()) \
                and all(isinstance(v, str) for v in names_raw.values()):
            return tuple(v for _, v in sorted(names_raw.items()))
        return ()
    return ()


def _build_split_stats(labels: List[Path]) -> SplitStats:
    """顺手算 3 个数字 — 在 build_snapshot 已经在循环图像了, 多读一次 label 也免不了"""
    image_count     = len(labels)
    annotated_count = 0
    total_instances = 0

    for lbl in labels:
        if not lbl.exists():
            continue
        try:
            content = lbl.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = [l for l in content.splitlines() if l.strip()]
        if not lines:
            continue   # 空 label 不算 annotated
        annotated_count += 1
        total_instances += len(lines)

    return SplitStats(
        image_count=image_count,
        annotated_count=annotated_count,
        total_instances=total_instances,
    )


# ============================================================
# 公开 API: build_snapshot
# ============================================================

@time_it(name="build_snapshot")
def build_snapshot(
    yaml_path: Path,
    task_type: Optional[str] = None,
) -> DatasetSnapshot:
    """构造数据集快照, 一次扫描提供后续 check 全部所需素材。

    错误处理原则: best-effort —— yaml 解析失败 / split 目录不存在都只装进
    snapshot 的 yaml_load_error / scan_warnings, 不抛异常。由对应 check
    负责把这些信号转成 CheckResult。

    Args:
        yaml_path: 数据集 yaml 路径
        task_type: 'detect' / 'segment'。优先级:
                   显式参数 > yaml.task 字段 > Task.DETECT 兜底

    Returns:
        DatasetSnapshot (永远不抛异常)
    """
    yaml_path = yaml_path.resolve()
    warnings: List[str] = []

    yaml_data, yaml_err = _load_yaml(yaml_path)
    if yaml_err:
        warnings.append(yaml_err)

    data_root = _resolve_data_root(yaml_path, yaml_data)

    nc          = yaml_data.get("nc") if isinstance(yaml_data.get("nc"), int) else None
    class_names = _normalize_names(yaml_data.get("names"))

    # task_type 优先级: 显式 > yaml.task > 兜底 detect
    resolved_task = task_type or yaml_data.get("task") or Task.DETECT
    if resolved_task not in (Task.DETECT, Task.SEGMENT):
        warnings.append(f"未知 task_type '{resolved_task}', 回退到 '{Task.DETECT}'")
        resolved_task = Task.DETECT

    images_per_split: Dict[str, Tuple[Path, ...]] = {}
    labels_per_split: Dict[str, Tuple[Path, ...]] = {}
    stats_per_split:  Dict[str, SplitStats]       = {}

    for split in ("train", "val", "test"):
        split_dir = _resolve_split_dir(data_root, yaml_data.get(split))
        if split_dir is None or not split_dir.exists():
            if split in yaml_data:
                warnings.append(f"split '{split}' 目录不可用: {split_dir}")
            continue

        images = _list_images(split_dir)
        if not images:
            warnings.append(f"split '{split}' 目录下无图像: {split_dir}")
            continue

        labels = [_label_path_for_image(img) for img in images]

        images_per_split[split] = tuple(images)
        labels_per_split[split] = tuple(labels)
        stats_per_split[split]  = _build_split_stats(labels)

    snapshot = DatasetSnapshot(
        yaml_path=yaml_path,
        yaml_data=yaml_data,
        yaml_load_error=yaml_err,
        data_root=data_root,
        nc=nc,
        class_names=class_names,
        task_type=resolved_task,
        images_per_split=images_per_split,
        labels_per_split=labels_per_split,
        stats_per_split=stats_per_split,
        scan_warnings=tuple(warnings),
    )
    logger.info(
        f"snapshot 构建完成: {snapshot.total_images} 张图像, "
        f"splits={list(snapshot.splits)}, task={resolved_task}"
    )
    return snapshot
