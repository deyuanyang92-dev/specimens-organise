"""v0.10.40：工作区 xlsx 被 Windows 短暂占用（杀毒 / 同步盘 / Excel / 刚 replace 完）不再崩溃。

崩溃现场（crash_20261003_151423.log）：后台字段保存完成 → 主线程 _update_task_indicator
→ store.get_specimen → xlsx_read_rows → zipfile 打开 标本信息.xlsx → PermissionError: [Errno 13]
→ 未捕获 → 程序退出。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from specimen_app import table_backend
from specimen_app.table_backend import retry_on_file_lock, xlsx_read_rows


def _s(v):
    return "" if v is None else str(v)


class RetryOnFileLockTests(unittest.TestCase):
    def test_transient_permission_error_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "Permission denied")
            return "ok"

        with mock.patch.object(table_backend.time, "sleep"):
            self.assertEqual(retry_on_file_lock(flaky), "ok")
        self.assertEqual(calls["n"], 3)

    def test_persistent_lock_raises_after_budget(self):
        def always():
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(table_backend.time, "sleep") as sl:
            with self.assertRaises(PermissionError):
                retry_on_file_lock(always)
        self.assertGreaterEqual(sl.call_count, 3)

    def test_other_errors_not_retried(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise ValueError("x")

        with self.assertRaises(ValueError):
            retry_on_file_lock(bad)
        self.assertEqual(calls["n"], 1)


class XlsxReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        wb = Workbook()
        ws = wb.active
        ws.append(["入库编号*", "种名"])
        ws.append(["YZZ000001", "A"])
        self.path = self.tmp / "标本信息.xlsx"
        wb.save(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_rows_survives_transient_lock(self):
        _, real_load = table_backend._openpyxl()
        calls = {"n": 0}

        def flaky_load(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied", str(self.path))
            return real_load(*a, **kw)

        with mock.patch.object(table_backend, "_openpyxl", return_value=(None, flaky_load)), \
                mock.patch.object(table_backend.time, "sleep"):
            rows = xlsx_read_rows(self.path, _s, {})
        self.assertEqual(rows, [{"入库编号*": "YZZ000001", "种名": "A"}])


class StoreStaleCacheFallbackTests(unittest.TestCase):
    """读失败（重试耗尽）时：有旧缓存 → 用旧缓存不崩；无缓存 → 抛友好的 WorkspaceFileBusyError。"""

    def setUp(self):
        from specimen_app.excel_store import ExcelStore
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ExcelStore(self.tmp)
        self.v = self.store.create_specimen() if hasattr(self.store, "create_specimen") else None

    def tearDown(self):
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_cache_used_when_file_locked(self):
        from specimen_app.models import SPECIMEN_FILE
        before = self.store.read_rows("specimen")
        # 让缓存失效（mtime 变化），再让底层读一直被占用
        self.store._file_mtimes[SPECIMEN_FILE] = -2.0
        with mock.patch.object(self.store._backend, "read_rows",
                               side_effect=PermissionError(13, "Permission denied")):
            after = self.store.read_rows("specimen")
        self.assertEqual(after, before)

    def test_no_cache_raises_friendly_busy_error(self):
        from specimen_app.models import SPECIMEN_FILE, WorkspaceFileBusyError
        self.store._row_cache.pop(SPECIMEN_FILE, None)
        self.store._file_mtimes.pop(SPECIMEN_FILE, None)
        with mock.patch.object(self.store._backend, "read_rows",
                               side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(WorkspaceFileBusyError) as cm:
                self.store.read_rows("specimen")
        self.assertIsInstance(cm.exception, PermissionError)  # 旧 except PermissionError 仍能接住
        self.assertIn("占用", str(cm.exception))


class WindowRecoveryTests(unittest.TestCase):
    """真实 SpecimenWindow（offscreen）：崩溃点不再冒泡；未落盘的修改能在重开时恢复。"""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        from specimen_app import app_settings
        from specimen_app.excel_store import ExcelStore
        self.tmp = Path(tempfile.mkdtemp(prefix="lockrec_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        store = ExcelStore(self.ws)
        self.voucher = store.create_specimen()
        store.release_lock()
        store.close()
        self._patches = [
            mock.patch.object(app_settings, "settings_path", return_value=self.tmp / "settings.json"),
            mock.patch.object(app_settings, "app_config_dir", return_value=self.tmp / "cfg"),
        ]
        for pt in self._patches:
            pt.start()

    def tearDown(self):
        for pt in self._patches:
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spin(self, secs):
        import time
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self._app.processEvents()
            time.sleep(0.01)

    def _open(self):
        from specimen_app.ui import SpecimenWindow
        win = SpecimenWindow(self.ws)
        self._spin(0.2)
        win.select_voucher(self.voucher)
        self._spin(0.1)
        return win

    def test_store_op_done_swallows_permission_error(self):
        win = self._open()
        try:
            with mock.patch.object(win, "_on_field_save_done",
                                   side_effect=PermissionError(13, "Permission denied")):
                # 旧：异常冒出槽函数 → 进程退出
                win._on_store_op_done(f"save_fields:specimen:{self.voucher}",
                                      ("specimen", self.voucher, True), 1.0)
            self.assertIn("占用", win.statusBar().currentMessage())
        finally:
            win.close()
            self._app.processEvents()

    def test_unsaved_edit_is_journaled_and_recovered(self):
        from PyQt5.QtWidgets import QMessageBox
        from specimen_app.edit_journal import EditJournal
        win = self._open()
        try:
            win.auto_save_enabled = False  # 模拟"还在 500ms 防抖窗口里"：只登记不落盘
            win.specimen_widgets["备注"].setText("崩溃前没保存的字")
            self._spin(0.05)
            journal = EditJournal(self.tmp / "cfg" / "recovery", self.ws)
            self.assertEqual(journal.pending()[0][2].get("备注"), "崩溃前没保存的字")
            # "崩溃"：清掉内存里的待保存，不走 flush
            win._save_timers.clear()
            win._pending_save_fields.clear()
        finally:
            win.close()
            self._app.processEvents()

        win2 = self._open()
        try:
            def fake_exec(box):
                for b in box.buttons():
                    if b.text() == "恢复写入":
                        b.click()
                return 0
            with mock.patch.object(QMessageBox, "exec_", fake_exec):
                win2._offer_edit_recovery()
            self.assertEqual(str(win2.store.get_specimen(self.voucher).get("备注")), "崩溃前没保存的字")
            self.assertEqual(EditJournal(self.tmp / "cfg" / "recovery", self.ws).pending(), [])
        finally:
            win2.close()
            self._app.processEvents()

    def test_saved_edit_leaves_no_journal(self):
        from specimen_app.edit_journal import EditJournal
        win = self._open()
        try:
            win.specimen_widgets["备注"].setText("正常保存")
            win._flush_pending_saves()
            import time
            end = time.monotonic() + 20
            while time.monotonic() < end and not (win._store_worker.is_idle() and not win._queued_field_saves):
                self._app.processEvents()
                time.sleep(0.01)
            self._spin(0.1)
        finally:
            win.close()
            self._app.processEvents()
        self.assertEqual(EditJournal(self.tmp / "cfg" / "recovery", self.ws).pending(), [])


if __name__ == "__main__":
    unittest.main()
