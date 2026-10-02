"""GUI 线程卡死探针：主线程停摆超过阈值 → 把主线程当时的调用栈写进崩溃日志目录（context=gui_stall）。

用户多次反馈"一打开就未响应"却无法定位。有了它，下次再卡，日志里直接是卡在哪一行。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication(["wd"])
    return _APP


def _busy_wait(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pass


class GuiStallWatchdogTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stall_dumps_main_thread_stack_once(self):
        from specimen_app import ui_watchdog

        dumps: list[str] = []
        wd = ui_watchdog.GuiStallWatchdog(threshold_seconds=0.3, poll_seconds=0.05, sink=dumps.append)
        wd.start()
        try:
            for _ in range(5):
                _app().processEvents()
                time.sleep(0.05)
            _busy_wait(0.8)  # 主线程停摆 0.8 s
            _app().processEvents()
            time.sleep(0.2)
        finally:
            wd.stop()
        self.assertEqual(len(dumps), 1, dumps)
        self.assertIn("_busy_wait", dumps[0])
        self.assertIn("GUI 线程停摆", dumps[0])

    def test_no_dump_when_responsive(self):
        from specimen_app import ui_watchdog

        dumps: list[str] = []
        wd = ui_watchdog.GuiStallWatchdog(threshold_seconds=0.3, poll_seconds=0.05, sink=dumps.append)
        wd.start()
        try:
            for _ in range(10):
                _app().processEvents()
                time.sleep(0.05)
        finally:
            wd.stop()
        self.assertEqual(dumps, [])

    def test_default_sink_writes_crash_log_file(self):
        from specimen_app import crash_log, ui_watchdog

        with patch.object(crash_log, "_config_dir", return_value=self.tmp):
            wd = ui_watchdog.GuiStallWatchdog(threshold_seconds=0.3, poll_seconds=0.05)
            wd.start()
            try:
                _app().processEvents()
                time.sleep(0.1)
                _busy_wait(0.8)
                _app().processEvents()
                time.sleep(0.2)
            finally:
                wd.stop()
        files = list(self.tmp.rglob("*.log")) + list(self.tmp.rglob("*.txt"))
        self.assertTrue(files, "应写出一份 gui_stall 日志")
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
        self.assertIn("gui_stall", text)
        self.assertIn("_busy_wait", text)


if __name__ == "__main__":
    unittest.main()
