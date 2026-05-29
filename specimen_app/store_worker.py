"""后台 Store 写操作队列。

所有 ExcelStore 写操作（create_specimen / undo_last / redo_last 等）
通过本模块的 StoreWorkerThread 在独立线程顺序执行，主线程只做 UI 更新，
避免 openpyxl I/O 阻塞 Qt 事件循环。

使用方式：
    worker = StoreWorkerThread(parent=self)
    worker.operation_done.connect(self._on_store_done)
    worker.operation_error.connect(self._on_store_error)
    worker.start()
    worker.enqueue("create_specimen", store.create_specimen, initial_fields=initial)
"""
from __future__ import annotations

import queue
import time
from typing import Any, Callable

from PyQt5.QtCore import QThread, pyqtSignal


class StoreWorkerThread(QThread):
    """顺序执行 ExcelStore 写操作的后台线程。

    同一时刻只有一个操作在执行（保证 ExcelStore 写入顺序一致性）。
    主线程不得在 busy=True 期间直接调用 ExcelStore 写方法。
    """

    # (op_id, result, elapsed_ms)
    operation_done: pyqtSignal = pyqtSignal(str, object, float)
    # (op_id, error_message)
    operation_error: pyqtSignal = pyqtSignal(str, str)
    # True = 正在处理，False = 空闲
    busy_changed: pyqtSignal = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue()
        self._running = True

    # ------------------------------------------------------------------
    # Public API (main thread)
    # ------------------------------------------------------------------

    def enqueue(
        self,
        op_id: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """把操作放入队列。结果通过 operation_done / operation_error 信号异步返回。"""
        self._queue.put((op_id, fn, args, kwargs))

    def request_stop(self) -> None:
        """请求停止工作线程（非阻塞）。调用方应随后 wait()。"""
        self._running = False
        self._queue.put(None)  # sentinel

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            op_id, fn, args, kwargs = item
            self.busy_changed.emit(True)
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.operation_done.emit(op_id, result, elapsed_ms)
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.operation_error.emit(op_id, str(exc))
            finally:
                if self._queue.empty():
                    self.busy_changed.emit(False)
