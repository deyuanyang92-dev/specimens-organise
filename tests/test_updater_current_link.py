from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from specimen_app.updater import (
    make_current_symlink,
    repoint_current,
)


@unittest.skipIf(sys.platform == "win32", "POSIX symlink path")
class CurrentSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.install_root = Path(self._tmp.name)
        self.bundle_a = self.install_root / "releases" / "v0.6.0" / "bundle_a"
        self.bundle_b = self.install_root / "releases" / "v0.7.0" / "bundle_b"
        for b in (self.bundle_a, self.bundle_b):
            b.mkdir(parents=True)
            (b / "marker.txt").write_text(b.name, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_symlink_pointing_at_bundle(self):
        ok = make_current_symlink(self.install_root, self.bundle_a)
        self.assertTrue(ok)
        current = self.install_root / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual((current / "marker.txt").read_text(encoding="utf-8"),
                         "bundle_a")

    def test_repoint_replaces_existing(self):
        self.assertTrue(make_current_symlink(self.install_root, self.bundle_a))
        self.assertTrue(make_current_symlink(self.install_root, self.bundle_b))
        current = self.install_root / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual((current / "marker.txt").read_text(encoding="utf-8"),
                         "bundle_b")

    def test_dispatcher_uses_symlink_on_posix(self):
        ok = repoint_current(self.install_root, self.bundle_a)
        self.assertTrue(ok)
        self.assertTrue((self.install_root / "current").is_symlink())

    def test_missing_bundle_raises(self):
        from specimen_app.updater import UpdateError
        with self.assertRaises(UpdateError):
            make_current_symlink(self.install_root, self.install_root / "nope")


if __name__ == "__main__":
    unittest.main()
