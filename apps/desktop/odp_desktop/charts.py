# -*- coding:utf-8 -*-
"""结果图表 — matplotlib 画图 + 嵌进 Qt.

validate 结果 → 各 split 数据量分组柱状图
train/val 结果 → 评估指标柱状图
不支持的 kind → 不画 (图表页留空)
"""
from __future__ import annotations

from typing import Any

import matplotlib

from PySide6.QtWidgets import QVBoxLayout, QWidget


def _setup_chart_font() -> None:
    """给图表配一个含 CJK 的字体, 避免中文标题/标签显示成方块 (tofu).

    matplotlib 默认 DejaVu Sans 没有中文字形; 这里按"谁有 CJK 谁优先"列出
    跨平台候选, matplotlib 自动选第一个可用的.
    """
    matplotlib.rcParams["font.sans-serif"] = [
        "Arial Unicode MS",      # macOS / 装 Office 的 Windows
        "PingFang SC",           # macOS
        "Hiragino Sans GB",      # macOS
        "Microsoft YaHei",       # Windows
        "SimHei",                # Windows
        "Noto Sans CJK SC",      # Linux
        "WenQuanYi Zen Hei",     # Linux
        "DejaVu Sans",           # 兜底 (无中文, 仅拉丁)
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False


_setup_chart_font()


def figure_for_result(result: dict[str, Any]):
    """按 result kind 生成 matplotlib Figure; 不支持的 kind 返回 None."""
    kind = result.get("kind")
    if kind == "validate":
        return _validation_figure(result)
    if kind in ("train", "val"):
        return _metrics_figure(result)
    return None


class ChartWidget(QWidget):
    """嵌一张 matplotlib Figure 的容器."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._canvas = None

    def set_result(self, result: dict[str, Any]) -> None:
        self.set_figure(figure_for_result(result))

    def set_figure(self, fig) -> None:
        """直接嵌入一张 matplotlib Figure (fig=None 则清空)."""
        self._clear()
        if fig is None:
            return
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        self._canvas = FigureCanvasQTAgg(fig)
        self._layout.addWidget(self._canvas)

    def _clear(self) -> None:
        if self._canvas is not None:
            self._layout.removeWidget(self._canvas)
            self._canvas.deleteLater()
            self._canvas = None


# ====================================================================
# 各 kind 的 Figure
# ====================================================================

def _validation_figure(result: dict[str, Any]):
    """各 split 的图像/有标注/实例 数量分组柱状图."""
    from matplotlib.figure import Figure

    stats = result.get("stats_per_split", {})
    splits = list(stats.keys())
    if not splits:
        return None

    images = [stats[s]["image_count"] for s in splits]
    annotated = [stats[s]["annotated_count"] for s in splits]
    instances = [stats[s]["total_instances"] for s in splits]

    fig = Figure(figsize=(6.5, 4.2))
    ax = fig.add_subplot(111)
    x = range(len(splits))
    w = 0.26
    ax.bar([i - w for i in x], images, w, label="图像")
    ax.bar(x, annotated, w, label="有标注")
    ax.bar([i + w for i in x], instances, w, label="实例")
    ax.set_xticks(list(x))
    ax.set_xticklabels(splits)
    ax.set_title("各 split 数据量")
    ax.set_ylabel("数量")
    ax.legend()
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig


def _metrics_figure(result: dict[str, Any]):
    """precision/recall/mAP50/mAP50-95 柱状图 (train/val)."""
    from matplotlib.figure import Figure

    metrics = result.get("metrics") or {}
    display_map = [
        ("metrics/precision(B)", "Precision"),
        ("metrics/recall(B)", "Recall"),
        ("metrics/mAP50(B)", "mAP50"),
        ("metrics/mAP50-95(B)", "mAP50-95"),
    ]
    labels: list[str] = []
    values: list[float] = []
    for key, disp in display_map:
        if key in metrics:
            labels.append(disp)
            values.append(float(metrics[key]))

    # 兜底: 万一 key 格式跟预期不符, 取所有数字值 (排除 fitness)
    if not values:
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and k != "fitness":
                labels.append(k)
                values.append(float(v))
    if not values:
        return None

    fig = Figure(figsize=(6.5, 4.2))
    ax = fig.add_subplot(111)
    bars = ax.bar(labels, values, color="#4C72B0")
    ax.set_ylim(0, max(1.0, max(values) * 1.15))
    ax.set_title("评估指标")
    ax.set_ylabel("分数 (0~1)")
    for b, v in zip(bars, values):
        ax.text(
            b.get_x() + b.get_width() / 2, v + max(values) * 0.02,
            f"{v:.3f}", ha="center", va="bottom", fontsize=9,
        )
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig


def training_curve_figure(results_csv: str):
    """从 ultralytics results.csv 画训练/验证 loss 曲线."""
    import csv
    from pathlib import Path

    from matplotlib.figure import Figure

    p = Path(results_csv)
    if not p.exists():
        return None
    try:
        raw = list(csv.DictReader(p.read_text(encoding="utf-8").splitlines()))
    except Exception:
        return None
    if not raw:
        return None

    # ultralytics 的列名带前导空格, 统一 strip
    rows = [{k.strip(): v for k, v in r.items()} for r in raw]
    epochs = [i + 1 for i in range(len(rows))]
    train = [float(r["train/box_loss"]) for r in rows if r.get("train/box_loss")]
    val = [float(r["val/box_loss"]) for r in rows if r.get("val/box_loss")]

    fig = Figure(figsize=(5.6, 3.4))
    ax = fig.add_subplot(111)
    if train:
        ax.plot(epochs[:len(train)], train, label="train loss", color="#007AFF", lw=1.6)
    if val:
        ax.plot(epochs[:len(val)], val, label="val loss", color="#FF9500", lw=1.6)
    ax.set_xlabel("epoch")
    ax.set_ylabel("loss")
    ax.legend()
    ax.grid(alpha=0.2)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig
