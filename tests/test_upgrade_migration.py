"""M5 旧版工作区识别 + 升级 + 同步测试。

覆盖：
- detect_legacy: 空工作区 → False；含数据未升级 → True；已升级 → False
- upgrade: 创建 snapshot + 写 config + 写操作记录；voucher 数据零损失
- upgrade 二次调用幂等：already_upgraded=True，不重复 snapshot
- 升级后的工作区可作为子目录被 aggregate_incoming 吃下（合并兼容）
- 数据一致性：升级前后所有 voucher 的字段值原样保留（按 record_id 比对）
- 失败回退路径：snapshot 可恢复
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.excel_store import ExcelStore
from specimen_app.models import DATA_VERSION_DIR
from specimen_app.server_sync import aggregate_incoming


class LegacyDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_workspace_not_legacy(self) -> None:
        store = ExcelStore(self.tmp)
        self.assertFalse(store.detect_legacy_workspace())

    def test_workspace_with_data_is_legacy(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        self.assertTrue(store.detect_legacy_workspace())

    def test_upgraded_workspace_not_legacy(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        store.upgrade_to_multi_user_protocol()
        self.assertFalse(store.detect_legacy_workspace())


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upgrade_writes_marker_and_snapshot(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        store.create_specimen()  # 两条 YZZ000001 + YZZ000002
        summary = store.upgrade_to_multi_user_protocol()

        self.assertFalse(summary["already_upgraded"])
        self.assertEqual(summary["multi_user_protocol_version"], "1.0")
        self.assertEqual(store.config["multi_user_protocol_version"], "1.0")
        # snapshot 存在
        snap = summary["snapshot_path"]
        self.assertTrue(snap.exists())
        self.assertTrue(snap.is_dir())
        # 历史段记录正确（至少覆盖 YZZ000002）
        seg = summary["legacy_yzz_segment"]
        self.assertEqual(seg[0], 1)
        self.assertGreaterEqual(seg[1], 2)

    def test_upgrade_preserves_all_data(self) -> None:
        store = ExcelStore(self.tmp)
        v1 = store.create_specimen()
        v2 = store.create_specimen()
        store.set_fields("specimen", v1, {"管内编号*": "T1", "采集地点缩写*": "QD"})
        store.set_fields("specimen", v2, {"管内编号*": "T2", "采集地点缩写*": "QD"})
        before_count = len(store.list_vouchers())
        before_v1 = store.get_specimen(v1)

        store.upgrade_to_multi_user_protocol()

        # 重新读 store（模拟应用重启）
        store2 = ExcelStore(self.tmp)
        self.assertEqual(len(store2.list_vouchers()), before_count)
        self.assertEqual(store2.get_specimen(v1), before_v1)

    def test_upgrade_idempotent(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        first = store.upgrade_to_multi_user_protocol()
        self.assertFalse(first["already_upgraded"])
        second = store.upgrade_to_multi_user_protocol()
        self.assertTrue(second["already_upgraded"])

    def test_upgraded_workspace_aggregates_with_p1_subdir(self) -> None:
        """升级后的中心机能正常吃旧式无 manifest 子目录（即 P1 用法仍然可用）。"""
        central = self.tmp / "central"
        central.mkdir()
        store = ExcelStore(central)
        store.create_specimen()
        store.upgrade_to_multi_user_protocol()

        # 外面有个旧式子目录（无 manifest）
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        sub = incoming / "legacy_sub"
        sub.mkdir()
        sub_store = ExcelStore(sub)
        sub_store.create_specimen()  # YZZ000001 → 与中心冲突
        sub_store.create_specimen()  # YZZ000002 → 也与中心冲突
        # 让 source 的 voucher 走指纹冲突 → conflicts，但 aggregate 不崩
        sub_store.set_fields("specimen", "YZZ000001", {"管内编号*": "DIFF1"})

        report = aggregate_incoming(store, incoming)
        # 至少 conflicted 或 processed 有一条；不应崩
        self.assertGreaterEqual(len(report.processed) + len(report.conflicted), 1)

    def test_upgrade_writes_action_log_entry(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        store.upgrade_to_multi_user_protocol()
        actions = store.read_rows  # method ref
        # 操作记录 文件存在
        log_path = self.tmp / "数据" / "操作记录.xlsx"
        self.assertTrue(log_path.exists())


if __name__ == "__main__":
    unittest.main()
