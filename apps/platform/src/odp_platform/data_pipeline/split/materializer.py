"""把 SplitManifest 落地到三个目标目录。

设计选择:
    - 用 DI 接收目标目录, 不 import paths.py——便于测试和复用
    - 复制 (而非链接), 因为目标目录可能跟 raw 在不同盘
      (例: raw 在本地 SSD, train 数据放在网络盘)
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from odp_platform.data_pipeline.split.manifest import (
    PairList, SplitManifest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SplitOutputDirs:
    """三组目标目录——materializer 落盘的去处。

    使用方在调用前从 paths.py 取常量构造这个对象, materializer
    本身不知道 paths.py 的存在 (DI)。
    """
    train_images: Path
    train_labels: Path
    val_images:   Path
    val_labels:   Path
    test_images:  Path
    test_labels:  Path

    def mkdir_all(self) -> None:
        for p in (
            self.train_images, self.train_labels,
            self.val_images,   self.val_labels,
            self.test_images,  self.test_labels,
        ):
            p.mkdir(parents=True, exist_ok=True)


def materialize(
    manifest: SplitManifest,
    output_dirs: SplitOutputDirs,
) -> dict:
    """把 manifest 的三组样本复制到 output_dirs。

    Returns:
        {split: 实际复制成功的样本数} 的字典
    """
    output_dirs.mkdir_all()

    counts = {}
    counts["train"] = _copy_pairs(
        manifest.train, output_dirs.train_images, output_dirs.train_labels
    )
    counts["val"] = _copy_pairs(
        manifest.val, output_dirs.val_images, output_dirs.val_labels
    )
    counts["test"] = _copy_pairs(
        manifest.test, output_dirs.test_images, output_dirs.test_labels
    )

    logger.info(
        f"materialize 完成: train={counts['train']}, "
        f"val={counts['val']}, test={counts['test']}"
    )
    return counts


def _copy_pairs(
    pairs: PairList,
    images_dst: Path,
    labels_dst: Path,
) -> int:
    """复制一组样本到目标目录。返回成功复制的对数。"""
    n_ok = 0
    for img_src, lbl_src in pairs:
        try:
            shutil.copy2(img_src, images_dst / img_src.name)
            shutil.copy2(lbl_src, labels_dst / lbl_src.name)
            n_ok += 1
        except OSError as e:
            logger.warning(f"复制失败 {img_src.name}: {e}")
    return n_ok
