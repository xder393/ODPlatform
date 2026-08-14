# -*- coding:utf-8 -*-
"""后台任务运行器 — QRunnable + 信号.

训练 / 评估 / 推理都是长任务, 放在线程池里跑, UI 不冻结.
任务函数在 tasks.py 里定义, 这里只负责"后台跑 + 回传结果/错误".
运行期间会重定向 stdout/stderr, 把 tqdm 进度条也送进日志面板.
"""
from __future__ import annotations

import sys
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .log_bridge import StreamBridge


class WorkerSignals(QObject):
    """worker 结束时回传结果/错误."""
    finished = Signal(object)     # 成功结果 (任意对象)
    error = Signal(str)           # 异常消息


class Worker(QRunnable):
    """跑一个可调用对象, 结束发信号."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        log_signals: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.log_signals = log_signals
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        if self.log_signals is not None:
            bridge = StreamBridge(self.log_signals)
            sys.stdout = bridge
            sys.stderr = bridge
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as e:  # noqa: BLE001 - 任何任务异常都要回到 UI
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
