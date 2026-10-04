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


@unittest.skipUnless(__import__("sys").platform == "win32", "PowerShell 助手只在 Windows 上真跑")
class HelperScriptRealRunTests(unittest.TestCase):
    """在真实 Windows（CI windows-latest）上执行助手脚本：等进程退出 → 跑"安装器" → 写结果 → 重新打开。"""

    def test_helper_runs_installer_and_records_result(self):
        import os
        import subprocess
        import sys
        tmp = Path(tempfile.mkdtemp(prefix="升级测试_"))
        try:
            root = tmp / "标本入库管理"
            (root / "current").mkdir(parents=True)
            sysdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            shutil.copy2(sysdir / "whoami.exe", root / "current" / "app.exe")  # 重新打开的"新版"：立即退出
            fake_installer = tmp / "installer_v0.10.99_windows.exe"
            shutil.copy2(sys.executable, fake_installer)  # python.exe /VERYSILENT … → 退出码 2，充当"安装失败"
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            updates = tmp / "updates"
            script = installer_update.write_helper_script(
                updates, pid=dead.pid, installer=fake_installer, install_root=root,
                from_version="0.10.41", to_version="0.10.99")
            r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            info = installer_update.read_and_clear_result(updates)
            self.assertIsNotNone(info, r.stdout + r.stderr)
            self.assertEqual(info["to_version"], "0.10.99")
            self.assertNotEqual(info["exit_code"], 0)  # 失败被如实记录，下次启动会提示用户
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
