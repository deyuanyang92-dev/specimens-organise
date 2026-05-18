"""M3 多人协作 — 任务包导出/导入测试。

覆盖：
- 导出生成合法 zip：含 manifest.json + 数据/工作区配置.json + 录入员系列配置
- 中心 alloc_log 含"任务开始"行 + task_id + 编号段
- 编号段不与中心 YZZ 撞
- 同 assignee 二次导出 → 同系列累加 next_counter（不重叠）
- 导入到空目录成功；非空目录拒绝
- 任务包工作区可被 ExcelStore 打开 + 创建标本编号落在预留段
- 端到端：export → import → 录入员 create_specimen 数次 → 工作区目录拷到中心 incoming → M1 aggregate 全部并入
- zip-slip 防护
- read_task_manifest 不解压窥探 manifest
- 缺 manifest 的 zip → import 拒绝
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from specimen_app.excel_store import ExcelStore
from specimen_app.server_sync import aggregate_incoming
from specimen_app.task_package import (
    MANIFEST_NAME,
    export_task_package,
    import_task_package,
    read_task_manifest,
)


class TaskPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.central.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ─────────────────────────── 导出基础 ───────────────────────────

    def test_export_produces_valid_zip(self) -> None:
        store = ExcelStore(self.central)
        zip_path = self.tmp / "张三_任务包.zip"

        result = export_task_package(store, "张三", count=50, prefix="ZS-", dest_zip_path=zip_path)
        self.assertEqual(result, zip_path.resolve())
        self.assertTrue(zip_path.exists())

        # zip 内必含 manifest 和工作区配置
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            self.assertIn(MANIFEST_NAME, names)
            self.assertIn("数据/工作区配置.json", names)
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))

        self.assertEqual(manifest["assignee"], "张三")
        self.assertEqual(manifest["voucher_count"], 50)
        self.assertEqual(manifest["series_prefix"], "ZS-")
        self.assertEqual(manifest["voucher_range"][0], "ZS-000001")
        self.assertEqual(manifest["voucher_range"][1], "ZS-000050")
        self.assertIn("task_id", manifest)

    def test_export_writes_alloc_log_entry(self) -> None:
        store = ExcelStore(self.central)
        export_task_package(store, "张三", count=50, prefix="ZS-", dest_zip_path=self.tmp / "p.zip")

        log = store.read_alloc_log()
        task_starts = [r for r in log if r.get("类型") == "任务开始"]
        self.assertEqual(len(task_starts), 1)
        row = task_starts[0]
        self.assertEqual(row["人员"], "张三")
        self.assertEqual(row["编号系列"], "张三_系列")
        self.assertEqual(row["编号起始"], "ZS-000001")
        self.assertEqual(row["编号结束"], "ZS-000050")
        self.assertEqual(row["数量"], "50")

    def test_export_voucher_range_does_not_collide_with_yzz(self) -> None:
        store = ExcelStore(self.central)
        # 中心先用 YZZ 预留 100 个
        store.batch_reserve_vouchers(100)
        export_task_package(store, "张三", count=50, prefix="ZS-", dest_zip_path=self.tmp / "p.zip")

        # 中心机继续 create_specimen() 不应撞张三的段（前缀不同）
        v = store.create_specimen()
        self.assertTrue(v.startswith("YZZ"))
        # 反之，再给张三发 30 个 — 接续 ZS-000051..ZS-000080
        export_task_package(store, "张三", count=30, prefix="ZS-", dest_zip_path=self.tmp / "p2.zip")
        manifest2 = read_task_manifest(self.tmp / "p2.zip")
        self.assertEqual(manifest2["voucher_range"][0], "ZS-000051")
        self.assertEqual(manifest2["voucher_range"][1], "ZS-000080")

    def test_export_dest_existing_raises(self) -> None:
        store = ExcelStore(self.central)
        zip_path = self.tmp / "exists.zip"
        zip_path.write_bytes(b"placeholder")
        with self.assertRaises(FileExistsError):
            export_task_package(store, "张三", count=10, prefix="ZS-", dest_zip_path=zip_path)

    def test_export_invalid_count_rejected(self) -> None:
        store = ExcelStore(self.central)
        with self.assertRaises(ValueError):
            export_task_package(store, "张三", count=0, prefix="ZS-", dest_zip_path=self.tmp / "x.zip")

    # ─────────────────────────── 导入基础 ───────────────────────────

    def test_import_into_empty_dir(self) -> None:
        store = ExcelStore(self.central)
        zip_path = self.tmp / "p.zip"
        export_task_package(store, "张三", count=20, prefix="ZS-", dest_zip_path=zip_path)

        target = self.tmp / "张三的工作区"
        result = import_task_package(zip_path, target)
        self.assertEqual(result, target.resolve())
        self.assertTrue((target / MANIFEST_NAME).exists())
        self.assertTrue((target / "数据" / "工作区配置.json").exists())

    def test_import_into_nonempty_dir_rejected(self) -> None:
        store = ExcelStore(self.central)
        zip_path = self.tmp / "p.zip"
        export_task_package(store, "张三", count=20, prefix="ZS-", dest_zip_path=zip_path)
        target = self.tmp / "已有内容"
        target.mkdir()
        (target / "placeholder.txt").write_text("not empty", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            import_task_package(zip_path, target)

    def test_imported_workspace_creates_specimen_in_reserved_range(self) -> None:
        # 录入员打开任务包工作区后，create_specimen() 的编号应该落在预留段
        store = ExcelStore(self.central)
        zip_path = self.tmp / "p.zip"
        export_task_package(store, "张三", count=10, prefix="ZS-", dest_zip_path=zip_path)
        target = self.tmp / "张三工作区"
        import_task_package(zip_path, target)

        recruit_store = ExcelStore(target)
        v1 = recruit_store.create_specimen()
        v2 = recruit_store.create_specimen()
        self.assertEqual(v1, "ZS-000001")
        self.assertEqual(v2, "ZS-000002")

    def test_import_rejects_zip_without_manifest(self) -> None:
        # 手工造一个缺 manifest 的 zip
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("数据/whatever.txt", "x")
        target = self.tmp / "out"
        with self.assertRaises(ValueError) as ctx:
            import_task_package(bad, target)
        self.assertIn("manifest", str(ctx.exception).lower())

    def test_import_rejects_zip_slip(self) -> None:
        bad = self.tmp / "slip.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../escape.txt", "danger")
        target = self.tmp / "out2"
        with self.assertRaises(ValueError) as ctx:
            import_task_package(bad, target)
        self.assertIn("非法路径", str(ctx.exception))

    # ─────────────────────── 端到端 ───────────────────────

    def test_end_to_end_export_record_aggregate(self) -> None:
        """端到端：中心机给张三发任务包 → 张三导入 + 录入 + 整目录回传到 incoming → 中心 M1 聚合。"""
        store = ExcelStore(self.central)
        zip_path = self.tmp / "张三_任务包.zip"
        export_task_package(store, "张三", count=10, prefix="ZS-", dest_zip_path=zip_path)

        # 张三机器解压
        zhang_root = self.tmp / "张三的电脑" / "工作区"
        import_task_package(zip_path, zhang_root)
        # 张三录入 3 条
        zhang_store = ExcelStore(zhang_root)
        for _ in range(3):
            zhang_store.create_specimen()
        self.assertEqual(zhang_store.list_vouchers(), ["ZS-000001", "ZS-000002", "ZS-000003"])

        # 张三把整工作区目录复制到中心 incoming/
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        zhang_subdir = incoming / "张三_20260516_1430"
        shutil.copytree(zhang_root, zhang_subdir)

        # 中心机聚合
        report = aggregate_incoming(store, incoming)
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.total_imported, 3)
        self.assertEqual(set(store.list_vouchers()), {"ZS-000001", "ZS-000002", "ZS-000003"})

    # ─────────────────────── manifest 窥探 ───────────────────────

    def test_read_task_manifest_does_not_extract(self) -> None:
        store = ExcelStore(self.central)
        zip_path = self.tmp / "p.zip"
        export_task_package(store, "张三", count=10, prefix="ZS-", dest_zip_path=zip_path)
        manifest = read_task_manifest(zip_path)
        self.assertEqual(manifest["assignee"], "张三")
        self.assertEqual(manifest["voucher_range"][0], "ZS-000001")


if __name__ == "__main__":
    unittest.main()
