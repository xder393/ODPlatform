"""SplitManifest——划分结果的数据载体。

设计选择:
    - 用 dataclass, 不用 dict——字段有类型, IDE 能补全
    - 同时携带"三组样本"和"划分元数据"(rates, random_state),
      让 yaml_writer 不需要从别处拼凑这些信息
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

# 类型别名: 一对样本 = (image_path, label_path)
Pair = Tuple[Path, Path]
PairList = List[Pair]


@dataclass
class SplitManifest:
    """划分结果 + 可复现元数据。

    Attributes:
        train / val / test: 三组样本列表
        train_rate / val_rate / test_rate: 实际使用的比例 (保留浮点原值,
            用于写入 yaml 的 odp_meta)
        random_state: 实际使用的随机种子
    """
    train: PairList = field(default_factory=list)
    val:   PairList = field(default_factory=list)
    test:  PairList = field(default_factory=list)

    train_rate:   float = 0.0
    val_rate:     float = 0.0
    test_rate:    float = 0.0
    random_state: int   = 0

    def summary(self) -> dict:
        return {
            "train": len(self.train),
            "val":   len(self.val),
            "test":  len(self.test),
            "total": len(self.train) + len(self.val) + len(self.test),
        }
