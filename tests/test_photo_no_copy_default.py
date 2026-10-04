"""v0.10.45 关联照片默认不复制到工作区 照片/（用户决定）；多人协作任务包工作区仍复制。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from specimen_app import app_settings
from specimen_app.excel_store import ExcelStore


class SettingsDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "settings.json"
        self._p = mock.patch.object(app_settings, "settings_path", return_value=self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_install_defaults_to_no_copy(self):
        self.assertEqual(app_settings.load_settings().photo_management_mode, "absolute_only")

    def test_old_default_copy_user_migrated_once_then_respected(self):
        self.path.write_text(json.dumps({"photo_management_mode": "copy_with_absolute"}), encoding="utf-8")
        st = app_settings.load_settings()
        self.assertEqual(st.photo_management_mode, "absolute_only")
        st.photo_management_mode = "copy_with_absolute"  # 之后用户自己改回"复制"
        app_settings.save_settings(st)
        self.assertEqual(app_settings.load_settings().photo_management_mode, "copy_with_absolute")

    def test_custom_library_choice_untouched(self):
        self.path.write_text(json.dumps({"photo_management_mode": "copy_to_custom_library",
                                         "photo_library_path": "D:/库"}), encoding="utf-8")
        self.assertEqual(app_settings.load_settings().photo_management_mode, "copy_to_custom_library")


class WindowPhotoLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(["t"])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._ps = [mock.patch.object(app_settings, "settings_path", return_value=self.tmp / "s.json"),
                    mock.patch.object(app_settings, "app_config_dir", return_value=self.tmp / "cfg")]
        for p in self._ps:
            p.start()
        self.src = self.tmp / "我的照片" / "YZZ000001-1.jpg"
        self.src.parent.mkdir()
        from PIL import Image
        Image.new("RGB", (8, 8)).save(self.src)

    def tearDown(self):
        for p in self._ps:
            p.stop()
        from tests import close_all_open_stores
        close_all_open_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _link(self, ws: Path):
        st = ExcelStore(ws)
        v = st.create_specimen()
        st.release_lock()
        st.close()
        from specimen_app.ui import SpecimenWindow
        win = SpecimenWindow(ws)
        try:
            import time
            end = time.monotonic() + 1.0  # 等 _finish_initial_load（refresh_list 等）跑完
            while time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
            win.select_voucher(v)
            with mock.patch("specimen_app.ui.QMessageBox"):
                self.assertEqual(win.add_photo_paths([str(self.src)]), 1)
            return list((ws / "照片").glob("*")) if (ws / "照片").exists() else [], win.store.get_photos(v)
        finally:
            win.close()
            self.app.processEvents()

    def test_normal_workspace_does_not_copy(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        copies, photos = self._link(ws)
        self.assertEqual(copies, [])  # 旧：照片/ 里多一份
        self.assertEqual(photos[0]["归档状态"], "仅记录")
        self.assertEqual(Path(photos[0]["绝对路径"]), self.src.resolve())

    def test_task_package_workspace_still_copies(self):
        ws = self.tmp / "张三任务"
        ws.mkdir()
        (ws / "manifest.json").write_text(json.dumps({"task_id": "t1", "assignee": "张三"}), encoding="utf-8")
        copies, photos = self._link(ws)
        self.assertEqual(len(copies), 1)  # 照片要随工作区交回中心机合并
        self.assertEqual(photos[0]["归档状态"], "已归档")
        self.assertTrue(self.src.exists())  # 原图不动


if __name__ == "__main__":
    unittest.main()
