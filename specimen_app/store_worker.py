"""后台 Store 写操作队列。

所有 ExcelStore 写操作（create_specimen / undo_last / redo_last / 字段保存 等）
通过本模块的 StoreWorkerThread 在独立线程顺序执行，主线程只做 UI 更新，
避免 openpyxl I/O 阻塞 Qt 事件循环。

使用方式：
    worker = StoreWorkerThread(parent=self)
    worker.operation_done.connect(self._on_store_done)
    worker.operation_error.connect(self._on_store_error)
    worker.start()
    worker.enqueue("create_specimen", store.create_specimen, initial_fields=initial)

P0-1（2026-10-02，字段保存后台化）新增：
  * ``wait_idle(timeout_ms)`` —— 等队列清空且当前操作完成（关窗 / 切工作区前排空，保证不丢数据）；
  * ``is_idle()`` / ``pending_count()`` —— 主线程查询；
  * ``accepting()`` —— ``request_stop()`` 之后拒收新任务（调用方回退到同步写，而不是把任务
    排在 sentinel 后面永远不执行）；
  * 运行循环改为"只认 sentinel"：旧 ``while self._running`` 在 ``request_stop()`` 之后处理完当前
    一个操作就退出，sentinel 之前已排队的操作全部丢弃 —— 对字段保存就是丢数据。
"""
from __future__ import annotations

import queue
import threading
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
        self._running = True  # 兼容字段：旧代码/测试可能读它；运行循环不再靠它退出
        self._accepting = True
        self._state_cv = threading.Condition()
        self._pending = 0  # 已入队但尚未完成（含正在执行）的操作数

    # ------------------------------------------------------------------
    # Public API (main thread)
    # ------------------------------------------------------------------
    def enqueue(
        self,
        op_id: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """把操作放入队列。结果通过 operation_done / operation_error 信号异步返回。

        返回 False = 线程已 ``request_stop()``，任务**未**入队（调用方应改走同步路径）。
        旧版无返回值且会把任务排到 sentinel 之后静默丢弃。
        """
        with self._state_cv:
            if not self._accepting:
                return False
            self._pending += 1
        self._queue.put((op_id, fn, args, kwargs))
        return True

    def accepting(self) -> bool:
        with self._state_cv:
            return self._accepting

    def pending_count(self) -> int:
        with self._state_cv:
            return self._pending

    def is_idle(self) -> bool:
        return self.pending_count() == 0

    def wait_idle(self, timeout_ms: int | None = None) -> bool:
        """阻塞直到队列清空且当前操作完成。返回 False = 超时时仍有未完成操作。

        只能从非工作线程调用（工作线程内调用会死锁）。
        """
        deadline = None if timeout_ms is None else time.monotonic() + max(0, timeout_ms) / 1000.0
        with self._state_cv:
            while self._pending > 0:
                if deadline is None:
                    self._state_cv.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_cv.wait(remaining)
            return True

    def request_stop(self) -> None:
        """请求停止工作线程（非阻塞）。已排队的操作会先执行完，再退出。调用方应随后 wait()。"""
        with self._state_cv:
            self._accepting = False
            self._running = False
        self._queue.put(None)  # sentinel —— 排在所有已入队操作之后

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------
    def run(self) -> None:
        # 旧：while self._running: ... —— request_stop() 后处理完当前一个就 break，
        #     sentinel 之前排队的操作全部丢弃。新：只认 sentinel，队列必然排空。
        while True:
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
                with self._state_cv:
                    self._pending = max(0, self._pending - 1)
                    self._state_cv.notify_all()
                if self._queue.empty():
                    self.busy_changed.emit(False)
