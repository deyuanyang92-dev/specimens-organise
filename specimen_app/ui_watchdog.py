"""ui_watchdog.py — GUI 线程卡死探针。

用户多次反馈"一打开就未响应 / 非常容易卡死"，但每次都无法定位卡在哪。
本探针常驻：GUI 线程每 poll 秒打一次心跳（QTimer）；一条守护线程发现心跳停摆超过
threshold 秒，就把**主线程此刻的调用栈**（sys._current_frames）写进崩溃日志目录
``gui_stall_<时间>.log``，并打到 stderr。一次停摆只记一次；恢复后再卡再记。

开销：一个 QTimer + 一个 sleep 循环，可忽略。不依赖 ExcelStore。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QObject, QTimer

DEFAULT_THRESHOLD_SECONDS = 2.0
_MIN_SECONDS_BETWEEN_DUMPS = 30.0


def _default_sink(text: str) -> None:
    """写到 <app_config_dir>/gui_stall_<ts>.log；失败只打 stderr，绝不抛。"""
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass
    try:
        from .crash_log import _config_dir

        cfg = _config_dir()
        if cfg is None:
            return
        Path(cfg).mkdir(parents=True, exist_ok=True)
        path = Path(cfg) / f"gui_stall_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        path.write_text(text, encoding="utf-8")
    except Exception:
        pass


def format_main_thread_stack(main_thread_ident: int) -> str:
    frame = sys._current_frames().get(main_thread_ident)
    if frame is None:
        return "(主线程栈不可用)"
    return "".join(traceback.format_stack(frame))


class GuiStallWatchdog(QObject):
    def __init__(
        self,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        poll_seconds: float = 0.25,
        sink: Callable[[str], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._threshold = max(0.05, float(threshold_seconds))
        self._poll = max(0.01, float(poll_seconds))
        self._sink = sink or _default_sink
        self._last_tick = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: QTimer | None = None
        self._main_ident = threading.get_ident()
        self._reported_this_stall = False
        self._last_dump_at = 0.0
        self.dump_count = 0

    # -- GUI 线程 ----------------------------------------------------------
    def start(self) -> None:
        """必须在 GUI 线程调用。"""
        self._main_ident = threading.get_ident()
        self._last_tick = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(self._poll * 1000 / 2)))
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gui-stall-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            try:
                self._timer.stop()
            except RuntimeError:
                pass
            self._timer = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _tick(self) -> None:
        with self._lock:
            self._last_tick = time.monotonic()
            self._reported_this_stall = False

    # -- 守护线程 ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            with self._lock:
                age = time.monotonic() - self._last_tick
                already = self._reported_this_stall
                last_dump = self._last_dump_at
            if age < self._threshold or already:
                continue
            if time.monotonic() - last_dump < _MIN_SECONDS_BETWEEN_DUMPS:
                with self._lock:
                    self._reported_this_stall = True
                continue
            text = (
                f"========== GUI 线程停摆 {age:.1f}s（阈值 {self._threshold:.1f}s）==========\n"
                f"context: gui_stall\n"
                f"time: {datetime.now().isoformat(timespec='seconds')}\n"
                f"主线程此刻的调用栈（最后一行就是卡住的位置）：\n"
                f"{format_main_thread_stack(self._main_ident)}"
            )
            with self._lock:
                self._reported_this_stall = True
                self._last_dump_at = time.monotonic()
                self.dump_count += 1
            try:
                self._sink(text)
            except Exception:
                pass
