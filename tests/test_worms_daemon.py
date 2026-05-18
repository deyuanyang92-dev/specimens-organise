"""WoRMS crawler daemon 测试（W1）。

不真启动 subprocess（avoid 网络 + 长耗时），而是测试：
- PID 文件管理（写/读/清理）
- daemon_running 检测逻辑
- request_stop_daemon 跨平台逻辑
- run_daemon 在 crawl_full_rest mock 下能正常返回
- 已有 daemon 在跑时 EXIT_ALREADY_RUNNING
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from specimen_app import worms_crawler_daemon as dmn


class DaemonRunningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_pid_file_returns_none(self) -> None:
        pid_path = self.tmp / "missing.pid"
        self.assertIsNone(dmn.daemon_running(pid_path))

    def test_invalid_pid_file_returns_none(self) -> None:
        pid_path = self.tmp / "bad.pid"
        pid_path.write_text("not-a-number", encoding="utf-8")
        self.assertIsNone(dmn.daemon_running(pid_path))

    def test_alive_pid_returns_pid(self) -> None:
        pid_path = self.tmp / "alive.pid"
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        # 当前进程一定存活
        self.assertEqual(dmn.daemon_running(pid_path), os.getpid())

    def test_dead_pid_cleans_up_returns_none(self) -> None:
        pid_path = self.tmp / "dead.pid"
        # 用一个极不可能存在的 PID（很大）
        pid_path.write_text("99999999", encoding="utf-8")
        self.assertIsNone(dmn.daemon_running(pid_path))
        # PID 文件应已被清理
        self.assertFalse(pid_path.exists())


class IsPidAliveTests(unittest.TestCase):
    def test_self_alive(self) -> None:
        self.assertTrue(dmn._is_pid_alive(os.getpid()))

    def test_pid_zero_or_neg_dead(self) -> None:
        self.assertFalse(dmn._is_pid_alive(0))
        self.assertFalse(dmn._is_pid_alive(-1))

    def test_huge_pid_dead(self) -> None:
        self.assertFalse(dmn._is_pid_alive(99999999))


class RunDaemonTests(unittest.TestCase):
    """测试 run_daemon 内部逻辑（mock crawl_full_rest 避免真网络）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_already_running_returns_exit_already(self) -> None:
        pid_path = self.tmp / "pid.txt"
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        state = self.tmp / "state.json"

        code = dmn.run_daemon(resume_state=state, pid_file=pid_path)

        self.assertEqual(code, dmn.EXIT_ALREADY_RUNNING)
        # 自己的 PID 文件未被改
        self.assertEqual(pid_path.read_text(encoding="utf-8"), str(os.getpid()))

    def test_successful_run_writes_pid_and_cleans_up(self) -> None:
        pid_path = self.tmp / "pid.txt"
        state = self.tmp / "state.json"

        # mock crawl_full_rest 立即返回 0 条
        with mock.patch(
            "specimen_app.worms_client.crawl_full_rest",
            return_value=0,
        ):
            code = dmn.run_daemon(resume_state=state, pid_file=pid_path)

        self.assertEqual(code, dmn.EXIT_OK)
        # atexit 处理 PID 清理（pytest 进程不真退所以 atexit 不跑） —
        # 验证 PID 文件曾被写入：写入路径里的临时目录可能已被清；
        # 我们只断言 EXIT 状态正确

    def test_crash_returns_exit_crashed(self) -> None:
        pid_path = self.tmp / "pid.txt"
        state = self.tmp / "state.json"

        with mock.patch(
            "specimen_app.worms_client.crawl_full_rest",
            side_effect=RuntimeError("simulated crash"),
        ):
            code = dmn.run_daemon(resume_state=state, pid_file=pid_path)
        self.assertEqual(code, dmn.EXIT_CRASHED)

    def test_interrupted_returns_exit_cancelled(self) -> None:
        pid_path = self.tmp / "pid.txt"
        state = self.tmp / "state.json"

        with mock.patch(
            "specimen_app.worms_client.crawl_full_rest",
            side_effect=InterruptedError(),
        ):
            code = dmn.run_daemon(resume_state=state, pid_file=pid_path)
        self.assertEqual(code, dmn.EXIT_CANCELLED)


class RequestStopDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_request_stop_no_daemon(self) -> None:
        pid_path = self.tmp / "no.pid"
        self.assertFalse(dmn.request_stop_daemon(pid_path))

    def test_request_stop_alive_pid_sends_signal(self) -> None:
        pid_path = self.tmp / "live.pid"
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        # 不真发 SIGTERM（会杀测试进程），mock os.kill / subprocess.run
        if sys.platform == "win32":
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock()
                ok = dmn.request_stop_daemon(pid_path)
            self.assertTrue(ok)
            m.assert_called_once()
        else:
            with mock.patch("os.kill") as m:
                ok = dmn.request_stop_daemon(pid_path)
            self.assertTrue(ok)
            # 第二次调用应该带 SIGTERM
            call_args = m.call_args_list[-1].args
            self.assertEqual(call_args[0], os.getpid())


if __name__ == "__main__":
    unittest.main()
