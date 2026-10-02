"""GUI 不被磁盘写拖住 + 多线程写互不丢数据（用户反馈"非常容易卡死"的系统性部分）。

  * read_rows（GUI 线程每次切换编号都会调）不能等后台线程的 xlsx 写盘：_rw_lock 只保护内存缓存，
    磁盘写在锁外。
  * 所有改数据的公开方法串行（_mutation_lock）：主线程的照片保存与后台线程的字段保存同时发生时，
    修改记录 / 操作记录 一条不丢。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from specimen_app.excel_store import ExcelStore


class ReadDoesNotWaitForDiskWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ExcelStore(self.tmp, backend="xlsx")
        self.v = self.store.create_specimen()
        self.store.read_rows("specimen")  # 预热缓存

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_rows_returns_while_background_write_is_on_disk(self):
        backend = self.store._backend
        real_replace = backend.replace_rows
        started = threading.Event()

        def slow_replace(key, headers, rows):
            started.set()
            time.sleep(1.0)  # 模拟 NAS 上一次 xlsx 写盘
            return real_replace(key, headers, rows)

        with patch.object(backend, "replace_rows", slow_replace):
            t = threading.Thread(target=lambda: self.store.set_fields("specimen", self.v, {"备注": "慢写"}))
            t.start()
            self.assertTrue(started.wait(2.0))
            t0 = time.perf_counter()
            rows = self.store.read_rows("specimen")
            elapsed = time.perf_counter() - t0
            t.join(5.0)
        self.assertLess(elapsed, 0.3, f"GUI 线程的 read_rows 等了 {elapsed:.2f}s（被磁盘写拖住）")
        self.assertEqual(len(rows), 1)


class WritersAreSerializedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ExcelStore(self.tmp, backend="xlsx")
        self.v = self.store.create_specimen()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_set_fields_lose_no_change_log_entries(self):
        n = 15
        errors: list[BaseException] = []

        def worker(category, field, prefix):
            try:
                for i in range(n):
                    self.store.set_fields(category, self.v, {field: f"{prefix}{i}"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        a = threading.Thread(target=worker, args=("specimen", "备注", "A"))
        b = threading.Thread(target=worker, args=("classification", "种名*", "B"))
        a.start(); b.start(); a.join(60); b.join(60)
        self.assertEqual(errors, [])
        detail = self.store._read_change_detail_rows()
        a_entries = [r for r in detail if r.get("字段名") == "备注"]
        b_entries = [r for r in detail if r.get("字段名") == "种名*"]
        self.assertEqual(len(a_entries), n, "specimen 修改明细丢行")
        self.assertEqual(len(b_entries), n, "classification 修改明细丢行")
        self.assertEqual(self.store.get_specimen(self.v)["备注"], f"A{n-1}")
        self.assertEqual(self.store.get_classification(self.v)["种名*"], f"B{n-1}")
        self.assertTrue(hasattr(self.store, "_mutation_lock"))


class StartupDoesNotParseWholeWorkbooksTests(unittest.TestCase):
    """启动时 _ensure_workbook 只该看表头；整表解析 7 本 xlsx 在网络盘上就是"一打开就未响应"的大头。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store = ExcelStore(self.tmp, backend="xlsx")
        for _ in range(3):
            store.create_specimen()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reopen_reads_rows_of_at_most_two_tables(self):
        from specimen_app import table_backend

        calls: list[str] = []
        real = table_backend.xlsx_read_rows

        def counting(path, *a, **k):
            calls.append(Path(path).name)
            return real(path, *a, **k)

        with patch.object(table_backend, "xlsx_read_rows", counting):
            store = ExcelStore(self.tmp, backend="xlsx")
            store.close()
        # 下限 4：标本/分类/照片（列表渲染必须）+ 编号索引（流水号）。改前是 11 次。
        self.assertLessEqual(len(calls), 4, f"启动整表读了 {len(calls)} 次：{calls}")

    def test_table_key_for_does_not_resolve_paths_inside_data_dir(self):
        from specimen_app.models import SPECIMEN_FILE

        store = ExcelStore(self.tmp, backend="xlsx")
        try:
            with patch.object(Path, "resolve", side_effect=AssertionError("resolve() 在网络盘上是一次往返，不该为数据目录内的表调用")):
                self.assertEqual(store._table_key_for(store.data_dir / SPECIMEN_FILE), SPECIMEN_FILE)
                self.assertIsNone(store._table_key_for(store.data_dir / "数据版本" / "x" / SPECIMEN_FILE))
        finally:
            store.close()


class SnapshotLockOrderTests(unittest.TestCase):
    """事务快照在 helper 线程里做（NAS 软超时设计）；它绝不能需要 _mutation_lock，否则持锁的外层方法等它 → 死锁。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_inside_mutation_lock(self, store, fn, timeout=25.0):
        done = threading.Event()
        err: list[BaseException] = []

        def run():
            try:
                with store._mutation_lock:
                    fn()
            except BaseException as exc:  # noqa: BLE001
                err.append(exc)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(timeout), "持有 _mutation_lock 时事务快照死锁")
        self.assertEqual(err, [])

    def test_transaction_journal_snapshot_inside_serialized_mutator_xlsx(self):
        store = ExcelStore(self.tmp, backend="xlsx")
        try:
            store.create_specimen()

            def body():
                with store.with_transaction_journal("t"):
                    pass

            self._run_inside_mutation_lock(store, body)
        finally:
            store.close()

    def test_transaction_journal_snapshot_inside_serialized_mutator_sqlite(self):
        store = ExcelStore(self.tmp, backend="sqlite")
        try:
            v = store.create_specimen()
            store.set_fields("specimen", v, {"备注": "快照前"})

            def body():
                with store.with_transaction_journal("t"):
                    pass

            self._run_inside_mutation_lock(store, body)
            snaps = sorted(p for p in (self.tmp / "数据" / "数据版本").iterdir() if p.is_dir())
            self.assertTrue(snaps)
            import sqlite3

            conn = sqlite3.connect(snaps[-1] / "标本数据.sqlite")
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM "标本信息"').fetchone()[0], 1)
            finally:
                conn.close()
        finally:
            store.close(export_excel=False)


if __name__ == "__main__":
    unittest.main()
