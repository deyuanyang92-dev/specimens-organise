"""M2 多人协作 — 录入员独立编号系列（前缀分人）测试。

覆盖的核心场景：
- 首次给某录入员创建独立系列：accession_series 多一条，next_counter=1
- 同录入员二次取号：复用同一系列，next_counter 累加
- 前缀格式校验：拒绝中文 / 空 / 非法字符
- 前缀冲突：拒绝两个录入员用同一前缀
- 两个录入员独立预留：编号无交集
- 与 YZZ 单系列共存：YZZ 计数器不被影响

不依赖 PyQt5（M2 核心是 store 层）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.accession_series import series_prefix_of
from specimen_app.excel_store import ExcelStore


class AssigneeSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_call_creates_new_series(self) -> None:
        store = ExcelStore(self.tmp)
        name = store.ensure_assignee_series("张三", prefix="ZS-")
        self.assertEqual(name, "张三_系列")
        # 系列已落到 accession_series 数组
        series_cfg = store._get_series_config(name)
        self.assertIsNotNone(series_cfg)
        self.assertEqual(series_cfg.prefix, "ZS-")
        self.assertEqual(series_cfg.next_counter, 1)

    def test_second_call_reuses_existing_series(self) -> None:
        store = ExcelStore(self.tmp)
        store.ensure_assignee_series("张三", prefix="ZS-")
        # 第一次取 5 个
        first = store.batch_reserve_vouchers(5, series_name="张三_系列")
        self.assertEqual(len(first), 5)
        self.assertTrue(all(v.startswith("ZS-") for v in first))

        # 复用同系列再取 3 个 — 不应新建系列、不应重置计数器
        same_name = store.ensure_assignee_series("张三", prefix="ZS-")
        self.assertEqual(same_name, "张三_系列")
        second = store.batch_reserve_vouchers(3, series_name="张三_系列")
        self.assertEqual(len(second), 3)
        self.assertEqual(set(first) & set(second), set())  # 无交集
        # 共 8 个编号 + accession_series 只有 1 条
        self.assertEqual(len(store.config.get("accession_series", [])), 1)

    def test_invalid_prefix_rejected(self) -> None:
        store = ExcelStore(self.tmp)
        # 空前缀
        with self.assertRaises(ValueError):
            store.ensure_assignee_series("张三", prefix="")
        # 中文前缀
        with self.assertRaises(ValueError):
            store.ensure_assignee_series("张三", prefix="张三-")
        # 含空格
        with self.assertRaises(ValueError):
            store.ensure_assignee_series("张三", prefix="ZS 1-")
        # 空 assignee
        with self.assertRaises(ValueError):
            store.ensure_assignee_series("", prefix="ZS-")

    def test_prefix_collision_rejected(self) -> None:
        store = ExcelStore(self.tmp)
        store.ensure_assignee_series("张三", prefix="ZS-")
        # 李四想用同一前缀 — 拒绝（防止两人都生成 ZS-000001 撞号）
        with self.assertRaises(ValueError) as ctx:
            store.ensure_assignee_series("李四", prefix="ZS-")
        self.assertIn("已被系列", str(ctx.exception))

    def test_two_assignees_no_voucher_collision(self) -> None:
        store = ExcelStore(self.tmp)
        store.ensure_assignee_series("张三", prefix="ZS-")
        store.ensure_assignee_series("李四", prefix="LS-")

        zhang = store.batch_reserve_vouchers(50, series_name="张三_系列")
        li = store.batch_reserve_vouchers(50, series_name="李四_系列")

        self.assertEqual(len(set(zhang) & set(li)), 0)
        self.assertTrue(all(series_prefix_of(v) == "ZS" for v in zhang))
        self.assertTrue(all(series_prefix_of(v) == "LS" for v in li))
        # 两个系列计数器都到了 51
        self.assertEqual(store._get_series_config("张三_系列").next_counter, 51)
        self.assertEqual(store._get_series_config("李四_系列").next_counter, 51)

    def test_yzz_default_series_unaffected(self) -> None:
        store = ExcelStore(self.tmp)
        # 先用 YZZ 单系列预留 10 个
        yzz_before = store.batch_reserve_vouchers(10)
        self.assertEqual(yzz_before[0], "YZZ000001")
        self.assertEqual(yzz_before[-1], "YZZ000010")
        reserved_through = store.config.get("reserved_through_serial")

        # 再给张三建独立前缀系列 + 取号
        store.ensure_assignee_series("张三", prefix="ZS-")
        zhang = store.batch_reserve_vouchers(5, series_name="张三_系列")
        self.assertTrue(all(v.startswith("ZS-") for v in zhang))

        # YZZ 的 reserved_through_serial 没变（不应被独立系列污染）
        self.assertEqual(store.config.get("reserved_through_serial"), reserved_through)

        # 再用 YZZ 取号 — 接 YZZ000011
        yzz_after = store.batch_reserve_vouchers(3)
        self.assertEqual(yzz_after[0], "YZZ000011")


if __name__ == "__main__":
    unittest.main()
