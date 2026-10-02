"""工作区锁：用户多次反馈"非常容易锁定"。锁死规则改为可解释、可自愈：

  * 本机持有 + PID 已不存在      → 立即 stale（同一内核，PID 判活可信）
  * 本机持有 + 心跳超 180 s      → stale（心跳每 60 s；被杀/卡死的进程最多 3 分钟自愈）
  * 外机持有 + 心跳超 600 s      → stale（对方的写前心跳自检 180 s 就会拦住它写，600 s 足够安全）
  * 外机持有 + 心跳新鲜          → 不 stale（真的有人在用）
  * 锁文件损坏                   → stale
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from specimen_app.excel_store import ExcelStore, LOCK_STALE_FOREIGN_SECONDS, LOCK_STALE_SAME_HOST_SECONDS, pid_is_running
from specimen_app.models import WorkspaceLockedError


class WorkspaceLockStaleRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ExcelStore(self.tmp, lock=False)
        self.lock = self.store.lock_file

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_lock(self, **over):
        payload = {
            "pid": os.getpid(),
            "time": datetime.now().isoformat(timespec="seconds"),
            "workspace": str(self.tmp),
            "hostname": "this-pc",
            "host_id": self.store._persistent_host_id,
            "instance_id": "someone-else",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "heartbeat_at": datetime.now().isoformat(timespec="seconds"),
        }
        payload.update(over)
        self.lock.write_text(json.dumps(payload), encoding="utf-8")

    def test_constants(self):
        self.assertEqual(LOCK_STALE_SAME_HOST_SECONDS, 180)
        self.assertEqual(LOCK_STALE_FOREIGN_SECONDS, 600)

    def test_same_host_live_pid_fresh_heartbeat_is_not_stale(self):
        self._write_lock()
        self.assertFalse(self.store._lock_is_stale())

    def test_same_host_dead_pid_is_stale_immediately(self):
        self._write_lock()
        with patch("specimen_app.excel_store.pid_is_running", return_value=False):
            self.assertTrue(self.store._lock_is_stale())

    def test_same_host_old_heartbeat_is_stale_after_180s(self):
        old = (datetime.now() - timedelta(seconds=LOCK_STALE_SAME_HOST_SECONDS + 5)).isoformat(timespec="seconds")
        self._write_lock(heartbeat_at=old)
        self.assertTrue(self.store._lock_is_stale())
        fresh = (datetime.now() - timedelta(seconds=LOCK_STALE_SAME_HOST_SECONDS - 30)).isoformat(timespec="seconds")
        self._write_lock(heartbeat_at=fresh)
        self.assertFalse(self.store._lock_is_stale())

    def test_foreign_host_fresh_heartbeat_is_not_stale_and_message_names_holder(self):
        self._write_lock(host_id="OTHER_HOST", hostname="remote-machine")
        self.assertFalse(self.store._lock_is_stale())
        with self.assertRaises(WorkspaceLockedError) as ctx:
            self.store.acquire_lock()
        self.assertIn("remote-machine", str(ctx.exception))
        self.assertIn("被占用", str(ctx.exception))

    def test_foreign_host_old_heartbeat_is_stale_after_600s(self):
        old = (datetime.now() - timedelta(seconds=LOCK_STALE_FOREIGN_SECONDS + 5)).isoformat(timespec="seconds")
        self._write_lock(host_id="OTHER_HOST", hostname="remote-machine", heartbeat_at=old)
        self.assertTrue(self.store._lock_is_stale())
        self.store.acquire_lock()  # 自愈：直接拿到锁
        self.assertTrue(self.store._locked)
        self.store.release_lock()

    def test_foreign_host_dead_pid_is_not_trusted(self):
        # 外机的 PID 在本机"不存在"是常态，不能据此 stale
        self._write_lock(host_id="OTHER_HOST", hostname="remote-machine", pid=999999)
        with patch("specimen_app.excel_store.pid_is_running", return_value=False):
            self.assertFalse(self.store._lock_is_stale())

    def test_corrupt_lock_is_stale(self):
        self.lock.write_text("{not json", encoding="utf-8")
        self.assertTrue(self.store._lock_is_stale())

    def test_pid_is_running_self_and_bogus(self):
        self.assertTrue(pid_is_running(os.getpid()))
        self.assertFalse(pid_is_running(2**22 + 12345))  # 几乎不可能存在的 PID

    def test_lock_holder_summary_for_dialog(self):
        old = (datetime.now() - timedelta(seconds=90)).isoformat(timespec="seconds")
        self._write_lock(host_id="OTHER_HOST", hostname="remote-machine", pid=4242, heartbeat_at=old)
        info = self.store.describe_lock_holder()
        self.assertEqual(info["hostname"], "remote-machine")
        self.assertEqual(info["pid"], 4242)
        self.assertFalse(info["same_host"])
        self.assertGreaterEqual(info["heartbeat_age_seconds"], 85)
        self.assertFalse(info["stale"])


if __name__ == "__main__":
    unittest.main()
