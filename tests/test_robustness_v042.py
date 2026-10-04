"""v0.10.42 稳健性审计修复的回归测试（每条都对应一次已核实的缺陷）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class SettingsFileRobustnessTests(unittest.TestCase):
    def setUp(self):
        from specimen_app import app_settings
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "settings.json"
        self._p = mock.patch.object(app_settings, "settings_path", return_value=self.path)
        self._p.start()
        self.s = app_settings

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_truncated_settings_falls_back_to_previous_good_copy(self):
        st = self.s.AppSettings()
        st.last_workspace = "H:/标本整理"
        self.s.save_settings(st)
        self.s.save_settings(st)  # 第二次保存时把第一份留成 .bak
        self.path.write_text('{"last_workspace": "H:/标', encoding="utf-8")  # 写到一半断电
        self.assertEqual(self.s.load_settings().last_workspace, "H:/标本整理")

    def test_non_utf8_settings_does_not_crash(self):
        self.path.write_bytes(b"\xff\xfe\x00garbage")
        self.s.load_settings()  # 旧：UnicodeDecodeError 冒出去 → 每次启动都崩

    def test_save_leaves_no_temp_files(self):
        self.s.save_settings(self.s.AppSettings())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.suffix == ".tmp"], [])
        json.loads(self.path.read_text(encoding="utf-8"))


class AutoUpdateDefaultTests(unittest.TestCase):
    def setUp(self):
        from specimen_app import app_settings
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "settings.json"
        self._p = mock.patch.object(app_settings, "settings_path", return_value=self.path)
        self._p.start()
        self.s = app_settings

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_user_without_key_gets_startup_check(self):
        self.path.write_text(json.dumps({"last_workspace": "H:/x"}), encoding="utf-8")
        self.assertEqual(self.s.load_settings().auto_update_mode, "notify")

    def test_old_user_stuck_off_is_switched_once_then_respected(self):
        self.path.write_text(json.dumps({"auto_update_mode": "off"}), encoding="utf-8")
        st = self.s.load_settings()
        self.assertEqual(st.auto_update_mode, "notify")
        st.auto_update_mode = "off"  # 之后用户自己关掉
        self.s.save_settings(st)
        self.assertEqual(self.s.load_settings().auto_update_mode, "off")


class ThreadHookTests(unittest.TestCase):
    def test_worker_thread_exception_does_not_break_hook(self):
        from PyQt5.QtWidgets import QApplication
        QApplication.instance() or QApplication(["t"])
        from specimen_app import ui
        saved = (threading.excepthook,)
        try:
            ui._install_qt_exception_dialog()
            errors = []
            orig = ui._post_crash_dialog_to_main_thread
            with mock.patch.object(ui, "_post_crash_dialog_to_main_thread", side_effect=lambda t: errors.append(t)):
                t = threading.Thread(target=lambda: 1 / 0)
                t.start()
                t.join()
            # 旧：args.exc_tb 抛 AttributeError，文本永远拿不到；且会在工作线程里建对话框
            self.assertTrue(errors and "ZeroDivisionError" in errors[0])
            self.assertIs(orig, ui._post_crash_dialog_to_main_thread)
        finally:
            threading.excepthook = saved[0]


class WindowStateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(["t"])

    def setUp(self):
        from specimen_app import app_settings
        from specimen_app.excel_store import ExcelStore
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        st = ExcelStore(self.ws)
        self.v1 = st.create_specimen()
        self.v2 = st.create_specimen()
        st.set_fields("specimen", self.v1, {"备注": "一号"})
        st.release_lock()
        st.close()
        self._ps = [mock.patch.object(app_settings, "settings_path", return_value=self.tmp / "s.json"),
                    mock.patch.object(app_settings, "app_config_dir", return_value=self.tmp / "cfg")]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spin(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_failed_voucher_load_keeps_previous_voucher_and_editing_works(self):
        from specimen_app.ui import SpecimenWindow
        win = SpecimenWindow(self.ws)
        try:
            self._spin(0.2)
            win.select_voucher(self.v1)
            with mock.patch.object(win.store, "get_specimen", side_effect=PermissionError(13, "被占用")):
                win.select_voucher(self.v2)
            # 旧：current_voucher 已变成 v2、控件仍是 v1 的值、_loading 卡在 True
            self.assertFalse(win._loading)
            self.assertEqual(win.current_voucher, self.v1)
            win.specimen_widgets["备注"].setText("一号-改")
            self.assertIn(f"{self.v1}:specimen", win._pending_save_fields)  # 编辑仍会被保存
        finally:
            win.close()
            self.app.processEvents()

    def test_heartbeat_follows_store_after_switch(self):
        from specimen_app.excel_store import ExcelStore
        from specimen_app.ui import SpecimenWindow
        other = self.tmp / "ws2"
        other.mkdir()
        st = ExcelStore(other)
        st.release_lock()
        st.close()
        win = SpecimenWindow(self.ws)
        try:
            self._spin(0.2)
            with mock.patch("specimen_app.ui.QMessageBox"):
                self.assertTrue(win._load_workspace_into_window(other, create_files=False))
            self.assertIs(win._lock_heartbeat_thread._store, win.store)
        finally:
            win.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
