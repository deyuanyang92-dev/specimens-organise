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

from specimen_app import updater
from specimen_app.updater import (
    LatestRelease,
    UpdateError,
    _extract_expected_hash,
    _parse_version,
    _safe_extract,
    download_release,
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


if __name__ == "__main__":
    unittest.main()
