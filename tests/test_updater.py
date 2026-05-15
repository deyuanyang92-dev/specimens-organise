from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from build_release import partition_bundle
from specimen_app import updater
from specimen_app.updater import (
    LatestRelease,
    UpdateError,
    _extract_expected_hash,
    _parse_version,
    _safe_extract,
    download_release,
    download_update,
    is_newer,
)


class VersionParsingTests(unittest.TestCase):
    def test_plain_and_v_prefixed_equal(self):
        self.assertEqual(_parse_version("0.3.0"), _parse_version("v0.3.0"))

    def test_numeric_ordering(self):
        self.assertLess(_parse_version("0.3.0"), _parse_version("0.3.1"))
        self.assertLess(_parse_version("0.9.0"), _parse_version("1.0.0"))

    def test_prerelease_lower_than_release(self):
        # 0.3.0-test.1 必须排在正式版 0.3.0 之前
        self.assertLess(_parse_version("0.3.0-test.1"), _parse_version("0.3.0"))
        self.assertLess(_parse_version("0.3.0-test.1"), _parse_version("0.3.0-test.2"))

    def test_is_newer(self):
        self.assertTrue(is_newer("0.3.0", current="0.3.0-test.1"))
        self.assertTrue(is_newer("0.3.1", current="0.3.0"))
        self.assertFalse(is_newer("0.3.0", current="0.3.0"))
        self.assertFalse(is_newer("0.2.9", current="0.3.0"))
        self.assertFalse(is_newer("0.3.0-test.1", current="0.3.0-test.1"))


class HashHelperTests(unittest.TestCase):
    def test_extract_hash_matches_filename(self):
        text = "aaa  other.zip\nbbb  target.zip\n"
        self.assertEqual(_extract_expected_hash(text, "target.zip"), "bbb")

    def test_extract_hash_single_line_fallback(self):
        self.assertEqual(_extract_expected_hash("deadbeef  whatever.zip", "x.zip"), "deadbeef")

    def test_extract_hash_missing(self):
        self.assertIsNone(_extract_expected_hash("aaa  a.zip\nbbb  b.zip\n", "c.zip"))


class SafeExtractTests(unittest.TestCase):
    def test_rejects_zip_slip(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            evil_zip = tmp_path / "evil.zip"
            with zipfile.ZipFile(evil_zip, "w") as archive:
                archive.writestr("../escaped.txt", "pwned")
            with self.assertRaises(UpdateError):
                _safe_extract(evil_zip, tmp_path / "out")


class ValidateUrlTests(unittest.TestCase):
    def test_rejects_non_https(self):
        with self.assertRaises(UpdateError):
            updater._validate_url("http://github.com/x.zip")

    def test_rejects_non_github_host(self):
        with self.assertRaises(UpdateError):
            updater._validate_url("https://evil.example.com/x.zip")

    def test_accepts_github(self):
        updater._validate_url("https://github.com/a/b/releases/download/v1/x.zip")
        updater._validate_url("https://objects.githubusercontent.com/x.zip")


def _make_release_zip(path: Path, version: str) -> None:
    """构造一个形如 build_release.py 输出的 zip：根目录为 onedir 文件夹。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"标本入库管理_v{version}/标本入库管理_v{version}", "fake-exe")
        archive.writestr(f"标本入库管理_v{version}/_internal/data.bin", "x")


class DownloadReleaseTests(unittest.TestCase):
    """用本地 HTTP server 模拟 GitHub 资产，跑通下载 → 校验 → 解压。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.served = self.root / "served"
        self.served.mkdir()
        self.zip_name = "setup_v0.4.0_linux.zip"
        self.zip_path = self.served / self.zip_name
        _make_release_zip(self.zip_path, "0.4.0")
        digest = updater._file_sha256(self.zip_path)
        (self.served / f"{self.zip_name}.sha256").write_text(
            f"{digest}  {self.zip_name}\n", encoding="utf-8"
        )

        served = self.served

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def do_GET(self):
                # GitHub 的 browser_download_url 是百分号编码的，server 端需解码
                name = urllib.parse.unquote(self.path.lstrip("/"))
                target = served / name
                if not target.exists():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # download_release 会校验 URL 为 github 域名；测试时放行 localhost。
        self._orig_validate = updater._validate_url
        updater._validate_url = lambda url: None
        # 绕过环境里的 HTTP 代理，直连本地 server。
        self._orig_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        updater._validate_url = self._orig_validate
        if self._orig_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._orig_no_proxy
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmp.cleanup()

    def _release(self) -> LatestRelease:
        base = f"http://127.0.0.1:{self.port}"
        # 模拟 GitHub：URL 中的文件名为百分号编码
        quoted = urllib.parse.quote(self.zip_name)
        return LatestRelease(
            version="0.4.0",
            tag="v0.4.0",
            zip_url=f"{base}/{quoted}",
            zip_name=self.zip_name,
            sha256_url=f"{base}/{quoted}.sha256",
            notes="test notes",
        )

    def test_download_extract_ok(self):
        dest_root = self.root / "releases"
        result = download_release(self._release(), dest_root)
        self.assertEqual(result, dest_root / "v0.4.0")
        self.assertTrue((result / "标本入库管理_v0.4.0" / "标本入库管理_v0.4.0").exists())

    def test_existing_dir_refused(self):
        dest_root = self.root / "releases"
        (dest_root / "v0.4.0").mkdir(parents=True)
        with self.assertRaises(UpdateError):
            download_release(self._release(), dest_root)

    def test_bad_sha256_aborts(self):
        # 篡改 sha256 文件 → 校验失败 → 不应留下版本目录
        (self.served / f"{self.zip_name}.sha256").write_text(
            f"{'0' * 64}  {self.zip_name}\n", encoding="utf-8"
        )
        dest_root = self.root / "releases"
        with self.assertRaises(UpdateError):
            download_release(self._release(), dest_root)
        self.assertFalse((dest_root / "v0.4.0").exists())


class PartitionBundleTests(unittest.TestCase):
    """build_release.partition_bundle 的拆分规则。"""

    def test_partition_app_vs_runtime(self):
        with TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "标本入库管理_v0.5.0"
            files = {
                "标本入库管理_v0.5.0": "exe",                       # 根目录 exe → 应用
                "_internal/specimen_app/ui.pyc": "app-code",         # 应用包模块 → 应用
                "_internal/specimen_app/sub/x.pyc": "app-code",      # 嵌套应用模块 → 应用
                "_internal/python3.dll": "rt",                       # 运行时
                "_internal/PyQt5/QtCore.so": "rt",                   # 运行时
            }
            for rel, content in files.items():
                p = bundle / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            app_files, runtime_files = partition_bundle(bundle)
            self.assertEqual(
                set(app_files),
                {"标本入库管理_v0.5.0", "_internal/specimen_app/ui.pyc", "_internal/specimen_app/sub/x.pyc"},
            )
            self.assertEqual(
                set(runtime_files),
                {"_internal/python3.dll", "_internal/PyQt5/QtCore.so"},
            )


def _make_zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for arcname, content in members.items():
            archive.writestr(arcname, content)


class DownloadUpdateTests(unittest.TestCase):
    """增量更新：本地 HTTP server 模拟 GitHub，验证复用 / 完整 / 回退 / 校验失败。"""

    NEW_VER = "0.5.0"
    RUNTIME_HASH = "abc123def456"

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.served = self.root / "served"
        self.served.mkdir()
        self.releases = self.root / "releases"
        self.releases.mkdir()

        bundle = f"标本入库管理_v{self.NEW_VER}"
        # 应用包：exe + 应用模块 + .update_meta.json（arcname 带 bundle 前缀）
        self.app_zip_name = f"app_v{self.NEW_VER}_linux.zip"
        _make_zip(self.served / self.app_zip_name, {
            f"{bundle}/标本入库管理_v{self.NEW_VER}": "new-exe",
            f"{bundle}/_internal/specimen_app/ui.pyc": "new-app-code",
            f"{bundle}/.update_meta.json": "{}",
        })
        # 运行时包：大的第三方文件
        self.runtime_zip_name = f"runtime_linux_{self.RUNTIME_HASH}.zip"
        _make_zip(self.served / self.runtime_zip_name, {
            f"{bundle}/_internal/runtime_big.bin": "RUNTIME-DATA",
        })
        self.app_files = [
            f"标本入库管理_v{self.NEW_VER}",
            "_internal/specimen_app/ui.pyc",
            ".update_meta.json",
        ]
        self.app_sha = updater._file_sha256(self.served / self.app_zip_name)
        self.runtime_sha = updater._file_sha256(self.served / self.runtime_zip_name)
        self._write_manifest()

        served = self.served
        self.requests: list[str] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                name = urllib.parse.unquote(self.path.lstrip("/"))
                requests.append(name)
                target = served / name
                if not target.exists():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        base = f"http://127.0.0.1:{self.port}"
        self._orig_validate = updater._validate_url
        self._orig_asset_url = updater._asset_url
        updater._validate_url = lambda url: None
        updater._asset_url = lambda tag, name: f"{base}/{urllib.parse.quote(name)}"
        self._orig_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        updater._validate_url = self._orig_validate
        updater._asset_url = self._orig_asset_url
        if self._orig_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._orig_no_proxy
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmp.cleanup()

    def _write_manifest(self, app_sha: str | None = None) -> None:
        (self.served / "update_manifest_linux.json").write_text(json.dumps({
            "version": self.NEW_VER,
            "platform": "linux",
            "app_zip": self.app_zip_name,
            "app_sha256": app_sha or self.app_sha,
            "runtime_zip": self.runtime_zip_name,
            "runtime_sha256": self.runtime_sha,
            "runtime_hash": self.RUNTIME_HASH,
            "app_files": self.app_files,
        }), encoding="utf-8")

    def _release(self, manifest_url: str | None = "default") -> LatestRelease:
        base = f"http://127.0.0.1:{self.port}"
        if manifest_url == "default":
            manifest_url = f"{base}/update_manifest_linux.json"
        return LatestRelease(
            version=self.NEW_VER, tag=f"v{self.NEW_VER}",
            zip_url=f"{base}/{urllib.parse.quote(self.app_zip_name)}",
            zip_name=self.app_zip_name, sha256_url=None, notes="",
            manifest_url=manifest_url,
        )

    def _make_local_version(self, version: str, runtime_hash: str) -> None:
        """造一个本地已装版本，作为运行时复用源。"""
        bundle = self.releases / f"v{version}" / f"标本入库管理_v{version}"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / f"标本入库管理_v{version}").write_text("old-exe", encoding="utf-8")
        (bundle / "_internal" / "specimen_app").mkdir()
        (bundle / "_internal" / "specimen_app" / "ui.pyc").write_text("old-app-code", encoding="utf-8")
        (bundle / "_internal" / "runtime_big.bin").write_text("RUNTIME-DATA", encoding="utf-8")
        (bundle / ".update_meta.json").write_text(json.dumps({
            "version": version, "runtime_hash": runtime_hash,
            "app_files": [f"标本入库管理_v{version}", "_internal/specimen_app/ui.pyc", ".update_meta.json"],
        }), encoding="utf-8")

    def test_incremental_reuses_runtime(self):
        # 本地有运行时 hash 匹配的版本 → 只下应用包
        self._make_local_version("0.4.5", self.RUNTIME_HASH)
        target, incremental = download_update(self._release(), self.releases, [self.releases])
        self.assertTrue(incremental)
        self.assertEqual(target, self.releases / f"v{self.NEW_VER}")
        bundle = target / f"标本入库管理_v{self.NEW_VER}"
        # 运行时文件从本地复用，应用文件来自下载
        self.assertEqual((bundle / "_internal" / "runtime_big.bin").read_text(), "RUNTIME-DATA")
        self.assertEqual((bundle / f"标本入库管理_v{self.NEW_VER}").read_text(), "new-exe")
        self.assertEqual((bundle / "_internal" / "specimen_app" / "ui.pyc").read_text(), "new-app-code")
        # 只请求了清单和应用包，没有请求运行时包
        self.assertNotIn(self.runtime_zip_name, self.requests)
        self.assertIn(self.app_zip_name, self.requests)

    def test_full_download_when_no_reusable_runtime(self):
        # 本地没有匹配运行时 → 下载应用包 + 运行时包
        target, incremental = download_update(self._release(), self.releases, [self.releases])
        self.assertFalse(incremental)
        bundle = target / f"标本入库管理_v{self.NEW_VER}"
        self.assertEqual((bundle / "_internal" / "runtime_big.bin").read_text(), "RUNTIME-DATA")
        self.assertEqual((bundle / f"标本入库管理_v{self.NEW_VER}").read_text(), "new-exe")
        self.assertIn(self.runtime_zip_name, self.requests)
        self.assertIn(self.app_zip_name, self.requests)

    def test_falls_back_to_full_zip_when_no_manifest(self):
        # 老 release 无 manifest_url → 回退 download_release（完整 zip 路径）
        # 用 app_zip 充当完整 zip（其内含 bundle 前缀，结构合法）
        target, incremental = download_update(self._release(manifest_url=None), self.releases, [self.releases])
        self.assertFalse(incremental)
        self.assertEqual(target, self.releases / f"v{self.NEW_VER}")

    def test_bad_app_sha256_aborts(self):
        self._make_local_version("0.4.5", self.RUNTIME_HASH)
        self._write_manifest(app_sha="0" * 64)  # 篡改应用包摘要
        with self.assertRaises(UpdateError):
            download_update(self._release(), self.releases, [self.releases])
        self.assertFalse((self.releases / f"v{self.NEW_VER}").exists())


if __name__ == "__main__":
    unittest.main()
