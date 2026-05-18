"""E1 crash_log 模块测试。

不依赖 PyQt5（crash_log 是纯 stdlib）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


# 测试需要 patch app_config_dir 指向临时目录；先 import 模块
from specimen_app import crash_log


class CrashLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # patch app_config_dir 让 crash_log 写到 tmp
        self._patcher = mock.patch.object(crash_log, "_config_dir", return_value=self.tmp)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ─────────────────── write_crash_log ───────────────────

    def test_write_crash_log_creates_file(self) -> None:
        try:
            raise ValueError("test error")
        except ValueError:
            exc_type, exc_value, tb = sys.exc_info()
        log_path = crash_log.write_crash_log(exc_type, exc_value, tb)
        self.assertIsNotNone(log_path)
        self.assertTrue(log_path.exists())
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("specimen-organise crash report", text)
        self.assertIn("ValueError", text)
        self.assertIn("test error", text)
        self.assertIn("Time:", text)
        self.assertIn("Platform:", text)

    def test_write_crash_log_includes_context(self) -> None:
        try:
            raise RuntimeError("with context")
        except RuntimeError:
            exc_type, exc_value, tb = sys.exc_info()
        log_path = crash_log.write_crash_log(exc_type, exc_value, tb, context_note="my_thread")
        self.assertIsNotNone(log_path)
        self.assertIn("Context:  my_thread", log_path.read_text(encoding="utf-8"))

    def test_trim_old_crash_logs(self) -> None:
        # 写 25 个伪 log，保留 20 个
        for i in range(25):
            (self.tmp / f"crash_{i:03d}.log").write_text(str(i), encoding="utf-8")
            # 让 mtime 严格递增（同秒会乱序）
            os.utime(self.tmp / f"crash_{i:03d}.log", (i, i))
        crash_log._trim_old_crash_logs(self.tmp, keep=20)
        remaining = list(self.tmp.glob("crash_*.log"))
        self.assertEqual(len(remaining), 20)
        # 应保留 mtime 最大的 20 个
        names = sorted(p.name for p in remaining)
        self.assertEqual(names[0], "crash_005.log")
        self.assertEqual(names[-1], "crash_024.log")

    # ─────────────────── 启动检测 ───────────────────

    def test_first_run_no_marker_returns_false(self) -> None:
        # marker 不存在 → 视为"上次未 clean exit"
        self.assertFalse(crash_log.mark_app_started())

    def test_after_clean_exit_returns_true(self) -> None:
        crash_log.mark_app_exiting_clean()
        # 这一次启动检测到 marker 存在 → True；并自动删除 marker
        self.assertTrue(crash_log.mark_app_started())
        # 再来一次 → marker 已被删 → False
        self.assertFalse(crash_log.mark_app_started())

    def test_mark_clean_creates_marker_file(self) -> None:
        crash_log.mark_app_exiting_clean()
        marker = self.tmp / "last_exit_clean"
        self.assertTrue(marker.exists())

    # ─────────────────── install_excepthook ───────────────────

    def test_excepthook_writes_log_on_unhandled(self) -> None:
        original_hook = sys.excepthook
        try:
            crash_log.install_excepthook()
            self.assertIsNot(sys.excepthook, original_hook)
            try:
                raise IndexError("hook test")
            except IndexError:
                exc_type, exc_value, tb = sys.exc_info()
            # 调用 hook 直接（模拟未捕异常被 Python 调度到 sys.excepthook）
            sys.excepthook(exc_type, exc_value, tb)
        finally:
            sys.excepthook = original_hook
        logs = list(self.tmp.glob("crash_*.log"))
        self.assertGreaterEqual(len(logs), 1)
        text = logs[-1].read_text(encoding="utf-8")
        self.assertIn("IndexError", text)
        self.assertIn("hook test", text)

    def test_thread_excepthook_writes_log(self) -> None:
        # threading.excepthook 在 Py3.8+ 才有；老版本跳过
        if not hasattr(threading, "excepthook"):
            self.skipTest("threading.excepthook 需要 Python 3.8+")
        original_hook = threading.excepthook
        original_sys_hook = sys.excepthook
        try:
            crash_log.install_excepthook()
            # 子线程抛异常 → threading.excepthook 触发 → 写 log
            def _crash():
                raise KeyError("thread test")

            t = threading.Thread(target=_crash, name="testworker")
            t.start()
            t.join()
        finally:
            threading.excepthook = original_hook
            sys.excepthook = original_sys_hook
        logs = list(self.tmp.glob("crash_*.log"))
        self.assertGreaterEqual(len(logs), 1)
        any_match = any("KeyError" in p.read_text(encoding="utf-8") for p in logs)
        self.assertTrue(any_match)

    # ─────────────────── list_recent_crash_logs ───────────────────

    def test_list_recent_crash_logs_returns_sorted_newest_first(self) -> None:
        for i, name in enumerate(["crash_001.log", "crash_002.log", "crash_003.log"]):
            (self.tmp / name).write_text("x", encoding="utf-8")
            os.utime(self.tmp / name, (1000 + i, 1000 + i))
        recent = crash_log.list_recent_crash_logs(limit=2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0].name, "crash_003.log")
        self.assertEqual(recent[1].name, "crash_002.log")


if __name__ == "__main__":
    unittest.main()
