"""S2 批量导入工作区目录测试。

aggregate_sources 直接合并多个源目录，不需要 incoming/。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.excel_store import ExcelStore
from specimen_app.server_sync import aggregate_sources


class AggregateSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.central.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_source(self, name: str, tube: str = "T") -> Path:
        d = self.tmp / name
        d.mkdir()
        s = ExcelStore(d)
        v = s.create_specimen()
        s.set_fields("specimen", v, {"管内编号*": f"{tube}-001", "采集地点缩写*": "QD"})
        return d

    def test_empty_source_list(self) -> None:
        store = ExcelStore(self.central)
        report = aggregate_sources(store, [])
        self.assertEqual(report.processed, [])
        self.assertIsNone(report.snapshot_path)

    def test_two_sources_merged_without_incoming(self) -> None:
        ydy = self._make_source("ydy", "Y")
        # 让 yss 用不同 voucher（避免 YZZ000001 撞号）
        yss = self.tmp / "yss"
        yss.mkdir()
        s = ExcelStore(yss)
        v1 = s.create_specimen()  # YZZ000001
        v2 = s.create_specimen()  # YZZ000002
        s.delete_specimen(v1)
        s.set_fields("specimen", v2, {"管内编号*": "S-001", "采集地点缩写*": "QD"})

        store = ExcelStore(self.central)
        report = aggregate_sources(store, [ydy, yss])

        # ydy 的 YZZ000001 + yss 的 YZZ000002 都应进
        self.assertEqual(len(report.processed), 2)
        self.assertEqual(report.total_imported, 2)
        self.assertEqual(set(store.list_vouchers()), {"YZZ000001", "YZZ000002"})

    def test_invalid_source_routes_to_errored(self) -> None:
        # 不含 数据/ 子目录的目录
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "some_file.txt").write_text("not a workspace", encoding="utf-8")
        store = ExcelStore(self.central)
        report = aggregate_sources(store, [bad])
        self.assertEqual(report.processed, [])
        self.assertEqual(len(report.errored), 1)
        self.assertIn("缺 数据/", report.errored[0][1])

    def test_does_not_move_source_dirs(self) -> None:
        ydy = self._make_source("ydy")
        store = ExcelStore(self.central)
        aggregate_sources(store, [ydy])
        # 源目录原样保留（与 aggregate_incoming 不同）
        self.assertTrue(ydy.exists())
        self.assertTrue((ydy / "数据").is_dir())

    def test_snapshot_created_before_merge(self) -> None:
        ydy = self._make_source("ydy")
        store = ExcelStore(self.central)
        report = aggregate_sources(store, [ydy])
        self.assertIsNotNone(report.snapshot_path)
        self.assertTrue(report.snapshot_path.exists())


if __name__ == "__main__":
    unittest.main()
