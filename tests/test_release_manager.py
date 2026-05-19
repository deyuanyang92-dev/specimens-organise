from __future__ import annotations

import sys
import unittest
from unittest import mock

from specimen_app.release_manager import (
    current_install_root,
    is_running_from_current_link,
)


class CurrentInstallRootTests(unittest.TestCase):
    def test_none_when_not_frozen(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(current_install_root())
            self.assertFalse(is_running_from_current_link())

    def test_none_when_frozen_but_not_under_current(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  "/home/u/specimens/releases/v0.7.0/bundle/标本入库管理_v0.7.0"):
            self.assertIsNone(current_install_root())
            self.assertFalse(is_running_from_current_link())

    def test_returns_grandparent_when_under_current_linux(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  "/home/u/specimens/current/标本入库管理_v0.7.0"):
            root = current_install_root()
            self.assertIsNotNone(root)
            self.assertEqual(str(root), "/home/u/specimens")
            self.assertTrue(is_running_from_current_link())


if __name__ == "__main__":
    unittest.main()
