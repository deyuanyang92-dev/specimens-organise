"""scripts/triage_log.py：把用户发来的错误日志变成可执行的排查起点（v0.10.44）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import triage_log  # noqa: E402

LOGS = Path(__file__).parent / "fixtures" / "logs"


class ParseTests(unittest.TestCase):
    def test_crash_log(self):
        info = triage_log.parse_log((LOGS / "crash_20261003_151423.log").read_text(encoding="utf-8"))
        self.assertEqual(info.kind, "crash")
        self.assertEqual(info.version, "0.10.37")
        self.assertEqual(info.context, "main_thread")
        self.assertEqual(info.exception_type, "PermissionError")
        self.assertIn("标本信息.xlsx", info.exception_message)
        app = info.app_frames
        self.assertEqual(app[0].file, "specimen_app/ui.py")
        self.assertEqual(app[0].func, "_on_store_op_done")
        self.assertEqual(app[-1].file, "specimen_app/table_backend.py")
        self.assertTrue(all("openpyxl" not in f.file for f in app))

    def test_faulthandler_boot_fault(self):
        info = triage_log.parse_log((LOGS / "boot_fault.log").read_text(encoding="utf-8"))
        self.assertEqual(info.kind, "native_crash")
        self.assertIn("access violation", info.exception_message)
        self.assertEqual(info.app_frames[0].file, "specimen_app/image_cache.py")
        self.assertEqual(info.app_frames[0].func, "_decode")

    def test_gui_stall(self):
        info = triage_log.parse_log((LOGS / "gui_stall_20261004_100000.log").read_text(encoding="utf-8"))
        self.assertEqual(info.kind, "gui_stall")
        self.assertEqual(info.version, "0.10.44")
        # 卡住的位置 = 最后一帧
        self.assertEqual(info.app_frames[-1].func, "read_rows")


class LocateTests(unittest.TestCase):
    def test_frames_mapped_to_current_source_by_function_name(self):
        info = triage_log.parse_log((LOGS / "crash_20261003_151423.log").read_text(encoding="utf-8"))
        located = triage_log.locate_in_source(info.app_frames, ROOT)
        by_func = {loc.frame.func: loc for loc in located}
        # 行号随版本漂移，按函数名找当前位置
        self.assertIsNotNone(by_func["_update_task_indicator"].current_line)
        self.assertIsNotNone(by_func["xlsx_read_rows"].current_line)
        self.assertIsNone(by_func["<genexpr>"].current_line)
        # table_backend 里 read_rows 有 Protocol 声明和 XlsxBackend 实现两个：应取离日志行（226）近的实现
        tb = [loc for loc in located if loc.frame.file == "specimen_app/table_backend.py" and loc.frame.func == "read_rows"][0]
        self.assertGreater(tb.current_line, 150)

    def test_version_compare(self):
        self.assertTrue(triage_log.is_older("0.10.37", "0.10.44"))
        self.assertFalse(triage_log.is_older("0.10.44", "0.10.44"))
        self.assertFalse(triage_log.is_older("", "0.10.44"))

    def test_report_mentions_already_fixed_when_older(self):
        text = (LOGS / "crash_20261003_151423.log").read_text(encoding="utf-8")
        report = triage_log.build_report(text, ROOT)
        self.assertIn("v0.10.37", report)
        self.assertIn("旧版本", report)
        self.assertIn("_update_task_indicator", report)


if __name__ == "__main__":
    unittest.main()
