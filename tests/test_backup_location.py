"""v0.10.43 每日备份位置可在设置里切换（默认 C 盘 %APPDATA% 是系统盘）。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from specimen_app import app_settings, daily_backup
from specimen_app.excel_store import ExcelStore


class BackupLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._ps = [mock.patch.object(app_settings, "settings_path", return_value=self.tmp / "s.json"),
                    mock.patch.object(app_settings, "app_config_dir", return_value=self.tmp / "cfg")]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()
        from tests import close_all_open_stores
        close_all_open_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_config_dir_and_custom_is_persisted(self):
        self.assertEqual(app_settings.local_backup_dir(), self.tmp / "cfg" / "backups")
        st = app_settings.load_settings()
        st.local_backup_dir = str(self.tmp / "D盘备份")
        app_settings.save_settings(st)
        self.assertEqual(app_settings.local_backup_dir(), self.tmp / "D盘备份")

    def test_restore_finds_backups_in_old_and_new_location(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        store = ExcelStore(ws)
        store.create_specimen()
        old_root, new_root = self.tmp / "cfg" / "backups", self.tmp / "D盘备份"
        snap, _ = daily_backup.run_daily_backup(store, old_root)
        shutil.rmtree(snap)  # 模拟"第二天"：工作区里没有今天的每日备份
        time.sleep(1.1)  # 快照目录名精确到秒
        daily_backup.run_daily_backup(store, new_root)
        found = daily_backup.list_local_backups_in([new_root, old_root, new_root], store)
        self.assertEqual(len(found), 2)
        self.assertTrue(str(found[0]).startswith(str(new_root)))  # 新的在前

    def test_writable_check(self):
        self.assertIsNone(daily_backup.check_backup_dir_writable(self.tmp / "ok"))
        self.assertIsNotNone(daily_backup.check_backup_dir_writable("relative/dir"))
        blocker = self.tmp / "a_file"
        blocker.write_text("x", encoding="utf-8")
        self.assertIsNotNone(daily_backup.check_backup_dir_writable(blocker / "sub"))

    def test_same_drive(self):
        self.assertTrue(daily_backup.same_drive(self.tmp / "a", self.tmp / "b") or not (self.tmp / "a").exists())
        if os.name == "nt":
            self.assertFalse(daily_backup.same_drive("C:\\x", "D:\\y"))

    def test_window_backs_up_to_configured_location(self):
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(["t"])
        ws = self.tmp / "ws2"
        ws.mkdir()
        st = ExcelStore(ws)
        st.create_specimen()
        st.release_lock()
        st.close()
        settings = app_settings.load_settings()
        settings.local_backup_dir = str(self.tmp / "移动硬盘备份")
        app_settings.save_settings(settings)
        from specimen_app.ui import SpecimenWindow
        win = SpecimenWindow(ws)
        try:
            win._schedule_daily_backup()
            end = time.monotonic() + 30
            while time.monotonic() < end and not win._store_worker.is_idle():
                app.processEvents()
                time.sleep(0.02)
            self.assertEqual(len(daily_backup.list_local_backups(self.tmp / "移动硬盘备份", win.store)), 1)
            self.assertEqual(daily_backup.list_local_backups(self.tmp / "cfg" / "backups", win.store), [])
        finally:
            win.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
