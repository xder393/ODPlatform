# -*- coding:utf-8 -*-
"""把 odp_platform 的 logging 流接到 Qt 信号.

业务模块(D3/D4/D5/D6/D7/D8)都是 `logger = logging.getLogger(__name__)` 发声,
通过冒泡到 'odp_platform' 根 logger. 桌面端在根 logger 上挂一个 Qt handler,
日志就实时流进 GUI 的日志面板, 不用改任何业务代码.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal


class LogSignals(QObject):
    """跨线程把日志文本送到主线程的信号载体."""
    message = Signal(str)


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
        except Exception:  # pragma: no cover - 信号 emit 失败不该崩日志
            pass


def install_log_bridge() -> LogSignals:
    """在 'odp_platform' 根 logger 上装 Qt handler, 返回信号载体.

    ★ 只装一次; 幂等 (重复调用返回新 handler, 由调用方保证单例).
    """
    signals = LogSignals()
    handler = QtLogHandler(signals)
    handler.setLevel(logging.DEBUG)

    root = logging.getLogger("odp_platform")
    root.setLevel(logging.DEBUG)   # 让 INFO/DEBUG 都能到达 handler
    root.propagate = False         # 阻断向 Python root 冒泡, 避免重复
    root.addHandler(handler)
    return signals
