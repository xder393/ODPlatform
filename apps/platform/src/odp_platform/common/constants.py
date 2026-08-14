# apps/platform/src/odp_platform/common/constants.py
"""项目级共享常量——所有模块的'共同词汇表'。

放在这里的标准:
    - 多模块共享 (>= 2 个模块用到)
    - 与具体业务无关 (纯定义, 不含逻辑)
    - 修改频率极低
"""
from typing import Tuple


# ============================================================
# 图像扩展名 (converter / splitter / validator 等模块共享)
# ============================================================
IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",
)


# ============================================================
# 标注格式名
# ============================================================
class AnnotationFormat:
    """支持的标注格式 (字符串常量, 不用 Enum 是为了 @register 装饰器能直接吃)"""
    PASCAL_VOC = "pascal_voc"
    COCO       = "coco"
    YOLO       = "yolo"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.PASCAL_VOC, cls.COCO, cls.YOLO)


# ============================================================
# 任务类型
# ============================================================
class Task:
    """模型任务类型"""
    DETECT  = "detect"
    SEGMENT = "segment"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.DETECT, cls.SEGMENT)


# ============================================================
# 浮点 / 划分相关
# ============================================================
DEFAULT_RANDOM_STATE: int = 42
"""默认随机种子。所有需要可复现的随机操作的默认值。"""

RATE_EPSILON: float = 1e-6
"""划分比例的浮点容差。

用途: 判断 train_rate / val_rate / test_rate 是否"实质等于 0 或 1"。
原因: Python 浮点运算的精度问题——比如 `1.0 - 0.7 - 0.3` 不等于 0,
      而是 5.55e-17, 必须用容差兜底。
"""


# ============================================================
# 数据集覆盖率阈值 (阶段 9 使用)
# ============================================================
COVERAGE_HARD_THRESHOLD: float = 0.5
"""图像-标注覆盖率硬阈值: 低于此值直接 fail-fast。"""

COVERAGE_SOFT_THRESHOLD: float = 0.9
"""图像-标注覆盖率软阈值: 低于此值仅警告。"""
