"""Task 7：Excel 镜像导出（脏表 / 全量 / 目标被占用不崩）+ 快照含 sqlite。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 2.2 节 / 第 3 节。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from specimen_app.excel_store import ExcelStore
from specimen_app.models import (
    CHANGE_LOG_FILE,
    CLASSIFICATION_FILE,
    MANAGED_TABLE_KEYS,
    PHOTO_FILE,
    SPECIMEN_FILE,
    SQLITE_DATA_FILE,
)
from specimen_app.table_backend import XlsxBackend


def _xlsx(data_dir):
    return XlsxBackend(data_dir, fit_row=lambda r, h: {k: str(r.get(k, "") or "") for k in h}, to_string=lambda v: "" if v is None else str(v))


class ExcelMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "数据"
        self.store = ExcelStore(self.tmp, backend="sqlite")
        self.v = self.store.create_specimen()
        self.store.set_fields("specimen", self.v, {"备注": "镜像 1"})

    def tearDown(self):
        try:
            self.store.close(export_excel=False)
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_counts_dirty_tables_and_export_clears_them(self):
        self.assertGreater(self.store.excel_mirror_pending(), 0)
        self.assertFalse((self.data / SPECIMEN_FILE).exists())
        result = self.store.export_excel_mirror()
        self.assertEqual(result.failed, {})
        self.assertIn(SPECIMEN_FILE, result.written)
        self.assertEqual(self.store.excel_mirror_pending(), 0)
        rows = _xlsx(self.data).read_rows(SPECIMEN_FILE)
        self.assertEqual([r.get("备注") for r in rows], ["镜像 1"])
        detail = _xlsx(self.data).read_rows(CHANGE_LOG_FILE + "::修改明细")
        self.assertTrue(any(r.get("新值") == "镜像 1" for r in detail))

    def test_export_only_dirty_tables(self):
        self.store.export_excel_mirror()
        self.store.set_fields("classification", self.v, {"种名*": "Nereis"})
        result = self.store.export_excel_mirror()
        self.assertIn(CLASSIFICATION_FILE, result.written)
        self.assertNotIn(PHOTO_FILE, result.written)
        self.assertIn(CHANGE_LOG_FILE, result.written)  # 修改记录也变了
        self.assertEqual(self.store.excel_mirror_pending(), 0)

    def test_export_all_rewrites_every_table(self):
        self.store.export_excel_mirror()
        result = self.store.export_excel_mirror(all_tables=True)
        expected_files = {k.split("::")[0] for k in MANAGED_TABLE_KEYS}
        self.assertEqual(set(result.written), expected_files)
        self.assertEqual(result.failed, {})

    def test_noop_export_when_clean(self):
        self.store.export_excel_mirror()
        result = self.store.export_excel_mirror()
        self.assertEqual(result.written, [])
        self.assertEqual(result.failed, {})

    def test_export_keeps_dirty_when_target_locked(self):
        # Windows 上 Excel 正开着文件 → tmp.replace(path) 抛 PermissionError：库不受影响、该表保持未导出、不崩
        self.store.export_excel_mirror()
        self.store.set_fields("specimen", self.v, {"备注": "锁住时的改动"})
        real_replace = Path.replace

        def flaky(self_path, target):
            if Path(target).name == SPECIMEN_FILE:
                raise PermissionError("[WinError 32] 文件被 Excel 占用")
            return real_replace(self_path, target)

        with patch.object(Path, "replace", flaky):
            result = self.store.export_excel_mirror()
        self.assertIn(SPECIMEN_FILE, result.failed)
        self.assertNotIn(SPECIMEN_FILE, result.written)
        self.assertGreater(self.store.excel_mirror_pending(), 0)
        self.assertEqual(self.store.get_specimen(self.v)["备注"], "锁住时的改动")
        self.assertEqual([p.name for p in self.data.glob("*.tmp")], [])
        result = self.store.export_excel_mirror()  # 释放后重试成功
        self.assertEqual(result.failed, {})
        self.assertEqual(self.store.excel_mirror_pending(), 0)
        self.assertEqual([r.get("备注") for r in _xlsx(self.data).read_rows(SPECIMEN_FILE)], ["锁住时的改动"])

    def test_close_exports_dirty_by_default(self):
        self.store.close()
        self.assertEqual([r.get("备注") for r in _xlsx(self.data).read_rows(SPECIMEN_FILE)], ["镜像 1"])
        self.store = ExcelStore(self.tmp)  # tearDown 还会关一次
        self.assertEqual(self.store.excel_mirror_pending(), 0)

    def test_xlsx_mode_export_is_noop(self):
        other = Path(tempfile.mkdtemp())
        try:
            s = ExcelStore(other, backend="xlsx")
            try:
                self.assertEqual(s.excel_mirror_pending(), 0)
                r = s.export_excel_mirror(all_tables=True)
                self.assertEqual((r.written, r.failed), ([], {}))
            finally:
                s.close()
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_snapshot_contains_sqlite_and_fresh_xlsx(self):
        self.store.set_fields("specimen", self.v, {"备注": "快照前"})
        snap = self.store.create_data_snapshot("手动快照", "t")
        self.assertTrue((snap / SQLITE_DATA_FILE).exists())
        self.assertTrue((snap / SPECIMEN_FILE).exists())
        self.assertEqual([r.get("备注") for r in _xlsx(snap).read_rows(SPECIMEN_FILE)], ["快照前"])
        self.assertEqual(self.store.excel_mirror_pending(), 0)

    def test_restore_snapshot_brings_back_sqlite(self):
        self.store.set_fields("specimen", self.v, {"备注": "版本 A"})
        snap = self.store.create_data_snapshot("手动快照", "A")
        self.store.set_fields("specimen", self.v, {"备注": "版本 B"})
        self.store.restore_data_snapshot(snap)
        self.assertEqual(self.store.storage_backend_name, "sqlite")
        self.assertEqual(self.store.get_specimen(self.v)["备注"], "版本 A")
        self.store.close(export_excel=False)
        self.store = ExcelStore(self.tmp)
        self.assertEqual(self.store.get_specimen(self.v)["备注"], "版本 A")


if __name__ == "__main__":
    unittest.main()
