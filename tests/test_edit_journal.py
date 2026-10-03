"""v0.10.40 未保存修改恢复日志（specimen_app/edit_journal.py）。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app.edit_journal import EditJournal


class EditJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.jdir = self.tmp / "recovery"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unsaved_edit_survives_crash(self):
        EditJournal(self.jdir, self.ws).record("YZZ000001", "specimen", {"种名": "海葵"})
        # 进程"崩溃"：不调 confirm_saved，重新打开
        again = EditJournal(self.jdir, self.ws)
        self.assertEqual([(v, c, f) for v, c, f, _ in again.pending()],
                         [("YZZ000001", "specimen", {"种名": "海葵"})])

    def test_confirm_saved_removes_entry_and_file(self):
        j = EditJournal(self.jdir, self.ws)
        j.record("YZZ000001", "specimen", {"种名": "海葵"})
        j.confirm_saved("YZZ000001", "specimen", {"种名": "海葵"})
        self.assertEqual(j.pending(), [])
        self.assertFalse(j.path.exists())

    def test_newer_edit_during_save_is_kept(self):
        j = EditJournal(self.jdir, self.ws)
        j.record("YZZ000001", "specimen", {"种名": "海"})
        j.record("YZZ000001", "specimen", {"种名": "海葵"})  # 写盘期间用户继续打字
        j.confirm_saved("YZZ000001", "specimen", {"种名": "海"})  # 后台写完的是旧值
        self.assertEqual(j.pending()[0][2], {"种名": "海葵"})

    def test_workspaces_are_isolated(self):
        other = self.tmp / "ws2"
        other.mkdir()
        EditJournal(self.jdir, self.ws).record("YZZ000001", "specimen", {"种名": "A"})
        self.assertEqual(EditJournal(self.jdir, other).pending(), [])

    def test_corrupt_file_is_ignored(self):
        j = EditJournal(self.jdir, self.ws)
        self.jdir.mkdir(parents=True, exist_ok=True)
        j.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(EditJournal(self.jdir, self.ws).pending(), [])

    def test_discard_all(self):
        j = EditJournal(self.jdir, self.ws)
        j.record("YZZ000001", "specimen", {"种名": "A"})
        j.record("YZZ000002", "classification", {"科": "B"})
        j.discard_all()
        self.assertEqual(EditJournal(self.jdir, self.ws).pending(), [])


if __name__ == "__main__":
    unittest.main()
