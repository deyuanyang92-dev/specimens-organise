"""图片检索：相关度排序 + 精准/模糊模式 + 索引 v2（无 tokens 表）。

用户 2026-10-01 报障：输入 GDLZ-LZC-OWC，结果没按相关度排、卡片「核心编号」只剩
gdlz-lzc；新增照片后建索引太慢。本文件锁死修复后的行为。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from specimen_app.image_index import (
    IMAGE_INDEX_SCHEMA_VERSION,
    ImageIndexStore,
    iter_images,
    scan_image_files,
)
from specimen_app.image_match import (
    MATCH_MODE_EXACT,
    MATCH_MODE_FUZZY,
    normalize_match_mode,
    rank_entries,
    token_match_level,
    tokenize,
)
from specimen_app.image_search import (
    ImageIndexEntry,
    ImageSearchIndex,
    image_search_results,
    suffixes_for_image_type,
)


def _entry(name: str, parent: Path = Path("/x")) -> ImageIndexEntry:
    path = parent / name
    return ImageIndexEntry(path=path, file_name=path.name, stem=path.stem, suffix=path.suffix.lower())


class TokenMatchTests(unittest.TestCase):
    def test_tokenize_splits_on_hyphen_underscore_and_lowercases(self) -> None:
        self.assertEqual(tokenize("GDLZ_LZC-OWC001--1"), ["gdlz", "lzc", "owc001", "1"])
        self.assertEqual(tokenize("  "), [])

    def test_alpha_query_token_may_extend_with_digits(self) -> None:
        # 用户敲站位码 OWC，期望 OWC001/OWC002 都算“编号延续”匹配
        self.assertEqual(token_match_level("owc", "owc"), 3)
        self.assertEqual(token_match_level("owc", "owc001"), 2)
        self.assertEqual(token_match_level("wensc", "wensc004"), 2)
        self.assertEqual(token_match_level("ow", "owc001"), 1)
        self.assertEqual(token_match_level("owc", "xowc"), 0)

    def test_numeric_query_token_must_not_extend_with_digits(self) -> None:
        # 旧规则保留：WenSC004 不能命中 WenSC0042
        self.assertEqual(token_match_level("sc004", "sc004"), 3)
        self.assertEqual(token_match_level("sc004", "sc0042"), 0)
        self.assertEqual(token_match_level("sc004", "sc004b"), 1)

    def test_normalize_match_mode_defaults_to_fuzzy(self) -> None:
        self.assertEqual(normalize_match_mode("exact"), MATCH_MODE_EXACT)
        self.assertEqual(normalize_match_mode("精准"), MATCH_MODE_EXACT)
        self.assertEqual(normalize_match_mode("whatever"), MATCH_MODE_FUZZY)
        self.assertEqual(normalize_match_mode(None), MATCH_MODE_FUZZY)


class RankEntriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            _entry("GDLZ-LZC-ONFC001-1-R-260602-杨等采集.tif"),
            _entry("GDLZ-LZC-ONFC001-2-R-260602-杨等采集.tif"),
            _entry("GDLZ-LZC-OWC002-2-20260601-杨等采集.tif"),
            _entry("GDLZ-LZC-OWC001-1-20260601-杨等采集.tif"),
            _entry("GDLZ-LZC-CK021-3-20260603.tif"),
            _entry("图200-GDLZ-LZC-OWC-背面.tif"),
        ]

    def test_full_match_ranks_above_partial_and_partial_is_hidden_when_full_exists(self) -> None:
        ranked = rank_entries(self.entries, "GDLZ-LZC-OWC", MATCH_MODE_FUZZY, limit=50)
        names = [item.entry.file_name for item in ranked]
        self.assertEqual(
            names,
            [
                "GDLZ-LZC-OWC001-1-20260601-杨等采集.tif",
                "GDLZ-LZC-OWC002-2-20260601-杨等采集.tif",
                "图200-GDLZ-LZC-OWC-背面.tif",
            ],
        )
        self.assertEqual([item.score for item in ranked], [95, 95, 90])
        self.assertTrue(all(item.matched_query == "GDLZ-LZC-OWC" for item in ranked))
        self.assertEqual([item.kind for item in ranked], ["prefix", "prefix", "inner"])

    def test_exact_mode_only_keeps_anchored_full_matches(self) -> None:
        ranked = rank_entries(self.entries, "GDLZ-LZC-OWC", MATCH_MODE_EXACT, limit=50)
        self.assertEqual(
            [item.entry.file_name for item in ranked],
            ["GDLZ-LZC-OWC001-1-20260601-杨等采集.tif", "GDLZ-LZC-OWC002-2-20260601-杨等采集.tif"],
        )

    def test_complete_number_scores_100_and_excludes_longer_numbers(self) -> None:
        entries = self.entries + [_entry("GDLZ-LZC-OWC0011-1.tif")]
        ranked = rank_entries(entries, "GDLZ-LZC-OWC001", MATCH_MODE_FUZZY, limit=50)
        self.assertEqual([item.entry.file_name for item in ranked], ["GDLZ-LZC-OWC001-1-20260601-杨等采集.tif"])
        self.assertEqual(ranked[0].score, 100)
        self.assertEqual(ranked[0].kind, "exact")

    def test_partial_fallback_reports_matched_prefix_and_low_score(self) -> None:
        ranked = rank_entries(self.entries, "GDLZ-LZC-OWC-99", MATCH_MODE_FUZZY, limit=50)
        self.assertEqual(
            [item.entry.file_name for item in ranked],
            [
                "GDLZ-LZC-OWC001-1-20260601-杨等采集.tif",
                "GDLZ-LZC-OWC002-2-20260601-杨等采集.tif",
                "图200-GDLZ-LZC-OWC-背面.tif",
            ],
        )
        self.assertTrue(all(item.kind == "partial" for item in ranked))
        self.assertTrue(all(item.score < 60 for item in ranked))
        self.assertTrue(all(item.matched_query == "GDLZ-LZC-OWC" for item in ranked))
        self.assertEqual(rank_entries(self.entries, "GDLZ-LZC-OWC-99", MATCH_MODE_EXACT, limit=50), [])

    def test_contains_match_scores_60_and_guards_digit_extension(self) -> None:
        entries = [_entry("sampleP001middle.webp"), _entry("x_QD-CK-WenSC0042.tif"), _entry("QDCKWenSC004x.tif")]
        ranked = rank_entries(entries, "P001", MATCH_MODE_FUZZY, limit=50)
        self.assertEqual([(item.entry.file_name, item.score, item.kind) for item in ranked], [("sampleP001middle.webp", 60, "contains")])
        # 没有任何真命中时才渐进退化：WenSC0042 以 partial（qd-ck）身份出现，分数 < 60，
        # 且不会冒充 WenSC004 的命中（matched_query 只到 QD-CK）
        ranked = rank_entries(entries, "QD-CK-WenSC004", MATCH_MODE_FUZZY, limit=50)
        self.assertEqual([(item.entry.file_name, item.kind, item.matched_query) for item in ranked], [("x_QD-CK-WenSC0042.tif", "partial", "QD-CK")])
        self.assertTrue(ranked[0].score < 60)
        self.assertEqual(rank_entries(entries, "QD-CK-WenSC004", MATCH_MODE_EXACT, limit=50), [])
        ranked = rank_entries(entries, "QDCKWenSC004", MATCH_MODE_FUZZY, limit=50)
        self.assertEqual([item.entry.file_name for item in ranked], ["QDCKWenSC004x.tif"])
        self.assertEqual(rank_entries(entries, "P001", MATCH_MODE_EXACT, limit=50), [])

    def test_limit_keeps_best_scores_not_first_filenames(self) -> None:
        entries = [_entry(f"AAA-{i:03d}-GDLZ-LZC-OWC.tif") for i in range(80)] + [_entry("GDLZ-LZC-OWC001-1.tif")]
        ranked = rank_entries(entries, "GDLZ-LZC-OWC", MATCH_MODE_FUZZY, limit=10)
        self.assertEqual(ranked[0].entry.file_name, "GDLZ-LZC-OWC001-1.tif")
        self.assertEqual(len(ranked), 10)


class ImageSearchResultsRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.photo_dir = self.tmp / "照片"
        self.photo_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *names: str) -> None:
        for name in names:
            (self.photo_dir / name).write_bytes(b"image")

    def test_results_sorted_by_relevance_and_label_is_typed_query(self) -> None:
        self._touch(
            "GDLZ-LZC-ONFC001-1-R-260602.tif",
            "GDLZ-LZC-ONFC001-2-R-260602.tif",
            "GDLZ-LZC-OWC002-2-20260601.tif",
            "GDLZ-LZC-OWC001-1-20260601.tif",
        )
        results = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC")
        self.assertEqual(
            [result.file_name for result in results],
            ["GDLZ-LZC-OWC001-1-20260601.tif", "GDLZ-LZC-OWC002-2-20260601.tif"],
        )
        self.assertEqual(results[0].matched_keywords, ("GDLZ-LZC-OWC",))
        self.assertEqual(results[0].score, 95)
        self.assertEqual(results[0].match_kind, "prefix")

    def test_exact_mode_drops_inner_and_partial_matches(self) -> None:
        self._touch("GDLZ-LZC-OWC001-1.tif", "图200-GDLZ-LZC-OWC-背面.tif", "GDLZ-LZC-ONFC001-1.tif")
        fuzzy = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC", match_mode="fuzzy")
        exact = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC", match_mode="exact")
        self.assertEqual([r.file_name for r in fuzzy], ["GDLZ-LZC-OWC001-1.tif", "图200-GDLZ-LZC-OWC-背面.tif"])
        self.assertEqual([r.file_name for r in exact], ["GDLZ-LZC-OWC001-1.tif"])
        partial = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC-77", match_mode="exact")
        self.assertEqual(partial, [])

    def test_tif_only_search_is_not_starved_by_many_jpgs(self) -> None:
        for i in range(300):
            (self.photo_dir / f"GDLZ-LZC-OWC{i:03d}-1.jpg").write_bytes(b"jpg")
        (self.photo_dir / "GDLZ-LZC-OWC999-1.tif").write_bytes(b"tif")
        results = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC", limit=50)
        self.assertEqual([r.file_name for r in results], ["GDLZ-LZC-OWC999-1.tif"])

    def test_in_memory_index_uses_same_token_rule(self) -> None:
        self._touch("GDLZ-LZC-OWC001-1.tif", "GDLZ-LZC-ONFC001-1.tif")
        index = ImageSearchIndex()
        index.build([_entry(p.name, self.photo_dir) for p in sorted(self.photo_dir.iterdir())])
        hits = index.search("GDLZ-LZC-OWC")
        self.assertEqual([index.entries[i].file_name for i in hits], ["GDLZ-LZC-OWC001-1.tif"])


class ImageIndexV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.photo_dir = self.tmp / "照片"
        self.photo_dir.mkdir()
        (self.tmp / "数据").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_stores_one_row_per_file_and_no_tokens_table(self) -> None:
        for i in range(40):
            (self.photo_dir / f"GDLZ-LZC-OWC{i:03d}-1-20260601-杨等采集-钟珅拍摄.tif").write_bytes(b"x")
        store = ImageIndexStore(self.tmp)
        update = store.reconcile_scope([self.photo_dir], 0)
        self.assertEqual((update.added, update.scanned), (40, 40))
        conn = sqlite3.connect(store.path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("tokens", tables)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 40)
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(int(version), IMAGE_INDEX_SCHEMA_VERSION)
        finally:
            conn.close()

    def test_legacy_tokens_table_is_dropped_without_rescanning_entries(self) -> None:
        (self.photo_dir / "GDLZ-LZC-OWC001-1.tif").write_bytes(b"x")
        store = ImageIndexStore(self.tmp)
        store.reconcile_scope([self.photo_dir], 0)
        # 伪造 v1 遗留：tokens 表 + 无 meta
        conn = sqlite3.connect(store.path)
        try:
            conn.execute("DROP TABLE IF EXISTS meta")
            conn.executescript(
                "CREATE TABLE tokens (scope_key TEXT, token TEXT, path TEXT, PRIMARY KEY (scope_key, token, path));"
                "CREATE INDEX idx_image_tokens_lookup ON tokens (scope_key, token, path);"
            )
            conn.execute("INSERT INTO tokens VALUES ('k', 'g', '/p')")
            conn.commit()
        finally:
            conn.close()
        # 新文件在磁盘上但索引里没有：只读查询不重扫（它不出现），也不迁移（tokens 还在，见 GUI 线程安全测试）
        (self.photo_dir / "GDLZ-LZC-OWC002-1.tif").write_bytes(b"x")
        reopened = ImageIndexStore(self.tmp)
        self.assertTrue(reopened.has_scope([self.photo_dir], 0))
        names = sorted(entry.file_name for entry in reopened.entries([self.photo_dir], 0))
        self.assertEqual(names, ["GDLZ-LZC-OWC001-1.tif"])
        # 2026-10-02 起：迁移只在 reconcile（工作线程）里做——entries 原样保留（不会因迁移丢数据），tokens 被 DROP
        update = reopened.reconcile_scope([self.photo_dir], 0)
        self.assertEqual(update.added, 1)
        conn = sqlite3.connect(store.path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("tokens", tables)
            self.assertIn("meta", tables)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        finally:
            conn.close()

    def test_scan_image_files_returns_stat_and_matches_iter_images(self) -> None:
        deep = self.photo_dir / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (self.photo_dir / "top.tif").write_bytes(b"1234")
        (deep / "deep.jpg").write_bytes(b"12")
        (self.photo_dir / "note.txt").write_bytes(b"no")
        scanned = scan_image_files([self.photo_dir], max_depth=0)
        by_name = {item.path.name: item for item in scanned}
        self.assertEqual(set(by_name), {"top.tif", "deep.jpg"})
        self.assertEqual(by_name["top.tif"].size, 4)
        self.assertGreater(by_name["top.tif"].mtime_ns, 0)
        self.assertEqual({p.name for p in iter_images([self.photo_dir])}, {"top.tif", "deep.jpg"})
        self.assertEqual({p.name for p in iter_images([self.photo_dir], max_depth=2)}, {"top.tif"})

    def test_reconcile_detects_added_changed_removed(self) -> None:
        first = self.photo_dir / "GDLZ-LZC-OWC001-1.tif"
        first.write_bytes(b"x")
        store = ImageIndexStore(self.tmp)
        self.assertEqual(store.reconcile_scope([self.photo_dir], 0).added, 1)
        time.sleep(0.02)
        first.write_bytes(b"xyz")
        second = self.photo_dir / "GDLZ-LZC-OWC002-1.tif"
        second.write_bytes(b"x")
        update = store.reconcile_scope([self.photo_dir], 0)
        self.assertEqual((update.added, update.changed, update.removed), (1, 1, 0))
        first.unlink()
        update = store.reconcile_scope([self.photo_dir], 0)
        self.assertEqual((update.added, update.changed, update.removed), (0, 0, 1))
        self.assertEqual([e.file_name for e in store.entries([self.photo_dir], 0)], ["GDLZ-LZC-OWC002-1.tif"])

    def test_search_candidates_filters_suffix_in_sql(self) -> None:
        (self.photo_dir / "GDLZ-LZC-OWC001-1.jpg").write_bytes(b"x")
        (self.photo_dir / "GDLZ-LZC-OWC001-2.tif").write_bytes(b"x")
        store = ImageIndexStore(self.tmp)
        store.reconcile_scope([self.photo_dir], 0)
        rows = list(store.iter_candidates([self.photo_dir], "gdlz", suffixes_for_image_type("tif"), 0))
        self.assertEqual([entry.file_name for entry in rows], ["GDLZ-LZC-OWC001-2.tif"])


class ImageIndexGuiThreadSafetyTests(unittest.TestCase):
    """只读查询（UI 线程会调）不得触发 v1→v2 迁移 / 建库 / DDL；迁移只在 reconcile（工作线程）里做。
    用户 0.10.32 升级后"一打开就未响应"：旧 tokens 表可达数百 MB，DROP 它在 GUI 线程上要几秒。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.photo_dir = self.tmp / "照片"
        self.photo_dir.mkdir()
        (self.tmp / "数据").mkdir()
        (self.photo_dir / "GDLZ-LZC-OWC001-1.tif").write_bytes(b"x")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_v1_db(self, store: ImageIndexStore) -> None:
        store.reconcile_scope([self.photo_dir], 0)
        conn = sqlite3.connect(store.path)
        try:
            conn.execute("DROP TABLE IF EXISTS meta")
            conn.executescript("CREATE TABLE tokens (scope_key TEXT, token TEXT, path TEXT, PRIMARY KEY (scope_key, token, path));")
            conn.execute("INSERT INTO tokens VALUES ('k', 'g', '/p')")
            conn.commit()
        finally:
            conn.close()

    def _tables(self, store: ImageIndexStore) -> set[str]:
        conn = sqlite3.connect(store.path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def test_read_queries_do_not_migrate_or_create(self):
        store = ImageIndexStore(self.tmp)
        self.assertFalse(store.has_scope([self.photo_dir], 0))
        self.assertIsNone(store.get_scope_last_scan_timestamp([self.photo_dir], 0))
        self.assertFalse(store.path.exists(), "只读查询不该建库文件")
        self._make_v1_db(store)
        reopened = ImageIndexStore(self.tmp)
        self.assertTrue(reopened.has_scope([self.photo_dir], 0))
        self.assertIsNotNone(reopened.get_scope_last_scan_timestamp([self.photo_dir], 0))
        self.assertEqual(len(reopened.entries([self.photo_dir], 0)), 1)
        self.assertIn("tokens", self._tables(reopened), "只读路径不得做迁移（DROP tokens 可能很慢）")

    def test_reconcile_performs_migration(self):
        store = ImageIndexStore(self.tmp)
        self._make_v1_db(store)
        ImageIndexStore(self.tmp).reconcile_scope([self.photo_dir], 0)
        self.assertNotIn("tokens", self._tables(store))


class SettingsMatchModeTests(unittest.TestCase):
    def test_match_mode_round_trips_through_settings(self) -> None:
        import os
        from unittest.mock import patch
        from specimen_app import app_settings
        tmp = Path(tempfile.mkdtemp())
        try:
            with patch.object(app_settings, "settings_path", return_value=tmp / "settings.json"):
                settings = app_settings.load_settings()
                self.assertEqual(settings.image_search_match_mode, "fuzzy")
                settings.image_search_match_mode = "exact"
                app_settings.save_settings(settings)
                self.assertEqual(app_settings.load_settings().image_search_match_mode, "exact")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
