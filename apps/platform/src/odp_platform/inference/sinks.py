#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : sinks.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : 推理结果输出适配器 (OutputSink 抽象 + LocalFileSink + NullSink)
"""推理结果输出适配器.

把"画好的 annotated 帧"送到不同目的地的抽象层:

  - OutputSink     : 抽象基类 (open/write/close 三个方法)
  - LocalFileSink  : 本地 mp4 / jpg (★ CLI 默认行为, 等价于老式 cv2.imwrite/VideoWriter)
  - NullSink       : 不写任何东西 (web 流推 / 只跑统计 / 不存盘 时用)

业务端在自己仓库继承 OutputSink 实现自定义 sink:
  - S3Sink         : 写对象存储
  - WebSocketSink  : 实时推流
  - QtSignalSink   : 桥接到 Qt 信号 (业务端自行实现)

实现纪律 (与 D8 InferService 的"永不抛"一脉相承):
  - write() 内部 try/except 包住, 单帧写入失败 logger.warning + 跳过, 不抛
  - close() 幂等 (允许多次调用)
  - open() 由 pipeline 在首批帧到达后调用 (此时 source_type 已知)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

# frame_source 是 D8 的前置子系统; 未落地时本模块 import 不崩, 推理阶段再报错
try:
    from odp_platform.frame_source import SourceType
except ImportError:  # pragma: no cover - 只在 frame_source 未落地的环境触发
    SourceType = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ============================================================================
# 抽象基类
# ============================================================================
class OutputSink(ABC):
    """推理结果输出适配器.

    生命周期: open() → write() × N → close().

    实现要求:
      - write 永不抛 (出错 logger.warning + 跳过这一帧)
      - close 幂等 (多次调不出错)
      - 线程亲缘: write 在 _Renderer 线程被调, open/close 在主线程
    """

    @abstractmethod
    def open(self, output_dir: Path, source_type: SourceType) -> None:
        """初始化. 由 ThreadedPipeline 在首批帧到达后调用.

        Args:
            output_dir:  推理输出根目录 (部分 sink 可能不用)
            source_type: 帧源类型 (决定按视频流还是单图处理)
        """

    @abstractmethod
    def write(self, frame, annotated: np.ndarray) -> None:
        """写一帧画好的图.

        Args:
            frame:     frame_source 给出的 Frame 对象 (含 image / info 元数据)
            annotated: 已画过框的 BGR 图像
        """

    @abstractmethod
    def close(self) -> None:
        """收尾. 由 ThreadedPipeline 在退出时调用. 必须幂等."""


# ============================================================================
# 内置实现 1: LocalFileSink (CLI 默认, 视频写 mp4 / 图片写 jpg)
# ============================================================================
class LocalFileSink(OutputSink):
    """本地文件 sink — 视频源写 mp4, 图片源写 jpg.

    实现细节:
      - 视频源 (VIDEO / CAMERA): lazy 打开 cv2.VideoWriter, 首帧拿到尺寸再建
      - 图片源 (IMAGE / IMAGES): 每帧 cv2.imwrite 一张 jpg
      - fps 从 frame.info.fps 拿; 摄像头/缺失时兜底 30
    """

    def __init__(self) -> None:
        self.output_dir: Path | None = None
        self._is_stream: bool = False
        self._video = None      # cv2.VideoWriter, lazy
        self._count: int = 0

    def open(self, output_dir: Path, source_type: SourceType) -> None:
        self.output_dir = output_dir
        self._is_stream = source_type in (SourceType.VIDEO, SourceType.CAMERA)

    def write(self, frame, annotated) -> None:
        import cv2
        try:
            if self._is_stream:
                if self._video is None:
                    h, w = annotated.shape[:2]
                    fps = float(frame.info.fps) if getattr(frame.info, "fps", None) else 30.0
                    out = self.output_dir / "output.mp4"
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    self._video = cv2.VideoWriter(str(out), fourcc, fps, (w, h))
                self._video.write(annotated)
            else:
                fname = frame.info.filename or f"frame_{frame.info.frame_index:06d}"
                cv2.imwrite(str(self.output_dir / f"{Path(fname).stem}.jpg"), annotated)
            self._count += 1
        except Exception as e:
            logger.warning(f"LocalFileSink.write 失败, 跳过: {e}")

    def close(self) -> None:
        if self._video is not None:
            try:
                self._video.release()
            except Exception as e:
                logger.warning(f"LocalFileSink.close release 失败 (已吞): {e}")
            finally:
                self._video = None


# ============================================================================
# 内置实现 2: NullSink (不写任何东西)
# ============================================================================
class NullSink(OutputSink):
    """什么也不写.

    用途:
      - web 流推: annotated 已通过 WebSocketSink 推走, 后端无需落盘
      - 业务端 UI: annotated 已通过自定义 sink (如 QtSignalSink) 给 UI, 不需要本地文件
      - 性能基准测试: 排除 IO 干扰
      - --no-save 模式下的 CLI
    """

    def open(self, output_dir: Path, source_type: SourceType) -> None:
        pass

    def write(self, frame, annotated) -> None:
        pass

    def close(self) -> None:
        pass
