"""表后端契约测试：XlsxBackend（第 1 段）与 SqliteBackend（第 2 段）跑同一套契约。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md
计划：docs/superpowers/plans/2026-10-02-sqlite-truth-excel-mirror.md（Task 1 / 2 / 4）
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from specimen_app.table_backend import SHEET_SEP, XlsxBackend, split_table_key, xlsx_read_rows


def _fit(row, headers):
    return {h: str(row.get(h, "") or "") for h in headers}


def _s(v):
    return "" if v is None else str(v)


class SplitKeyTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_table_key("标本信息.xlsx"), ("标本信息.xlsx", None))
        self.assertEqual(split_table_key("修改记录.xlsx" + SHEET_SEP + "修改汇总"), ("修改记录.xlsx", "修改汇总"))


class _ContractMixin:
    """两种后端都必须满足的行为。子类提供 make_backend()。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "数据"
        self.data.mkdir()
        self.b = self.make_backend()

    def tearDown(self):
        try:
            self.b.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replace_then_read_is_sparse_and_ordered(self):
        self.b.replace_rows("t.xlsx", ["a", "b"], [{"a": "1", "b": ""}, {"a": "", "b": "2"}])
        self.assertTrue(self.b.exists("t.xlsx"))
        self.assertEqual(self.b.headers("t.xlsx"), ["a", "b"])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"a": "1"}, {"b": "2"}])

    def test_missing_table_reads_empty(self):
        self.assertFalse(self.b.exists("nope.xlsx"))
        self.assertEqual(self.b.headers("nope.xlsx"), [])
        self.assertEqual(self.b.read_rows("nope.xlsx"), [])
        self.assertEqual(list(self.b.stream_columns("nope.xlsx", {"a"})), [])
        self.assertEqual(self.b.version_token("nope.xlsx"), 0)

    def test_append_rows_appends_in_order(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        self.b.append_rows("t.xlsx", ["a"], [{"a": "2"}, {"a": "3"}])
        self.assertEqual([r["a"] for r in self.b.read_rows("t.xlsx")], ["1", "2", "3"])

    def test_append_creates_missing_table(self):
        self.b.append_rows("n.xlsx", ["a"], [{"a": "x"}])
        self.assertEqual(self.b.read_rows("n.xlsx"), [{"a": "x"}])

    def test_replace_rows_preserves_row_order_and_blank_rows_are_dropped(self):
        self.b.replace_rows("t.xlsx", ["a", "b"], [{"a": "3"}, {}, {"b": "1"}, {"a": "2"}])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"a": "3"}, {"b": "1"}, {"a": "2"}])

    def test_sheet_keys_are_independent_tables_of_one_file(self):
        self.b.replace_many([
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d1"}]),
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s1"}]),
        ])
        self.assertTrue(self.b.exists("c.xlsx" + SHEET_SEP + "修改汇总"))
        self.assertFalse(self.b.exists("c.xlsx" + SHEET_SEP + "不存在"))
        self.assertEqual(self.b.read_rows("c.xlsx" + SHEET_SEP + "修改汇总"), [{"y": "s1"}])
        self.b.append_rows("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s2"}])
        self.assertEqual([r["y"] for r in self.b.read_rows("c.xlsx" + SHEET_SEP + "修改汇总")], ["s1", "s2"])
        self.assertEqual(self.b.read_rows("c.xlsx" + SHEET_SEP + "修改明细"), [{"x": "d1"}])
        self.b.replace_rows("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d9"}])
        self.assertEqual(self.b.read_rows("c.xlsx" + SHEET_SEP + "修改明细"), [{"x": "d9"}])
        self.assertEqual([r["y"] for r in self.b.read_rows("c.xlsx" + SHEET_SEP + "修改汇总")], ["s1", "s2"])

    def test_file_level_key_reflects_its_sheets(self):
        # store 用文件级 key（"修改记录.xlsx"）判"文件存在"和缓存失效，两种后端都要支持
        self.assertFalse(self.b.exists("c.xlsx"))
        self.assertEqual(self.b.version_token("c.xlsx"), 0)
        self.b.replace_many([
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d1"}]),
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s1"}]),
        ])
        self.assertTrue(self.b.exists("c.xlsx"))
        v1 = self.b.version_token("c.xlsx")
        self.assertGreater(v1, 0)
        time.sleep(0.02)
        self.b.append_rows("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s2"}])
        self.assertGreater(self.b.version_token("c.xlsx"), v1)

    def test_read_many_keeps_order_and_handles_missing(self):
        self.b.replace_many([
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d1"}]),
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s1"}]),
        ])
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        got = self.b.read_many([
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"]),
            ("t.xlsx", None),
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"]),
            ("missing.xlsx", ["z"]),
            ("c.xlsx" + SHEET_SEP + "没有", ["q"]),
        ])
        self.assertEqual(got, [[{"y": "s1"}], [{"a": "1"}], [{"x": "d1"}], [], []])

    def test_version_token_changes_on_write(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        v1 = self.b.version_token("t.xlsx")
        self.assertGreater(v1, 0)
        time.sleep(0.02)
        self.b.append_rows("t.xlsx", ["a"], [{"a": "2"}])
        self.assertGreater(self.b.version_token("t.xlsx"), v1)

    def test_stream_columns_only_wanted(self):
        self.b.replace_rows("t.xlsx", ["a", "b"], [{"a": "1", "b": "2"}, {"a": "3", "b": ""}])
        self.assertEqual(list(self.b.stream_columns("t.xlsx", {"b"})), [{"b": "2"}])

    def test_values_are_strings_even_for_numbers(self):
        self.b.replace_rows("t.xlsx", ["n"], [{"n": 12}])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"n": "12"}])

    def test_headers_can_grow_on_replace(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        self.b.replace_rows("t.xlsx", ["a", "b"], [{"a": "1"}, {"a": "2", "b": "x"}])
        self.assertEqual(self.b.headers("t.xlsx"), ["a", "b"])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"a": "1"}, {"a": "2", "b": "x"}])


class XlsxBackendContractTests(_ContractMixin, unittest.TestCase):
    def make_backend(self):
        return XlsxBackend(
            self.data, fit_row=_fit, to_string=_s, verify_file=None,
            column_aliases={"采集地点缩写*": "采集地缩写*"},
        )

    def test_sheet_keys_share_one_workbook_file(self):
        self.b.replace_many([
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d1"}]),
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s1"}]),
        ])
        wb = load_workbook(self.data / "c.xlsx", read_only=True)
        self.assertEqual(wb.sheetnames, ["修改明细", "修改汇总"])
        wb.close()

    def test_replace_many_preserves_unknown_extra_sheets(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "修改明细"
        ws.append(["x"])
        ws.append(["d1"])
        wb.create_sheet("修改汇总").append(["y"])
        extra = wb.create_sheet("用户自加")
        extra.append(["keep"])
        extra.append(["me"])
        wb.save(self.data / "c.xlsx")
        wb.close()
        self.b.replace_many([
            ("c.xlsx" + SHEET_SEP + "修改明细", ["x"], [{"x": "d2"}]),
            ("c.xlsx" + SHEET_SEP + "修改汇总", ["y"], [{"y": "s2"}]),
        ])
        wb = load_workbook(self.data / "c.xlsx", read_only=True)
        self.assertEqual(wb.sheetnames, ["修改明细", "修改汇总", "用户自加"])
        self.assertEqual([r[0] for r in wb["用户自加"].iter_rows(values_only=True)], ["keep", "me"])
        wb.close()
        self.assertEqual(self.b.read_rows("c.xlsx" + SHEET_SEP + "修改明细"), [{"x": "d2"}])

    def test_read_normalises_column_aliases(self):
        self.b.replace_rows("t.xlsx", ["采集地点缩写*"], [{"采集地点缩写*": "QD"}])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"采集地缩写*": "QD"}])

    def test_no_tmp_left_behind(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        self.assertEqual([p.name for p in self.data.iterdir()], ["t.xlsx"])

    def test_module_reader_reads_external_path(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        self.assertEqual(xlsx_read_rows(self.data / "t.xlsx", _s, {}), [{"a": "1"}])
        self.assertEqual(xlsx_read_rows(self.data / "missing.xlsx", _s, {}), [])


class SqliteBackendContractTests(_ContractMixin, unittest.TestCase):
    def make_backend(self):
        from specimen_app.table_backend_sqlite import SqliteBackend

        return SqliteBackend(self.data / "标本数据.sqlite", fit_row=_fit, to_string=_s)

    def test_version_increments_per_write_and_export_marks(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        v1 = self.b.table_version("t.xlsx")
        self.b.append_rows("t.xlsx", ["a"], [{"a": "2"}])
        self.assertEqual(self.b.table_version("t.xlsx"), v1 + 1)
        self.assertEqual(self.b.exported_version("t.xlsx"), 0)
        self.b.mark_exported("t.xlsx", v1 + 1)
        self.assertEqual(self.b.exported_version("t.xlsx"), v1 + 1)

    def test_rejects_blank_or_duplicate_headers(self):
        with self.assertRaises(ValueError):
            self.b.replace_rows("t.xlsx", ["a", ""], [])
        with self.assertRaises(ValueError):
            self.b.replace_rows("t.xlsx", ["a", "a"], [])
        self.assertFalse(self.b.exists("t.xlsx"))

    def test_replace_many_is_atomic(self):
        self.b.replace_rows("t.xlsx", ["a"], [{"a": "old"}])
        with self.assertRaises(ValueError):
            self.b.replace_many([("t.xlsx", ["a"], [{"a": "new"}]), ("u.xlsx", ["", "x"], [])])
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"a": "old"}])
        self.assertFalse(self.b.exists("u.xlsx"))

    def test_table_names_and_quick_check(self):
        from specimen_app.table_backend_sqlite import SqliteBackend

        self.assertEqual(SqliteBackend.table_name("标本信息.xlsx"), "标本信息")
        self.assertEqual(SqliteBackend.table_name("修改记录.xlsx" + SHEET_SEP + "修改汇总"), "修改记录__修改汇总")
        self.assertTrue(self.b.quick_check())

    def test_reopen_sees_data_and_versions(self):
        from specimen_app.table_backend_sqlite import SqliteBackend

        self.b.replace_rows("t.xlsx", ["a"], [{"a": "1"}])
        v = self.b.table_version("t.xlsx")
        self.b.close()
        self.b = SqliteBackend(self.data / "标本数据.sqlite", fit_row=_fit, to_string=_s)
        self.assertEqual(self.b.read_rows("t.xlsx"), [{"a": "1"}])
        self.assertEqual(self.b.table_version("t.xlsx"), v)

    def test_network_safe_mode_uses_delete_journal(self):
        from specimen_app.table_backend_sqlite import SqliteBackend

        b = SqliteBackend(self.data / "net.sqlite", fit_row=_fit, to_string=_s, network_safe=True)
        try:
            self.assertEqual(b.journal_mode(), "delete")
        finally:
            b.close()
        self.assertEqual(self.b.journal_mode(), "wal")


class StoreUsesBackendTests(unittest.TestCase):
    """Task 2：ExcelStore 的读写原语全部经过 self._backend。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_routes_reads_and_writes_through_backend(self):
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import CHANGE_LOG_FILE, SPECIMEN_FILE

        store = ExcelStore(self.tmp)
        try:
            self.assertEqual(store.storage_backend_name, "xlsx")
            v = store.create_specimen()
            store.set_fields("specimen", v, {"备注": "via backend"})
            calls: list[str] = []
            orig = store._backend.read_rows

            def spy(key, fallback_headers=None):
                calls.append(key)
                return orig(key, fallback_headers)

            store._backend.read_rows = spy  # type: ignore[method-assign]
            store._invalidate_cache(SPECIMEN_FILE)
            self.assertEqual(store.get_specimen(v)["备注"], "via backend")
            self.assertIn(SPECIMEN_FILE, calls)
            detail = store._backend.read_rows(CHANGE_LOG_FILE + SHEET_SEP + "修改明细", None)
            self.assertTrue(any(r.get("字段名") == "备注" and r.get("新值") == "via backend" for r in detail))
        finally:
            store.close()

    def test_table_key_for_external_path_is_none(self):
        from specimen_app.excel_store import ExcelStore

        store = ExcelStore(self.tmp)
        try:
            self.assertEqual(store._table_key_for(store.data_dir / "标本信息.xlsx"), "标本信息.xlsx")
            self.assertIsNone(store._table_key_for(self.tmp / "别处" / "标本信息.xlsx"))
            self.assertIsNone(store._table_key_for(store.data_dir / "数据版本" / "x" / "标本信息.xlsx"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
