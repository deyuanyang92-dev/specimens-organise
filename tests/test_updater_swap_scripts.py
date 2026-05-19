from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from specimen_app.updater_swap import (
    write_swap_script_linux,
    write_swap_script_windows,
)


class SwapScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_windows_script_has_expected_markers(self):
        path = write_swap_script_windows(dest_dir=self.dir, wait_seconds=12)
        self.assertTrue(path.exists())
        self.assertTrue(path.name.startswith("specimen_swap_"))
        self.assertTrue(path.suffix == ".bat")
        content = path.read_text(encoding="utf-8")
        self.assertIn("@echo off", content)
        self.assertIn("tasklist", content)
        self.assertIn("mklink /J", content)
        self.assertIn("--workspace", content)
        # wait_seconds substituted into the for /L loop
        self.assertIn("(1,1,12)", content)

    def test_linux_script_has_expected_markers_and_exec_bit(self):
        path = write_swap_script_linux(dest_dir=self.dir, wait_seconds=7)
        self.assertTrue(path.exists())
        self.assertTrue(path.name.startswith("specimen_swap_"))
        self.assertTrue(path.suffix == ".sh")
        content = path.read_text(encoding="utf-8")
        self.assertIn("#!/usr/bin/env bash", content)
        self.assertIn("set -eu", content)
        self.assertIn("kill -0", content)
        self.assertIn("ln -sfn", content)
        self.assertIn("mv -Tf", content)
        self.assertIn("--workspace", content)
        self.assertIn("seq 1 7", content)
        # Linux script must be executable.
        mode = path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "owner exec bit not set")

    def test_distinct_script_paths_per_invocation(self):
        a = write_swap_script_linux(dest_dir=self.dir)
        b = write_swap_script_linux(dest_dir=self.dir)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
