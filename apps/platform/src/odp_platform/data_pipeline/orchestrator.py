"""DatasetPipeline——端到端数据集准备编排器。

只串顺序, 不实现业务规则。所有规则都在它调的 6 个组件里。
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import List, Optional

from odp_platform.common.constants import (
    DEFAULT_RANDOM_STATE, IMAGE_EXTENSIONS, Task,
)
from odp_platform.common.paths import (
    TRAIN_IMAGES_DIR, TRAIN_LABELS_DIR,
    VAL_IMAGES_DIR,   VAL_LABELS_DIR,
    TEST_IMAGES_DIR,  TEST_LABELS_DIR,
    raw_dataset_root, dataset_yaml_path,
)
from odp_platform.data_pipeline.registry import (
    ConvertOptions, get_converter,
)
from odp_platform.data_pipeline.service import convert_data_to_yolo
from odp_platform.data_pipeline.split.manifest import PairList
from odp_platform.data_pipeline.split.materializer import (
    SplitOutputDirs, materialize,
)
from odp_platform.data_pipeline.split.splitter import split_pairs
from odp_platform.data_pipeline.split.yaml_writer import write_dataset_yaml

logger = logging.getLogger(__name__)


class DatasetPipeline:
    """端到端编排: raw -> yolo txt -> split -> 落盘 -> yaml。"""

    def __init__(
        self,
        dataset_name: str,
        annotation_format: str,
        *,
        task: str = Task.DETECT,
        train_rate: float = 0.8,
        val_rate: float = 0.1,
        classes: Optional[List[str]] = None,
        coco_cls91to80: bool = False,
        random_state: int = DEFAULT_RANDOM_STATE,
    ):
        self.dataset_name = dataset_name
        self.annotation_format = annotation_format
        self.task = task
        self.train_rate = train_rate
        self.val_rate = val_rate
        self.random_state = random_state

        # ⚠️ "用户传入的 classes" 是入参不可变状态;
        #    "最终的 classes (含 converter 自动发现的)" 是运行时确定的状态——
        #    分开存避免互相污染
        self._user_classes: Optional[List[str]] = classes
        self._final_classes: List[str] = []

        self._options = ConvertOptions(
            task=task,
            classes=classes,
            coco_cls91to80=coco_cls91to80,
        )

        # 路径全部从 paths.py 取, 不在这里拼字符串
        self.raw_root = raw_dataset_root(dataset_name)
        self.raw_images = self.raw_root / "images"
        self.raw_annotations = self.raw_root / "annotations"
        self.output_dirs = SplitOutputDirs(
            train_images=TRAIN_IMAGES_DIR,
            train_labels=TRAIN_LABELS_DIR,
            val_images=VAL_IMAGES_DIR,
            val_labels=VAL_LABELS_DIR,
            test_images=TEST_IMAGES_DIR,
            test_labels=TEST_LABELS_DIR,
        )
        self.yaml_out = dataset_yaml_path(dataset_name)

    def run(self) -> dict:
        """跑完端到端。返回 {train/val/test 计数 + yaml 路径}。"""
        logger.info(
            f"开始处理数据集 {self.dataset_name!r} "
            f"(format={self.annotation_format}, task={self.task})"
        )

        # 1. 校验 raw 目录 + 覆盖率前置
        self._check_raw()

        # 2. 校验 converter 支持当前 task
        entry = get_converter(self.annotation_format)
        if not entry.supports(self.task):
            raise ValueError(
                f"格式 {self.annotation_format!r} 不支持 task={self.task!r}。"
                f"支持: {entry.supported_tasks}"
            )

        # 3. 用 tempfile 中转: converter 写到临时目录, 不污染 data/raw/
        with tempfile.TemporaryDirectory(prefix="odp_pipe_") as tmp:
            staging = Path(tmp) / "labels"
            classes = convert_data_to_yolo(
                input_dir=self.raw_annotations,
                output_labels_dir=staging,
                annotation_format=self.annotation_format,
                options=self._options,
            )
            self._final_classes = classes
            logger.info(f"转换得到 {len(classes)} 个类别")

            # 4. 配对
            pairs = self._pair_images_with_labels(staging)
            logger.info(f"图像-标签配对: {len(pairs)} 对")

            # 5. 划分
            manifest = split_pairs(
                pairs,
                train_rate=self.train_rate,
                val_rate=self.val_rate,
                random_state=self.random_state,
            )
            logger.info(f"划分结果: {manifest.summary()}")

            # 6. 落盘
            counts = materialize(manifest, self.output_dirs)

            # 7. 写 yaml
            write_dataset_yaml(
                self.yaml_out,
                dataset_root=self.output_dirs.train_images.parent.parent,
                classes=classes,
                manifest=manifest,
                dataset_name=self.dataset_name,
                source_format=self.annotation_format,
                task=self.task,
            )

        return {"counts": counts, "yaml": str(self.yaml_out)}

    # ------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------
    def _check_raw(self) -> None:
        """目录检查 + 图像-标注覆盖率前置 (fail-fast)。"""
        from odp_platform.common.constants import (
            AnnotationFormat,
            COVERAGE_HARD_THRESHOLD, COVERAGE_SOFT_THRESHOLD,
            IMAGE_EXTENSIONS,
        )

        # ----- 基础目录检查 -----
        if not self.raw_root.is_dir():
            raise FileNotFoundError(f"数据集目录不存在: {self.raw_root}")
        if not self.raw_images.is_dir():
            raise FileNotFoundError(f"缺少 images 子目录: {self.raw_images}")
        if not self.raw_annotations.is_dir():
            raise FileNotFoundError(
                f"缺少 annotations 子目录: {self.raw_annotations}"
            )

        # ----- 覆盖率前置 -----
        # 数图像 (大小写不敏感, 见 _pair_images_with_labels 同款处理)
        image_exts_lower = {ext.lower() for ext in IMAGE_EXTENSIONS}
        n_images = 0
        image_stems = set()
        for img in self.raw_images.iterdir():
            if img.is_file() and img.suffix.lower() in image_exts_lower:
                n_images += 1
                image_stems.add(img.stem)

        if n_images == 0:
            raise FileNotFoundError(f"images/ 目录为空: {self.raw_images}")

        # 数标注 (按格式过滤扩展名, 但这里粗略统计 stem 即可)
        ann_ext_map = {
            AnnotationFormat.PASCAL_VOC: "*.xml",
            AnnotationFormat.COCO:       "*.json",
            AnnotationFormat.YOLO:       "*.txt",
        }
        pattern = ann_ext_map.get(self.annotation_format, "*")
        annotation_stems = {
            p.stem for p in self.raw_annotations.glob(pattern)
        }

        # ★ 对 coco, 一个 json 含整个数据集——覆盖率检查不适用,
        #   跳过即可 (coco 的覆盖率问题是另一种, 不在这里管)
        if self.annotation_format == AnnotationFormat.COCO:
            logger.debug("COCO 格式跳过 stem 覆盖率检查")
            return

        # 算覆盖率: 有标注的图像数 / 总图像数
        matched = len(image_stems & annotation_stems)
        coverage = matched / n_images

        logger.info(
            f"图像-标注覆盖率: {matched}/{n_images} = {coverage:.1%}"
        )

        if coverage < COVERAGE_HARD_THRESHOLD:
            raise ValueError(
                f"图像-标注覆盖率过低: {coverage:.1%} "
                f"(硬阈值 {COVERAGE_HARD_THRESHOLD:.0%})。\n"
                f"  总图像数: {n_images}\n"
                f"  有标注图像: {matched}\n"
                f"建议检查 annotations/ 目录是否完整, 或确认 --format 参数正确。"
            )
        elif coverage < COVERAGE_SOFT_THRESHOLD:
            logger.warning(
                f"⚠️  图像-标注覆盖率偏低: {coverage:.1%} "
                f"(软阈值 {COVERAGE_SOFT_THRESHOLD:.0%})。"
                f" 训练可继续, 但建议核对数据完整性。"
            )

    def _pair_images_with_labels(self, labels_dir: Path) -> PairList:
        """按 stem 配对 raw_images/ 下的图像 和 labels_dir 下的 yolo txt。"""
        # 建立 image stem -> Path 索引
        # ★ 大小写处理: pathlib.glob 在 Linux 是大小写敏感的, "*.jpg" 不会匹配
        #   IMG_001.JPG (相机默认大写)。改用 iterdir + suffix.lower() 一次列全部,
        #   再按 IMAGE_EXTENSIONS 过滤——跨平台一致, 且只扫一次目录。
        image_exts_lower = {ext.lower() for ext in IMAGE_EXTENSIONS}
        image_index = {}
        for img in self.raw_images.iterdir():
            if img.is_file() and img.suffix.lower() in image_exts_lower:
                image_index[img.stem] = img

        pairs: PairList = []
        for lbl in sorted(labels_dir.glob("*.txt")):
            img = image_index.get(lbl.stem)
            if img is None:
                logger.debug(f"标签 {lbl.name} 无对应图像, 跳过")
                continue
            pairs.append((img, lbl))

        return pairs
