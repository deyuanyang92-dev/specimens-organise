"""第 2 段：模式识别（Task 5）与旧工作区自动转换（Task 6）。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 1–2 节。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.excel_store import ExcelStore
from specimen_app.models import SPECIMEN_FILE, SQLITE_DATA_FILE, WORKSPACE_CONFIG_FILE


class ModeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "数据"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sqlite_workspace_basics(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        try:
            self.assertEqual(store.storage_backend_name, "sqlite")
            self.assertTrue((self.data / SQLITE_DATA_FILE).exists())
            self.assertFalse((self.data / SPECIMEN_FILE).exists(), "镜像 xlsx 要到导出时才有")
            self.assertEqual(store.config.get("storage_backend"), "sqlite")
            v = store.create_specimen()
            store.set_fields("specimen", v, {"备注": "sqlite 模式"})
            self.assertEqual(store.get_specimen(v)["备注"], "sqlite 模式")
            self.assertEqual(store.get_specimen(v)["入库编号*"], v)
        finally:
            store.close()

    def test_backend_kwarg_forces_xlsx(self):
        store = ExcelStore(self.tmp, backend="xlsx")
        try:
            self.assertEqual(store.storage_backend_name, "xlsx")
            self.assertFalse((self.data / SQLITE_DATA_FILE).exists())
            self.assertTrue((self.data / SPECIMEN_FILE).exists())
            self.assertNotIn("storage_backend", store.config)
        finally:
            store.close()

    def test_reopen_detects_sqlite(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        v = store.create_specimen()
        store.close()
        store = ExcelStore(self.tmp)
        try:
            self.assertEqual(store.storage_backend_name, "sqlite")
            self.assertEqual(store.get_specimen(v)["入库编号*"], v)
        finally:
            store.close()

    def test_open_repairs_config_when_sqlite_exists_without_flag(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        store.close()
        cfg = self.data / WORKSPACE_CONFIG_FILE
        data = json.loads(cfg.read_text("utf-8"))
        data.pop("storage_backend")
        cfg.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        store = ExcelStore(self.tmp)
        try:
            self.assertEqual(store.storage_backend_name, "sqlite")
            self.assertEqual(store.config.get("storage_backend"), "sqlite")
            self.assertEqual(json.loads(cfg.read_text("utf-8")).get("storage_backend"), "sqlite")
            self.assertEqual(store.config.get("data_schema_version"), "1.2.0")
        finally:
            store.close()

    def test_read_only_open_of_sqlite_workspace_has_zero_side_effects(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        v = store.create_specimen()
        store.close()
        before = sorted((p.name, p.stat().st_size) for p in self.data.iterdir() if p.is_file() and not p.name.endswith("-wal") and not p.name.endswith("-shm"))
        ro = ExcelStore(self.tmp, read_only=True)
        try:
            self.assertEqual(ro.storage_backend_name, "sqlite")
            self.assertEqual(ro.get_specimen(v)["入库编号*"], v)
        finally:
            ro.close()
        after = sorted((p.name, p.stat().st_size) for p in self.data.iterdir() if p.is_file() and not p.name.endswith("-wal") and not p.name.endswith("-shm"))
        self.assertEqual(before, after)

    def test_sqlite_mode_still_writes_unmanaged_action_log_fallback_as_xlsx(self):
        # 操作记录.xlsx 不受管：sqlite 模式下仍是 xlsx 文件（Tier B 兜底路径）
        store = ExcelStore(self.tmp, backend="sqlite")
        try:
            self.assertTrue((self.data / "操作记录.xlsx").exists())
        finally:
            store.close()

    def test_schema_guard_blocks_older_software_on_sqlite_workspace(self):
        from unittest.mock import patch
        from specimen_app import excel_store as es
        from specimen_app.models import ImportConflictError

        store = ExcelStore(self.tmp, backend="sqlite")
        store.close()
        with patch.object(es, "CURRENT_DATA_SCHEMA_VERSION", "1.1.3"):
            with self.assertRaises(ImportConflictError):
                ExcelStore(self.tmp)

    def test_undo_redo_work_in_sqlite_mode(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        try:
            v = store.create_specimen()
            store.set_fields("specimen", v, {"备注": "一"})
            store.set_fields("specimen", v, {"备注": "二"})
            store.undo_last()
            self.assertEqual(store.get_specimen(v)["备注"], "一")
            store.redo_last()
            self.assertEqual(store.get_specimen(v)["备注"], "二")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
