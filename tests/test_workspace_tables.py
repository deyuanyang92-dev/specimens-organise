"""Task 9：跨工作区只读读取器——有 sqlite 读 sqlite，否则读 xlsx。

场景：对方工作区是 SQLite 模式且**还没生成 Excel 镜像**时，服务器同步预览 / 导入合并 / 聚合必须仍能读到数据。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from specimen_app.excel_store import ExcelStore
from specimen_app.models import CLASSIFICATION_FILE, PHOTO_FILE, SPECIMEN_FILE, SQLITE_DATA_FILE
from specimen_app.server_sync import _read_source_vouchers, _read_xlsx_rows_safe, aggregate_incoming
from specimen_app.workspace_tables import read_table, read_table_raw, sqlite_file_for, table_exists


class CrossWorkspaceReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "source"
        self.src.mkdir()
        store = ExcelStore(self.src, backend="sqlite")
        self.v1 = store.create_specimen()
        self.v2 = store.create_specimen()
        store.set_fields("specimen", self.v1, {"管内编号*": "QD-LSD-SC001-1-R-250923", "采集地缩写*": "QD"})
        store.set_fields("specimen", self.v2, {"管内编号*": "QD-LSD-SC002-1-R-250923", "采集地缩写*": "QD"})
        store.set_fields("classification", self.v1, {"种名*": "Nereis"})
        img = self.tmp / "外部" / "QD-LSD-SC001-1.jpg"
        img.parent.mkdir()
        Image.new("RGB", (8, 8), "blue").save(img, "JPEG")
        store.add_photo(self.v1, img, allow_outside=True)
        store.close(export_excel=False)  # 故意不生成镜像
        self.data = self.src / "数据"
        self.assertFalse((self.data / SPECIMEN_FILE).exists())
        self.assertTrue((self.data / SQLITE_DATA_FILE).exists())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_table_prefers_sqlite(self):
        self.assertIsNotNone(sqlite_file_for(self.data / SPECIMEN_FILE))
        self.assertTrue(table_exists(self.data / SPECIMEN_FILE))
        headers, rows = read_table(self.data / SPECIMEN_FILE)
        self.assertIn("入库编号*", headers)
        self.assertEqual(sorted(r["入库编号*"] for r in rows), sorted([self.v1, self.v2]))
        header, body = read_table_raw(self.data / CLASSIFICATION_FILE)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0][header.index("种名*")], "Nereis")

    def test_read_table_falls_back_to_xlsx(self):
        other = self.tmp / "xlsx_ws"
        other.mkdir()
        store = ExcelStore(other, backend="xlsx")
        v = store.create_specimen()
        store.close()
        self.assertIsNone(sqlite_file_for(other / "数据" / SPECIMEN_FILE))
        headers, rows = read_table(other / "数据" / SPECIMEN_FILE)
        self.assertEqual([r["入库编号*"] for r in rows], [v])
        self.assertFalse(table_exists(other / "数据" / "没有的表.xlsx"))

    def test_server_sync_helpers_read_sqlite_source(self):
        self.assertEqual(sorted(_read_source_vouchers(self.src)), sorted([self.v1, self.v2]))
        header, body = _read_xlsx_rows_safe(self.data / SPECIMEN_FILE)
        self.assertIn("入库编号*", header)
        self.assertEqual(len(body), 2)
        header, body = _read_xlsx_rows_safe(self.data / PHOTO_FILE)
        self.assertEqual(len(body), 1)

    def test_import_workspace_from_sqlite_source_without_mirror(self):
        central = self.tmp / "central"
        central.mkdir()
        target = ExcelStore(central, backend="xlsx")
        try:
            target.import_workspace(self.src)
            self.assertTrue({self.v1, self.v2}.issubset(set(target.list_vouchers())))
            self.assertEqual(target.get_classification(self.v1)["种名*"], "Nereis")
            self.assertEqual(len(target.get_photos(self.v1)), 1)
        finally:
            target.close()

    def test_aggregate_incoming_from_sqlite_source(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        shutil.move(str(self.src), str(incoming / "assignee-a"))
        (incoming / "assignee-a" / "manifest.json").write_text(
            json.dumps({"assignee": "assignee-a", "packed_at": "2026-10-02T10:00:00", "software_version": "test", "data_schema_version": "1.2.0"}, ensure_ascii=False),
            encoding="utf-8",
        )
        central = self.tmp / "central"
        central.mkdir()
        target = ExcelStore(central, backend="xlsx")
        try:
            report = aggregate_incoming(target, incoming)
            self.assertTrue({self.v1, self.v2}.issubset(set(target.list_vouchers())), getattr(report, "errors", report))
        finally:
            target.close()


if __name__ == "__main__":
    unittest.main()
