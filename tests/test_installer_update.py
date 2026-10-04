"""v0.10.42 一键升级（安装器静默安装）。"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from specimen_app import installer_update
from specimen_app.updater import LatestRelease, UpdateError


def _release(ver="0.10.99"):
    return LatestRelease(version=ver, tag=f"v{ver}", zip_url="", zip_name="", sha256_url=None, notes="")


class DownloadInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.payload = b"MZ fake installer"
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dl(self, data):
        calls = []

        def download_to(url, dest, cb=None):
            calls.append(url)
            Path(dest).write_bytes(data)
        return download_to, calls

    def test_downloads_and_verifies(self):
        name = installer_update.installer_name("0.10.99")
        dl, calls = self._dl(self.payload)
        path = installer_update.download_installer(
            _release(), self.tmp, http_get=lambda u: f"{self.digest}  {name}".encode(), download_to=dl)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertTrue(calls[0].endswith(name))

    def test_missing_checksum_is_refused(self):
        dl, _ = self._dl(self.payload)
        with self.assertRaises(UpdateError):
            installer_update.download_installer(_release(), self.tmp, http_get=lambda u: b"", download_to=dl)

    def test_bad_checksum_is_refused_and_nothing_left(self):
        name = installer_update.installer_name("0.10.99")
        dl, _ = self._dl(b"tampered")
        with self.assertRaises(UpdateError):
            installer_update.download_installer(
                _release(), self.tmp, http_get=lambda u: f"{self.digest}  {name}".encode(), download_to=dl)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_existing_verified_download_is_reused(self):
        name = installer_update.installer_name("0.10.99")
        (self.tmp / name).write_bytes(self.payload)
        dl, calls = self._dl(b"x")
        installer_update.download_installer(
            _release(), self.tmp, http_get=lambda u: f"{self.digest}  {name}".encode(), download_to=dl)
        self.assertEqual(calls, [])


class InstallRootAndScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detects_inno_install_root(self):
        root = self.tmp / "标本入库管理"
        exe = root / "releases" / "v0.10.41" / "bundle" / "app.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        (root / "current").mkdir()
        (root / "unins000.exe").write_bytes(b"")
        self.assertEqual(installer_update.find_inno_install_root(exe), root.resolve())
        self.assertIsNone(installer_update.find_inno_install_root(self.tmp / "portable" / "app.exe"))

    def test_helper_script_contents(self):
        script = installer_update.write_helper_script(
            self.tmp, pid=1234, installer=self.tmp / "installer_v0.10.99_windows.exe",
            install_root=Path("C:/Users/x/AppData/Local/Programs/标本入库管理"),
            workspace="H:\\标本整理\\it's", from_version="0.10.41", to_version="0.10.99")
        raw = script.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # BOM：PowerShell 5.1 读中文路径
        text = raw.decode("utf-8-sig")
        self.assertIn("/VERYSILENT", text)
        self.assertIn("Wait-Process -Id $appPid", text)
        self.assertIn("'H:\\标本整理\\it''s'", text)  # 单引号正确转义
        self.assertIn("标本入库管理", text)

    def test_result_is_read_once(self):
        (self.tmp / installer_update.RESULT_FILE).write_text(
            json.dumps({"exit_code": 0, "to_version": "0.10.99"}), encoding="utf-8-sig")
        self.assertEqual(installer_update.read_and_clear_result(self.tmp)["exit_code"], 0)
        self.assertIsNone(installer_update.read_and_clear_result(self.tmp))


if __name__ == "__main__":
    unittest.main()
