"""Unit tests for worms_client.py — all HTTP calls and SQLite are mocked."""

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from specimen_app.worms_client import WoRMSRecord, WormsError, query_worms


def _make_response(data, status=200):
    """Return a mock urllib response with JSON *data*."""
    body = json.dumps(data).encode()
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=body)
    mock_resp.status = status
    return mock_resp


_ACCEPTED_RECORD = {
    "AphiaID": 126436,
    "status": "accepted",
    "valid_name": "Gadus morhua",
    "valid_AphiaID": 126436,
    "scientificname": "Gadus morhua",
    "kingdom": "Animalia",
    "phylum": "Chordata",
    "class": "Actinopteri",
    "order": "Gadiformes",
    "family": "Gadidae",
    "genus": "Gadus",
    "authority": "Linnaeus, 1758",
    "rank": "Species",
}

_UNACCEPTED_RECORD = dict(_ACCEPTED_RECORD, AphiaID=999, status="unaccepted")


class TestQueryWorms(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_accepted_record_returned(self, mock_open):
        mock_open.return_value = _make_response([_ACCEPTED_RECORD])
        rec = query_worms("Gadus morhua")
        self.assertIsInstance(rec, WoRMSRecord)
        self.assertEqual(rec.aphia_id, 126436)
        self.assertEqual(rec.valid_name, "Gadus morhua")
        self.assertEqual(rec.phylum, "Chordata")
        self.assertEqual(rec.class_, "Actinopteri")
        self.assertEqual(rec.order, "Gadiformes")
        self.assertEqual(rec.family, "Gadidae")
        self.assertEqual(rec.genus, "Gadus")
        self.assertEqual(rec.status, "accepted")

    @patch("urllib.request.urlopen")
    def test_empty_list_returns_none(self, mock_open):
        mock_open.return_value = _make_response([])
        result = query_worms("Unknown species xyz")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_only_unaccepted_returns_none(self, mock_open):
        mock_open.return_value = _make_response([_UNACCEPTED_RECORD])
        result = query_worms("Some synonym")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_accepted_preferred_over_unaccepted(self, mock_open):
        # First record unaccepted, second accepted — should return accepted.
        mock_open.return_value = _make_response([_UNACCEPTED_RECORD, _ACCEPTED_RECORD])
        rec = query_worms("Gadus morhua")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, "accepted")
        self.assertEqual(rec.aphia_id, 126436)

    @patch("urllib.request.urlopen")
    def test_network_error_raises_worms_error(self, mock_open):
        mock_open.side_effect = urllib.error.URLError("connection refused")
        with self.assertRaises(WormsError):
            query_worms("Gadus morhua")

    @patch("urllib.request.urlopen")
    def test_malformed_json_raises_worms_error(self, mock_open):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=b"not json {{{")
        mock_open.return_value = mock_resp
        with self.assertRaises(WormsError):
            query_worms("Gadus morhua")

    def test_empty_name_returns_none_without_network(self):
        # Must not make any HTTP request.
        with patch("urllib.request.urlopen") as mock_open:
            result = query_worms("")
            mock_open.assert_not_called()
        self.assertIsNone(result)

    def test_whitespace_name_returns_none_without_network(self):
        with patch("urllib.request.urlopen") as mock_open:
            result = query_worms("   ")
            mock_open.assert_not_called()
        self.assertIsNone(result)

    def test_bad_hostname_raises_worms_error(self):
        """URL construction should never hit a foreign host; safety check fires."""
        # Patch _WORMS_REST to an evil URL so hostname check trips.
        with patch("specimen_app.worms_client._WORMS_REST", "https://evil.example.com/rest"):
            with self.assertRaises(WormsError) as ctx:
                query_worms("Gadus morhua")
            self.assertIn("evil.example.com", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_fields_stripped_and_coerced(self, mock_open):
        """None values in JSON → empty string, not 'None'."""
        rec_data = dict(_ACCEPTED_RECORD, phylum=None, order=None)
        mock_open.return_value = _make_response([rec_data])
        rec = query_worms("Gadus morhua")
        self.assertEqual(rec.phylum, "")
        self.assertEqual(rec.order, "")


# ---------------------------------------------------------------------------
# SQLite cache tests
# ---------------------------------------------------------------------------

def _make_record(**kwargs) -> WoRMSRecord:
    defaults = dict(
        aphia_id=126436, status="accepted", valid_name="Gadus morhua",
        valid_aphia_id=126436, phylum="Chordata", class_="Actinopteri",
        order="Gadiformes", family="Gadidae", genus="Gadus",
        authority="Linnaeus, 1758", rank="Species",
    )
    defaults.update(kwargs)
    return WoRMSRecord(**defaults)


class TestSQLiteCache(unittest.TestCase):
    """Tests for lookup_cache / write_to_cache / cache_stats / clear_cache."""

    def setUp(self):
        # Use a temp file as the SQLite DB for each test.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._db_path = Path(self._tmp.name)
        self._tmp.close()
        # Patch _cache_db_path to return our temp file.
        self._patcher = patch(
            "specimen_app.worms_client._cache_db_path",
            return_value=self._db_path,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            self._db_path.unlink(missing_ok=True)
        except Exception:
            pass

    def test_write_and_lookup(self):
        from specimen_app.worms_client import lookup_cache, write_to_cache
        rec = _make_record()
        write_to_cache(rec)
        result = lookup_cache("Gadus morhua")
        self.assertIsNotNone(result)
        self.assertEqual(result.aphia_id, 126436)
        self.assertEqual(result.family, "Gadidae")

    def test_lookup_case_insensitive(self):
        from specimen_app.worms_client import lookup_cache, write_to_cache
        rec = _make_record()
        write_to_cache(rec)
        result = lookup_cache("gadus morhua")
        self.assertIsNotNone(result)

    def test_lookup_missing_returns_none(self):
        from specimen_app.worms_client import lookup_cache
        result = lookup_cache("Nonexistent species xyz")
        self.assertIsNone(result)

    def test_write_replaces_existing(self):
        from specimen_app.worms_client import lookup_cache, write_to_cache
        rec1 = _make_record(phylum="Chordata")
        rec2 = _make_record(phylum="UpdatedPhylum")
        write_to_cache(rec1)
        write_to_cache(rec2)
        result = lookup_cache("Gadus morhua")
        self.assertEqual(result.phylum, "UpdatedPhylum")

    def test_cache_stats_empty(self):
        from specimen_app.worms_client import cache_stats
        stats = cache_stats()
        self.assertEqual(stats["count"], 0)

    def test_cache_stats_after_write(self):
        from specimen_app.worms_client import cache_stats, write_to_cache
        write_to_cache(_make_record())
        stats = cache_stats()
        self.assertEqual(stats["count"], 1)
        self.assertIsNotNone(stats["last_import"])

    def test_clear_cache(self):
        from specimen_app.worms_client import cache_stats, clear_cache, write_to_cache
        write_to_cache(_make_record())
        clear_cache()
        stats = cache_stats()
        self.assertEqual(stats["count"], 0)

    @patch("urllib.request.urlopen")
    def test_query_with_cache_hits_cache_first(self, mock_open):
        from specimen_app.worms_client import query_worms_with_cache, write_to_cache
        rec = _make_record()
        write_to_cache(rec)
        result = query_worms_with_cache("Gadus morhua")
        # Network should not be called — cache hit.
        mock_open.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result.aphia_id, 126436)

    @patch("urllib.request.urlopen")
    def test_query_with_cache_writes_on_miss(self, mock_open):
        from specimen_app.worms_client import lookup_cache, query_worms_with_cache
        mock_open.return_value = _make_response([_ACCEPTED_RECORD])
        result = query_worms_with_cache("Gadus morhua")
        self.assertIsNotNone(result)
        # Should now be in cache.
        cached = lookup_cache("Gadus morhua")
        self.assertIsNotNone(cached)

    @patch("urllib.request.urlopen")
    def test_query_with_cache_no_write_on_none(self, mock_open):
        from specimen_app.worms_client import cache_stats, query_worms_with_cache
        mock_open.return_value = _make_response([])
        result = query_worms_with_cache("Unknown xyz")
        self.assertIsNone(result)
        # Nothing written to cache.
        stats = cache_stats()
        self.assertEqual(stats["count"], 0)


# ---------------------------------------------------------------------------
# DwC-A import tests
# ---------------------------------------------------------------------------

import zipfile
import io as _io


def _make_dwca_zip(tsv_content: str, filename: str = "Taxon.tsv") -> bytes:
    """Return bytes of a zip containing *tsv_content* as *filename*."""
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, tsv_content.encode("utf-8"))
    return buf.getvalue()


_DWCA_HEADER = "\t".join([
    "taxonID", "scientificName", "acceptedNameUsageID",
    "taxonomicStatus", "taxonRank", "scientificNameAuthorship",
    "phylum", "class", "order", "family", "genus",
])

_DWCA_ACCEPTED = "\t".join([
    "126436", "Gadus morhua", "126436",
    "accepted", "Species", "Linnaeus, 1758",
    "Chordata", "Actinopteri", "Gadiformes", "Gadidae", "Gadus",
])

_DWCA_UNACCEPTED = "\t".join([
    "999", "Gadus morrhua", "126436",
    "unaccepted", "Species", "Bloch, 1789",
    "Chordata", "Actinopteri", "Gadiformes", "Gadidae", "Gadus",
])


class TestDwcaImport(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._db_path = Path(self._tmp.name)
        self._tmp.close()
        self._patcher = patch(
            "specimen_app.worms_client._cache_db_path",
            return_value=self._db_path,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            self._db_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_zip(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.write(_make_dwca_zip(content))
        tmp.close()
        return Path(tmp.name)

    def test_import_accepted_only(self):
        from specimen_app.worms_client import import_dwca, lookup_cache
        tsv = "\n".join([_DWCA_HEADER, _DWCA_ACCEPTED, _DWCA_UNACCEPTED])
        zip_path = self._write_zip(tsv)
        try:
            count = import_dwca(zip_path)
            self.assertEqual(count, 1)
            rec = lookup_cache("Gadus morhua")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.family, "Gadidae")
            # Unaccepted record should NOT be in cache.
            self.assertIsNone(lookup_cache("Gadus morrhua"))
        finally:
            zip_path.unlink(missing_ok=True)

    def test_import_progress_callback(self):
        from specimen_app.worms_client import import_dwca
        tsv = "\n".join([_DWCA_HEADER, _DWCA_ACCEPTED])
        zip_path = self._write_zip(tsv)
        calls = []
        try:
            import_dwca(zip_path, progress_cb=lambda n, t: calls.append((n, t)))
            # Final call must be (total, total).
            self.assertTrue(len(calls) > 0)
            last = calls[-1]
            self.assertEqual(last[0], last[1])
        finally:
            zip_path.unlink(missing_ok=True)

    def test_import_missing_file_raises(self):
        from specimen_app.worms_client import import_dwca, WormsError
        with self.assertRaises(WormsError):
            import_dwca("/nonexistent/path/file.zip")

    def test_import_zip_without_taxon_tsv_raises(self):
        from specimen_app.worms_client import import_dwca, WormsError
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "no taxon file")
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.write(buf.getvalue())
        tmp.close()
        try:
            with self.assertRaises(WormsError):
                import_dwca(tmp.name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_import_empty_after_filter(self):
        from specimen_app.worms_client import import_dwca
        tsv = "\n".join([_DWCA_HEADER, _DWCA_UNACCEPTED])
        zip_path = self._write_zip(tsv)
        try:
            count = import_dwca(zip_path)
            self.assertEqual(count, 0)
        finally:
            zip_path.unlink(missing_ok=True)


class TestErrorClassification(unittest.TestCase):
    """query_worms() 网络异常按 reason 分类，给出可操作中文提示。"""

    def _expect_message(self, reason, expected_substring):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError(reason)
            with self.assertRaises(WormsError) as ctx:
                query_worms("Gadus morhua")
            self.assertIn(expected_substring, str(ctx.exception))

    def test_dns_failure_message(self):
        import socket
        self._expect_message(socket.gaierror(-2, "Name or service not known"),
                             "DNS 解析失败")

    def test_timeout_message(self):
        import socket
        self._expect_message(socket.timeout("timed out"), "请求超时")

    def test_ssl_error_message(self):
        import ssl
        self._expect_message(ssl.SSLError("certificate verify failed"),
                             "SSL 错误")

    def test_generic_url_error_message(self):
        self._expect_message("connection refused", "网络不可达")


class TestBootstrapCache(unittest.TestCase):
    """ensure_bootstrap_cache() 首启动注入逻辑。"""

    def _make_bootstrap_gz(self, dir_path: Path) -> Path:
        """构造一个含 worms_taxa 表的合法 SQLite + gzip。"""
        import gzip
        import shutil
        src = dir_path / "src.sqlite"
        conn = sqlite3.connect(str(src))
        conn.execute(
            "CREATE TABLE worms_taxa (aphia_id INTEGER PRIMARY KEY, "
            "status TEXT, valid_name TEXT, valid_aphia_id INTEGER, "
            "phylum TEXT, class_name TEXT, ord TEXT, family TEXT, "
            "genus TEXT, authority TEXT, rank TEXT, cached_at TEXT)"
        )
        conn.execute(
            "INSERT INTO worms_taxa VALUES (1, 'accepted', 'TestSpecies', 1, "
            "'TestPhylum', 'TestClass', 'TestOrder', 'TestFamily', "
            "'TestGenus', 'auth', 'Species', '2026-01-01')"
        )
        conn.commit()
        conn.close()
        gz = dir_path / "bootstrap.sqlite.gz"
        with open(src, "rb") as f_in, gzip.open(gz, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        src.unlink()
        return gz

    def test_install_when_user_cache_empty(self):
        from specimen_app.worms_client import ensure_bootstrap_cache
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            gz = self._make_bootstrap_gz(td_path)
            user_cache = td_path / "user.sqlite"
            with patch("specimen_app.worms_client._resolve_bootstrap_path",
                       return_value=gz), \
                 patch("specimen_app.worms_client._cache_db_path",
                       return_value=user_cache):
                result = ensure_bootstrap_cache()
            self.assertEqual(result, str(gz))
            self.assertTrue(user_cache.exists())
            self.assertGreater(user_cache.stat().st_size, 0)

    def test_skip_when_user_cache_has_records(self):
        """已有用户缓存里有真实记录 → 跳过 bootstrap。"""
        from specimen_app.worms_client import ensure_bootstrap_cache, write_to_cache, WoRMSRecord
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            user_cache = td_path / "user.sqlite"
            gz = self._make_bootstrap_gz(td_path)
            with patch("specimen_app.worms_client._resolve_bootstrap_path",
                       return_value=gz), \
                 patch("specimen_app.worms_client._cache_db_path",
                       return_value=user_cache):
                # 预填一条真实记录
                write_to_cache(WoRMSRecord(
                    aphia_id=999, status="accepted", valid_name="ExistingSpecies",
                    valid_aphia_id=999, phylum="P", class_="C", order="O",
                    family="F", genus="G", authority="auth", rank="Species",
                ))
                result = ensure_bootstrap_cache()
            self.assertIsNone(result)

    def test_install_when_empty_sqlite_present(self):
        """用户缓存文件存在但 0 行（只有 schema）→ 应当注入。"""
        from specimen_app.worms_client import ensure_bootstrap_cache, _open_cache
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            user_cache = td_path / "user.sqlite"
            gz = self._make_bootstrap_gz(td_path)
            with patch("specimen_app.worms_client._resolve_bootstrap_path",
                       return_value=gz), \
                 patch("specimen_app.worms_client._cache_db_path",
                       return_value=user_cache):
                # 先打开一次，创建空 SQLite（有 schema 无数据）
                _open_cache().close()
                self.assertTrue(user_cache.exists())
                result = ensure_bootstrap_cache()
            self.assertEqual(result, str(gz))

    def test_skip_when_no_bootstrap(self):
        """无 bootstrap 文件 → 返回 None，不尝试安装。"""
        from specimen_app.worms_client import ensure_bootstrap_cache
        with tempfile.TemporaryDirectory() as td:
            user_cache = Path(td) / "user.sqlite"
            with patch("specimen_app.worms_client._resolve_bootstrap_path",
                       return_value=None), \
                 patch("specimen_app.worms_client._cache_db_path",
                       return_value=user_cache):
                result = ensure_bootstrap_cache()
            self.assertIsNone(result)
            # 注：cache_stats() 调 _open_cache() 会创建空 SQLite，这是预期行为。
            # 关键是 install_cache_gz 未被调用——通过 result is None 验证。


if __name__ == "__main__":
    unittest.main()
