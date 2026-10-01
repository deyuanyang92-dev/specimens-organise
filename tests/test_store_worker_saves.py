"""P0-1：字段保存后台化 —— StoreWorkerThread 排空语义 + 主窗口保存链路不丢数据。

用户要求（2026-10-02）：向下兼容、稳健、不丢数据。本文件锁死：
  * request_stop() 之后已排队的操作必须全部执行（旧实现会丢）；
  * enqueue() 在 stop 之后返回 False（调用方回退同步写）；
  * 主窗口字段编辑 → 后台写盘 → xlsx 落盘；关窗 / 切换编号 / 切换工作区前全部排空；
  * 同一编号同面板的多次编辑合并成一次 set_fields。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from specimen_app.store_worker import StoreWorkerThread  # noqa: E402


_APP: QApplication | None = None  # 必须持有引用，否则 PyQt 回收 QApplication → "Must construct a QApplication before a QWidget"


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication(["test"])
    return _APP


class StoreWorkerThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_queued_ops_all_run_before_stop(self) -> None:
        worker = StoreWorkerThread()
        worker.start()
        ran: list[int] = []

        def slow(i: int) -> int:
            time.sleep(0.05)
            ran.append(i)
            return i

        for i in range(5):
            self.assertTrue(worker.enqueue(f"op{i}", slow, i))
        worker.request_stop()  # 旧实现：处理完当前一个就退出，后面 4 个丢掉
        self.assertTrue(worker.wait(10000))
        self.assertEqual(ran, [0, 1, 2, 3, 4])

    def test_enqueue_after_stop_is_refused(self) -> None:
        worker = StoreWorkerThread()
        worker.start()
        worker.request_stop()
        self.assertFalse(worker.accepting())
        self.assertFalse(worker.enqueue("late", lambda: None))
        self.assertTrue(worker.wait(5000))

    def test_wait_idle_blocks_until_queue_drained(self) -> None:
        worker = StoreWorkerThread()
        worker.start()
        done = threading.Event()

        def slow() -> None:
            time.sleep(0.3)
            done.set()

        worker.enqueue("slow", slow)
        self.assertFalse(worker.is_idle())
        self.assertFalse(worker.wait_idle(50))  # 太短 → 超时 False
        self.assertTrue(worker.wait_idle(5000))
        self.assertTrue(done.is_set())
        self.assertTrue(worker.is_idle())
        worker.request_stop()
        worker.wait(5000)

    def test_error_in_op_does_not_break_queue(self) -> None:
        worker = StoreWorkerThread()
        worker.start()
        seen: list[str] = []
        worker.operation_error.connect(lambda op, msg: seen.append(f"err:{op}"))
        worker.operation_done.connect(lambda op, res, ms: seen.append(f"ok:{op}"))

        def boom() -> None:
            raise RuntimeError("x")

        worker.enqueue("a", boom)
        worker.enqueue("b", lambda: 1)
        self.assertTrue(worker.wait_idle(5000))
        _app().processEvents()
        worker.request_stop()
        worker.wait(5000)
        _app().processEvents()
        self.assertEqual(seen, ["err:a", "ok:b"])


class MainWindowBackgroundSaveTests(unittest.TestCase):
    """真实 SpecimenWindow（offscreen）上的保存链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        from specimen_app import app_settings
        from specimen_app.excel_store import ExcelStore

        self.tmp = Path(tempfile.mkdtemp(prefix="p0_save_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        store = ExcelStore(self.ws)
        self.voucher = store.create_specimen()
        store.release_lock()
        store.close()
        self._settings_patch = patch.object(app_settings, "settings_path", return_value=self.tmp / "settings.json")
        self._settings_patch.start()

    def tearDown(self) -> None:
        self._settings_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_window(self):
        from specimen_app.ui import SpecimenWindow

        win = SpecimenWindow(self.ws)
        self._spin(lambda: True, 0.2)
        win.select_voucher(self.voucher)
        self._spin(lambda: True, 0.1)
        return win

    @staticmethod
    def _spin(cond, timeout: float) -> bool:
        app = _app()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if cond():
                app.processEvents()
                return True
            time.sleep(0.01)
        return cond()

    def _wait_saves_done(self, win, timeout: float = 20.0) -> None:
        ok = self._spin(lambda: win._store_worker.is_idle() and not win._queued_field_saves, timeout)
        self.assertTrue(ok, "background saves did not finish")

    def _read_remark_from_disk(self) -> str:
        from specimen_app.excel_store import ExcelStore

        store = ExcelStore(self.ws, lock=False)
        try:
            return str((store.get_specimen(self.voucher) or {}).get("备注", ""))
        finally:
            store.close()

    def test_field_edit_is_saved_in_background_thread(self) -> None:
        win = self._open_window()
        try:
            worker_thread_ids: list[int] = []
            original = win.store.set_fields

            def spy(*args, **kwargs):
                worker_thread_ids.append(threading.get_ident())
                return original(*args, **kwargs)

            win.store.set_fields = spy  # type: ignore[method-assign]
            win.specimen_widgets["备注"].setText("后台保存-1")
            win._flush_pending_saves()
            self._wait_saves_done(win)
            self.assertEqual(worker_thread_ids and worker_thread_ids[0] != threading.get_ident(), True, "set_fields ran on GUI thread")
            self.assertEqual(str(win.store.get_specimen(self.voucher).get("备注")), "后台保存-1")
        finally:
            win.close()
            _app().processEvents()
        self.assertEqual(self._read_remark_from_disk(), "后台保存-1")

    def test_edits_merge_into_one_set_fields_while_worker_busy(self) -> None:
        win = self._open_window()
        try:
            calls: list[dict] = []
            original = win.store.set_fields

            def spy(category, voucher, updates, *a, **k):
                calls.append(dict(updates))
                return original(category, voucher, updates, *a, **k)

            win.store.set_fields = spy  # type: ignore[method-assign]
            gate = threading.Event()
            win._store_worker.enqueue("block", gate.wait, 5)  # 占住 worker
            win.specimen_widgets["备注"].setText("合并-A")
            win._flush_pending_saves()
            win.specimen_widgets["标本存放位置"].setText("柜1")
            win._flush_pending_saves()
            gate.set()
            self._wait_saves_done(win)
            self.assertEqual(len(calls), 1, calls)
            self.assertEqual(calls[0].get("备注"), "合并-A")
            self.assertEqual(calls[0].get("标本存放位置"), "柜1")
        finally:
            win.close()
            _app().processEvents()
        self.assertEqual(self._read_remark_from_disk(), "合并-A")

    def test_close_window_drains_pending_saves(self) -> None:
        win = self._open_window()
        win.specimen_widgets["备注"].setText("关窗前最后一笔")
        # 不等防抖、不等 worker：直接关窗。closeEvent 必须先 flush 再排空 worker。
        win.close()
        _app().processEvents()
        self.assertEqual(self._read_remark_from_disk(), "关窗前最后一笔")

    def test_switching_voucher_does_not_lose_pending_edit(self) -> None:
        from specimen_app.excel_store import ExcelStore

        store = ExcelStore(self.ws, lock=False)
        second = store.create_specimen()
        store.close()
        win = self._open_window()
        try:
            win.refresh_list()
            win.select_voucher(self.voucher)
            self._spin(lambda: True, 0.1)
            win.specimen_widgets["备注"].setText("切换前的编辑")
            win.select_voucher(second)  # 500ms 防抖还没到就切编号
            self._wait_saves_done(win)
            self.assertEqual(str(win.store.get_specimen(self.voucher).get("备注")), "切换前的编辑")
            self.assertEqual(str(win.store.get_specimen(second).get("备注", "")), "")
        finally:
            win.close()
            _app().processEvents()

    def test_worker_unavailable_falls_back_to_synchronous_save(self) -> None:
        win = self._open_window()
        try:
            win._store_worker.request_stop()
            win._store_worker.wait(5000)
            win.specimen_widgets["备注"].setText("同步兜底")
            win._flush_pending_saves()
            self.assertEqual(str(win.store.get_specimen(self.voucher).get("备注")), "同步兜底")
        finally:
            win.close()
            _app().processEvents()


if __name__ == "__main__":
    unittest.main()
