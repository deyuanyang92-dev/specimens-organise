"""多人协作 — 收件箱聚合测试（M1 含 P1 等价的降级模式）。

覆盖的核心场景：
- 空 incoming：不创快照、空报告
- 基础合并：含 manifest 的子目录被合并到中心、移动到 processed/
- 降级模式：缺 manifest.json 也能合并（P1 等价）
- 幂等：同一源数据二次聚合不重复写入（指纹比对走 skipped）
- 冲突分流：同 voucher 不同内容的源 → conflicts/，原报告随行
- 跨合并员锁：processing_* 前缀目录被跳过
- 错误分流：源数据损坏 → errors/ 并写 error.log

不依赖 PyQt5（M1 核心逻辑是纯 stdlib + openpyxl 范围内的事）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.excel_store import ExcelStore
from specimen_app.models import AggregatePreview, AggregateReport
from specimen_app.server_sync import (
    PROCESSING_PREFIX,
    aggregate_incoming,
    preview_aggregate,
)


class ServerSyncAggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.incoming = self.tmp / "incoming"
        self.central.mkdir()
        self.incoming.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ─────────────────────────── 辅助构造源子目录 ───────────────────────────

    def _make_source(
        self,
        name: str,
        voucher_count: int = 1,
        tube_prefix: str = "T",
        with_manifest: bool = True,
    ) -> Path:
        """在 incoming/ 下造一个含 `数据/` 的源子目录，返回路径。"""
        source_dir = self.incoming / name
        source_dir.mkdir()
        store = ExcelStore(source_dir)
        for i in range(voucher_count):
            voucher = store.create_specimen()
            store.set_fields(
                "specimen",
                voucher,
                {
                    "管内编号*": f"{tube_prefix}-LSD-SC{i+1:03d}-1-R-250923",
                    "采集地点缩写*": "QD",
                },
            )
        if with_manifest:
            manifest = {
                "assignee": name,
                "packed_at": "2026-05-16T14:30:00",
                "software_version": "test",
                "data_schema_version": "1.1.2",
            }
            (source_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
        return source_dir

    # ────────────────────────────── 场景 1：空 ──────────────────────────────

    def test_empty_incoming_returns_empty_report(self) -> None:
        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)

        self.assertIsInstance(report, AggregateReport)
        self.assertEqual(report.processed, [])
        self.assertEqual(report.conflicted, [])
        self.assertEqual(report.errored, [])
        self.assertEqual(report.total_imported, 0)
        self.assertEqual(report.total_photos, 0)
        self.assertIsNone(report.snapshot_path)

    # ─────────────────────────── 场景 2：基础合并 ────────────────────────────

    def test_basic_two_sources_merge_into_central(self) -> None:
        # 源 A 含 2 个 voucher（YZZ000001、YZZ000002）
        self._make_source("张三_20260516_1430", voucher_count=2, tube_prefix="A")
        target = ExcelStore(self.central)

        report = aggregate_incoming(target, self.incoming)

        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.processed[0], "张三_20260516_1430")
        self.assertEqual(report.total_imported, 2)
        self.assertEqual(len(report.conflicted), 0)
        self.assertEqual(len(report.errored), 0)
        self.assertIsNotNone(report.snapshot_path)
        # 源子目录已被 rename 到 processed/
        self.assertFalse((self.incoming / "张三_20260516_1430").exists())
        self.assertTrue((self.incoming / "processed" / "张三_20260516_1430").exists())
        # 中心机已有这两条
        self.assertEqual(set(target.list_vouchers()), {"YZZ000001", "YZZ000002"})

    # ────────────────────────── 场景 3：降级模式（P1） ──────────────────────────

    def test_no_manifest_downgrade_still_merges(self) -> None:
        # 不写 manifest.json — 等同于 P1 极简方案：任意 含 数据/ 子目录 的文件夹都能聚合
        self._make_source("anonymous_workspace", voucher_count=1, with_manifest=False)
        target = ExcelStore(self.central)

        report = aggregate_incoming(target, self.incoming)

        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.processed[0], "anonymous_workspace")
        self.assertEqual(report.total_imported, 1)
        self.assertTrue((self.incoming / "processed" / "anonymous_workspace").exists())

    # ─────────────────────────── 场景 4：幂等 ────────────────────────────────

    def test_idempotent_second_run_no_new_imports(self) -> None:
        self._make_source("张三_first", voucher_count=2)
        target = ExcelStore(self.central)
        first = aggregate_incoming(target, self.incoming)
        self.assertEqual(first.total_imported, 2)

        # 第二次跑：incoming 下没有新候选子目录（都已 processed/）
        second = aggregate_incoming(target, self.incoming)
        self.assertEqual(second.processed, [])
        self.assertEqual(second.total_imported, 0)

        # 进一步：再造一个内容完全一样的源 → 指纹相同 → skipped
        same = self._make_source("张三_second_same", voucher_count=2)
        third = aggregate_incoming(target, self.incoming)
        # 注：第二个源的 voucher 也叫 YZZ000001/YZZ000002，但 tube 用 "T"，与第一个源同。
        # 实际上 _make_source 默认 tube_prefix="T"，但 first 是 "T" 也 same 是 "T"，应同指纹 → skipped
        self.assertEqual(third.total_imported, 0)
        self.assertEqual(len(third.processed), 1)
        self.assertEqual(target.list_vouchers(), ["YZZ000001", "YZZ000002"])

    # ─────────────────────── 场景 5：冲突分流 ──────────────────────────

    def test_conflict_routes_to_conflicts_dir(self) -> None:
        # 中心机先有一条 YZZ000001（管内编号 = 中心的 tube）
        target = ExcelStore(self.central)
        v = target.create_specimen()
        target.set_fields("specimen", v, {"管内编号*": "CENTER-001"})

        # 源也是 YZZ000001 但内容不同 → 指纹冲突 → ImportConflictError
        self._make_source("conflicting_source", voucher_count=1, tube_prefix="SRC")

        report = aggregate_incoming(target, self.incoming)

        self.assertEqual(len(report.processed), 0)
        self.assertEqual(len(report.conflicted), 1)
        name, message, report_path = report.conflicted[0]
        self.assertEqual(name, "conflicting_source")
        self.assertIn("冲突", message)
        self.assertIsNotNone(report_path)
        # 源已被 rename 到 conflicts/
        self.assertTrue((self.incoming / "conflicts" / "conflicting_source").exists())
        # 中心机数据未变
        self.assertEqual(target.list_vouchers(), [v])
        # 冲突报告 xlsx 也被复制到 conflicts/<name>/ 下
        archived = self.incoming / "conflicts" / "conflicting_source"
        self.assertTrue(any(p.suffix == ".xlsx" and "冲突报告" in p.name for p in archived.iterdir()))

    # ──────────────────── 场景 6：跨合并员锁（processing_* 跳过）────────────────────

    def test_processing_locked_subdir_is_skipped(self) -> None:
        # 模拟另一合并员持锁：手动构造 processing_xxx/ 目录
        locked = self.incoming / (PROCESSING_PREFIX + "张三_being_processed")
        locked.mkdir()
        (locked / "数据").mkdir()
        # 同时有一个正常子目录
        self._make_source("张三_normal", voucher_count=1)

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)

        # 只处理正常子目录；processing_* 完全无视（既不算 processed 也不算 errored）
        self.assertEqual(report.processed, ["张三_normal"])
        self.assertEqual(len(report.errored), 0)
        # processing_* 目录原样留在 incoming/ 下
        self.assertTrue(locked.exists())

    # ───────────────────────── 场景 7：错误分流 ──────────────────────────────

    def test_corrupt_source_routes_to_errors_dir(self) -> None:
        # 造一个含 数据/ 但 标本信息.xlsx 损坏的源
        broken = self.incoming / "broken_source"
        broken.mkdir()
        (broken / "数据").mkdir()
        (broken / "数据" / "标本信息.xlsx").write_bytes(b"not a real xlsx file")

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)

        self.assertEqual(len(report.processed), 0)
        self.assertEqual(len(report.errored), 1)
        name, message = report.errored[0]
        self.assertEqual(name, "broken_source")
        self.assertTrue(message)  # 非空错误描述
        # 源被 mv 到 errors/
        self.assertTrue((self.incoming / "errors" / "broken_source").exists())
        # error.log 已写
        self.assertTrue((self.incoming / "errors" / "broken_source" / "error.log").exists())

    # ─────────────────────── 场景 9：S1 嵌套目录递归扫描 ──────────────────────

    def test_nested_workspace_two_levels_deep(self) -> None:
        """incoming/ydy/工作区A/数据/ 嵌套两层应被识别（S1）。"""
        ydy_dir = self.incoming / "ydy"
        ydy_dir.mkdir()
        nested_ws = ydy_dir / "工作区A"
        nested_ws.mkdir()
        store = ExcelStore(nested_ws)
        v = store.create_specimen()
        store.set_fields("specimen", v, {"管内编号*": "A-001", "采集地点缩写*": "QD"})

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)
        self.assertEqual(len(report.processed), 1)
        # 扁平化名字含 ydy__ 前缀以保留来源信息
        self.assertTrue(any("ydy" in p and "工作区A" in p for p in report.processed))
        self.assertEqual(report.total_imported, 1)

    def test_nested_workspaces_two_assignees_each_in_subfolder(self) -> None:
        """incoming/ydy/工作区A/数据/ + incoming/yss/工作区B/数据/ 同时嵌套两人。

        两人各自独立工作区都从 YZZ000001 起 → 第二个进 conflicts/（指纹不同），
        但**两个候选都被识别和处理**才是 S1 扫描正确性的关键断言。
        """
        for name in ("ydy", "yss"):
            sub = self.incoming / name / f"{name}工作区"
            sub.mkdir(parents=True)
            s = ExcelStore(sub)
            v = s.create_specimen()
            s.set_fields("specimen", v, {"管内编号*": f"{name}-001", "采集地点缩写*": "QD"})

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)
        # 关键断言：两个嵌套候选都被发现（无论是 processed 还是 conflicted）
        total_handled = len(report.processed) + len(report.conflicted) + len(report.errored)
        self.assertEqual(total_handled, 2)

    def test_three_level_nesting_supported(self) -> None:
        """incoming/某项目/某人/工作区/数据/ — 三层嵌套上限内仍能扫到。"""
        deep = self.incoming / "项目X" / "录入员Y" / "工作区Z"
        deep.mkdir(parents=True)
        s = ExcelStore(deep)
        s.create_specimen()

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)
        self.assertEqual(len(report.processed), 1)

    def test_workspace_not_descended_into_after_match(self) -> None:
        """命中"含 `数据/` 子目录"后不再深入，防止重复合并嵌套工作区。"""
        # 主工作区
        main = self.incoming / "主工作区"
        main.mkdir()
        main_store = ExcelStore(main)
        main_store.create_specimen()
        # 在主工作区**内部**再造一个嵌套工作区（合法但不应被扫到第二次）
        inner = main / "意外嵌套" / "另一工作区"
        inner.mkdir(parents=True)
        inner_store = ExcelStore(inner)
        inner_store.create_specimen()

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)
        # 主工作区被识别一次；其内部的嵌套工作区不再被扫描（避免对已被锁住的目录二次操作）
        self.assertEqual(len(report.processed), 1)

    # ─────────────────────── 场景 8：默认分流目录被跳过 ──────────────────────

    def test_reserved_dirs_are_not_treated_as_candidates(self) -> None:
        # 已存在的 processed/conflicts/errors/duplicates 子目录不被当作待聚合候选
        for name in ("processed", "conflicts", "errors", "duplicates"):
            sub = self.incoming / name
            sub.mkdir()
            (sub / "数据").mkdir()  # 有 数据/ 也不行

        target = ExcelStore(self.central)
        report = aggregate_incoming(target, self.incoming)
        self.assertEqual(report.processed, [])
        self.assertEqual(report.total_imported, 0)
        # 既然没有候选 → 不应创快照
        self.assertIsNone(report.snapshot_path)


class PreviewAggregateTests(unittest.TestCase):
    """S7: preview_aggregate 只读 dry-run 测试。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.central = self.tmp / "central"
        self.incoming = self.tmp / "incoming"
        self.central.mkdir()
        self.incoming.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_incoming_returns_empty_preview(self) -> None:
        store = ExcelStore(self.central)
        preview = preview_aggregate(store, self.incoming)
        self.assertIsInstance(preview, AggregatePreview)
        self.assertEqual(preview.total_candidates, 0)
        self.assertEqual(preview.predicted_new_vouchers, 0)

    def test_preview_predicts_new_vouchers(self) -> None:
        # 源含 2 voucher，中心机空 → 预测 2 新增
        sub = self.incoming / "源A"
        sub.mkdir()
        s = ExcelStore(sub)
        s.create_specimen()
        s.create_specimen()
        store = ExcelStore(self.central)
        preview = preview_aggregate(store, self.incoming)
        self.assertEqual(preview.total_candidates, 1)
        self.assertEqual(preview.predicted_new_vouchers, 2)
        self.assertEqual(preview.predicted_conflicts, 0)
        self.assertEqual(preview.predicted_skipped_vouchers, 0)

    def test_preview_predicts_conflict(self) -> None:
        # 中心已有 YZZ000001（含 tube CENTER），源也含 YZZ000001 但 tube 不同 → 预测冲突
        central_store = ExcelStore(self.central)
        v = central_store.create_specimen()
        central_store.set_fields("specimen", v, {"管内编号*": "CENTER"})

        sub = self.incoming / "源B"
        sub.mkdir()
        s = ExcelStore(sub)
        v2 = s.create_specimen()
        s.set_fields("specimen", v2, {"管内编号*": "DIFFERENT"})

        preview = preview_aggregate(central_store, self.incoming)
        self.assertEqual(preview.predicted_conflicts, 1)

    def test_preview_does_not_modify_workspace(self) -> None:
        """关键不变量：preview 完全只读。不创快照、不动 incoming、不动中心。"""
        sub = self.incoming / "源C"
        sub.mkdir()
        s = ExcelStore(sub)
        s.create_specimen()
        store = ExcelStore(self.central)
        before_central_vouchers = list(store.list_vouchers())
        before_snapshots = list((self.central / "数据" / "数据版本").glob("v*")) if (self.central / "数据" / "数据版本").exists() else []

        preview_aggregate(store, self.incoming)

        after_central_vouchers = list(store.list_vouchers())
        after_snapshots = list((self.central / "数据" / "数据版本").glob("v*")) if (self.central / "数据" / "数据版本").exists() else []
        self.assertEqual(before_central_vouchers, after_central_vouchers)
        self.assertEqual(len(before_snapshots), len(after_snapshots))
        # 源子目录原样
        self.assertTrue(sub.exists())
        self.assertFalse((self.incoming / "processed").exists())

    def test_preview_detects_segment_violation(self) -> None:
        """manifest 含 voucher_range，源 voucher 超出 → segment_violation 预测。"""
        sub = self.incoming / "源D"
        sub.mkdir()
        s = ExcelStore(sub)
        s.create_specimen()  # YZZ000001
        # 写 manifest 声明 range=[ZS-000001..ZS-000050]（与 YZZ 前缀不符 → 一定超界）
        (sub / "manifest.json").write_text(
            json.dumps({"voucher_range": ["ZS-000001", "ZS-000050"]}),
            encoding="utf-8",
        )
        store = ExcelStore(self.central)
        preview = preview_aggregate(store, self.incoming)
        self.assertEqual(preview.total_candidates, 1)
        outcome = preview.candidates[0][1]
        self.assertEqual(outcome, "segment_violation")


if __name__ == "__main__":
    unittest.main()
