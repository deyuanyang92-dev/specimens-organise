from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from specimen_app import install_kind
from specimen_app.install_kind import (
    installation_kind,
    is_built_in_upgrade_supported,
    is_frozen_via_current_link,
    kind_description,
    upgrade_advice,
)


class InstallationKindTests(unittest.TestCase):
    def test_source_when_not_frozen(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(installation_kind(), "source")

    def test_source_when_frozen_attr_missing(self):
        # getattr(sys, "frozen", False) → False if attribute missing
        if hasattr(sys, "frozen"):
            with mock.patch.object(sys, "frozen", False, create=True):
                self.assertEqual(installation_kind(), "source")
        else:
            self.assertEqual(installation_kind(), "source")

    def _patch_frozen(self, exe_path: str, env: dict[str, str] | None = None):
        env = env or {}
        return (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", exe_path),
            mock.patch.dict(os.environ, env, clear=False),
        )

    def _run_with(self, exe_path: str, env: dict[str, str] | None = None) -> str:
        patches = self._patch_frozen(exe_path, env)
        for p in patches:
            p.start()
        try:
            return installation_kind()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_appimage_by_suffix(self):
        kind = self._run_with("/home/u/Apps/标本入库管理.AppImage")
        self.assertEqual(kind, "appimage")

    def test_appimage_by_env(self):
        kind = self._run_with(
            "/tmp/.mount_xxxx/standin",
            env={"APPIMAGE": "/home/u/Apps/标本入库管理.AppImage"},
        )
        self.assertEqual(kind, "appimage")

    def test_system_package_usr_bin(self):
        kind = self._run_with("/usr/bin/specimen-app")
        self.assertEqual(kind, "system-package")

    def test_system_package_opt(self):
        kind = self._run_with("/opt/specimen/标本入库管理")
        self.assertEqual(kind, "system-package")

    def test_system_package_program_files(self):
        kind = self._run_with("C:\\Program Files\\Specimen\\app.exe")
        self.assertEqual(kind, "system-package")

    def test_frozen_current_linux(self):
        kind = self._run_with("/home/u/specimens/current/标本入库管理_v0.7.0")
        self.assertEqual(kind, "frozen-current")

    def test_frozen_current_windows(self):
        kind = self._run_with(
            "C:\\Users\\u\\specimens\\current\\标本入库管理_v0.7.0.exe"
        )
        self.assertEqual(kind, "frozen-current")

    def test_frozen_direct(self):
        kind = self._run_with(
            "/home/u/specimens/releases/v0.7.0/标本入库管理_v0.7.0/标本入库管理_v0.7.0"
        )
        self.assertEqual(kind, "frozen-direct")

    def test_helpers_match_kind(self):
        with mock.patch.object(install_kind, "installation_kind", return_value="frozen-current"):
            self.assertTrue(is_frozen_via_current_link())
            self.assertTrue(is_built_in_upgrade_supported())
        with mock.patch.object(install_kind, "installation_kind", return_value="system-package"):
            self.assertFalse(is_frozen_via_current_link())
            self.assertFalse(is_built_in_upgrade_supported())
        with mock.patch.object(install_kind, "installation_kind", return_value="source"):
            self.assertFalse(is_frozen_via_current_link())
            self.assertTrue(is_built_in_upgrade_supported())

    def test_descriptions_cover_all_kinds(self):
        for kind in ("frozen-current", "frozen-direct", "source", "appimage", "system-package"):
            self.assertTrue(kind_description(kind))
            self.assertTrue(upgrade_advice(kind))


if __name__ == "__main__":
    unittest.main()
