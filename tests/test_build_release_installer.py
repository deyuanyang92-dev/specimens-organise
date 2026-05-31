from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build_release import _write_inno_setup_script


class WindowsInstallerScriptTests(unittest.TestCase):
    def test_inno_script_installs_current_entry_without_desktop_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_dir = root / "releases" / "v1.2.3"
            bundle_dir = release_dir / "标本入库管理_v1.2.3"
            bundle_dir.mkdir(parents=True)
            exe = bundle_dir / "标本入库管理_v1.2.3.exe"
            exe.write_bytes(b"MZ")

            script = _write_inno_setup_script(
                release_dir=release_dir,
                version="1.2.3",
                versioned_dir=bundle_dir,
                versioned_exe=exe,
                icon_file=None,
            )

            content = script.read_text(encoding="utf-8-sig")
            self.assertIn("DefaultDirName={autopf}\\{#MyAppName}", content)
            self.assertIn("PrivilegesRequiredOverridesAllowed=dialog", content)
            self.assertIn("DestDir: \"{app}\\releases\\v1.2.3\\标本入库管理_v1.2.3\"", content)
            self.assertIn("mklink /J", content)
            self.assertIn("\"{app}\\current\"", content)
            self.assertIn("Name: \"{group}\\{#MyAppName}\"", content)
            self.assertIn("[UninstallDelete]", content)
            self.assertIn("Name: \"{app}\\releases\"", content)
            self.assertNotIn("{commondesktop}", content.lower())
            self.assertNotIn("{userdesktop}", content.lower())
            self.assertNotIn("Desktop", content)


if __name__ == "__main__":
    unittest.main()
