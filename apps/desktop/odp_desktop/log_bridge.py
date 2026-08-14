# -*- coding:utf-8 -*-
"""把日志流接到 Qt 信号.

两条通道:
  1. logging → Qt handler: 捕获 odp_platform + ultralytics 两个 logger 的
     行式日志 (ultralytics 训练时的 header / 每轮结构 / 最终结果走这条)
  2. sys.stdout / sys.stderr → StreamBridge: 捕获 tqdm 进度条和 print
     (训练时逐 batch 的 loss 进度条走这条, 它们写 stderr 不进 logging)
"""
from __future__ import annotations

import logging
import re

from PySide6.QtCore import QObject, Signal


class LogSignals(QObject):
    """跨线程把日志文本送到主线程的信号载体."""
    message = Signal(str)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class QtLogHandler(logging.Handler):
    """logging.Handler → Qt 信号."""

    def __init__(self, signals: LogSignals) -> None:
        super().__init__()
        self.signals = signals
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            "%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.signals.message.emit(self.format(record))
        except Exception:  # pragma: no cover
            pass


class StreamBridge:
    """把 write() 调用按行转成 Qt 信号, 用于捕获 print / tqdm 输出.

    \r 和 \n 都当行结束符处理 — tqdm 用 \r 原地刷新, 拆成一行行快照
    显示在日志面板里 (每行是一条进度快照).
    """

    def __init__(self, signals: LogSignals) -> None:
        self._signals = signals
        self._buf = ""

    def write(self, s: str) -> None:
        self._buf += s
        while True:
            nl = self._buf.find("\n")
            cr = self._buf.find("\r")
            if nl < 0 and cr < 0:
                return
            if nl < 0:
                idx = cr
            elif cr < 0:
                idx = nl
            else:
                idx = min(nl, cr)
            line = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            line = _strip_ansi(line).rstrip()
            if line:
                self._signals.message.emit(line)

    def flush(self) -> None:
        if self._buf.strip():
            self._signals.message.emit(_strip_ansi(self._buf).rstrip())
            self._buf = ""


def install_log_bridge() -> LogSignals:
    """在 'odp_platform' 和 'ultralytics' 两个 logger 上装 Qt handler.

    返回信号载体 (单例, 由 MainWindow 持有).
    """
    signals = LogSignals()
    handler = QtLogHandler(signals)
    handler.setLevel(logging.DEBUG)

    # 平台自己的业务日志
    root = logging.getLogger("odp_platform")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    root.addHandler(handler)

    # ultralytics 训练/推理日志 (header / epoch / 最终结果)
    ul = logging.getLogger("ultralytics")
    ul.setLevel(logging.INFO)
    ul.addHandler(handler)

    return signals
