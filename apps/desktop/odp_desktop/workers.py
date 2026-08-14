# -*- coding:utf-8 -*-
"""后台任务运行器.

两类 worker:
  - Worker:            快任务(转换/质检/生成配置/导入), 进程内跑
  - SubprocessWorker:  重任务(训练/评估/推理), 起子进程跑, 可取消

SubprocessWorker 的可取消是"训练截停"的基础: 点停止 → SIGINT(等价 Ctrl+C,
ultralytics 会保存最后 checkpoint) → 超时 SIGTERM → SIGKILL.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .log_bridge import StreamBridge


class WorkerSignals(QObject):
    """in-process worker 结束回传结果/错误."""
    finished = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """进程内跑一个可调用对象 (快任务)."""

    def __init__(self, fn: Callable[..., Any], *args: Any,
                 log_signals: Any = None, **kwargs: Any) -> None:
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
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


class SubprocessSignals(QObject):
    """子进程 worker 信号."""
    line = Signal(str)        # 每行输出 (已去 ANSI)
    finished = Signal(int)    # 退出码
    error = Signal(str)       # 启动失败


class SubprocessWorker(QRunnable):
    """起子进程跑命令, 实时回传输出, 可取消."""

    def __init__(self, args: list[str], log_signals: Any = None) -> None:
        super().__init__()
        self.args = args
        self.log_signals = log_signals
        self.signals = SubprocessSignals()
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self.setAutoDelete(False)   # 需要存活到取消被调用

    @Slot()
    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{type(e).__name__}: {e}")
            return

        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = _strip_ansi(raw).rstrip()
            if line:
                self.signals.line.emit(line)
                if self.log_signals is not None:
                    self.log_signals.message.emit(line)
        self._proc.wait()
        self.signals.finished.emit(self._proc.returncode)

    def cancel(self) -> None:
        """优雅停止: SIGINT → SIGTERM → SIGKILL."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.send_signal(signal.SIGINT)     # 等价 Ctrl+C
        try:
            self._proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        self._proc.terminate()                     # SIGTERM
        try:
            self._proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        self._proc.kill()                          # SIGKILL


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
