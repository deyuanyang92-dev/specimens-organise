"""v0.10.42 每日自动备份（3-2-1 思路：工作区内快照 + 本机第二份）。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from specimen_app import daily_backup
from specimen_app.excel_store import ExcelStore, SNAPSHOT_MANIFEST_FILENAME
from specimen_app.models import DATA_VERSION_DIR


class DailyBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.local = self.tmp / "local_backups"
        self.store = ExcelStore(self.ws)
        self.v = self.store.create_specimen()
        self.store.set_fields("specimen", self.v, {"备注": "原始值"})

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _versions_dir(self) -> Path:
        return self.store.data_dir / DATA_VERSION_DIR

    def test_first_run_of_day_creates_workspace_and_local_copy(self):
        result = daily_backup.run_daily_backup(self.store, self.local, today=date(2026, 10, 4))
        self.assertIsNotNone(result)
        snap, local_copy = result
        self.assertTrue((snap / ".snapshot.complete").exists())
        self.assertTrue((local_copy / ".snapshot.complete").exists())
        self.assertTrue((local_copy / "标本信息.xlsx").exists())
        manifest = json.loads((snap / SNAPSHOT_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["operation_type"], daily_backup.DAILY_OPERATION_TYPE)

    def test_malicious_workspace_id_cannot_escape_backup_dir(self):
        self.store.config["workspace_id"] = "..\\..\\..\\Windows"
        key = daily_backup.workspace_backup_key(self.store)
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.store.config["workspace_id"] = "../../etc"
        self.assertNotIn("..", daily_backup.workspace_backup_key(self.store))

    def test_second_run_same_day_is_noop(self):
        # 不能注入假日期：快照目录名来自 ExcelStore.create_data_snapshot 里的
        # datetime.now()（excel_store.py `version_id = datetime.now()...`），而
        # has_daily_backup_for() 按传入的 today 去匹配目录名前缀。两个时钟一旦不同，
        # 第二次必然判定"今天还没备过"。生产路径里 today 就是真实日期，所以这里也用
        # 真实日期 —— 用例本来要验证的"同一天只备一次"才真的被覆盖到。
        # 注入日期的那条分支由 test_has_daily_backup_for_matches_by_date 单独覆盖。
        today = date.today()
        self.assertIsNotNone(daily_backup.run_daily_backup(self.store, self.local, today=today))
        self.assertIsNone(daily_backup.run_daily_backup(self.store, self.local, today=today))

    def test_has_daily_backup_for_matches_by_date(self):
        """纯函数：按日期前缀匹配快照目录，不碰时钟。"""
        versions = self._versions_dir()
        versions.mkdir(parents=True, exist_ok=True)
        self.assertFalse(daily_backup.has_daily_backup_for(versions, date(2026, 10, 4)))

        snap = self.store.create_data_snapshot(daily_backup.DAILY_OPERATION_TYPE, "x")
        made_on = date(*(int(x) for x in (snap.name[1:5], snap.name[5:7], snap.name[7:9])))
        self.assertTrue(daily_backup.has_daily_backup_for(versions, made_on))
        self.assertFalse(
            daily_backup.has_daily_backup_for(versions, made_on.replace(year=made_on.year - 1))
        )

    def test_prune_keeps_only_recent_daily_and_never_touches_other_snapshots(self):
        manual = self.store.create_data_snapshot("手动快照", "用户自己做的")
        made = []
        for i in range(5):
            snap = self.store.create_data_snapshot(daily_backup.DAILY_OPERATION_TYPE, "x")
            made.append(snap)
        removed = daily_backup.prune_daily_snapshots(self._versions_dir(), keep=2)
        self.assertEqual(len(removed), 3)
        self.assertTrue(manual.exists())
        self.assertTrue(made[-1].exists() and made[-2].exists())
        self.assertFalse(made[0].exists())

    def test_restore_from_local_backup_when_workspace_snapshot_is_gone(self):
        _, local_copy = daily_backup.run_daily_backup(self.store, self.local, today=date(2026, 10, 4))
        shutil.rmtree(self._versions_dir())  # 工作区盘上的快照没了（盘坏 / 被删）
        self.store.set_fields("specimen", self.v, {"备注": "后来改坏了"})
        backups = daily_backup.list_local_backups(self.local, self.store)
        self.assertEqual([b.name for b in backups], [local_copy.name])
        daily_backup.restore_from_local_backup(self.store, backups[0])
        self.assertEqual(self.store.get_specimen(self.v)["备注"], "原始值")


class WindowDailyBackupTests(unittest.TestCase):
    """真实窗口：打开工作区后后台做当天备份，工作区与本机各一份。"""

    def test_window_runs_daily_backup_in_background(self):
        import os
        import time
        from unittest import mock
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(["test"])
        from specimen_app import app_settings
        tmp = Path(tempfile.mkdtemp())
        try:
            ws = tmp / "ws"
            ws.mkdir()
            st = ExcelStore(ws)
            st.create_specimen()
            st.release_lock()
            st.close()
            with mock.patch.object(app_settings, "settings_path", return_value=tmp / "s.json"), \
                    mock.patch.object(app_settings, "app_config_dir", return_value=tmp / "cfg"):
                from specimen_app.ui import SpecimenWindow
                win = SpecimenWindow(ws)
                try:
                    win._schedule_daily_backup()
                    end = time.monotonic() + 30
                    while time.monotonic() < end and not win._store_worker.is_idle():
                        app.processEvents()
                        time.sleep(0.02)
                    seen = []
                    win.statusBar().messageChanged.connect(seen.append)
                    end = time.monotonic() + 3
                    while time.monotonic() < end and not any("自动备份已完成" in m for m in seen):
                        app.processEvents()
                        time.sleep(0.02)
                    self.assertEqual(len(daily_backup.list_local_backups(tmp / "cfg" / "backups", win.store)), 1)
                    self.assertTrue(any("自动备份已完成" in m for m in seen), seen)
                finally:
                    win.close()
                    app.processEvents()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
