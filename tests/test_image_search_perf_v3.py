"""图片检索性能 v3（用户：加载超级慢 / 像每次重建索引 / 建索引时卡顿；工作区在网络盘 M:）。

  * 索引库与缩略图缓存放本机（app 配置目录），不再放工作区 —— 网络盘工作区的每次读写不再走 SMB。
  * 只读打开用正确的 file:/// URI（Windows 盘符 + 中文路径）。
  * 作用域状态（有没有索引 / 上次扫描时间）进程内记忆：主线程刷新/打字不再打开数据库文件。
  * 根目录 mtime 没变且上次扫描在 10 分钟内 → 不扫描（Everything 式：不变就不动）。
  * 结果装配不再对每条候选 resolve()；exists() 只对最终返回的结果做。
  * 缩略图路径不 resolve（一次网络往返）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from specimen_app import app_settings


class LocalCacheDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "cfg"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self._p = patch.object(app_settings, "app_config_dir", return_value=self.cfg)
        self._p.start()
        # tests/__init__.py 给全体测试设了 SPECIMEN_LOCAL_CACHE_DIR；本类要验证"默认落在 app 配置目录"，故清掉
        self._env = patch.dict(os.environ, {"SPECIMEN_LOCAL_CACHE_DIR": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_dir_is_under_app_config_not_workspace(self):
        from specimen_app.local_cache import local_cache_dir

        d = local_cache_dir(self.ws, "image_index")
        self.assertTrue(str(d).startswith(str(self.cfg)))
        self.assertFalse(str(d).startswith(str(self.ws)))
        self.assertTrue(d.is_dir())
        self.assertEqual(local_cache_dir(self.ws, "image_index"), d)  # 稳定
        other = self.tmp / "ws2"
        other.mkdir()
        self.assertNotEqual(local_cache_dir(other, "image_index"), d)
        self.assertNotEqual(local_cache_dir(self.ws, "thumbnails"), d)

    def test_image_index_store_and_thumbnail_cache_live_locally(self):
        from specimen_app.image_cache import ThumbnailCache
        from specimen_app.image_index import ImageIndexStore

        store = ImageIndexStore(self.ws)
        self.assertTrue(str(store.path).startswith(str(self.cfg)))
        self.assertFalse((self.ws / "数据" / "图片搜索索引缓存").exists())
        cache = ThumbnailCache(self.ws)
        self.assertTrue(str(cache.cache_dir).startswith(str(self.cfg)))
        self.assertFalse((self.ws / "数据" / "缩略图缓存").exists())

    def test_read_only_uri_is_file_scheme_with_percent_encoding(self):
        from specimen_app.local_cache import read_only_sqlite_uri

        p = self.tmp / "标本 数据.sqlite"
        uri = read_only_sqlite_uri(p)
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertTrue(uri.endswith("?mode=ro"), uri)
        self.assertNotIn(" ", uri)
        self.assertNotIn("标", uri)  # 已 percent-encode
        import sqlite3

        sqlite3.connect(str(p)).close()
        conn = sqlite3.connect(uri, uri=True)
        conn.close()


class ScopeFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "cfg"
        self.ws = self.tmp / "ws"
        self.photos = self.ws / "照片"
        self.photos.mkdir(parents=True)
        (self.ws / "数据").mkdir()
        (self.photos / "GDLZ-LZC-OWC001-1.tif").write_bytes(b"x")
        self._p = patch.object(app_settings, "app_config_dir", return_value=self.cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scope_needs_reconcile_rules(self):
        from specimen_app.image_search import clear_image_index, reconcile_image_index, scope_needs_reconcile

        clear_image_index()
        self.assertTrue(scope_needs_reconcile(self.ws))  # 从未扫描
        reconcile_image_index(self.ws)
        self.assertFalse(scope_needs_reconcile(self.ws))  # 刚扫过，根目录没变
        time.sleep(1.1)
        (self.photos / "GDLZ-LZC-OWC002-1.tif").write_bytes(b"x")  # 根目录 mtime 变了
        self.assertTrue(scope_needs_reconcile(self.ws))
        reconcile_image_index(self.ws)
        self.assertFalse(scope_needs_reconcile(self.ws))
        self.assertTrue(scope_needs_reconcile(self.ws, max_age_seconds=0))  # 超龄强制

    def test_scope_state_is_memoised_and_main_thread_does_not_open_db(self):
        from specimen_app import image_index, image_search

        image_search.clear_image_index()
        image_search.reconcile_image_index(self.ws)
        self.assertTrue(image_search.image_index_exists(self.ws))
        with patch.object(image_index.ImageIndexStore, "_connect_read_only", side_effect=AssertionError("主线程不该打开索引库")):
            self.assertTrue(image_search.image_index_exists(self.ws))
            self.assertIsNotNone(image_search.get_image_index_last_scan_timestamp(self.ws))
        image_search.clear_image_index(self.ws)
        self.assertFalse(image_search.image_index_exists(self.ws))


class ResultAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "cfg"
        self.ws = self.tmp / "ws"
        self.photos = self.ws / "照片"
        self.photos.mkdir(parents=True)
        (self.ws / "数据").mkdir()
        for i in range(120):
            (self.photos / f"GDLZ-LZC-OWC{i:03d}-1.tif").write_bytes(b"x")
        self._p = patch.object(app_settings, "app_config_dir", return_value=self.cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_resolve_and_exists_only_for_returned_results(self):
        from specimen_app.image_search import clear_image_index, image_search_results

        clear_image_index()
        image_search_results(self.ws, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC", limit=10)  # 建索引
        exists_calls = []
        real_exists = Path.exists

        def counting_exists(self_path):
            exists_calls.append(str(self_path))
            return real_exists(self_path)

        with patch.object(Path, "resolve", side_effect=AssertionError("结果装配不该 resolve()")), patch.object(Path, "exists", counting_exists):
            results = image_search_results(self.ws, "YZZ000001", {}, {}, [], query="GDLZ-LZC-OWC", limit=10)
        self.assertEqual(len(results), 10)
        tif_checks = [p for p in exists_calls if p.endswith(".tif")]
        self.assertLessEqual(len(tif_checks), 12, f"exists() 调了 {len(tif_checks)} 次（应只对返回的结果做）")

    def test_thumbnail_cache_does_not_resolve_source(self):
        from PIL import Image

        from specimen_app.image_cache import ThumbnailCache

        img = self.photos / "real.jpg"
        Image.new("RGB", (64, 48), "red").save(img, "JPEG")
        cache = ThumbnailCache(self.ws)
        with patch.object(Path, "resolve", side_effect=AssertionError("缩略图不该 resolve()")):
            out = cache.thumbnail(img, (32, 32))
        self.assertLessEqual(max(out.size), 32)

    def test_legacy_workspace_thumbnail_is_reused_not_regenerated(self):
        from PIL import Image

        from specimen_app import image_cache
        from specimen_app.image_cache import ThumbnailCache

        img = self.photos / "legacy.jpg"
        Image.new("RGB", (64, 48), "green").save(img, "JPEG")
        legacy_dir = self.ws / "数据" / "缩略图缓存"
        legacy_dir.mkdir(parents=True)
        cache = ThumbnailCache(self.ws)
        key = cache._cache_key(Path(os.path.abspath(str(img))), (32, 32))
        Image.new("RGB", (32, 24), "magenta").save(legacy_dir / f"{key}.jpg", "JPEG")
        with patch.object(image_cache, "load_source_image", side_effect=AssertionError("旧缩略图存在时不该重新解码原图")):
            out = cache.thumbnail(img, (32, 32))
        self.assertEqual(out.size, (32, 24))
        self.assertTrue((cache.cache_dir / f"{key}.jpg").exists())

    def test_reconcile_discards_legacy_workspace_index_dir(self):
        from specimen_app.image_index import ImageIndexStore

        legacy = self.ws / "数据" / "图片搜索索引缓存"
        legacy.mkdir(parents=True)
        (legacy / "image_search.sqlite3").write_bytes(b"old")
        (legacy / "abc.json").write_text("{}", encoding="utf-8")
        ImageIndexStore(self.ws).reconcile_scope([self.photos], 0)
        self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
