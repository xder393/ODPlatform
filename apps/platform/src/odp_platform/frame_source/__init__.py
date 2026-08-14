#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : __init__.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : frame_source 子系统 — 图片/视频/文件夹/摄像头 → 逐帧 Frame
"""帧源子系统.

接口约定 (供 inference.pipeline 消费):

    with create_frame_source(source, camera_config=cfg) as src:
        src.get_source_type()          # → SourceType
        for frame in src:              # → Frame(image=BGR ndarray, info=FrameInfo)

source 可以是:
    - 图片路径 (jpg/png/...)      → SourceType.IMAGE
    - 视频文件路径 (mp4/avi/...)  → SourceType.VIDEO
    - 文件夹 (里面放图片)         → SourceType.DIRECTORY
    - 摄像头号 (int 或 "0")       → SourceType.CAMERA
    - rtsp/rtmp/http 流           → SourceType.VIDEO
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")


class SourceType:
    """帧源类型."""
    IMAGE = "image"
    VIDEO = "video"
    CAMERA = "camera"
    DIRECTORY = "directory"


@dataclass
class CameraConfig:
    """摄像头配置 (infer_pipeline.yaml 的 camera 块)."""
    index: int = 0
    width: int = 1280
    height: int = 720
    fps: float = 30.0


@dataclass
class FrameInfo:
    """单帧元信息."""
    fps: float | None = None
    filename: str | None = None
    frame_index: int = 0


@dataclass
class Frame:
    """单帧: BGR 图像 + 元信息."""
    image: np.ndarray
    info: FrameInfo


def create_frame_source(source, *, camera_config: CameraConfig | None = None):
    """按 source 类型分派帧源 (返回上下文管理器)."""
    if isinstance(source, int):
        return _CameraSource(source, camera_config)
    s = str(source)
    if s.isdigit():
        return _CameraSource(int(s), camera_config)
    if s.lower().startswith(("rtsp://", "rtmp://", "http://")):
        return _StreamSource(s, camera_config)
    p = Path(s)
    if p.is_dir():
        return _DirectorySource(p)
    if p.suffix.lower() in IMAGE_EXTENSIONS:
        return _ImageSource(p)
    return _StreamSource(s, camera_config)   # 视频文件


class _ImageSource:
    """单张图片 → 一帧."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get_source_type(self) -> str:
        return SourceType.IMAGE

    def __iter__(self):
        img = cv2.imread(str(self.path))
        if img is None:
            raise FileNotFoundError(f"读图失败: {self.path}")
        yield Frame(image=img, info=FrameInfo(filename=self.path.name, frame_index=0))


class _DirectorySource:
    """文件夹 → 逐张图片一帧."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get_source_type(self) -> str:
        return SourceType.DIRECTORY

    def __iter__(self):
        files = sorted(
            p for p in self.path.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
        if not files:
            raise FileNotFoundError(f"文件夹里没有图片: {self.path}")
        for i, f in enumerate(files):
            img = cv2.imread(str(f))
            if img is None:
                logger.warning(f"读图失败, 跳过: {f.name}")
                continue
            yield Frame(image=img, info=FrameInfo(filename=f.name, frame_index=i))


class _StreamSource:
    """视频文件 / 网络流 → 逐帧."""

    def __init__(self, source: str, camera_config: CameraConfig | None) -> None:
        self.source = source
        self.camera_config = camera_config
        self._cap: cv2.VideoCapture | None = None

    def __enter__(self):
        self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            raise FileNotFoundError(f"打不开视频源: {self.source}")
        return self

    def __exit__(self, *exc) -> bool:
        if self._cap is not None:
            self._cap.release()
        return False

    def get_source_type(self) -> str:
        return SourceType.VIDEO

    def __iter__(self):
        assert self._cap is not None
        fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        idx = 0
        while True:
            ok, frame = self._cap.read()
            if not ok:
                break
            yield Frame(image=frame, info=FrameInfo(fps=fps, frame_index=idx))
            idx += 1


class _CameraSource:
    """摄像头 → 逐帧 (live, 读到断流为止)."""

    def __init__(self, index: int, camera_config: CameraConfig | None) -> None:
        self.cfg = camera_config or CameraConfig(index=index)
        self._cap: cv2.VideoCapture | None = None

    def __enter__(self):
        self._cap = cv2.VideoCapture(self.cfg.index)
        if self.cfg.width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        if self.cfg.height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        if not self._cap.isOpened():
            raise RuntimeError(f"打不开摄像头 #{self.cfg.index}")
        return self

    def __exit__(self, *exc) -> bool:
        if self._cap is not None:
            self._cap.release()
        return False

    def get_source_type(self) -> str:
        return SourceType.CAMERA

    def __iter__(self):
        assert self._cap is not None
        idx = 0
        while True:
            ok, frame = self._cap.read()
            if not ok:
                break
            yield Frame(image=frame, info=FrameInfo(fps=self.cfg.fps, frame_index=idx))
            idx += 1
