"""split_pairs——把图像-标签对划分成 train/val/test。

设计选择:
    - 纯函数 (不接触硬盘), 输入输出全是内存对象
    - 边界处理集中在这里, 不漏到调用方
"""
from __future__ import annotations

import logging

from sklearn.model_selection import train_test_split

from odp_platform.common.constants import (
    DEFAULT_RANDOM_STATE, RATE_EPSILON,
)
from odp_platform.data_pipeline.split.manifest import (
    PairList, SplitManifest,
)

logger = logging.getLogger(__name__)


def split_pairs(
    pairs: PairList,
    train_rate: float = 0.8,
    val_rate: float = 0.1,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> SplitManifest:
    """把 pairs 划成 train/val/test, 返回 SplitManifest。

    Args:
        pairs:        [(image_path, label_path), ...]
        train_rate:   训练集占比 [0, 1]
        val_rate:     验证集占比 [0, 1]
                      (test_rate 自动 = 1 - train_rate - val_rate)
        random_state: 随机种子
    """
    test_rate = 1.0 - train_rate - val_rate
    if not (0 <= train_rate <= 1 and 0 <= val_rate <= 1 and 0 <= test_rate <= 1):
        raise ValueError(
            f"比例越界: train={train_rate}, val={val_rate}, test={test_rate:.4f}"
        )

    manifest = SplitManifest(
        train_rate=train_rate,
        val_rate=val_rate,
        test_rate=test_rate,
        random_state=random_state,
    )

    n = len(pairs)
    if n == 0:
        return manifest

    # ---------- 边界 1: 样本数 < 3, 全归 train ----------
    if n < 3:
        logger.warning(f"split_pairs: 样本数 {n} < 3, 全归 train")
        manifest.train = list(pairs)
        return manifest

    # ---------- 边界 2: train_rate 实质 = 1, 全归 train ----------
    if train_rate >= 1.0 - RATE_EPSILON:
        manifest.train = list(pairs)
        return manifest

    # ---------- 第一次切: train vs temp ----------
    images = [p[0] for p in pairs]
    labels = [p[1] for p in pairs]

    train_i, temp_i, train_l, temp_l = train_test_split(
        images, labels,
        train_size=train_rate,
        random_state=random_state,
    )
    manifest.train = list(zip(train_i, train_l))

    if not temp_i:
        return manifest

    # ---------- 边界 3: val+test 实质 = 0 ----------
    remaining = val_rate + test_rate
    if remaining < RATE_EPSILON or len(temp_i) < 2:
        manifest.val = list(zip(temp_i, temp_l))
        return manifest

    # ---------- 边界 4: test_rate 实质 = 0, temp 全归 val ----------
    # 必须在算 val_size 之前判, 否则:
    #   val_size = val_rate / (val_rate + ε) ≈ 0.9999...8 < 1.0
    # 会让 sklearn 把 1 个样本错挤进 test
    if test_rate < RATE_EPSILON:
        manifest.val = list(zip(temp_i, temp_l))
        return manifest

    # ---------- 边界 5: val_rate 实质 = 0, temp 全归 test (对称) ----------
    if val_rate < RATE_EPSILON:
        manifest.test = list(zip(temp_i, temp_l))
        return manifest

    # ---------- 第二次切: temp -> val vs test ----------
    val_size = val_rate / remaining
    val_i, test_i, val_l, test_l = train_test_split(
        temp_i, temp_l,
        train_size=val_size,
        random_state=random_state,
    )
    manifest.val  = list(zip(val_i, val_l))
    manifest.test = list(zip(test_i, test_l))

    return manifest
