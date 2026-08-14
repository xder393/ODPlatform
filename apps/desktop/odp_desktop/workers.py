# -*- coding:utf-8 -*-
"""后台任务运行器 — QRunnable + 信号.

训练 / 评估 / 推理都是长任务, 放在线程池里跑, UI 不冻结.
任务函数在 tasks.py 里定义, 这里只负责"后台跑 + 回传结果/错误".
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    """worker 结束时回传结果/错误."""
    finished = Signal(object)     # 成功结果 (任意对象)
    error = Signal(str)           # 异常消息


class Worker(QRunnable):
    """跑一个可调用对象, 结束发信号."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as e:  # noqa: BLE001 - 任何任务异常都要回到 UI, 不能崩线程
            self.signals.error.emit(f"{type(e).__name__}: {e}")
