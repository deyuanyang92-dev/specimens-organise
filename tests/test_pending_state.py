from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from specimen_app import updater_pending
from specimen_app.updater_pending import (
    PendingUpdate,
    PostUpdateSentinel,
    clear_pending,
    clear_post_update_sentinel,
    read_pending,
    read_post_update_sentinel,
    write_pending,
    write_post_update_sentinel,
)


class PendingStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = mock.patch.object(
            updater_pending, "app_config_dir",
            return_value=Path(self._tmp.name),
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_round_trip_pending(self):
        p = PendingUpdate(
            version="0.8.0",
            bundle_dir=self._tmp.name,
            exe_name="标本入库管理_v0.8.0",
            from_version="0.7.0",
            staged_at="2026-05-19T03:30:00",
            incremental=True,
            workspace="/home/u/workspace",
        )
        write_pending(p)
        loaded = read_pending()
        self.assertEqual(loaded, p)

    def test_read_returns_none_when_missing(self):
        self.assertIsNone(read_pending())

    def test_read_returns_none_on_corruption(self):
        path = Path(self._tmp.name) / "pending_update.json"
        path.write_text("{ not valid json", encoding="utf-8")
        self.assertIsNone(read_pending())

    def test_clear_pending_removes_file(self):
        p = PendingUpdate(
            version="0.8.0", bundle_dir="/x", exe_name="a", from_version="0.7.0",
            staged_at="t",
        )
        write_pending(p)
        clear_pending()
        self.assertIsNone(read_pending())

    def test_clear_when_missing_does_not_raise(self):
        clear_pending()  # should not throw

    def test_is_stale_when_bundle_dir_missing(self):
        p = PendingUpdate(
            version="0.8.0", bundle_dir="/nope_xyzzy_404", exe_name="a",
            from_version="0.7.0", staged_at="t",
        )
        self.assertTrue(p.is_stale())

    def test_is_stale_false_when_bundle_dir_exists(self):
        p = PendingUpdate(
            version="0.8.0", bundle_dir=self._tmp.name, exe_name="a",
            from_version="0.7.0", staged_at="t",
        )
        self.assertFalse(p.is_stale())


class SentinelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = mock.patch.object(
            updater_pending, "app_config_dir",
            return_value=Path(self._tmp.name),
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_round_trip_sentinel(self):
        s = PostUpdateSentinel(
            from_version="0.7.0",
            current_version="0.8.0",
            started_at="2026-05-19T03:30:00",
            from_bundle_dir="/old/bundle",
        )
        write_post_update_sentinel(s)
        self.assertEqual(read_post_update_sentinel(), s)

    def test_clear_sentinel(self):
        write_post_update_sentinel(PostUpdateSentinel(
            from_version="0.7.0", current_version="0.8.0", started_at="t",
        ))
        clear_post_update_sentinel()
        self.assertIsNone(read_post_update_sentinel())

    def test_sentinel_missing_returns_none(self):
        self.assertIsNone(read_post_update_sentinel())


if __name__ == "__main__":
    unittest.main()
