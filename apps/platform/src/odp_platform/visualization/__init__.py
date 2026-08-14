#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : __init__.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : visualization 子系统 — 检测框 + 标签的美化绘制 (BGR)
"""美化可视化.

接口约定 (供 inference.service / pipeline 消费):

    style = DrawStyle.from_image_size(h, w, **overrides)
    viz = BeautifyVisualizer(labels=..., label_mapping=..., color_mapping=...)
    dets = BeautifyVisualizer.from_yolo_results(boxes, confidences, labels)
    annotated = viz.draw(image, dets, style=style, use_label_mapping=True)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2

logger = logging.getLogger(__name__)


@dataclass
class DrawStyle:
    """绘制样式. 用 from_image_size 按帧尺寸给合理默认值."""
    box_thickness: int = 2
    font_scale: float = 0.6
    font_thickness: int = 1
    conf_precision: int = 2

    @classmethod
    def from_image_size(cls, h: int, w: int, **overrides):
        base = max(h, w)
        kwargs = {
            "box_thickness": max(1, base // 320),
            "font_scale": max(0.4, base / 1400),
            "font_thickness": max(1, base // 500),
        }
        for k, v in overrides.items():
            if v is not None:
                kwargs[k] = v
        return cls(**kwargs)


@dataclass
class Detection:
    """单个检测结果 (供 draw 用)."""
    box: tuple[float, float, float, float]   # xyxy
    conf: float
    label: str


class BeautifyVisualizer:
    """把检测框画到 BGR 图上, 支持标签映射(如英文→中文)和按类上色."""

    def __init__(
        self,
        labels,
        label_mapping=None,
        color_mapping=None,
        default_color=(0, 255, 0),
        font_path=None,
    ) -> None:
        self.labels = list(labels)
        self.label_mapping = dict(label_mapping or {})
        self.color_mapping = dict(color_mapping or {})
        self.default_color = tuple(default_color)
        self.font_path = font_path

    @classmethod
    def from_yolo_results(cls, boxes, confidences, labels):
        """把 yolo 结果的三件套 (xyxy 框 / 置信度 / 类别名) 组装成 Detection 列表."""
        dets = []
        for box, conf, label in zip(boxes, confidences, labels):
            dets.append(Detection(
                box=tuple(float(v) for v in box),
                conf=float(conf),
                label=str(label),
            ))
        return dets

    def draw(self, image, dets, style: DrawStyle, use_label_mapping: bool = True):
        """在 BGR 图上画框 + 标签, 返回新图 (不改原图)."""
        out = image.copy()
        for det in dets:
            x1, y1, x2, y2 = (int(v) for v in det.box)
            label = det.label
            if use_label_mapping:
                label = self.label_mapping.get(det.label, det.label)
            color = self.color_mapping.get(det.label, self.default_color)

            cv2.rectangle(out, (x1, y1), (x2, y2), color, style.box_thickness)

            text = f"{label} {det.conf:.{style.conf_precision}f}"
            (tw, th), _ = cv2.getTextSize(
                text, cv2.FONT_HERSHEY_SIMPLEX, style.font_scale, style.font_thickness)
            cv2.rectangle(out, (x1, max(0, y1 - th - 4)), (x1 + tw, y1), color, -1)
            cv2.putText(out, text, (x1, max(0, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                        style.font_scale, (255, 255, 255), style.font_thickness, cv2.LINE_AA)
        return out
