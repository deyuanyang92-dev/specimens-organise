"""Unit tests for the REST recursive full-tree crawler.

All HTTP calls are mocked via ``urllib.request.urlopen``; SQLite cache is
redirected to a per-test temp file. Tests cover:

- BFS recursion: kingdoms → phyla → … species
- Pagination: 50-per-page exhaustion + empty short page
- Resume state: load + save + delete-on-completion
- Cancel: should_stop callback raises InterruptedError, state persisted
- 429 retry with exponential backoff
- HTTP 403 / network errors produce diagnostic WormsError
- Malformed records skipped
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from specimen_app.worms_client import (
    WormsError,
    cache_stats,
    crawl_full_rest,
)


def _make_response(data, status: int = 200):
    """Return a context-manager mock urlopen response with JSON *data*."""
    body = json.dumps(data).encode() if data is not None else b""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=body)
    mock_resp.status = status
    return mock_resp


def _child(aphia_id: int, name: str, rank: str = "Species", **extra) -> dict:
    """Minimal AphiaChildrenByAphiaID JSON record."""
    rec = {
        "AphiaID": aphia_id,
        "valid_AphiaID": aphia_id,
        "scientificname": name,
        "valid_name": name,
        "status": "accepted",
        "rank": rank,
        "phylum": "",
        "class": "",
        "order": "",
        "family": "",
        "genus": "",
        "authority": "",
    }
    rec.update(extra)
    return rec


class _CrawlerTestBase(unittest.TestCase):
    """Redirect SQLite cache to a temp file; sleep is a no-op for fast tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._db_path = Path(self._tmp.name)
        self._tmp.close()
        self._tmp_state = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._state_path = Path(self._tmp_state.name)
        self._tmp_state.close()
        # Delete state file so crawler starts fresh (load_resume returns None)
        self._state_path.unlink(missing_ok=True)
        self._cache_patcher = patch(
            "specimen_app.worms_client._cache_db_path",
            return_value=self._db_path,
        )
        self._cache_patcher.start()
        # 让 rate-limit sleep / 重试 backoff 不阻塞测试
        self._sleep_patcher = patch("specimen_app.worms_client.time.sleep")
        self._sleep_patcher.start()

    def tearDown(self) -> None:
        self._sleep_patcher.stop()
        self._cache_patcher.stop()
        self._db_path.unlink(missing_ok=True)
        self._state_path.unlink(missing_ok=True)


class TestBfsRecursion(_CrawlerTestBase):
    """Verify BFS recursion descends through Genus → Species and stops at leaves."""

    @patch("urllib.request.urlopen")
    def test_two_level_tree(self, mock_open):
        # Kingdom (id=2) → 1 Genus (id=10, rank=Genus, must recurse)
        # Genus (id=10)  → 2 Species (ids 100, 101, rank=Species, leaves)
        # Species ids should NOT be recursed (rank=Species short-circuits queue)
        def _side(req, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "AphiaChildrenByAphiaID/2?" in url:
                return _make_response([_child(10, "Genus_A", rank="Genus")])
            if "AphiaChildrenByAphiaID/10?" in url:
                return _make_response([
                    _child(100, "Species_one", rank="Species"),
                    _child(101, "Species_two", rank="Species"),
                ])
            # Catch-all: no further children
            return _make_response([])

        mock_open.side_effect = _side
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 3)  # Genus_A + 2 species

        # Verify DB rows present
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute("SELECT aphia_id, valid_name, rank FROM worms_taxa ORDER BY aphia_id").fetchall()
        conn.close()
        self.assertEqual(
            [(r[0], r[1], r[2]) for r in rows],
            [(10, "Genus_A", "Genus"), (100, "Species_one", "Species"), (101, "Species_two", "Species")],
        )

        # Species leaves should NOT have been recursed (urlopen calls = 2: for id=2 + id=10)
        # If species WERE recursed, urlopen would be called for id=100 and id=101 too.
        urls_called = [c.args[0].full_url for c in mock_open.call_args_list]
        self.assertEqual(len(urls_called), 2)
        self.assertTrue(any("AphiaChildrenByAphiaID/2?" in u for u in urls_called))
        self.assertTrue(any("AphiaChildrenByAphiaID/10?" in u for u in urls_called))
        self.assertFalse(any("AphiaChildrenByAphiaID/100?" in u for u in urls_called))


class TestPagination(_CrawlerTestBase):
    """Verify offset-based pagination: continues while page is full (50)."""

    @patch("urllib.request.urlopen")
    def test_two_pages(self, mock_open):
        # Page 1 (offset=1): 50 species (full page → fetch more)
        # Page 2 (offset=51): 3 species (short → stop pagination)
        full_page = [_child(1000 + i, f"sp_{i}", rank="Species") for i in range(50)]
        short_page = [_child(2000 + i, f"sp_b_{i}", rank="Species") for i in range(3)]

        def _side(req, **kw):
            url = req.full_url
            if "offset=1" in url:
                return _make_response(full_page)
            if "offset=51" in url:
                return _make_response(short_page)
            return _make_response([])

        mock_open.side_effect = _side
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 53)
        stats = cache_stats()
        self.assertEqual(stats["count"], 53)

    @patch("urllib.request.urlopen")
    def test_empty_first_page_stops(self, mock_open):
        mock_open.return_value = _make_response([])
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 0)
        # Only 1 HTTP call (offset=1 → empty)
        self.assertEqual(mock_open.call_count, 1)


class TestResume(_CrawlerTestBase):
    """Verify resume state persists across runs and is deleted on completion."""

    @patch("urllib.request.urlopen")
    def test_state_deleted_on_completion(self, mock_open):
        mock_open.return_value = _make_response([])
        # State file gets written at progress thresholds; finally on completion it's removed.
        # Pre-seed an existing file: crawler should delete on success.
        self._state_path.write_text('{"visited": [], "queue": [], "imported": 0}')
        crawl_full_rest(
            kingdoms=(2,),
            rate_limit_qps=1000.0,
            resume_state_path=self._state_path,
        )
        self.assertFalse(self._state_path.exists())

    @patch("urllib.request.urlopen")
    def test_resume_skips_visited_kingdoms(self, mock_open):
        # Pre-seed visited set with kingdom 2 + empty queue → crawler should do nothing.
        self._state_path.write_text(
            json.dumps({"visited": [2], "queue": [], "imported": 999})
        )
        mock_open.return_value = _make_response([])  # should not be called
        count = crawl_full_rest(
            kingdoms=(2,),
            rate_limit_qps=1000.0,
            resume_state_path=self._state_path,
        )
        self.assertEqual(count, 0)
        self.assertEqual(mock_open.call_count, 0)


class TestCancel(_CrawlerTestBase):
    """should_stop callback aborts the crawl with InterruptedError; state persisted."""

    @patch("urllib.request.urlopen")
    def test_cancel_raises_interrupted(self, mock_open):
        mock_open.return_value = _make_response([_child(99, "x", rank="Species")])
        with self.assertRaises(InterruptedError):
            crawl_full_rest(
                kingdoms=(2,),
                rate_limit_qps=1000.0,
                resume_state_path=self._state_path,
                should_stop=lambda: True,  # cancel before first HTTP call
            )
        # State file should exist (saved on cancel)
        self.assertTrue(self._state_path.exists())


class TestRetry(_CrawlerTestBase):
    """429 / 503 retry with exponential backoff."""

    @patch("urllib.request.urlopen")
    def test_429_retried_then_succeeds(self, mock_open):
        # First call → 429, second → success
        http_err = urllib.error.HTTPError(
            "http://example/", 429, "Too Many Requests",
            hdrs=MagicMock(get=MagicMock(return_value=None)), fp=None,
        )
        ok = _make_response([_child(50, "sp", rank="Species")])
        mock_open.side_effect = [http_err, ok]
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 1)
        self.assertEqual(mock_open.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_403_skips_node_and_records_in_failed(self, mock_open):
        # 2026-05-16-b 行为变更：单节点 4xx 不再 abort 整库；
        # 该节点记入 state["failed"]，crawl 返回 0 并完成。
        http_err = urllib.error.HTTPError(
            "http://example/", 403, "Forbidden",
            hdrs=MagicMock(get=MagicMock(return_value=None), items=lambda: []),
            fp=None,
        )
        http_err.read = lambda: b"Access denied"
        mock_open.side_effect = http_err
        progress_records = []
        count = crawl_full_rest(
            kingdoms=(2,),
            rate_limit_qps=1000.0,
            resume_state_path=self._state_path,
            progress_cb=lambda n, name: progress_records.append((n, name)),
        )
        self.assertEqual(count, 0)
        # state file 在完成时被删，但 *_failed.json 应保留
        failed_log = self._state_path.with_name(self._state_path.stem + "_failed.json")
        self.assertTrue(failed_log.exists())
        try:
            payload = json.loads(failed_log.read_text())
            self.assertIn(2, payload["failed_aphia_ids"])
        finally:
            failed_log.unlink(missing_ok=True)
        # 进度回调应至少触发一次「跳过」通知
        self.assertTrue(any("AphiaID 2 跳过" in name for _, name in progress_records))

    @patch("urllib.request.urlopen")
    def test_ssl_error_retried_then_succeeds(self, mock_open):
        # 2026-05-16-b 修复：ssl.SSLError 加入瞬时重试白名单。
        # 首次 raise SSLError，第二次成功 → 总共 2 次调用，count=1。
        import ssl as _ssl
        ok = _make_response([_child(77, "sp", rank="Species")])
        mock_open.side_effect = [
            _ssl.SSLError("EOF occurred in violation of protocol"),
            ok,
        ]
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 1)
        self.assertEqual(mock_open.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_incomplete_read_retried_then_succeeds(self, mock_open):
        # http.client.IncompleteRead 在 resp.read() 阶段抛出，不经 URLError 包装。
        import http.client as _hc
        ok = _make_response([_child(88, "sp_b", rank="Species")])
        mock_open.side_effect = [
            _hc.IncompleteRead(partial=b"", expected=100),
            ok,
        ]
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 1)
        self.assertEqual(mock_open.call_count, 2)


class TestSkipFailed(_CrawlerTestBase):
    """2026-05-16-b 韧性：单节点持续失败 → 跳过 + 继续其他节点。"""

    @patch("urllib.request.urlopen")
    def test_persistent_failure_skips_and_continues(self, mock_open):
        # Kingdom 2 → 2 个 Genus 子节点（id=10, 20）。
        # Genus 10 整页拉取持续 SSL 失败（重试 7 次都失败）→ 跳过；
        # Genus 20 正常返回 Species 100。最终 count = 2（Genus 10 自身 + Species 100）+ Genus 20 自身 = 3。
        # （注：Genus 10 自身的记录在 Kingdom 2 的子列表中已写入，跳过的是其向下递归子节点）
        import ssl as _ssl
        ssl_err = _ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")

        def _side(req, **kw):
            url = req.full_url
            if "AphiaChildrenByAphiaID/2?" in url:
                return _make_response([
                    _child(10, "Genus_bad", rank="Genus"),
                    _child(20, "Genus_good", rank="Genus"),
                ])
            if "AphiaChildrenByAphiaID/10?" in url:
                # Genus 10 永久失败
                raise ssl_err
            if "AphiaChildrenByAphiaID/20?" in url:
                return _make_response([_child(100, "Species_ok", rank="Species")])
            return _make_response([])

        mock_open.side_effect = _side
        progress_records = []
        count = crawl_full_rest(
            kingdoms=(2,),
            rate_limit_qps=1000.0,
            resume_state_path=self._state_path,
            progress_cb=lambda n, name: progress_records.append((n, name)),
        )
        # 写入：Genus 10 (在 Kingdom 2 的子列表中) + Genus 20 (同) + Species 100 = 3
        self.assertEqual(count, 3)
        # Genus 10 应在 failed log 中
        failed_log = self._state_path.with_name(self._state_path.stem + "_failed.json")
        self.assertTrue(failed_log.exists())
        payload = json.loads(failed_log.read_text())
        self.assertIn(10, payload["failed_aphia_ids"])
        self.assertNotIn(20, payload["failed_aphia_ids"])
        failed_log.unlink(missing_ok=True)
        # 应有跳过通知
        self.assertTrue(any("AphiaID 10 跳过" in name for _, name in progress_records))

    @patch("urllib.request.urlopen")
    def test_visited_not_set_until_pagination_complete(self, mock_open):
        # 2026-05-16-b Bug2 修复：当节点分页中途抛错时，visited 不应含 current_id。
        # 用例：kingdom 2 第 1 页返回 50 条（满页 → 继续下一页），第 2 页 SSL 永久失败。
        # 结果：kingdom 2 进 failed（不在 visited），失败日志记录 2。
        import ssl as _ssl
        ssl_err = _ssl.SSLError("EOF mid-pagination")
        full_page = [_child(1000 + i, f"sp_{i}", rank="Species") for i in range(50)]

        call_log = []

        def _side(req, **kw):
            url = req.full_url
            call_log.append(url)
            if "offset=1" in url and "AphiaChildrenByAphiaID/2?" in url:
                return _make_response(full_page)
            if "offset=51" in url and "AphiaChildrenByAphiaID/2?" in url:
                raise ssl_err
            return _make_response([])

        mock_open.side_effect = _side
        crawl_full_rest(
            kingdoms=(2,),
            rate_limit_qps=1000.0,
            resume_state_path=self._state_path,
        )
        # 完成态 state 已删，但 *_failed.json 应记录 kingdom 2
        failed_log = self._state_path.with_name(self._state_path.stem + "_failed.json")
        self.assertTrue(failed_log.exists())
        payload = json.loads(failed_log.read_text())
        self.assertIn(2, payload["failed_aphia_ids"])
        failed_log.unlink(missing_ok=True)
        # 验证未触发"满页后无限请求"——重试 7 次 = offset=51 共 7 次调用
        offset_51_calls = sum(1 for u in call_log if "offset=51" in u)
        self.assertEqual(offset_51_calls, 7)


class TestMalformed(_CrawlerTestBase):
    """Malformed records (missing AphiaID, non-dict) are skipped silently."""

    @patch("urllib.request.urlopen")
    def test_skips_records_without_aphia_id(self, mock_open):
        def _side(req, **kw):
            url = req.full_url
            if "AphiaChildrenByAphiaID/2?" in url:
                return _make_response([
                    {"valid_name": "no_id"},      # missing AphiaID
                    "not a dict",                 # not a dict
                    _child(77, "good", rank="Species"),
                ])
            return _make_response([])
        mock_open.side_effect = _side
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        self.assertEqual(count, 1)
        stats = cache_stats()
        self.assertEqual(stats["count"], 1)


class TestDedup(_CrawlerTestBase):
    """Same AphiaID encountered twice (cycle) is visited only once."""

    @patch("urllib.request.urlopen")
    def test_visited_dedup(self, mock_open):
        # Kingdom 2 children = [Genus 10], Genus 10 children = [Kingdom 2 again, Species 100]
        # Crawler should detect 2 already visited and not infinite-loop.
        def _side(req, **kw):
            url = req.full_url
            if "AphiaChildrenByAphiaID/2?" in url:
                return _make_response([_child(10, "G", rank="Genus")])
            if "AphiaChildrenByAphiaID/10?" in url:
                return _make_response([
                    _child(2, "Kingdom_redundant", rank="Kingdom"),
                    _child(100, "sp", rank="Species"),
                ])
            return _make_response([])
        mock_open.side_effect = _side
        count = crawl_full_rest(kingdoms=(2,), rate_limit_qps=1000.0)
        # Records written: Genus 10, Kingdom-as-child 2, Species 100 → 3 rows
        self.assertEqual(count, 3)
        # But Kingdom 2 NOT re-fetched (visited) → urlopen called for id=2 + id=10 only
        urls = [c.args[0].full_url for c in mock_open.call_args_list]
        kingdom_calls = sum(1 for u in urls if "AphiaChildrenByAphiaID/2?" in u)
        self.assertEqual(kingdom_calls, 1)


if __name__ == "__main__":
    unittest.main()
