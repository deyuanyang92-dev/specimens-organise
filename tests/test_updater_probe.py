from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from specimen_app import updater
from specimen_app.updater import UpdateError, probe_zip


APP_NAME = "标本入库管理"


def _make_full_zip(path: Path, version: str = "0.8.0") -> None:
    bundle = f"{APP_NAME}_v{version}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{bundle}/{bundle}.exe", b"MZfake")
        zf.writestr(
            f"{bundle}/.update_meta.json",
            json.dumps({
                "version": version,
                "runtime_hash": "abcdef012345",
                "app_files": [f"{bundle}.exe"],
            }, ensure_ascii=False),
        )
        zf.writestr(f"{bundle}/_internal/PyQt5/lib.so", b"runtime")
        zf.writestr(f"{bundle}/_internal/python313.dll", b"runtime")
        zf.writestr(f"{bundle}/_internal/specimen_app/ui.py", b"# app code")


def _make_app_only_zip(path: Path, version: str = "0.8.1") -> None:
    bundle = f"{APP_NAME}_v{version}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{bundle}/{bundle}.exe", b"MZfake")
        zf.writestr(
            f"{bundle}/.update_meta.json",
            json.dumps({
                "version": version,
                "runtime_hash": "ffffff111111",
                "app_files": [f"{bundle}.exe", "_internal/specimen_app/ui.py"],
            }, ensure_ascii=False),
        )
        zf.writestr(f"{bundle}/_internal/specimen_app/ui.py", b"# app only, no runtime")


def _make_unknown_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("random_dir/some_file.txt", b"hello")


class ProbeZipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_setup_zip(self):
        zip_path = self.dir / "setup_v0.8.0_windows.zip"
        _make_full_zip(zip_path, "0.8.0")
        probe = probe_zip(zip_path)
        self.assertEqual(probe.kind, "full")
        self.assertEqual(probe.version, "0.8.0")
        self.assertEqual(probe.platform, "windows")
        self.assertEqual(probe.runtime_hash, "abcdef012345")
        self.assertEqual(probe.bundle_dir_name, f"{APP_NAME}_v0.8.0")

    def test_app_only_zip(self):
        zip_path = self.dir / "app_v0.8.1_linux.zip"
        _make_app_only_zip(zip_path, "0.8.1")
        probe = probe_zip(zip_path)
        self.assertEqual(probe.kind, "app-only")
        self.assertEqual(probe.version, "0.8.1")
        self.assertEqual(probe.platform, "linux")
        self.assertEqual(probe.runtime_hash, "ffffff111111")

    def test_unknown_zip(self):
        zip_path = self.dir / "mystery.zip"
        _make_unknown_zip(zip_path)
        probe = probe_zip(zip_path)
        self.assertEqual(probe.kind, "unknown")

    def test_missing_file_raises(self):
        with self.assertRaises(UpdateError):
            probe_zip(self.dir / "nope.zip")

    def test_corrupted_zip_raises(self):
        bad = self.dir / "broken_v0.7.0_windows.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(UpdateError):
            probe_zip(bad)


class ImportLocalZipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dest = self.dir / "releases"

    def tearDown(self):
        self._tmp.cleanup()

    def test_imports_full_zip_to_versioned_dir(self):
        zip_path = self.dir / "setup_v0.8.0_linux.zip"
        _make_full_zip(zip_path, "0.8.0")
        target, probe = updater.import_local_zip(zip_path, self.dest,
                                                  expected_platform="linux")
        self.assertEqual(probe.kind, "full")
        self.assertTrue(target.exists())
        self.assertEqual(target.name, "v0.8.0")
        exe = target / f"{APP_NAME}_v0.8.0.exe"
        self.assertTrue(exe.exists())

    def test_rejects_wrong_platform(self):
        zip_path = self.dir / "setup_v0.8.0_windows.zip"
        _make_full_zip(zip_path, "0.8.0")
        with self.assertRaises(UpdateError) as cm:
            updater.import_local_zip(zip_path, self.dest, expected_platform="linux")
        self.assertIn("平台不匹配", str(cm.exception))

    def test_rejects_app_only_zip(self):
        zip_path = self.dir / "app_v0.8.1_linux.zip"
        _make_app_only_zip(zip_path, "0.8.1")
        with self.assertRaises(UpdateError) as cm:
            updater.import_local_zip(zip_path, self.dest)
        self.assertIn("应用增量包", str(cm.exception))

    def test_rejects_unknown_zip(self):
        zip_path = self.dir / "mystery.zip"
        _make_unknown_zip(zip_path)
        with self.assertRaises(UpdateError):
            updater.import_local_zip(zip_path, self.dest)

    def test_sha256_verification_pass(self):
        from hashlib import sha256
        zip_path = self.dir / "setup_v0.8.0_linux.zip"
        _make_full_zip(zip_path, "0.8.0")
        digest = sha256(zip_path.read_bytes()).hexdigest()
        sha_path = self.dir / f"{zip_path.name}.sha256"
        sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
        target, _ = updater.import_local_zip(zip_path, self.dest,
                                              expected_platform="linux",
                                              sha256_path=sha_path)
        self.assertTrue(target.exists())

    def test_sha256_verification_fail(self):
        zip_path = self.dir / "setup_v0.8.0_linux.zip"
        _make_full_zip(zip_path, "0.8.0")
        bad_sha = self.dir / f"{zip_path.name}.sha256"
        bad_sha.write_text("0" * 64 + f"  {zip_path.name}\n", encoding="utf-8")
        with self.assertRaises(UpdateError):
            updater.import_local_zip(zip_path, self.dest,
                                      expected_platform="linux",
                                      sha256_path=bad_sha)

    def test_rejects_existing_target_dir(self):
        zip_path = self.dir / "setup_v0.8.0_linux.zip"
        _make_full_zip(zip_path, "0.8.0")
        (self.dest / "v0.8.0").mkdir(parents=True)
        with self.assertRaises(UpdateError) as cm:
            updater.import_local_zip(zip_path, self.dest,
                                      expected_platform="linux")
        self.assertIn("目标目录已存在", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
