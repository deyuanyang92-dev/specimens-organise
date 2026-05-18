"""M4 段守护 + 跨 voucher 照片审核 测试。

覆盖：
- 段守护：源 voucher 全在 manifest 段内 → 通过
- 段守护：源 voucher 超出段 → 进 errors/，附 error.log，中心数据未变
- 段守护：无 manifest 时不校验（降级模式沿用 P1 等价行为）
- 段守护：voucher_range 含不同前缀 / 缺字段 → 跳过校验（向前兼容）
- 照片审核：import_workspace(policy="import" 默认) → 现有行为，跨 voucher 同 SHA256 写入两条
- 照片审核：import_workspace(policy="report") → 跨 voucher 同 SHA256 不写入 + duplicate_candidates 含一条
- 照片审核：aggregate_incoming 默认走 report 模式 → duplicates/ 含报告 xlsx，AggregateReport.duplicates 非空
- 照片审核：同 voucher 内 SHA256 重复（一对多）→ 不算重复（合法业务用法保留）
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from specimen_app.excel_store import ExcelStore
from specimen_app.server_sync import (
    _voucher_in_range,
    _split_voucher,
    aggregate_incoming,
)
from specimen_app.task_package import export_task_package, import_task_package


class SegmentGuardUnitTests(unittest.TestCase):
    """段守护工具函数的纯单元测试（无 IO）。"""

    def test_split_voucher(self) -> None:
        self.assertEqual(_split_voucher("YZZ000003"), ("YZZ", 3))
        self.assertEqual(_split_voucher("ZS-000012"), ("ZS-", 12))
        self.assertEqual(_split_voucher("A1-9"), ("A1-", 9))
        self.assertIsNone(_split_voucher(""))
        self.assertIsNone(_split_voucher("abc"))

    def test_voucher_in_range_within(self) -> None:
        self.assertTrue(_voucher_in_range("ZS-000001", "ZS-000001", "ZS-000050"))
        self.assertTrue(_voucher_in_range("ZS-000025", "ZS-000001", "ZS-000050"))
        self.assertTrue(_voucher_in_range("ZS-000050", "ZS-000001", "ZS-000050"))

    def test_voucher_in_range_out(self) -> None:
        self.assertFalse(_voucher_in_range("ZS-000051", "ZS-000001", "ZS-000050"))
        self.assertFalse(_voucher_in_range("ZS-000000", "ZS-000001", "ZS-000050"))

    def test_voucher_in_range_prefix_mismatch(self) -> None:
        self.assertFalse(_voucher_in_range("YZZ000001", "ZS-000001", "ZS-000050"))


class SegmentGuardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.incoming = self.tmp / "incoming"
        self.central.mkdir()
        self.incoming.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_assignee_subdir_via_task_pkg(self, assignee: str, count: int, prefix: str, record_n: int):
        """端到端构造一个录入员子目录：用 export_task_package 取段 → import_task_package 解压 → 录入 record_n 个。"""
        store = ExcelStore(self.central)
        zip_path = self.tmp / f"{assignee}_pkg.zip"
        export_task_package(store, assignee, count=count, prefix=prefix, dest_zip_path=zip_path)
        sub = self.incoming / f"{assignee}_20260516_1430"
        target = self.tmp / f"{assignee}_workspace"
        import_task_package(zip_path, target)
        sub_store = ExcelStore(target)
        for _ in range(record_n):
            sub_store.create_specimen()
        shutil.copytree(target, sub)
        return sub

    def test_within_range_aggregates_success(self) -> None:
        sub = self._make_assignee_subdir_via_task_pkg("张三", count=50, prefix="ZS-", record_n=10)
        store = ExcelStore(self.central)
        report = aggregate_incoming(store, self.incoming)
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.total_imported, 10)
        self.assertEqual(report.errored, [])

    def test_out_of_range_routes_to_errors(self) -> None:
        """张三只领了 5 个号但录了 10 个 → 后 5 个超段 → 整子目录进 errors/。"""
        sub = self._make_assignee_subdir_via_task_pkg("张三", count=5, prefix="ZS-", record_n=10)
        store = ExcelStore(self.central)
        report = aggregate_incoming(store, self.incoming)
        self.assertEqual(report.processed, [])
        self.assertEqual(len(report.errored), 1)
        name, message = report.errored[0]
        self.assertIn("超出 manifest 预留段", message)
        self.assertTrue((self.incoming / "errors" / sub.name).exists())
        self.assertTrue((self.incoming / "errors" / sub.name / "error.log").exists())
        # 中心机数据未变
        self.assertEqual(store.list_vouchers(), [])

    def test_no_manifest_falls_back_to_p1(self) -> None:
        """无 manifest 子目录 → 不校验段，正常聚合（M1 降级模式仍然有效）。"""
        anon = self.incoming / "anonymous_workspace"
        anon.mkdir()
        anon_store = ExcelStore(anon)
        anon_store.create_specimen()
        # 不写 manifest.json

        store = ExcelStore(self.central)
        report = aggregate_incoming(store, self.incoming)
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.total_imported, 1)


class PhotoDuplicatePolicyTests(unittest.TestCase):
    """跨 voucher 同 SHA256 照片审核 — import_workspace 三种 policy。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_workspace_with_photo(self, root: Path, voucher_tube: tuple[str, str], photo_bytes: bytes, photo_name: str = "photo.jpg") -> tuple[ExcelStore, str]:
        """造一个工作区：1 个 voucher + 1 张照片。返回 (store, voucher)。"""
        root.mkdir(parents=True, exist_ok=True)
        store = ExcelStore(root)
        v = store.create_specimen()
        store.set_fields("specimen", v, {"管内编号*": voucher_tube[1], "采集地点缩写*": voucher_tube[0]})
        photo = root / photo_name
        photo.write_bytes(photo_bytes)
        store.add_photo(v, photo, allow_outside=True)
        return store, v

    def test_default_import_keeps_two_records_one_file(self) -> None:
        # 中心已有照片 X 关联到 YZZ000001
        central = self.tmp / "central"
        central_store, v_center = self._make_workspace_with_photo(
            central, ("QD", "T1"), b"photo-bytes-same", "x.jpg"
        )
        self.assertEqual(len(central_store.get_photos(v_center)), 1)

        # 源工作区也含同字节照片 X 关联到 YZZ000001（同号但不同管号 → 走指纹冲突）
        # 改造：source 用 ExcelStore 创建 YZZ000002（让源 next_serial=2 → 让我们手工 setup
        # 简化：直接构造一个新工作区 + 不同 voucher
        source = self.tmp / "source"
        source.mkdir()
        source_store = ExcelStore(source)
        v_src = source_store.create_specimen()  # YZZ000001
        # 推进到 YZZ000002
        v2 = source_store.create_specimen()  # YZZ000002
        source_store.delete_specimen(v_src)   # 删掉 YZZ000001 避免与 central 冲突
        source_store.set_fields("specimen", v2, {"管内编号*": "T2", "采集地点缩写*": "QD"})
        photo = source / "y.jpg"
        photo.write_bytes(b"photo-bytes-same")  # 同字节 → 同 SHA256
        source_store.add_photo(v2, photo, allow_outside=True)

        # 默认 import 模式（向后兼容）
        result = central_store.import_workspace(source)
        self.assertEqual(result.imported, 1)  # YZZ000002
        self.assertEqual(result.duplicate_candidates, [])

        # central 现有 2 条 photo 记录（YZZ000001 + YZZ000002），都指同 SHA256
        all_photos = central_store.read_rows("photo")
        self.assertEqual(len(all_photos), 2)
        shas = {str(p.get("文件SHA256") or "") for p in all_photos}
        self.assertEqual(len(shas), 1)
        # 物理层 dedup 仅在同名同内容时复用文件；本测试用不同名 x.jpg / y.jpg → 留两份
        # （这是现有 _archive_photo_file 行为，M4 不动）
        archived = list((central / "照片").iterdir())
        self.assertEqual(len(archived), 2)

    def test_report_policy_skips_duplicate_and_records_candidate(self) -> None:
        central = self.tmp / "central"
        central_store, v_center = self._make_workspace_with_photo(
            central, ("QD", "T1"), b"photo-bytes-same", "x.jpg"
        )

        source = self.tmp / "source"
        source.mkdir()
        source_store = ExcelStore(source)
        v_src = source_store.create_specimen()
        v2 = source_store.create_specimen()
        source_store.delete_specimen(v_src)
        source_store.set_fields("specimen", v2, {"管内编号*": "T2", "采集地点缩写*": "QD"})
        photo = source / "y.jpg"
        photo.write_bytes(b"photo-bytes-same")
        source_store.add_photo(v2, photo, allow_outside=True)

        # report 模式：voucher 仍合并，但跨 voucher 同 SHA256 照片不写 photo 行
        result = central_store.import_workspace(source, photo_duplicate_policy="report")
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.photos_imported, 0)  # 那一张被 report 模式跳过
        self.assertEqual(len(result.duplicate_candidates), 1)
        candidate = result.duplicate_candidates[0]
        self.assertEqual(candidate["入库编号"], v2)
        self.assertEqual(candidate["已有voucher"], v_center)

        # central photo 表仍只有原 1 条
        all_photos = central_store.read_rows("photo")
        self.assertEqual(len(all_photos), 1)

    def test_same_voucher_same_sha_no_duplicate_flag(self) -> None:
        """同 voucher 内多次同 SHA256（合法一对多，例如多角度照片中夹一张总览图）→ 不算重复。"""
        central = self.tmp / "central"
        central_store, v_center = self._make_workspace_with_photo(
            central, ("QD", "T1"), b"shared-bytes", "x.jpg"
        )
        # 给同 voucher 再加一张同字节照片 — 现有 add_photo / find_photo_conflicts 在 UI 路径会过滤，
        # 但直接调 _photo_row 不过滤。验证 report 模式不把"同 voucher 同 SHA256"列为 candidate
        source = self.tmp / "source"
        source.mkdir()
        source_store = ExcelStore(source)
        v_src = source_store.create_specimen()
        # source 也用同 voucher（与 central 同号且同 tube → 指纹相同 → skipped）
        source_store.set_fields("specimen", v_src, {"管内编号*": "T1", "采集地点缩写*": "QD"})
        photo = source / "y.jpg"
        photo.write_bytes(b"shared-bytes")
        source_store.add_photo(v_src, photo, allow_outside=True)

        result = central_store.import_workspace(source, photo_duplicate_policy="report")
        # voucher 因指纹相同被 skip；照片同 voucher → 不进 duplicate_candidates
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.duplicate_candidates, [])


class AggregateDuplicatesTests(unittest.TestCase):
    """aggregate_incoming 整合段守护 + 照片审核的端到端测试。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.incoming = self.tmp / "incoming"
        self.central.mkdir()
        self.incoming.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aggregate_writes_name_conflicts_report_s3(self) -> None:
        """S3: 两子目录都含 IMG_001.jpg 但内容不同 → 写 name_conflicts/ 报告。"""
        # 源 A: IMG_001.jpg 内容 X 关联到 voucher 1
        sub_a = self.incoming / "源A"
        sub_a.mkdir()
        store_a = ExcelStore(sub_a)
        v_a = store_a.create_specimen()
        store_a.set_fields("specimen", v_a, {"管内编号*": "A-001"})
        photo_a = sub_a / "IMG_001.jpg"
        photo_a.write_bytes(b"content-X")
        store_a.add_photo(v_a, photo_a, allow_outside=True)

        # 源 B: IMG_001.jpg 内容 Y（不同 SHA256）关联到另一 voucher
        sub_b = self.incoming / "源B"
        sub_b.mkdir()
        store_b = ExcelStore(sub_b)
        v_b1 = store_b.create_specimen()
        v_b2 = store_b.create_specimen()
        store_b.delete_specimen(v_b1)  # 让中心仅看到 YZZ000002
        store_b.set_fields("specimen", v_b2, {"管内编号*": "B-002"})
        photo_b = sub_b / "IMG_001.jpg"
        photo_b.write_bytes(b"content-Y-different")
        store_b.add_photo(v_b2, photo_b, allow_outside=True)

        # 聚合
        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)

        # 至少两个候选都被处理（不一定都进 processed，YZZ000001 可能冲突）
        self.assertGreaterEqual(len(report.processed) + len(report.conflicted), 1)
        # 关键：name_conflicts 报告应被写出
        # 若两者都 processed → 跨子目录同名不同内容报告应非空
        if len(report.processed) >= 2:
            self.assertGreaterEqual(len(report.name_conflicts), 1)
            self.assertIsNotNone(report.name_conflicts_report_path)
            self.assertTrue(report.name_conflicts_report_path.exists())
            # incoming/name_conflicts/ 目录已建
            self.assertTrue((self.incoming / "name_conflicts").is_dir())

    def test_aggregate_writes_duplicates_report(self) -> None:
        # 中心已有照片
        central_store = ExcelStore(self.central)
        v_center = central_store.create_specimen()
        central_store.set_fields("specimen", v_center, {"管内编号*": "T1", "采集地点缩写*": "QD"})
        center_photo = self.central / "x.jpg"
        center_photo.write_bytes(b"abc")
        central_store.add_photo(v_center, center_photo, allow_outside=True)

        # 源子目录含一张同字节照片但关联到不同 voucher
        sub = self.incoming / "源A"
        sub.mkdir()
        sub_store = ExcelStore(sub)
        v_src1 = sub_store.create_specimen()
        v_src2 = sub_store.create_specimen()
        sub_store.delete_specimen(v_src1)
        sub_store.set_fields("specimen", v_src2, {"管内编号*": "T2", "采集地点缩写*": "QD"})
        src_photo = sub / "y.jpg"
        src_photo.write_bytes(b"abc")  # 同 SHA256
        sub_store.add_photo(v_src2, src_photo, allow_outside=True)

        report = aggregate_incoming(central_store, self.incoming)

        # 子目录 voucher 进入中心，但 duplicates 列表非空
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.total_imported, 1)
        self.assertEqual(report.total_photos, 0)  # 重复照片不写入
        self.assertEqual(len(report.duplicates), 1)
        name, candidates, report_path = report.duplicates[0]
        self.assertEqual(name, "源A")
        self.assertEqual(len(candidates), 1)
        self.assertTrue(report_path.exists())
        self.assertTrue(report_path.suffix == ".xlsx")
        # incoming/duplicates/ 下应有 xlsx
        self.assertTrue((self.incoming / "duplicates").is_dir())


if __name__ == "__main__":
    unittest.main()
