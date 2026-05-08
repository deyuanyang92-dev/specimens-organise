from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from PIL import Image

from specimen_app.app_settings import load_settings, save_settings, settings_path
from specimen_app.excel_store import ExcelStore
from specimen_app.image_cache import ThumbnailCache
from specimen_app.image_search import (
    append_images_to_index,
    clear_image_index,
    default_image_query,
    extract_core_identifier,
    image_index_exists,
    image_search_results,
    iter_workspace_images,
    suffixes_for_image_type,
)
from specimen_app.models import ImportConflictError, WorkspaceNotInitializedError
from specimen_app.parsing import extract_bottle_label, extract_collection_date, extract_location_code
from specimen_app.release_manager import list_releases
from specimen_app.ui import grid_shape
from specimen_app.workspace import has_workspace_data, initialize_workspace, is_generated_workspace_path, is_workspace


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_image_index()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)
        clear_image_index()

    def test_create_vouchers_increment(self) -> None:
        store = ExcelStore(self.tmp)
        self.assertEqual(store.create_specimen(), "YZZ000001")
        self.assertEqual(store.create_specimen(), "YZZ000002")
        self.assertEqual(store.list_vouchers(), ["YZZ000001", "YZZ000002"])

    def test_tube_number_parsing(self) -> None:
        self.assertEqual(extract_location_code("QD-LSD-SC001-1-R-250923"), "QD-LSD")
        self.assertEqual(extract_bottle_label("QD-LSD-SC001-1-R-250923"), "QD-LSD-SC001")
        self.assertEqual(extract_bottle_label("QD-CK-SC008-260827"), "QD-CK-SC008")
        self.assertEqual(extract_collection_date("QD-LSD-SC001-1-R-250923"), "2025-09-23")
        self.assertEqual(extract_collection_date("QD-LSD-SC001-20250923"), "2025-09-23")

    def test_set_field_autofills_tube_derived_fields(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        row = store.get_specimen(voucher)
        self.assertEqual(row["采集日期"], "2025-09-23")
        self.assertEqual(row["采集地点缩写*"], "QD-LSD")
        store.set_fields("specimen", voucher, {"管内编号*": "XM-ABC-SC001-1-R-250924"})
        row = store.get_specimen(voucher)
        self.assertEqual(row["采集日期"], "2025-09-24")
        self.assertEqual(row["采集地点缩写*"], "XM-ABC")

    def test_undo_redo_field_update(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")
        store.undo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")

    def test_redo_order_after_multiple_undo(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        store.set_fields("specimen", voucher, {"保存方式": "RE"})
        store.undo_last()
        store.undo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "")
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "RE")

    def test_import_conflict_blocks_write(self) -> None:
        target = self.tmp / "target"
        source = self.tmp / "source"
        target.mkdir()
        source.mkdir()
        target_store = ExcelStore(target)
        voucher = target_store.create_specimen()
        target_store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})

        source_store = ExcelStore(source)
        source_voucher = source_store.create_specimen()
        self.assertEqual(source_voucher, voucher)
        source_store.set_fields("specimen", source_voucher, {"管内编号*": "QD-CK-SC008-1-R-250923"})

        with self.assertRaises(ImportConflictError) as ctx:
            target_store.import_workspace(source)
        self.assertIsNotNone(ctx.exception.report_path)
        self.assertEqual(target_store.list_vouchers(), [voucher])

    def test_import_same_record_skips(self) -> None:
        target = self.tmp / "target_same"
        source = self.tmp / "source_same"
        target.mkdir()
        source.mkdir()
        target_store = ExcelStore(target)
        voucher = target_store.create_specimen()
        target_store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})

        source_store = ExcelStore(source)
        source_voucher = source_store.create_specimen()
        source_store.set_fields("specimen", source_voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})

        result = target_store.import_workspace(source)
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(target_store.list_vouchers(), [voucher])

    def test_duplicate_detection(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        path = self.tmp / "数据" / "标本信息.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["入库编号*", "管内编号*", "保存方式", "采集日期", "采集地点缩写*", "入库日期", "标本存放位置", "备注"])
        ws.append(["YZZ000001", "A", "", "", "", "", "", ""])
        ws.append(["YZZ000001", "B", "", "", "", "", "", ""])
        wb.save(path)
        with self.assertRaises(ImportConflictError):
            store.assert_unique_vouchers()

    def test_multiple_photo_records_are_preserved(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "photo1.tif"
        second = photo_dir / "photo2.jpg"
        first.write_bytes(b"fake-tiff")
        second.write_bytes(b"fake-jpg")
        store.add_photo(voucher, first)
        store.add_photo(voucher, second)
        photos = store.get_photos(voucher)
        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0]["文件名"], "photo1.tif")
        self.assertEqual(photos[1]["文件名"], "photo2.jpg")
        self.assertEqual(photos[0]["相对路径"], "./照片/photo1.tif")
        self.assertEqual(photos[1]["相对路径"], "./照片/photo2.jpg")

    def test_grid_filename_setting_defaults_and_roundtrips(self) -> None:
        old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.tmp / "config")
        try:
            path = settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"preview_quality":"standard"}', encoding="utf-8")
            settings = load_settings()
            self.assertTrue(settings.show_grid_filenames)

            settings.show_grid_filenames = False
            save_settings(settings)
            self.assertFalse(load_settings().show_grid_filenames)
        finally:
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata

    def test_batch_photo_records_undo_and_redo_as_one_action(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        paths = []
        for name in ["batch1.jpg", "batch2.jpg", "batch3.jpg"]:
            path = photo_dir / name
            path.write_bytes(b"fake-jpg")
            paths.append(path)

        added = store.add_photos(voucher, paths)
        self.assertEqual(len(added), 3)
        self.assertEqual(len(store.get_photos(voucher)), 3)
        self.assertEqual(store.undo_last(), "add_photos")
        self.assertEqual(store.get_photos(voucher), [])
        self.assertEqual(store.redo_last(), "add_photos")
        self.assertEqual([row["文件名"] for row in store.get_photos(voucher)], ["batch1.jpg", "batch2.jpg", "batch3.jpg"])

    def test_image_scan_excludes_generated_and_version_dirs(self) -> None:
        kept = self.tmp / "采集照片" / "YZZ000001_QD-LSD.jpg"
        kept.parent.mkdir()
        kept.write_bytes(b"jpg")
        for relative in [
            "build/tmp.jpg",
            "dist/app.jpg",
            "releases/v0.2.2/old.jpg",
            "数据/数据版本/snapshot.jpg",
            "数据/缩略图缓存/cache.jpg",
        ]:
            path = self.tmp / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"jpg")

        scanned = {path.relative_to(self.tmp).as_posix() for path in iter_workspace_images(self.tmp)}
        self.assertEqual(scanned, {"采集照片/YZZ000001_QD-LSD.jpg"})

    def test_image_search_requires_core_identifier_at_file_name_start(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "QD-CK-WenSC004-2-20250923-青岛沧口吻沙蚕.tif"
        second = photo_dir / "QD_CK_WenSC004_3_20250923.tif"
        wrong_number = photo_dir / "QD-CK-WenSC0042-20250923.tif"
        wrong_core = photo_dir / "QD-CK-WenSC005-20250923.tif"
        camera_name = photo_dir / "PB060001.tif"
        jpg_match = photo_dir / "QD-CK-WenSC004-4-20250923.jpg"
        for path in [first, second, wrong_number, wrong_core, camera_name, jpg_match]:
            path.write_bytes(b"image")

        results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [second],
            query="QD-CK-WenSC004",
        )
        self.assertEqual([result.file_name for result in results], [first.name, second.name])
        self.assertEqual(results[0].matched_keywords, ("QD-CK-WenSC004",))
        self.assertTrue(results[1].is_linked)

    def test_image_search_can_switch_to_jpg_or_tif_jpg(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        tif = photo_dir / "QD-CK-WenSC004-1.tif"
        jpg = photo_dir / "QD-CK-WenSC004-2.jpg"
        jpeg = photo_dir / "QD-CK-WenSC004-3.jpeg"
        for path in [tif, jpg, jpeg]:
            path.write_bytes(b"image")

        default_results = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in default_results], [tif.name])

        jpg_results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="QD-CK-WenSC004",
            suffixes=suffixes_for_image_type("jpg"),
        )
        self.assertEqual([result.file_name for result in jpg_results], [jpg.name, jpeg.name])

        combined_results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="QD-CK-WenSC004",
            suffixes=suffixes_for_image_type("tif_jpg"),
        )
        self.assertEqual([result.file_name for result in combined_results], [tif.name, jpg.name, jpeg.name])

    def test_image_search_empty_query_does_not_scan_or_match(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        path = photo_dir / "QD-CK-WenSC004-1.tif"
        path.write_bytes(b"image")
        self.assertEqual(image_search_results(self.tmp, "YZZ000001", {}, {}, [], query=""), [])

    def test_image_search_reuses_index_and_force_rebuilds(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "QD-CK-WenSC004-1.tif"
        second = photo_dir / "QD-CK-WenSC004-2.tif"
        first.write_bytes(b"image")
        initial = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in initial], [first.name])

        second.write_bytes(b"image")
        cached = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in cached], [first.name])
        rebuilt = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="QD-CK-WenSC004",
            force_rebuild=True,
        )
        self.assertEqual([result.file_name for result in rebuilt], [first.name, second.name])

    def test_image_search_index_appends_new_photos_without_rescan(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "QD-CK-WenSC004-1.tif"
        second = photo_dir / "QD-CK-WenSC004-2.tif"
        third = photo_dir / "QD-CK-WenSC004-3.jpg"
        first.write_bytes(b"image")

        initial = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in initial], [first.name])
        self.assertTrue(image_index_exists(self.tmp))

        second.write_bytes(b"image")
        self.assertEqual(append_images_to_index(self.tmp, [second]), 1)
        appended = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in appended], [first.name, second.name])
        self.assertEqual(append_images_to_index(self.tmp, [second]), 0)

        third.write_bytes(b"image")
        clear_image_index()
        self.assertEqual(append_images_to_index(self.tmp, [third]), 1)
        combined = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="QD-CK-WenSC004",
            suffixes=suffixes_for_image_type("tif_jpg"),
        )
        self.assertEqual([result.file_name for result in combined], [first.name, second.name, third.name])

    def test_image_search_uses_core_identifier_from_tube_number(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "QD-CK-SC008-1-20250923-青岛沧口沙蚕.tif"
        second = photo_dir / "QD-CK-SC008-2-20250923-青岛沧口沙蚕.tif"
        unrelated = photo_dir / "QD-CK-WSC003-20250923-青岛沧口围沙蚕.tif"
        for path in [first, second, unrelated]:
            path.write_bytes(b"image")

        specimen = {"管内编号*": "QD-CK-SC008-260827", "采集地点缩写*": "QD-CK"}
        self.assertEqual(default_image_query(specimen), "QD-CK-SC008")
        self.assertEqual(extract_core_identifier("QD_CK_SC008_260827"), "QD-CK-SC008")
        results = image_search_results(self.tmp, "YZZ000003", specimen, {}, [], query="QD-CK-SC008-260827")
        self.assertEqual([result.file_name for result in results], [first.name, second.name])

    def test_thumbnail_cache_reuses_and_invalidates_by_source_metadata(self) -> None:
        source = self.tmp / "照片"
        source.mkdir()
        image_path = source / "large.jpg"
        Image.new("RGB", (120, 80), "red").save(image_path)
        cache = ThumbnailCache(self.tmp)
        first = cache.thumbnail(image_path, (40, 40))
        cached_files = sorted((self.tmp / "数据" / "缩略图缓存").glob("*.jpg"))
        self.assertEqual(len(cached_files), 1)
        second = cache.thumbnail(image_path, (40, 40))
        self.assertEqual(second.size, first.size)
        self.assertEqual(sorted((self.tmp / "数据" / "缩略图缓存").glob("*.jpg")), cached_files)
        Image.new("RGB", (130, 90), "blue").save(image_path)
        third = cache.thumbnail(image_path, (40, 40))
        self.assertLessEqual(third.width, 40)
        self.assertEqual(len(list((self.tmp / "数据" / "缩略图缓存").glob("*.jpg"))), 2)

    def test_grid_shape_options(self) -> None:
        self.assertEqual(grid_shape(2), (2, 1))
        self.assertEqual(grid_shape(4), (2, 2))
        self.assertEqual(grid_shape(6), (3, 2))
        self.assertEqual(grid_shape(8), (4, 2))

    def test_snapshot_and_restore(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        snapshot = store.create_data_snapshot("测试快照", "保存初始管内编号")
        store.set_fields("specimen", voucher, {"管内编号*": "XM-ABC-SC001-1-R-250924"})
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "XM-ABC-SC001-1-R-250924")
        store.restore_data_snapshot(snapshot)
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")
        versions = store.list_data_versions()
        self.assertTrue(any(row["操作类型"] == "回退数据版本" for row in versions))

    def test_workspace_initialization_and_detection(self) -> None:
        source = self.tmp / "source_workspace"
        target = self.tmp / "new_workspace"
        (source / "字段模版").mkdir(parents=True)
        (source / "字段模版" / "表格信息预设字段.xlsx").write_text("placeholder", encoding="utf-8")
        initialize_workspace(target, source)
        self.assertTrue(is_workspace(target))
        self.assertTrue((target / "数据").exists())
        self.assertTrue((target / "字段模版" / "表格信息预设字段.xlsx").exists())
        self.assertTrue(has_workspace_data(ExcelStore(target).root))

    def test_excel_store_does_not_create_uninitialized_workspace_without_permission(self) -> None:
        target = self.tmp / "empty_workspace"
        target.mkdir()
        with self.assertRaises(WorkspaceNotInitializedError):
            ExcelStore(target, create_if_missing=False)
        self.assertFalse((target / "数据").exists())

    def test_generated_release_dirs_are_not_valid_workspaces(self) -> None:
        release_dir = self.tmp / "releases" / "v0.2.3"
        (release_dir / "数据").mkdir(parents=True)
        (release_dir / "数据" / "工作区配置.json").write_text("{}", encoding="utf-8")
        self.assertTrue(is_generated_workspace_path(release_dir))
        self.assertFalse(is_workspace(release_dir))

    def test_release_listing(self) -> None:
        release_dir = self.tmp / "releases" / "v0.2.0"
        release_dir.mkdir(parents=True)
        if sys.platform == "win32":
            exe = release_dir / "标本入库管理_v0.2.0.exe"
            exe.write_bytes(b"exe")
        else:
            exe = release_dir / "标本入库管理_v0.2.0"
            exe.write_text("#!/bin/sh", encoding="utf-8")
            os.chmod(exe, 0o755)
        (release_dir / "release_notes.md").write_text("# notes", encoding="utf-8")
        releases = list_releases(self.tmp)
        self.assertEqual(releases[0].version, "v0.2.0")
        self.assertEqual(releases[0].exe_path, exe)


if __name__ == "__main__":
    unittest.main()
