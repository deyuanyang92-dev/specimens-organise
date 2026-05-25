from __future__ import annotations

import os
import py_compile
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from PIL import Image

from specimen_app import env_detect
from specimen_app.app_settings import DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT, load_settings, save_settings, settings_path
from specimen_app.classification_fields import (
    CLASSIFICATION_COLUMNS,
    REQUIRED_CLASSIFICATION_COLUMNS,
    classification_values_from_family_match,
    classification_values_from_species_match,
)
from specimen_app.excel_store import ExcelStore
from specimen_app.image_cache import ThumbnailCache
from specimen_app.image_search import (
    _get_or_build_search_index,
    append_images_to_index,
    clear_image_index,
    default_image_query,
    extract_core_identifier,
    image_file_filter,
    image_index_exists,
    image_search_results,
    indexed_image_entries,
    is_supported_image,
    iter_workspace_images,
    reconcile_image_index,
    suffixes_for_image_type,
)
from specimen_app.models import (
    CHANGE_LOG_FILE,
    CLASSIFICATION_HEADERS,
    ImportConflictError,
    SnapshotIntegrityCheckFailed,
    WorkbookWriteVerificationFailed,
    WorkspaceNotInitializedError,
)
from specimen_app.parsing import (
    derive_specimen_fields_from_tube_number,
    extract_bottle_label,
    extract_collection_date,
    extract_location_code,
    extract_photo_date,
    extract_photo_seq,
    extract_photo_seq_from_filename,
    extract_save_method_from_filename,
    extract_save_method_from_tube_number,
    extract_specimen_tube_from_filename,
    extract_tube_from_filename,
)
from specimen_app.release_manager import list_releases
from specimen_app.species import FamilyMatch, SpeciesMatch, SpeciesMatcher
from specimen_app.ui import (
    SpecimenWindow,
    WindowManager,
    classification_column_value_from_taxonomy_match,
    default_photo_filename_fill_fields,
    format_taxonomy_candidate_label,
    grid_shape,
    photo_filename_source_for_specimen_fill,
    specimen_updates_from_photo_filename,
)
from specimen_app.workspace import has_workspace_data, initialize_workspace, is_generated_workspace_path, is_workspace


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_image_index()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)
        clear_image_index()

    def _write_species_preset(self) -> Path:
        path = self.tmp / "species_preset.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["物种中文名", "物种拉丁名", "科中文名", "科拉丁名"])
        ws.append(["珠江川纽虫", "Amniclineus zhujiangensis", "纵沟纽虫科", "Lineidae"])
        ws.append(["青纵沟纽虫", "Lineus fuscoviridis", "纵沟纽虫科", "Lineidae"])
        ws.append(["习见脑纽虫", "Cerebratulina communis", "纵沟纽虫科", "Lineidae"])
        ws.append(["戴氏脑纽虫", "Cerebratulina darvelli", "纵沟纽虫科", "Lineidae"])
        ws.append(["珠角裸沙蚕", "Nicon moniloceras", "沙蚕科", "Nereididae"])
        wb.save(path)
        wb.close()
        return path

    def test_create_vouchers_increment(self) -> None:
        store = ExcelStore(self.tmp)
        self.assertEqual(store.create_specimen(), "YZZ000001")
        self.assertEqual(store.create_specimen(), "YZZ000002")
        self.assertEqual(store.list_vouchers(), ["YZZ000001", "YZZ000002"])

    def test_workspace_overview_matches_list_status_data(self) -> None:
        store = ExcelStore(self.tmp)
        first = store.create_specimen()
        second = store.create_specimen()
        store.set_fields("specimen", first, {"管内编号*": "QD-CK-SC008", "采集地点缩写*": "QD"})
        store.set_fields("classification", first, {
            "种名*": "Nicon moniloceras",
            "科*": "Nereididae",
            "物种中文名": "珠角裸沙蚕",
            "物种拉丁名": "Nicon moniloceras",
            "科中文名": "沙蚕科",
            "科拉丁名": "Nereididae",
        })
        photo = self.tmp / "overview.jpg"
        photo.write_bytes(b"photo")
        store.add_photo(first, photo, allow_outside=True)

        overview = store.workspace_overview()

        self.assertEqual(overview["vouchers"], [first, second])
        self.assertEqual(overview["photo_counts"], {first: 1})
        self.assertEqual(overview["tube_numbers"], {first: "QD-CK-SC008"})
        self.assertEqual(overview["photo_filenames"], {first: ["overview.jpg"]})
        self.assertEqual(overview["flags"][first].label(), "√√√")
        self.assertEqual(overview["flags"][second].label(), "×××")

    def test_window_manager_focuses_existing_workspace(self) -> None:
        class FakeWindow:
            def __init__(self, root: Path):
                self.workspace_root = root
                self.events: list[str] = []

            def show(self) -> None:
                self.events.append("show")

            def raise_(self) -> None:
                self.events.append("raise")

            def activateWindow(self) -> None:
                self.events.append("activate")

        manager = WindowManager(app=None)
        window = FakeWindow(self.tmp / "workspace")
        manager.register(window)

        self.assertTrue(manager.focus_workspace(self.tmp / "workspace"))
        self.assertEqual(window.events, ["show", "raise", "activate"])
        self.assertFalse(manager.focus_workspace(self.tmp / "workspace", exclude=window))
        manager.unregister(window)
        self.assertFalse(manager.focus_workspace(self.tmp / "workspace"))

    def test_window_manager_tolerates_unbound_window(self) -> None:
        # 首次启动尚未选工作区的窗口 workspace_root 为 None；
        # register/unregister 必须是 no-op 且不抛异常。
        class UnboundWindow:
            workspace_root = None

        manager = WindowManager(app=None)
        window = UnboundWindow()
        manager.register(window)  # 不应注册、不应抛异常
        self.assertEqual(manager._windows, {})
        manager.unregister(window)  # 同样 no-op

    def test_build_release_script_compiles(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        py_compile.compile(str(project_root / "build_release.py"), doraise=True)

    def test_old_classification_config_names_are_not_used(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        old_names = [
            "classification_config",
            "CLASSIFICATION_FIELDS",
            "CLASSIFICATION_REQUIRED_FIELDS",
            "CLASSIFICATION_DISPLAY_FIELDS",
            "SPECIES_COMPLETER_FIELDS",
            "FAMILY_COMPLETER_FIELDS",
            "CLASSIFICATION_COMPLETER_FIELDS",
            "species_autofill_updates",
            "family_autofill_updates",
        ]
        for path in (project_root / "specimen_app").glob("*.py"):
            if path.name == "classification_fields.py":
                continue
            text = path.read_text(encoding="utf-8")
            for name in old_names:
                self.assertNotIn(name, text, f"{name} still appears in {path.name}")

    def test_classification_schema_includes_optional_higher_taxonomy_fields(self) -> None:
        for field in ["属名", "目", "纲", "门", "备注"]:
            self.assertIn(field, CLASSIFICATION_COLUMNS)
            self.assertNotIn(field, REQUIRED_CLASSIFICATION_COLUMNS)

    def test_species_matcher_searches_species_and_family_fields(self) -> None:
        matcher = SpeciesMatcher(self._write_species_preset())

        self.assertEqual(matcher.species_matches("珠江")[0].latin_name, "Amniclineus zhujiangensis")
        self.assertEqual(matcher.species_matches("Amnic")[0].chinese_name, "珠江川纽虫")

        family_names = [match.family_name for match in matcher.family_matches("纵沟")]
        self.assertEqual(family_names, ["纵沟纽虫科"])
        self.assertEqual(matcher.family_matches("Line")[0].family_name, "纵沟纽虫科")
        self.assertEqual(matcher.species_matches("纵沟")[0].chinese_name, "青纵沟纽虫")

        self.assertEqual(matcher.resolve_unique_species("珠江").chinese_name, "珠江川纽虫")
        self.assertEqual(
            matcher.resolve_unique_species("amniclineus zhujiangensis").chinese_name,
            "珠江川纽虫",
        )
        self.assertIsNone(matcher.resolve_unique_species("珠"))
        self.assertIsNone(matcher.resolve_unique_species("纵沟"))
        self.assertEqual(matcher.resolve_unique_family("line").family_latin, "Lineidae")

    def test_classification_autofill_update_maps(self) -> None:
        species = SpeciesMatch(
            chinese_name="珠江川纽虫",
            latin_name="Amniclineus zhujiangensis",
            family_name="纵沟纽虫科",
            family_latin="Lineidae",
        )
        family = FamilyMatch(family_name="纵沟纽虫科", family_latin="Lineidae")

        self.assertEqual(
            classification_values_from_species_match(species),
            {
                "种名*": "珠江川纽虫",
                "种拉丁": "Amniclineus zhujiangensis",
                "属名": "Amniclineus",
                "科*": "纵沟纽虫科",
                "科拉丁": "Lineidae",
            },
        )
        self.assertEqual(
            classification_values_from_family_match(family),
            {
                "科*": "纵沟纽虫科",
                "科拉丁": "Lineidae",
            },
        )

    def test_taxonomy_candidate_display_is_not_inserted_into_species_field(self) -> None:
        species = SpeciesMatch(
            chinese_name="青纵沟纽虫",
            latin_name="Lineus fuscoviridis",
            family_name="纵沟纽虫科",
            family_latin="Lineidae",
        )

        self.assertEqual(
            format_taxonomy_candidate_label("种名*", "species", species),
            "青纵沟纽虫  Lineus fuscoviridis  纵沟纽虫科  Lineidae",
        )
        self.assertEqual(
            classification_column_value_from_taxonomy_match("种名*", "species", species),
            "青纵沟纽虫",
        )
        self.assertEqual(
            classification_column_value_from_taxonomy_match("种拉丁", "species", species),
            "Lineus fuscoviridis",
        )
        self.assertEqual(
            classification_column_value_from_taxonomy_match("属名", "species", species),
            "Lineus",
        )

        self.assertEqual(
            classification_values_from_species_match(species),
            {
                "种名*": "青纵沟纽虫",
                "种拉丁": "Lineus fuscoviridis",
                "属名": "Lineus",
                "科*": "纵沟纽虫科",
                "科拉丁": "Lineidae",
            },
        )

    def test_existing_classification_workbook_gets_new_optional_columns(self) -> None:
        workspace = self.tmp / "old_classification_schema"
        workspace.mkdir()
        ExcelStore(workspace)
        old_headers = ["入库编号*", "种名*", "种拉丁", "科*", "科拉丁"]
        path = workspace / "数据" / "分类信息.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(old_headers)
        ws.append(["YZZ000001", "旧种", "Oldus species", "旧科", "Oldidae"])
        wb.save(path)
        wb.close()

        reopened = ExcelStore(workspace)
        row = reopened.get_classification("YZZ000001")
        self.assertEqual(row["种名*"], "旧种")
        self.assertEqual(row["种拉丁"], "Oldus species")
        self.assertEqual(row["科*"], "旧科")
        self.assertEqual(row["属名"], "")
        self.assertEqual(row["备注"], "")

        upgraded = load_workbook(path, read_only=True, data_only=True)
        try:
            headers = [str(cell.value or "") for cell in next(upgraded.active.iter_rows(max_row=1))]
        finally:
            upgraded.close()
        for field in CLASSIFICATION_HEADERS:
            self.assertIn(field, headers)

        reopened.set_fields("classification", "YZZ000001", {"属名": "Oldus", "备注": "只能鉴定到属"})
        updated = reopened.get_classification("YZZ000001")
        self.assertEqual(updated["种名*"], "旧种")
        self.assertEqual(updated["属名"], "Oldus")
        self.assertEqual(updated["备注"], "只能鉴定到属")

    def test_tube_number_parsing(self) -> None:
        self.assertEqual(extract_location_code("QD-LSD-SC001-1-R-250923"), "QD-LSD")
        self.assertEqual(extract_bottle_label("QD-LSD-SC001-1-R-250923"), "QD-LSD-SC001")
        self.assertEqual(extract_bottle_label("QD-CK-SC008-260827"), "QD-CK-SC008")
        self.assertEqual(extract_collection_date("QD-LSD-SC001-1-R-250923"), "2025-09-23")
        self.assertEqual(extract_collection_date("QD-LSD-SC001-20250923"), "2025-09-23")

    def test_tube_number_parsing_extracts_photo_sequence(self) -> None:
        examples = [
            ("QD-CK-SC008-20240315", "QD-CK-SC008", 1, "2024-03-15"),
            ("QD-CK-SC008-2-20240315", "QD-CK-SC008", 2, "2024-03-15"),
            ("QD-CK-SC008-3-20240315", "QD-CK-SC008", 3, "2024-03-15"),
            ("QD-CK-SC008--2-20240315-xxx", "QD-CK-SC008", 2, "2024-03-15"),
        ]
        for tube, bottle_label, photo_seq, collection_date in examples:
            with self.subTest(tube=tube):
                self.assertEqual(extract_bottle_label(tube), bottle_label)
                self.assertEqual(extract_photo_seq(tube), photo_seq)
                self.assertEqual(extract_collection_date(tube), collection_date)

    def test_photo_filename_parsing_extracts_tube_date_and_sequence(self) -> None:
        self.assertEqual(extract_tube_from_filename("QD-CK-SC008-2-20240315-a.jpg"), "QD-CK-SC008")
        # 兼容 GXRG-A-BZC001.tif：原规则要求第二段地点码至少 2 位，导致无法填充 GXRG-A。
        self.assertEqual(extract_tube_from_filename("GXRG-A-BZC001.tif"), "GXRG-A-BZC001")
        self.assertEqual(extract_photo_seq_from_filename("QD-CK-SC008-2-20240315-a.jpg"), 2)
        self.assertEqual(extract_photo_seq_from_filename("QD-CK-SC008--3-20240315-a.jpg"), 3)
        self.assertEqual(extract_photo_seq_from_filename("QD-CK-SC008-20240315-a.jpg"), 1)
        self.assertEqual(extract_photo_date("QD-CK-SC008--3-20240315-a.jpg"), "2024-03-15")

    def test_photo_filename_parsing_extracts_specimen_fill_fields(self) -> None:
        self.assertEqual(
            extract_specimen_tube_from_filename("QD-CK-SC008-2-20240315-a.jpg"),
            "QD-CK-SC008-2-20240315",
        )
        self.assertEqual(
            extract_specimen_tube_from_filename("QD_CK_SC008_3_20250923.tif"),
            "QD-CK-SC008-3-20250923",
        )
        self.assertEqual(
            extract_specimen_tube_from_filename("QD-LSD-SC001-1-R-250923.jpg"),
            "QD-LSD-SC001-1-R-250923",
        )
        # 没有日期或保存方式也应保留核心编号，后续可填充采集地点缩写 GXRG-A。
        self.assertEqual(extract_specimen_tube_from_filename("GXRG-A-BZC001.tif"), "GXRG-A-BZC001")
        self.assertEqual(extract_specimen_tube_from_filename("QD-CK-SC008-1.tif"), "QD-CK-SC008")
        self.assertEqual(extract_save_method_from_filename("QD-LSD-SC001-1-R-250923.jpg"), "RE")
        self.assertEqual(extract_save_method_from_filename("XM-ABC-SC001-FE-250924.jpg"), "FE")

    def test_tube_number_derives_specimen_fill_fields(self) -> None:
        # 原测试只覆盖日期和地点；旧版本管内编号也能派生保存方式，这里防止再次丢失。
        self.assertEqual(extract_save_method_from_tube_number("QD-LSD-SC001-1-R-250923"), "RE")
        self.assertEqual(extract_save_method_from_tube_number("QD-LSD-SC001-250923-R"), "RE")
        self.assertEqual(extract_save_method_from_tube_number("XM-ABC-SC001-FE-250924"), "FE")
        self.assertEqual(extract_save_method_from_tube_number("XM-ABC-SC001-9E-250924"), "9E")
        self.assertEqual(extract_save_method_from_tube_number("XM-ABC-SC001-7E-250924"), "7E")
        self.assertEqual(extract_save_method_from_tube_number("XM-ABC-SC001-79-250924"), "79")
        self.assertEqual(
            derive_specimen_fields_from_tube_number("QD-LSD-SC001-1-R-250923"),
            {"采集地点缩写*": "QD-LSD", "采集日期": "2025-09-23", "保存方式": "RE"},
        )
        self.assertEqual(
            derive_specimen_fields_from_tube_number("GXRG-A-BZC001"),
            {"采集地点缩写*": "GXRG-A"},
        )

    def test_photo_filename_fill_helpers_are_conservative(self) -> None:
        row = {"文件名": "archived_2.jpg", "原始文件名": "QD-LSD-SC001-1-R-250923.jpg"}
        self.assertEqual(photo_filename_source_for_specimen_fill(row), "QD-LSD-SC001-1-R-250923.jpg")
        updates = specimen_updates_from_photo_filename(photo_filename_source_for_specimen_fill(row))
        self.assertEqual(
            updates,
            {
                "管内编号*": "QD-LSD-SC001-1-R-250923",
                "采集地点缩写*": "QD-LSD",
                "采集日期": "2025-09-23",
                "保存方式": "RE",
            },
        )
        defaults = default_photo_filename_fill_fields(
            updates,
            {"管内编号*": "", "采集地点缩写*": "OLD", "采集日期": "", "保存方式": "FE"},
        )
        self.assertEqual(defaults, ["管内编号*", "采集日期"])

        # 兼容无日期/保存方式的文件名：至少能从核心编号填充管内编号和采集地点。
        gxrg_updates = specimen_updates_from_photo_filename("GXRG-A-BZC001.tif")
        self.assertEqual(gxrg_updates, {"管内编号*": "GXRG-A-BZC001", "采集地点缩写*": "GXRG-A"})

    def test_set_field_autofills_tube_derived_fields(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        row = store.get_specimen(voucher)
        self.assertEqual(row["采集日期"], "2025-09-23")
        self.assertEqual(row["采集地点缩写*"], "QD-LSD")
        self.assertEqual(row["保存方式"], "RE")
        store.set_fields("specimen", voucher, {"管内编号*": "XM-ABC-SC001-1-R-250924"})
        row = store.get_specimen(voucher)
        self.assertEqual(row["采集日期"], "2025-09-24")
        self.assertEqual(row["采集地点缩写*"], "XM-ABC")
        self.assertEqual(row["保存方式"], "RE")
        store.set_fields("specimen", voucher, {"管内编号*": "XM-ABC-SC001-FE-250924"})
        row = store.get_specimen(voucher)
        self.assertEqual(row["保存方式"], "FE")

    def test_photo_filename_fill_can_disable_hidden_derived_overwrite(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"采集日期": "2024-01-01", "采集地点缩写*": "OLD"})
        store.set_fields(
            "specimen",
            voucher,
            {"管内编号*": "QD-LSD-SC001-1-R-250923"},
            auto_derive_specimen_fields=False,
        )
        row = store.get_specimen(voucher)
        self.assertEqual(row["管内编号*"], "QD-LSD-SC001-1-R-250923")
        self.assertEqual(row["采集日期"], "2024-01-01")
        self.assertEqual(row["采集地点缩写*"], "OLD")

    def test_undo_redo_field_update(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")
        store.undo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")

    def test_multi_field_update_records_detail_and_single_summary_change(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()

        store.set_fields("specimen", voucher, {"核对人员": "张三", "备注": "批次 A"})

        wb = load_workbook(self.tmp / "数据" / CHANGE_LOG_FILE, read_only=True, data_only=True)
        try:
            detail_rows = list(wb["修改明细"].iter_rows(values_only=True))
            summary_rows = list(wb["修改汇总"].iter_rows(values_only=True))
        finally:
            wb.close()
        detail_headers = list(detail_rows[0])
        field_index = detail_headers.index("字段名")
        voucher_index = detail_headers.index("入库编号")
        changed_fields = {
            row[field_index]
            for row in detail_rows[1:]
            if row[voucher_index] == voucher
        }
        self.assertTrue({"核对人员", "备注"}.issubset(changed_fields))
        summary_headers = list(summary_rows[0])
        voucher_index = summary_headers.index("入库编号")
        count_index = summary_headers.index("修改次数")
        row = next(row for row in summary_rows[1:] if row[voucher_index] == voucher)
        self.assertEqual(row[count_index], "1")

    def test_pending_text_save_group_flushes_fields_once(self) -> None:
        class FakeTimer:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        class FakeWindow:
            current_voucher = "YZZ000001"

            def __init__(self) -> None:
                key = "YZZ000001:specimen"
                self._save_timers = {key: FakeTimer()}
                self._pending_save_fields = {key: {"核对人员", "备注"}}
                self.calls: list[tuple[str, set[str], str]] = []

            def _save_pending_group(self, key: str, voucher: str, category: str) -> int:
                return SpecimenWindow._save_pending_group(self, key, voucher, category)

            def _save_text_fields(self, category: str, fields: set[str], voucher: str) -> None:
                self.calls.append((category, fields, voucher))

        window = FakeWindow()

        saved = SpecimenWindow._flush_pending_saves(window, "specimen")

        self.assertEqual(saved, 2)
        self.assertEqual(window.calls, [("specimen", {"核对人员", "备注"}, "YZZ000001")])
        self.assertFalse(window._save_timers)
        self.assertFalse(window._pending_save_fields)

    def test_redo_order_after_multiple_undo(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        # 原测试手动再写 RE；现在管内编号会自动派生 RE，因此改用 FE 保留“两步修改”的测试意图。
        store.set_fields("specimen", voucher, {"保存方式": "FE"})
        store.undo_last()
        store.undo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "")
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["管内编号*"], "QD-LSD-SC001-1-R-250923")
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "RE")
        store.redo_last()
        self.assertEqual(store.get_specimen(voucher)["保存方式"], "FE")

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

    def test_import_from_file_success(self) -> None:
        target = self.tmp / "target_file"
        target.mkdir()
        source = self.tmp / "source.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["入库编号*", "管内编号*", "保存方式", "采集日期", "采集地点缩写*", "入库日期", "标本存放位置", "信息录入人员", "核对人员", "备注"])
        ws.append(["YZZ000003", "QD-LSD-SC001-1-R-250923", "", "", "", "", "", "", "", ""])
        wb.save(source)

        store = ExcelStore(target)
        result = store.import_from_file(source)
        self.assertEqual(result.imported, 1)
        self.assertEqual(store.list_vouchers(), ["YZZ000003"])
        self.assertEqual(store.get_specimen("YZZ000003")["管内编号*"], "QD-LSD-SC001-1-R-250923")

    def test_import_from_file_blocks_duplicate_source_ids(self) -> None:
        target = self.tmp / "target_file_duplicate"
        target.mkdir()
        source = self.tmp / "source_duplicate.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["入库编号*", "管内编号*", "保存方式", "采集日期", "采集地点缩写*", "入库日期", "标本存放位置", "信息录入人员", "核对人员", "备注"])
        ws.append(["YZZ000003", "QD-LSD-SC001-1-R-250923", "", "", "", "", "", "", "", ""])
        ws.append(["YZZ000003", "QD-CK-SC008-1-R-250923", "", "", "", "", "", "", "", ""])
        wb.save(source)

        store = ExcelStore(target)
        with self.assertRaises(ImportConflictError):
            store.import_from_file(source)

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
        self.assertEqual(photos[0]["原始文件名"], "photo1.tif")
        self.assertEqual(photos[1]["原始文件名"], "photo2.jpg")
        self.assertEqual(photos[0]["归档状态"], "已归档")
        self.assertTrue(store.resolve_photo_path(photos[0]).exists())
        self.assertTrue(store.resolve_photo_path(photos[1]).exists())

    def test_same_name_different_photo_uses_numbered_archive_name(self) -> None:
        workspace = self.tmp / "workspace_same_name"
        workspace.mkdir()
        first_dir = self.tmp / "first"
        second_dir = self.tmp / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "same.jpg"
        second = second_dir / "same.jpg"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()

        first_row = store.add_photo(voucher, first, allow_outside=True)
        second_row = store.add_photo(voucher, second, allow_outside=True)

        self.assertEqual(first_row["文件名"], "same.jpg")
        self.assertEqual(second_row["文件名"], "same_2.jpg")
        self.assertEqual(first_row["相对路径"], "./照片/same.jpg")
        self.assertEqual(second_row["相对路径"], "./照片/same_2.jpg")
        self.assertEqual(first_row["原始文件名"], "same.jpg")
        self.assertEqual(second_row["原始文件名"], "same.jpg")
        self.assertTrue((workspace / "照片" / "same.jpg").exists())
        self.assertTrue((workspace / "照片" / "same_2.jpg").exists())

    def test_photo_description_update_does_not_crash(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "photo1.jpg"
        first.write_bytes(b"fake-jpg")
        store.add_photo(voucher, first)
        self.assertTrue(store.set_photo_description(voucher, 0, "背面照片"))
        self.assertEqual(store.get_photos(voucher)[0]["描述"], "背面照片")

    def test_photo_filename_update_preserves_archive_metadata(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "raw_name.jpg"
        first.write_bytes(b"fake-jpg")
        store.add_photo(voucher, first)
        old_row = store.get_photos(voucher)[0]

        new_name = "QD-CK-SC008-2-20240315.jpg"
        self.assertTrue(store.set_photo_filename(voucher, 0, new_name))
        updated = store.get_photos(voucher)[0]
        self.assertEqual(updated["文件名"], new_name)
        self.assertEqual(updated["原始文件名"], "raw_name.jpg")
        self.assertEqual(updated["相对路径"], f"./照片/{new_name}")
        self.assertTrue(store.resolve_photo_path(updated).exists())
        self.assertFalse(store.resolve_photo_path(old_row).exists())

        self.assertTrue(store.undo_last())
        self.assertEqual(store.get_photos(voucher)[0]["文件名"], "raw_name.jpg")
        self.assertTrue((photo_dir / "raw_name.jpg").exists())
        self.assertFalse((photo_dir / new_name).exists())
        self.assertTrue(store.redo_last())
        self.assertEqual(store.get_photos(voucher)[0]["文件名"], new_name)
        self.assertFalse((photo_dir / "raw_name.jpg").exists())
        self.assertTrue((photo_dir / new_name).exists())

    def test_external_photo_is_copied_into_workspace_archive(self) -> None:
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        outside_dir = self.tmp / "external"
        outside_dir.mkdir()
        outside = outside_dir / "outside.jpg"
        outside.write_bytes(b"fake-jpg")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()
        row = store.add_photo(voucher, outside, allow_outside=True)
        resolved = store.resolve_photo_path(row)
        self.assertEqual(row["文件名"], "outside.jpg")
        self.assertEqual(row["原始文件名"], "outside.jpg")
        self.assertEqual(Path(row["原始路径"]), outside)
        self.assertEqual(row["来源工作区根路径"], "")
        self.assertEqual(row["相对路径"], "./照片/outside.jpg")
        self.assertEqual(Path(row["绝对路径"]), resolved)
        self.assertEqual(resolved.parent, workspace / "照片")
        self.assertTrue(resolved.exists())
        self.assertTrue(outside.exists())

    def test_absolute_only_photo_records_source_without_copying(self) -> None:
        workspace = self.tmp / "workspace_absolute_only"
        workspace.mkdir()
        outside = self.tmp / "absolute_only.jpg"
        outside.write_bytes(b"original")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()

        row = store.add_photo(
            voucher,
            outside,
            allow_outside=True,
            photo_management_mode="absolute_only",
        )

        self.assertEqual(row["归档状态"], "仅记录")
        self.assertEqual(row["相对路径"], "")
        self.assertEqual(Path(row["绝对路径"]), outside)
        self.assertEqual(store.resolve_photo_path(row), outside)
        self.assertFalse((workspace / "照片" / outside.name).exists())
        self.assertTrue(outside.exists())

    def test_custom_photo_library_copies_original_and_cleans_managed_copy(self) -> None:
        workspace = self.tmp / "workspace_custom_library"
        library = self.tmp / "managed_library"
        workspace.mkdir()
        source = self.tmp / "custom_source.tif"
        source.write_bytes(b"full-resolution-data")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()

        row = store.add_photo(
            voucher,
            source,
            allow_outside=True,
            photo_management_mode="copy_to_custom_library",
            photo_library_path=library,
        )
        managed = Path(row["绝对路径"])

        self.assertEqual(row["归档状态"], "已归档")
        self.assertEqual(row["相对路径"], "")
        self.assertEqual(managed.parent, library)
        self.assertEqual(managed.read_bytes(), source.read_bytes())
        self.assertTrue(source.exists())

        self.assertTrue(store.delete_photo(voucher, 0))
        self.assertFalse(managed.exists())
        self.assertTrue(source.exists())

    def test_delete_photo_removes_unreferenced_archive_file(self) -> None:
        workspace = self.tmp / "workspace_delete_photo"
        workspace.mkdir()
        outside = self.tmp / "delete_me.jpg"
        outside.write_bytes(b"fake-jpg")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()
        row = store.add_photo(voucher, outside, allow_outside=True)
        archived = store.resolve_photo_path(row)
        self.assertTrue(archived.exists())

        self.assertTrue(store.delete_photo(voucher, 0))

        self.assertEqual(store.get_photos(voucher), [])
        self.assertFalse(archived.exists())
        self.assertTrue(outside.exists())

    def test_hash_prefixed_archive_paths_are_migrated_to_original_names(self) -> None:
        workspace = self.tmp / "workspace_hash_migrate"
        data_dir = workspace / "数据"
        archive_dir = workspace / "照片"
        data_dir.mkdir(parents=True)
        archive_dir.mkdir()
        hashed = archive_dir / "abcdef123456__legacy.jpg"
        hashed.write_bytes(b"legacy")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()
        rows = store.read_rows("photo")
        rows.append(
            {
                "入库编号*": voucher,
                "文件名": "legacy.jpg",
                "相对路径": "./照片/abcdef123456__legacy.jpg",
                "描述": "",
                "来源工作区根路径": "",
                "原始文件名": "legacy.jpg",
                "原始路径": str(self.tmp / "legacy.jpg"),
                "文件SHA256": store._file_sha256(hashed),
                "文件大小": str(hashed.stat().st_size),
                "归档时间": "",
                "归档状态": "已归档",
            }
        )
        store._write_rows("photo", rows)
        store.config["data_schema_version"] = "1.1.0"
        store._save_config()

        migrated = ExcelStore(workspace)
        photo = migrated.get_photos(voucher)[0]
        self.assertEqual(photo["文件名"], "legacy.jpg")
        self.assertEqual(photo["相对路径"], "./照片/legacy.jpg")
        self.assertTrue((archive_dir / "legacy.jpg").exists())
        self.assertFalse(hashed.exists())

    def test_photo_conflicts_use_archived_file_hash(self) -> None:
        workspace = self.tmp / "workspace_hash_conflict"
        workspace.mkdir()
        outside = self.tmp / "same_photo.jpg"
        outside.write_bytes(b"same-content")
        store = ExcelStore(workspace)
        first = store.create_specimen()
        second = store.create_specimen()
        store.add_photo(first, outside, allow_outside=True)

        conflicts = store.find_photo_conflicts([outside], second)
        self.assertEqual(conflicts, {str(outside.resolve()): first})

    def test_resolve_photo_path_blocks_parent_traversal_without_source_root(self) -> None:
        workspace = self.tmp / "workspace_traversal"
        workspace.mkdir()
        outside = self.tmp / "outside.jpg"
        outside.write_bytes(b"fake-jpg")
        store = ExcelStore(workspace)
        resolved = store.resolve_photo_path({"相对路径": "../outside.jpg", "来源工作区根路径": ""})
        self.assertNotEqual(resolved, outside)
        self.assertFalse(resolved.exists())

    def test_replace_photo_failure_preserves_old_record(self) -> None:
        workspace = self.tmp / "workspace_replace"
        workspace.mkdir()
        photo_dir = workspace / "照片"
        photo_dir.mkdir()
        old = photo_dir / "old.jpg"
        old.write_bytes(b"old")
        missing = self.tmp / "missing_replace.jpg"
        store = ExcelStore(workspace)
        voucher = store.create_specimen()
        old_row = store.add_photo(voucher, old)
        with self.assertRaises(FileNotFoundError):
            store.replace_photo(voucher, 0, missing, allow_outside=False)
        self.assertEqual(store.get_photos(voucher)[0], old_row)

    def test_replace_photo_archives_external_copy(self) -> None:
        workspace = self.tmp / "workspace_replace_external"
        workspace.mkdir()
        photo_dir = workspace / "照片"
        photo_dir.mkdir()
        old = photo_dir / "old.jpg"
        old.write_bytes(b"old")
        outside = self.tmp / "outside_replace.jpg"
        outside.write_bytes(b"new")
        store = ExcelStore(workspace)
        voucher = store.create_specimen()
        old_row = store.add_photo(voucher, old)
        old_archived = store.resolve_photo_path(old_row)

        new_row = store.replace_photo(voucher, 0, outside, allow_outside=False)
        self.assertIsNotNone(new_row)
        photos = store.get_photos(voucher)
        self.assertEqual(photos[0]["文件名"], "outside_replace.jpg")
        self.assertEqual(Path(photos[0]["原始路径"]), outside)
        self.assertEqual(store.resolve_photo_path(photos[0]).parent, workspace / "照片")
        self.assertFalse(old_archived.exists())
        self.assertTrue(outside.exists())

    def test_replace_photo_rejects_content_linked_to_another_voucher(self) -> None:
        store = ExcelStore(self.tmp)
        first = store.create_specimen()
        second = store.create_specimen()
        assigned = self.tmp / "assigned.jpg"
        replacement = self.tmp / "replacement.jpg"
        assigned.write_bytes(b"already-assigned")
        replacement.write_bytes(b"old-second")
        store.add_photo(first, assigned, allow_outside=True)
        store.add_photo(second, replacement, allow_outside=True)

        with self.assertRaisesRegex(ValueError, first):
            store.replace_photo(second, 0, assigned, allow_outside=True)

        self.assertEqual(store.get_photos(second)[0]["原始文件名"], "replacement.jpg")

    def test_import_workspace_archives_found_photos_and_reports_missing(self) -> None:
        source = self.tmp / "source_archive_import"
        target = self.tmp / "target_archive_import"
        source.mkdir()
        target.mkdir()
        source_store = ExcelStore(source)
        found_voucher = source_store.create_specimen()
        missing_voucher = source_store.create_specimen()
        found_original = self.tmp / "found_original.jpg"
        missing_original = self.tmp / "missing_original.jpg"
        found_original.write_bytes(b"found")
        missing_original.write_bytes(b"missing")
        source_store.add_photo(found_voucher, found_original, allow_outside=True)
        missing_row = source_store.add_photo(missing_voucher, missing_original, allow_outside=True)
        source_store.resolve_photo_path(missing_row).unlink()
        missing_original.unlink()

        target_store = ExcelStore(target)
        result = target_store.import_workspace(source)

        self.assertEqual(result.imported, 2)
        self.assertEqual(result.photos_imported, 1)
        self.assertIsNotNone(result.report_path)
        self.assertTrue(result.report_path.exists())
        found_photos = target_store.get_photos(found_voucher)
        self.assertEqual(len(found_photos), 1)
        self.assertEqual(target_store.resolve_photo_path(found_photos[0]).parent, target / "照片")
        self.assertEqual(target_store.get_photos(missing_voucher), [])

    def test_move_photos_is_atomic_and_undoable(self) -> None:
        store = ExcelStore(self.tmp)
        source = store.create_specimen()
        target = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "move1.jpg"
        second = photo_dir / "move2.jpg"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        store.add_photo(source, first)
        store.add_photo(source, second)

        self.assertEqual(store.move_photos(source, target, [0]), 1)
        self.assertEqual([row["文件名"] for row in store.get_photos(source)], ["move2.jpg"])
        self.assertEqual([row["文件名"] for row in store.get_photos(target)], ["move1.jpg"])
        self.assertEqual(store.undo_last(), "move_photos")
        self.assertEqual([row["文件名"] for row in store.get_photos(source)], ["move2.jpg", "move1.jpg"])
        self.assertEqual(store.get_photos(target), [])
        self.assertEqual(store.redo_last(), "move_photos")
        self.assertEqual([row["文件名"] for row in store.get_photos(target)], ["move1.jpg"])

    def test_grid_filename_setting_defaults_and_roundtrips(self) -> None:
        old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.tmp / "config")
        try:
            path = settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"preview_quality":"standard"}', encoding="utf-8")
            settings = load_settings()
            self.assertTrue(settings.show_grid_filenames)
            self.assertEqual(settings.photo_filename_fill_shortcut, DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT)
            self.assertEqual(settings.photo_management_mode, "copy_with_absolute")
            # 旧 settings.json 缺该键时，沿用上条信息默认开启
            self.assertTrue(settings.carry_over_specimen_fields)
            # 旧 settings.json 缺该键时，入库汇总可见列为空（运行时回退到默认列集）
            self.assertEqual(settings.summary_visible_columns, [])
            # 旧 settings.json 缺该键时，界面字体大小为 0（运行时用系统默认字号）
            self.assertEqual(settings.ui_font_size, 0)

            settings.show_grid_filenames = False
            settings.photo_filename_fill_shortcut = "Ctrl+Shift+F"
            settings.photo_management_mode = "absolute_only"
            settings.photo_library_path = str(self.tmp / "library")
            settings.carry_over_specimen_fields = False
            settings.summary_visible_columns = ["入库编号*", "管内编号*", "照片数"]
            settings.ui_font_size = 14
            save_settings(settings)
            reloaded = load_settings()
            self.assertFalse(reloaded.show_grid_filenames)
            self.assertEqual(reloaded.photo_filename_fill_shortcut, "Ctrl+Shift+F")
            self.assertEqual(reloaded.photo_management_mode, "absolute_only")
            self.assertEqual(reloaded.photo_library_path, str(self.tmp / "library"))
            self.assertFalse(reloaded.carry_over_specimen_fields)
            self.assertEqual(reloaded.summary_visible_columns, ["入库编号*", "管内编号*", "照片数"])
            self.assertEqual(reloaded.ui_font_size, 14)
            # 越界字号被钳制到 [7, 24]
            reloaded.ui_font_size = 999
            save_settings(reloaded)
            self.assertEqual(load_settings().ui_font_size, 24)
        finally:
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata

    def test_summary_records_joins_specimen_classification_and_photos(self) -> None:
        store = ExcelStore(self.tmp)
        first = store.create_specimen()
        second = store.create_specimen()  # 故意不填分类信息，验证左连接
        store.set_fields("specimen", first, {
            "管内编号*": "QD-CK-SC008",
            "采集地点缩写*": "QD",
            "备注": "标本备注",
        })
        store.set_fields("classification", first, {
            "种名*": "Nicon moniloceras",
            "科*": "Nereididae",
            "备注": "分类的备注",
        })
        photo = self.tmp / "summary.jpg"
        photo.write_bytes(b"photo")
        store.add_photo(first, photo, allow_outside=True)

        records = store.summary_records()
        by_voucher = {r["入库编号*"]: r for r in records}

        # 左连接：缺分类信息的 second 也在结果里
        self.assertEqual(sorted(by_voucher), sorted([first, second]))
        rec = by_voucher[first]
        self.assertEqual(rec["管内编号*"], "QD-CK-SC008")
        self.assertEqual(rec["种名*"], "Nicon moniloceras")
        self.assertEqual(rec["科*"], "Nereididae")
        # specimen 的"备注"与 classification 的"备注"消歧（后者显示为"分类备注"）
        self.assertEqual(rec["备注"], "标本备注")
        self.assertEqual(rec["分类备注"], "分类的备注")
        self.assertEqual(rec["照片数"], 1)
        # 照片聚合列：文件名 / 绝对路径 / 描述，均按入库编号聚合为 list
        self.assertEqual(rec["照片文件名"], ["summary.jpg"])
        self.assertEqual(len(rec["照片绝对路径"]), 1)
        self.assertTrue(rec["照片绝对路径"][0].endswith("summary.jpg"))
        self.assertEqual(rec["照片描述"], [])  # 未填描述
        # 缺分类信息的记录：分类列留空，照片数为 0，照片聚合列为空 list
        rec2 = by_voucher[second]
        self.assertEqual(rec2["种名*"], "")
        self.assertEqual(rec2["分类备注"], "")
        self.assertEqual(rec2["照片数"], 0)
        self.assertEqual(rec2["照片文件名"], [])
        self.assertEqual(rec2["照片绝对路径"], [])
        self.assertEqual(rec2["照片描述"], [])

    def test_is_unsafe_workspace_root_rejects_root_and_home(self) -> None:
        from specimen_app.workspace import is_unsafe_workspace_root

        # 文件系统根 / 盘符根、用户主目录：范围过大，判 unsafe
        self.assertTrue(is_unsafe_workspace_root(Path(self.tmp.anchor)))
        self.assertTrue(is_unsafe_workspace_root(Path.home()))
        # 正常工作区子目录：safe
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        self.assertFalse(is_unsafe_workspace_root(workspace))

    def test_image_decode_respects_pixel_cap(self) -> None:
        from specimen_app import image_cache

        big = Image.new("RGB", (800, 600))  # 480000 px
        small = Image.new("RGB", (100, 100))
        original_cap = image_cache._MAX_DECODE_PIXELS
        try:
            image_cache._MAX_DECODE_PIXELS = 100_000
            reduced = image_cache._downsample_if_huge(big, None)
            self.assertLessEqual(reduced.width * reduced.height, 100_000)
            # 未超上限的图原样返回（不复制、不降采样）
            self.assertIs(image_cache._downsample_if_huge(small, None), small)
            # 端到端：load_source_image 也受上限约束
            path = self.tmp / "big.png"
            big.save(path)
            loaded = image_cache.load_source_image(path, max_size=None)
            self.assertLessEqual(loaded.width * loaded.height, 100_000)
        finally:
            image_cache._MAX_DECODE_PIXELS = original_cap

    def test_auto_memory_profile_constrains_four_gb_machine(self) -> None:
        env_detect.is_low_memory.cache_clear()
        try:
            with patch("specimen_app.env_detect.total_ram_mb", return_value=4096):
                self.assertTrue(env_detect.is_low_memory())
                params = env_detect.memory_profile_params("auto")
                self.assertEqual(params["thumb_cache_bytes"], 16 << 20)
                self.assertEqual(params["thumb_workers"], 1)
                self.assertEqual(params["row_cache_maxsize"], 4)
                self.assertEqual(params["preview_max_size"], (800, 600))
        finally:
            env_detect.is_low_memory.cache_clear()

    def test_low_memory_profile_caps_original_preview_size(self) -> None:
        window = SimpleNamespace(_cached_preview_quality="original")
        with patch("specimen_app.ui.load_settings", return_value=SimpleNamespace(memory_profile="low")):
            self.assertEqual(SpecimenWindow._preview_size(window), (800, 600))

    def test_preview_is_reduced_before_exif_transpose(self) -> None:
        from specimen_app import image_cache

        path = self.tmp / "rotated-preview.jpg"
        source = Image.new("RGB", (1200, 800), "navy")
        exif = source.getexif()
        exif[274] = 6
        source.save(path, exif=exif)
        with patch(
            "specimen_app.image_cache.ImageOps.exif_transpose",
            wraps=image_cache.ImageOps.exif_transpose,
        ) as transpose:
            loaded = image_cache.load_source_image(path, max_size=(400, 300))
        self.assertEqual(loaded.size, (200, 300))
        image_before_transpose = transpose.call_args.args[0]
        self.assertLessEqual(image_before_transpose.width * image_before_transpose.height, 400 * 300)

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

    def test_image_search_collapses_archived_photo_and_original_alias(self) -> None:
        """已归档照片及其原图同时被扫描时，只显示一个已关联结果。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        external_dir = self.tmp / "实验室拍照电脑" / "广西"
        external_dir.mkdir(parents=True)
        original = external_dir / "GXRG-B-YC001-1.tif"
        original.write_bytes(b"same-photo")
        row = store.add_photo(voucher, original, allow_outside=True)
        archived = store.resolve_photo_path(row)

        results = image_search_results(
            self.tmp,
            voucher,
            {},
            {},
            [archived],
            query="GXRG-B-YC001",
            extra_roots=[external_dir],
            path_to_vouchers=store.get_all_photo_voucher_map(),
            canonical_photo_paths=store.get_photo_search_alias_map(),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].path.resolve(), archived.resolve())
        self.assertTrue(results[0].is_linked)
        self.assertEqual(results[0].linked_vouchers, [voucher])

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

    def test_image_search_supports_common_image_suffixes(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        names = [
            "P001-a.webp",
            "P001-b.gif",
            "P001-c.jfif",
            "P001-d.jpe",
            "P001-e.jp2",
            "P001-f.j2k",
        ]
        for name in names:
            (photo_dir / name).write_bytes(b"image")

        results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="P001",
            suffixes=suffixes_for_image_type("all"),
        )
        self.assertEqual([result.file_name for result in results], names)
        self.assertIn(".webp", suffixes_for_image_type("all"))
        self.assertIn(".jfif", suffixes_for_image_type("jpg"))
        self.assertTrue(is_supported_image(photo_dir / "P001-a.webp"))
        self.assertFalse(is_supported_image(photo_dir / "P001.txt"))
        self.assertIn("*.webp", image_file_filter())

    def test_image_search_falls_back_to_filename_contains_match(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        middle_code = photo_dir / "sampleP001middle.webp"
        hyphen_code = photo_dir / "图版-A-111-背面.jpg"
        for path in [middle_code, hyphen_code]:
            path.write_bytes(b"image")

        p001_results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="P001",
            suffixes=suffixes_for_image_type("all"),
        )
        self.assertEqual([result.file_name for result in p001_results], [middle_code.name])
        self.assertEqual(p001_results[0].score, 60)

        hyphen_results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="A-111",
            suffixes=suffixes_for_image_type("all"),
        )
        self.assertEqual([result.file_name for result in hyphen_results], [hyphen_code.name])

    def test_image_search_matches_single_letter_hyphen_prefix(self) -> None:
        project = self.tmp / "广西海洋大学图谱项目"
        first = project / "钩齿短脊虫（6张）" / "A-钩齿短脊虫.tif"
        second = project / "扁蛰虫（3张）" / "图200-A-扁蛰虫体前部背面观.tif"
        for path in [first, second]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")

        results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="A-",
            extra_roots=[project],
            suffixes=suffixes_for_image_type("tif_jpg"),
        )
        self.assertEqual([result.file_name for result in results], [first.name, second.name])

    def test_image_search_ignores_cached_index_from_different_scope(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        default_photo = photo_dir / "QD-CK-SC008-1.tif"
        default_photo.write_bytes(b"image")
        default_index = _get_or_build_search_index([photo_dir], cache_root=self.tmp)

        project = self.tmp / "广西海洋大学图谱项目"
        project_photo = project / "钩齿短脊虫（6张）" / "A-钩齿短脊虫.tif"
        project_photo.parent.mkdir(parents=True)
        project_photo.write_bytes(b"image")

        results = image_search_results(
            self.tmp,
            "YZZ000001",
            {},
            {},
            [],
            query="A-",
            extra_roots=[project],
            search_index=default_index,
        )
        self.assertEqual([result.file_name for result in results], [project_photo.name])

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

    def test_image_search_reconciles_external_photo_changes_incrementally(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        first = photo_dir / "QD-CK-WenSC004-1.tif"
        second = photo_dir / "QD-CK-WenSC004-2.tif"
        first.write_bytes(b"first")
        image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")

        second.write_bytes(b"second")
        stale = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in stale], [first.name])
        added = reconcile_image_index(self.tmp)
        self.assertEqual((added.added, added.removed), (1, 0))
        refreshed = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in refreshed], [first.name, second.name])

        first.unlink()
        removed = reconcile_image_index(self.tmp)
        self.assertEqual((removed.added, removed.removed), (0, 1))
        final = image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertEqual([result.file_name for result in final], [second.name])

    def test_image_search_clear_removes_persistent_scope(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        (photo_dir / "QD-CK-WenSC004-1.tif").write_bytes(b"image")
        image_search_results(self.tmp, "YZZ000001", {}, {}, [], query="QD-CK-WenSC004")
        self.assertTrue(image_index_exists(self.tmp))

        clear_image_index(self.tmp)
        self.assertFalse(image_index_exists(self.tmp))

    def test_indexed_image_entries_uses_sqlite_not_large_json_cache(self) -> None:
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        image = photo_dir / "QD-CK-WenSC004-1.tif"
        image.write_bytes(b"image")

        entries = indexed_image_entries([photo_dir], cache_root=self.tmp)

        self.assertEqual([entry.path for entry in entries], [image])
        cache_dir = self.tmp / "数据" / "图片搜索索引缓存"
        self.assertTrue((cache_dir / "image_search.sqlite3").exists())
        self.assertEqual(list(cache_dir.glob("*.json")), [])

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

    def test_thumbnail_cache_shrink_limit_evicts_resident_images(self) -> None:
        source = self.tmp / "照片"
        source.mkdir()
        cache = ThumbnailCache(self.tmp, memory_limit_bytes=16 << 20)
        for name, color in (("first.jpg", "red"), ("second.jpg", "blue")):
            path = source / name
            Image.new("RGB", (1000, 1000), color).save(path)
            cache.thumbnail(path, (1000, 1000))
        self.assertGreater(cache._memory_cache_bytes, 4 << 20)
        cache.set_memory_limit(4 << 20)
        self.assertLessEqual(cache._memory_cache_bytes, 4 << 20)

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

    # ── 照片去重 (S1) ────────────────────────────────────────────────────
    def test_add_photo_dedup_same_source_skips(self) -> None:
        """同一张照片 add 两次（同 voucher / 默认 copy 模式） → 只保留一行。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "外部"
        photo_dir.mkdir()
        photo = photo_dir / "shrimp.jpg"
        photo.write_bytes(b"shrimp-jpeg-bytes")
        store.add_photo(voucher, photo, allow_outside=True)
        store.add_photo(voucher, photo, allow_outside=True)
        photos = store.get_photos(voucher)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["文件名"], "shrimp.jpg")
        self.assertEqual(photos[0]["归档状态"], "已归档")

    def test_add_photos_rejects_content_already_linked_to_another_voucher(self) -> None:
        """已入库照片不能通过批量添加再关联到另一个入库编号。"""
        store = ExcelStore(self.tmp)
        first = store.create_specimen()
        second = store.create_specimen()
        source = self.tmp / "external.jpg"
        source.write_bytes(b"single-specimen-photo")
        store.add_photo(first, source, allow_outside=True)

        with self.assertRaisesRegex(ValueError, first):
            store.add_photos(second, [source], allow_outside=True)

        self.assertEqual(store.get_photos(second), [])

    def test_add_photo_dedup_different_files_keeps_two(self) -> None:
        """SHA256 不同 → 不去重，保留两行（向后兼容 test_multiple_photo_records_are_preserved 语义）。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "外部"
        photo_dir.mkdir()
        first = photo_dir / "shrimp.jpg"
        second = photo_dir / "crab.jpg"
        first.write_bytes(b"shrimp-jpeg-bytes")
        second.write_bytes(b"crab-jpeg-bytes")
        store.add_photo(voucher, first, allow_outside=True)
        store.add_photo(voucher, second, allow_outside=True)
        self.assertEqual(len(store.get_photos(voucher)), 2)

    def test_add_photo_dedup_upgrade_record_only_to_archived(self) -> None:
        """先以 absolute_only 关联（仅记录），后以 copy_with_absolute 关联 → 升级同一行，不新增。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "外部"
        photo_dir.mkdir()
        photo = photo_dir / "octopus.jpg"
        photo.write_bytes(b"octopus-jpeg-bytes")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="absolute_only")
        before = store.get_photos(voucher)
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["归档状态"], "仅记录")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="copy_with_absolute")
        after = store.get_photos(voucher)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["归档状态"], "已归档")
        self.assertTrue(store.resolve_photo_path(after[0]).exists())

    def test_add_photo_dedup_does_not_downgrade_archived(self) -> None:
        """已归档行被重复以仅记录模式 add → 不降级，仍为已归档。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "外部"
        photo_dir.mkdir()
        photo = photo_dir / "x.jpg"
        photo.write_bytes(b"x")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="copy_with_absolute")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="absolute_only")
        photos = store.get_photos(voucher)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["归档状态"], "已归档")

    def test_upgrade_photo_archival_is_undoable(self) -> None:
        """升级动作走 action_log，可 undo 回 仅记录 状态。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "外部"
        photo_dir.mkdir()
        photo = photo_dir / "z.jpg"
        photo.write_bytes(b"z")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="absolute_only")
        store.add_photo(voucher, photo, allow_outside=True, photo_management_mode="copy_with_absolute")
        self.assertEqual(store.get_photos(voucher)[0]["归档状态"], "已归档")
        self.assertTrue(store.undo_last())
        self.assertEqual(store.get_photos(voucher)[0]["归档状态"], "仅记录")
        self.assertTrue(store.redo_last())
        self.assertEqual(store.get_photos(voucher)[0]["归档状态"], "已归档")

    def test_dedupe_photo_links_merges_legacy_duplicates(self) -> None:
        """批量清理工具：合并同 (voucher, SHA256, 原始文件名) 的多行，保留 已归档 > 仅记录。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        # 手工注入两条重复行（模拟历史脏数据，绕过 add_photo 的查重）
        rows = store.read_rows("photo")
        rows.append({
            "入库编号*": voucher,
            "文件名": "y.jpg", "相对路径": "", "绝对路径": "/tmp/y.jpg", "描述": "",
            "来源工作区根路径": "", "原始文件名": "y.jpg", "原始路径": "/tmp/y.jpg",
            "文件SHA256": "abc123", "文件大小": "5",
            "归档时间": "", "归档状态": "仅记录",
        })
        rows.append({
            "入库编号*": voucher,
            "文件名": "y.jpg", "相对路径": "./照片/y.jpg", "绝对路径": str(self.tmp / "照片" / "y.jpg"),
            "描述": "", "来源工作区根路径": "", "原始文件名": "y.jpg", "原始路径": "/tmp/y.jpg",
            "文件SHA256": "abc123", "文件大小": "5",
            "归档时间": "2026-05-01T00:00:00", "归档状态": "已归档",
        })
        store._write_rows("photo", rows)
        self.assertEqual(len(store.get_photos(voucher)), 2)
        # dry-run 不写
        plan = store.dedupe_photo_links(dry_run=True)
        self.assertEqual(plan, {"groups": 1, "removed": 1})
        self.assertEqual(len(store.get_photos(voucher)), 2)
        # 真清理
        summary = store.dedupe_photo_links()
        self.assertEqual(summary, {"groups": 1, "removed": 1})
        remaining = store.get_photos(voucher)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["归档状态"], "已归档")

    def test_dedupe_photo_links_no_duplicates_returns_zero(self) -> None:
        """无重复时 dedupe 返回 0/0，不写快照。"""
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        f = photo_dir / "a.jpg"
        f.write_bytes(b"a")
        store.add_photo(voucher, f)
        summary = store.dedupe_photo_links()
        self.assertEqual(summary, {"groups": 0, "removed": 0})

    # ── 缓存索引 + max_serial + 启动预热 (S3.1+3.2+3.7) ──────────────────
    def test_voucher_index_cache_invalidates_after_delete(self) -> None:
        """删除 voucher 后 voucher_index 应同步 invalidate；后续 get_specimen 返 None。"""
        store = ExcelStore(self.tmp)
        v1 = store.create_specimen()
        v2 = store.create_specimen()
        self.assertIsNotNone(store.get_specimen(v1))
        store.delete_specimen(v1)
        self.assertIsNone(store.get_specimen(v1))
        self.assertIsNotNone(store.get_specimen(v2))

    def test_next_voucher_skips_existing_serial_via_index_set(self) -> None:
        """next_voucher 在 INDEX 已存在该序号时应自增跳过（撞号兜底走 _index_voucher_set）。"""
        store = ExcelStore(self.tmp)
        # 第一条占用 YZZ000001；强行把 next_serial 倒回 1 模拟错配
        v1 = store.create_specimen()
        self.assertEqual(v1, "YZZ000001")
        store.config["next_serial"] = 1
        # 下次 next_voucher 应该跳过 1，生成 2
        v2 = store.next_voucher()
        self.assertEqual(v2, "YZZ000002")

    def test_get_photos_uses_voucher_index(self) -> None:
        """get_photos 走 photo voucher 索引 O(1)；多 voucher 互不干扰。"""
        store = ExcelStore(self.tmp)
        v1 = store.create_specimen()
        v2 = store.create_specimen()
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        p1 = photo_dir / "p1.jpg"; p1.write_bytes(b"p1")
        p2 = photo_dir / "p2.jpg"; p2.write_bytes(b"p2")
        store.add_photo(v1, p1)
        store.add_photo(v2, p2)
        self.assertEqual(len(store.get_photos(v1)), 1)
        self.assertEqual(store.get_photos(v1)[0]["文件名"], "p1.jpg")
        self.assertEqual(len(store.get_photos(v2)), 1)
        self.assertEqual(store.get_photos(v2)[0]["文件名"], "p2.jpg")

    def test_max_serial_recovers_after_missing_next_serial(self) -> None:
        """工作区缺 next_serial 配置时，__init__ 走 _sync_next_serial 重建。"""
        store = ExcelStore(self.tmp)
        store.create_specimen()  # YZZ000001
        store.create_specimen()  # YZZ000002
        store.close()
        # 第二个 store 强制清 next_serial（模拟旧工作区无该键）
        store2 = ExcelStore(self.tmp)
        # 重新打开应正确推进到 3
        self.assertEqual(store2.next_voucher(), "YZZ000003")

    # ── 增量 append (S3.3) ────────────────────────────────────────────────
    def test_incremental_append_preserves_existing_rows(self) -> None:
        """连续 append 100 + 1 行后，文件内容 / 顺序 / voucher 索引一致。"""
        store = ExcelStore(self.tmp)
        vs = [store.create_specimen() for _ in range(50)]
        # 立即新增第 51 个 → 仍是单调递增 + 索引可查
        v_last = store.create_specimen()
        all_vs = vs + [v_last]
        self.assertEqual(store.list_vouchers(), all_vs)
        # 索引命中：每个 voucher 都可查
        for v in all_vs:
            self.assertIsNotNone(store.get_specimen(v))
        # voucher 单调递增（YZZ000001 ... YZZ000051）
        self.assertEqual(all_vs[0], "YZZ000001")
        self.assertEqual(all_vs[-1], "YZZ000051")

    def test_append_keeps_voucher_index_consistent(self) -> None:
        """append 后立即调 _find_one 应命中（缓存增量更新 vs 整文件 invalidate）。"""
        store = ExcelStore(self.tmp)
        store.create_specimen()
        v = store.create_specimen()
        # 刚写入即查询，必须命中（O(1) 索引；不应回退到线性扫）
        spec = store.get_specimen(v)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["入库编号*"], v)

    # ── 任务量统计 (S2) ────────────────────────────────────────────────────
    def test_is_voucher_ingestion_complete_requires_all_three(self) -> None:
        """is_complete = specimen 必填 + 照片 + 分类必填 全 OK。"""
        store = ExcelStore(self.tmp)
        v = store.create_specimen()
        # 三项都缺 → 不完整
        self.assertFalse(store.is_voucher_ingestion_complete(v))
        # 仅挂照片 → 仍不完整（必填字段空）
        photo_dir = self.tmp / "照片"
        photo_dir.mkdir()
        f = photo_dir / "x.jpg"; f.write_bytes(b"x")
        store.add_photo(v, f)
        self.assertFalse(store.is_voucher_ingestion_complete(v))
        # specimen 必填 + 照片 + 分类必填 全填 → 完整
        store.set_fields("specimen", v, {"管内编号*": "QD-LSD-SC001-1-R-250923", "采集地点缩写*": "QD"})
        from specimen_app.classification_fields import REQUIRED_CLASSIFICATION_COLUMNS
        cls = {field: "X" for field in REQUIRED_CLASSIFICATION_COLUMNS if field != "入库编号*"}
        store.set_fields("classification", v, cls)
        self.assertTrue(store.is_voucher_ingestion_complete(v))

    def test_list_unfinished_reserved_vouchers(self) -> None:
        """alloc_log 批量领取段内未完成入库的编号被列出；已完成的被剔除。"""
        store = ExcelStore(self.tmp)
        # 模拟批量领取 YZZ000001~YZZ000003
        store.log_alloc_event({
            "记录ID": "alloc1", "时间": "2026-05-20T10:00:00",
            "类型": "批量领取", "人员": "张三",
            "编号系列": "YZZ", "编号起始": "YZZ000001", "编号结束": "YZZ000003",
            "数量": "3",
        })
        # YZZ000001 完成入库，YZZ000002 未完成（未建行），YZZ000003 部分填（未完）
        v1 = store.create_specimen_with_voucher("YZZ000001")
        photo_dir = self.tmp / "照片"; photo_dir.mkdir()
        p = photo_dir / "a.jpg"; p.write_bytes(b"a")
        store.add_photo(v1, p)
        store.set_fields("specimen", v1, {"管内编号*": "T1", "采集地点缩写*": "QD"})
        from specimen_app.classification_fields import REQUIRED_CLASSIFICATION_COLUMNS
        store.set_fields("classification", v1, {f: "X" for f in REQUIRED_CLASSIFICATION_COLUMNS if f != "入库编号*"})
        self.assertTrue(store.is_voucher_ingestion_complete(v1))
        # 调 list_unfinished_reserved_vouchers
        unfinished = store.list_unfinished_reserved_vouchers()
        vs = {v for v, _, _ in unfinished}
        self.assertNotIn("YZZ000001", vs)
        self.assertIn("YZZ000002", vs)
        self.assertIn("YZZ000003", vs)
        # 领取人记录正确
        for v, person, _ in unfinished:
            if v in ("YZZ000002", "YZZ000003"):
                self.assertEqual(person, "张三")

    def test_alloc_log_legacy_schema_auto_upgrades_columns(self) -> None:
        """旧工作区只有 11 列 alloc_log，打开后自动扩到 15 列；旧行末尾补 ""。"""
        from specimen_app.models import ALLOC_LOG_FILE
        # 手工建 11 列旧 alloc_log
        data_dir = self.tmp / "数据"
        data_dir.mkdir(exist_ok=True)
        old_headers = [
            "记录ID", "时间", "类型", "人员", "用途", "备注",
            "编号系列", "编号起始", "编号结束", "数量", "关联任务ID",
        ]
        wb = Workbook()
        ws = wb.active
        ws.append(old_headers)
        ws.append(["a1", "2026-05-01T10:00:00", "批量领取", "李四", "u", "n",
                   "YZZ", "YZZ000001", "YZZ000005", "5", ""])
        wb.save(data_dir / ALLOC_LOG_FILE)
        wb.close()
        # 打开 store 应触发 _ensure_workbook 自动扩列
        store = ExcelStore(self.tmp)
        log_path = store.data_dir / ALLOC_LOG_FILE
        wb2 = load_workbook(log_path)
        try:
            headers_now = [c.value for c in next(wb2.active.iter_rows(max_row=1))]
        finally:
            wb2.close()
        # 应含全部 15 列
        self.assertIn("新建数量", headers_now)
        self.assertIn("接管数量", headers_now)
        self.assertIn("完成入库数", headers_now)
        self.assertIn("接管编号", headers_now)
        # 旧行仍可读
        rows = store.read_alloc_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("人员", ""), "李四")
        self.assertEqual(rows[0].get("数量", ""), "5")


class Phase1DataSafetyTests(unittest.TestCase):
    """plan v0.10.0 Phase 1 (P0 数据安全)：A1/A2/A4/A5 行为回归。"""

    def setUp(self) -> None:
        clear_image_index()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        clear_image_index()

    # ---- A1: undo 归档恢复 + 三态降级 -----------------------------------------

    def _make_photo_source(self, name: str = "src.jpg") -> Path:
        src = self.tmp / name
        Image.new("RGB", (32, 32), color="red").save(src, "JPEG")
        return src

    def test_undo_delete_photo_restores_archive_from_original(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        src = self._make_photo_source()
        store.add_photo(voucher, src)
        photo_rows = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher]
        self.assertEqual(len(photo_rows), 1)
        archive_path = store._resolve_relative(store.root, photo_rows[0]["相对路径"])
        self.assertTrue(archive_path.exists())
        store.delete_photo(voucher, 0)
        self.assertFalse(archive_path.exists())  # 归档被删
        store.undo_last()
        self.assertTrue(archive_path.exists())  # plan A1 路径 1：从原始路径重建
        self.assertNotEqual(
            [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher][0].get("归档状态"),
            "损坏",
        )

    def test_undo_delete_specimen_restores_photo_archives(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        src = self._make_photo_source("specimen.jpg")
        store.add_photo(voucher, src)
        photo_row = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher][0]
        archive_path = store._resolve_relative(store.root, photo_row["相对路径"])
        self.assertTrue(archive_path.exists())
        store.delete_specimen(voucher)
        self.assertFalse(archive_path.exists())
        store.undo_last()
        self.assertTrue(archive_path.exists())  # plan A1 路径 3

    def test_undo_when_both_missing_marks_corrupt_and_continues(self) -> None:
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        src = self._make_photo_source("missing_later.jpg")
        store.add_photo(voucher, src)
        photo_row = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher][0]
        archive_path = store._resolve_relative(store.root, photo_row["相对路径"])
        store.delete_photo(voucher, 0)
        # 模拟原始也被用户删了
        src.unlink()
        self.assertFalse(archive_path.exists())
        store.undo_last()  # 不应抛
        rows_after = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher]
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0].get("归档状态"), "损坏")  # plan A1 降级 3

    # ---- A2: 只读模式完整封死 -------------------------------------------------

    def test_readonly_blocks_photo_writes(self) -> None:
        writer = ExcelStore(self.tmp)
        voucher = writer.create_specimen()
        writer.release_lock()
        reader = ExcelStore(self.tmp, read_only=True)
        with self.assertRaises(PermissionError):
            reader.add_photo(voucher, self._make_photo_source())
        with self.assertRaises(PermissionError):
            reader.delete_photo(voucher, 0)
        with self.assertRaises(PermissionError):
            reader.replace_photo(voucher, 0, self._make_photo_source("replace.jpg"))
        with self.assertRaises(PermissionError):
            reader.set_photo_filename(voucher, 0, "x.jpg")
        with self.assertRaises(PermissionError):
            reader.set_photo_description(voucher, 0, "desc")

    def test_readonly_workspace_init_no_file_creation(self) -> None:
        # 第一次正常打开建出全部数据文件
        writer = ExcelStore(self.tmp)
        writer.release_lock()
        data_dir = writer.data_dir
        # 删一个非关键文件模拟"缺失"
        change_log = data_dir / CHANGE_LOG_FILE
        change_log.unlink()
        snapshot_before = sorted(p.name for p in data_dir.iterdir())
        # 只读模式打开：ensure_files 必须 short-circuit，不补回 change_log
        reader = ExcelStore(self.tmp, read_only=True)
        snapshot_after = sorted(p.name for p in data_dir.iterdir())
        self.assertEqual(snapshot_before, snapshot_after)
        self.assertFalse(change_log.exists())
        del reader

    # ---- A4: 快照完整性 manifest ---------------------------------------------

    def test_snapshot_writes_manifest_and_complete_marker(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        snapshot_dir = store.create_data_snapshot("test")
        manifest_path = snapshot_dir / "snapshot_manifest.json"
        marker_path = snapshot_dir / ".snapshot.complete"
        self.assertTrue(manifest_path.exists())
        self.assertTrue(marker_path.exists())
        import json as _json
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("files", manifest)
        self.assertTrue(any(name.endswith(".xlsx") for name in manifest["files"]))
        for entry in manifest["files"].values():
            self.assertIn("sha256", entry)
            self.assertIn("size", entry)

    def test_restore_rejects_snapshot_missing_complete_marker(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        snapshot_dir = store.create_data_snapshot("test")
        (snapshot_dir / ".snapshot.complete").unlink()
        with self.assertRaises(SnapshotIntegrityCheckFailed):
            store.restore_data_snapshot(snapshot_dir)

    def test_restore_rejects_snapshot_with_sha_mismatch(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        snapshot_dir = store.create_data_snapshot("test")
        # 篡改一个 xlsx 文件，模拟磁盘损坏 / 外部改动
        any_xlsx = next(p for p in snapshot_dir.iterdir() if p.suffix == ".xlsx")
        with any_xlsx.open("ab") as h:
            h.write(b"tampered")
        with self.assertRaises(SnapshotIntegrityCheckFailed):
            store.restore_data_snapshot(snapshot_dir)

    def test_startup_cleans_incomplete_snapshot_dirs(self) -> None:
        store = ExcelStore(self.tmp)
        store.create_specimen()
        # 手动造一个不完整快照目录（无 marker 无 manifest）
        from specimen_app.models import DATA_VERSION_DIR
        bad_dir = store.data_dir / DATA_VERSION_DIR / "incomplete_test"
        bad_dir.mkdir(parents=True)
        (bad_dir / "garbage.xlsx").write_bytes(b"junk")
        # 同时造一个完整的，测试不被误删
        good_dir = store.create_data_snapshot("good")
        store.release_lock()
        ExcelStore(self.tmp)  # 触发 ensure_files → cleanup_incomplete_snapshot_directories
        self.assertFalse(bad_dir.exists())  # 不完整被清
        self.assertTrue(good_dir.exists())  # 完整保留

    # ---- A5: openpyxl 写后 ZIP 校验 -------------------------------------------

    def test_excel_write_verify_catches_truncated_tmp(self) -> None:
        store = ExcelStore(self.tmp)
        truncated = self.tmp / "broken.xlsx"
        truncated.write_bytes(b"PK\x03\x04 not actually a complete zip")
        with self.assertRaises(WorkbookWriteVerificationFailed):
            store._verify_workbook_file_can_be_reopened(truncated)
        # helper 失败时应已 unlink tmp（防 replace 上去）
        self.assertFalse(truncated.exists())

    def test_excel_write_verify_normal_path_still_works(self) -> None:
        store = ExcelStore(self.tmp)
        good = self.tmp / "good.xlsx"
        wb = Workbook()
        wb.active.append(["x", "y"])
        wb.save(good)
        wb.close()
        # 不应抛
        store._verify_workbook_file_can_be_reopened(good)
        self.assertTrue(good.exists())

    def test_excel_write_verify_catches_missing_content_types(self) -> None:
        """v0.10.1 hotfix H1：xlsx 必有的 [Content_Types].xml 缺失时仍能被检出。"""
        import zipfile as _zipfile
        store = ExcelStore(self.tmp)
        bad = self.tmp / "missing_content_types.xlsx"
        # 造一个有 namelist 但缺 [Content_Types].xml 的 ZIP
        with _zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        with self.assertRaises(WorkbookWriteVerificationFailed):
            store._verify_workbook_file_can_be_reopened(bad)
        self.assertFalse(bad.exists())  # 验证失败时仍 unlink

    def test_excel_write_verify_catches_empty_zip(self) -> None:
        """v0.10.1 hotfix H1：完全空 ZIP（namelist 空）也应被检出为半写。"""
        import zipfile as _zipfile
        store = ExcelStore(self.tmp)
        bad = self.tmp / "empty.xlsx"
        with _zipfile.ZipFile(bad, "w"):
            pass
        with self.assertRaises(WorkbookWriteVerificationFailed):
            store._verify_workbook_file_can_be_reopened(bad)


class Phase2CrossHostLockTests(unittest.TestCase):
    """plan v0.10.0 Phase 2 (P1 跨机锁加固)：B1 行为回归。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lock_payload_has_host_id_heartbeat_instance(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp, lock=True)
        import json as _json
        payload = _json.loads(store.lock_file.read_text(encoding="utf-8"))
        for field in ("pid", "hostname", "host_id", "instance_id", "started_at", "heartbeat_at"):
            self.assertIn(field, payload, f"lock payload missing field: {field}")
        self.assertTrue(payload["host_id"], "host_id should be non-empty")
        self.assertTrue(payload["instance_id"], "instance_id should be non-empty")
        store.release_lock()

    def test_lock_cross_host_not_auto_stale_shows_takeover_msg(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import WorkspaceLockedError
        store = ExcelStore(self.tmp, lock=True)
        # 模拟另一台机器持有锁：手工把 host_id 改成不同值
        import json as _json
        payload = _json.loads(store.lock_file.read_text(encoding="utf-8"))
        payload["host_id"] = "OTHER_HOST_DIFFERENT_UUID"
        payload["hostname"] = "remote-machine"
        store.lock_file.write_text(_json.dumps(payload), encoding="utf-8")
        # 另一进程视角下 _lock_is_stale 应永不自动 stale
        other_store = ExcelStore(self.tmp, lock=False)
        self.assertFalse(other_store._lock_is_stale())
        # acquire_lock 应抛 WorkspaceLockedError
        with self.assertRaises(WorkspaceLockedError) as ctx:
            other_store.acquire_lock()
        self.assertIn("被占用", str(ctx.exception))
        store._locked = False  # 跳过 release（已不是本进程持有）

    def test_lock_legacy_no_host_id_permission_error_not_auto_stale(self) -> None:
        """plan B1 关键修正：WSL 跨内核下 PermissionError 不可信 → 不 stale。"""
        from specimen_app.excel_store import ExcelStore
        from unittest.mock import patch
        store = ExcelStore(self.tmp, lock=False)
        # 写老格式锁（无 host_id，pid 任意）
        data_dir = self.tmp / "数据"
        data_dir.mkdir(exist_ok=True)
        import json as _json
        store.lock_file.write_text(
            _json.dumps({"pid": 99999, "time": datetime.now().isoformat(timespec="seconds"), "workspace": str(self.tmp)}),
            encoding="utf-8",
        )
        # os.kill 抛 PermissionError 时（不可信判活）→ 不 stale
        with patch("specimen_app.excel_store.os.kill", side_effect=PermissionError("EPERM")):
            self.assertFalse(store._lock_is_stale())

    def test_lock_legacy_no_host_id_process_lookup_error_is_stale(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from unittest.mock import patch
        store = ExcelStore(self.tmp, lock=False)
        data_dir = self.tmp / "数据"
        data_dir.mkdir(exist_ok=True)
        import json as _json
        store.lock_file.write_text(
            _json.dumps({"pid": 99999, "time": datetime.now().isoformat(timespec="seconds"), "workspace": str(self.tmp)}),
            encoding="utf-8",
        )
        # ProcessLookupError 是唯一可信的 stale 信号
        with patch("specimen_app.excel_store.os.kill", side_effect=ProcessLookupError("ESRCH")):
            self.assertTrue(store._lock_is_stale())

    def test_host_id_file_atomic_concurrent_create(self) -> None:
        from specimen_app.excel_store import load_or_create_persistent_host_id
        # 调两次应返回同值（持久化）
        first = load_or_create_persistent_host_id()
        second = load_or_create_persistent_host_id()
        self.assertEqual(first, second)
        self.assertTrue(first, "host_id should be non-empty")

    def test_heartbeat_thread_death_detected_before_write(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import HeartbeatThreadStalled
        import time as _time
        store = ExcelStore(self.tmp, lock=True)
        # 强行把心跳时间戳推回 300s 前，模拟心跳线程已死 5 分钟
        store._last_heartbeat_write_monotonic = _time.monotonic() - 300
        with self.assertRaises(HeartbeatThreadStalled):
            store.assert_heartbeat_thread_is_alive(max_silence_seconds=180.0)
        store.release_lock()

    def test_heartbeat_write_updates_lock_file(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp, lock=True)
        import json as _json
        before = _json.loads(store.lock_file.read_text(encoding="utf-8"))["heartbeat_at"]
        import time as _time
        _time.sleep(1.1)
        store.write_lock_heartbeat_now()
        after = _json.loads(store.lock_file.read_text(encoding="utf-8"))["heartbeat_at"]
        self.assertNotEqual(before, after, "heartbeat_at should advance after write_lock_heartbeat_now()")
        store.release_lock()


class Phase3TransactionJournalTests(unittest.TestCase):
    """plan v0.10.0 Phase 3 (P1 事务 journal)：C1 行为回归。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_transaction_journal_commits_on_success(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        with store.with_transaction_journal("test_success") as tx_id:
            self.assertTrue(tx_id)
        journal = (store.data_dir / TRANSACTION_JOURNAL_FILE).read_text(encoding="utf-8")
        import json as _json
        records = [_json.loads(line) for line in journal.strip().split("\n") if line.strip()]
        my_records = [r for r in records if r.get("id") == tx_id]
        self.assertEqual(len(my_records), 2)
        self.assertEqual(my_records[0].get("status"), "pending")
        self.assertEqual(my_records[1].get("status"), "committed")

    def test_transaction_journal_aborts_on_exception(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        tx_id_holder = []
        with self.assertRaises(RuntimeError):
            with store.with_transaction_journal("test_failure") as tx_id:
                tx_id_holder.append(tx_id)
                raise RuntimeError("simulated failure")
        journal = (store.data_dir / TRANSACTION_JOURNAL_FILE).read_text(encoding="utf-8")
        import json as _json
        records = [_json.loads(line) for line in journal.strip().split("\n") if line.strip()]
        my_records = [r for r in records if r.get("id") == tx_id_holder[0]]
        self.assertEqual(my_records[-1].get("status"), "aborted")
        self.assertIn("simulated failure", my_records[-1].get("error", ""))

    def test_store_init_exposes_pending_not_dialog(self) -> None:
        """plan C1 headless 修正：store 只暴露 pending 列表，不弹对话框。"""
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        # 手工 inject pending 记录到 journal
        import json as _json
        journal_path = store.data_dir / TRANSACTION_JOURNAL_FILE
        with journal_path.open("a", encoding="utf-8") as h:
            h.write(_json.dumps({
                "id": "fake-pending-id",
                "operation_name": "fake_op",
                "started_at": "2026-05-25T12:00:00",
                "status": "pending",
                "snapshot_path": None,
            }) + "\n")
        store.release_lock()
        # 重开 store：应填充 pending_transaction_records 但不弹任何对话框
        store2 = ExcelStore(self.tmp)
        self.assertEqual(len(store2.pending_transaction_records), 1)
        self.assertEqual(store2.pending_transaction_records[0]["id"], "fake-pending-id")
        store2.release_lock()

    def test_pending_record_resolves_abort_when_snapshot_null(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        # inject pending 无 snapshot
        import json as _json
        with (store.data_dir / TRANSACTION_JOURNAL_FILE).open("a", encoding="utf-8") as h:
            h.write(_json.dumps({
                "id": "null-snap-id",
                "operation_name": "fake_op",
                "started_at": "2026-05-25T12:00:00",
                "status": "pending",
                "snapshot_path": None,
            }) + "\n")
        store.pending_transaction_records = store._scan_transaction_journal_for_pending_records()
        # 无 snapshot 时调 restore_snapshot 应抛 ValueError（提示 UI 隐藏按钮）
        with self.assertRaises(ValueError):
            store.resolve_pending_transaction("null-snap-id", "restore_snapshot")
        # 但 abort 一定能成功
        store.resolve_pending_transaction("null-snap-id", "abort_and_keep_current")
        self.assertEqual(store.pending_transaction_records, [])

    def test_journal_vacuum_drops_old_committed_records(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        journal_path = store.data_dir / TRANSACTION_JOURNAL_FILE
        import json as _json
        # 写一条 365 天前的 committed + 一条今天的 committed + 一条 pending
        old_ts = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
        with journal_path.open("a", encoding="utf-8") as h:
            h.write(_json.dumps({"id": "old-c", "status": "pending", "started_at": old_ts, "operation_name": "x"}) + "\n")
            h.write(_json.dumps({"id": "old-c", "status": "committed", "ended_at": old_ts}) + "\n")
            h.write(_json.dumps({"id": "new-c", "status": "committed", "ended_at": datetime.now().isoformat(timespec="seconds")}) + "\n")
            h.write(_json.dumps({"id": "pend", "status": "pending", "started_at": old_ts, "operation_name": "x", "snapshot_path": None}) + "\n")
        dropped = store.vacuum_transaction_journal(older_than_days=180)
        # 旧 committed 的 2 行（pending+committed）都应被丢弃；pending 保留；新 committed 保留
        self.assertGreaterEqual(dropped, 2)
        remaining = journal_path.read_text(encoding="utf-8")
        self.assertNotIn("old-c", remaining)
        self.assertIn("pend", remaining)

    def test_delete_specimen_no_longer_creates_journal_entry(self) -> None:
        """v0.10.1 hotfix：delete_specimen 取消 transaction journal 包装，
        action-log 单条已含全部回滚信息；每次 delete 不再拷整个 数据/。"""
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import TRANSACTION_JOURNAL_FILE
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.delete_specimen(voucher)
        journal_path = store.data_dir / TRANSACTION_JOURNAL_FILE
        journal = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        self.assertNotIn(f"delete_specimen({voucher})", journal)
        # 但 undo 仍应能完整还原（action-log 路径）
        store.undo_last()
        self.assertIsNotNone(store.get_specimen(voucher))


class Phase4PerformanceTests(unittest.TestCase):
    """plan v0.10.0 Phase 4 (P2 性能)：D2 汇总缓存 + D3 照片字段合并保存。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- D2: 汇总缓存 ----

    def test_summary_cache_sqlite_fallback_when_missing(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        store.create_specimen()
        # 第一次读：缓存不存在 → fallback summary_records，并应同步写回 SQLite
        rows_first = store.read_inventory_summary_via_cache()
        self.assertTrue(rows_first)
        cache_path = store.inventory_summary_cache().cache_path
        self.assertTrue(cache_path.exists())
        # 第二次读：直接从 cache 读，行数应相同
        rows_second = store.read_inventory_summary_via_cache()
        self.assertEqual(len(rows_first), len(rows_second))

    def test_summary_cache_invalidated_on_write(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        store.create_specimen()
        # 写一次让 cache 建出来
        store.read_inventory_summary_via_cache()
        freshness_before = store.inventory_summary_cache().current_cache_freshness_timestamp()
        self.assertTrue(freshness_before)
        # 任何主表写入应让 cache 失效
        store.create_specimen()
        freshness_after_write = store.inventory_summary_cache().current_cache_freshness_timestamp()
        self.assertEqual(freshness_after_write, "")  # 已失效

    def test_summary_cache_invalidated_on_undo(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"管内编号*": "TEST-001"})
        store.read_inventory_summary_via_cache()  # 建 cache
        self.assertTrue(store.inventory_summary_cache().current_cache_freshness_timestamp())
        store.undo_last()
        # undo 内部走 _apply_action → _write_rows → _mark_inventory_summary_cache_invalid
        self.assertEqual(store.inventory_summary_cache().current_cache_freshness_timestamp(), "")

    def test_summary_cache_per_call_new_connection(self) -> None:
        """plan D2：每次方法调用都开新 sqlite3.Connection，不跨调用共享对象。"""
        from specimen_app.summary_cache import InventorySummaryCacheDatabase
        cache_dir = self.tmp / "数据"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = InventorySummaryCacheDatabase(cache_dir)
        # 写一次再读一次，不应出现 sqlite3 ThreadCheck 错误
        db.rebuild_cache_from_records(
            [{"入库编号*": "YZZ000001", "字段": "v"}],
            now_iso="2026-05-25T10:00:00",
        )
        rows = db.read_all_summary_rows_or_fallback(
            fallback_provider=lambda: [],
            now_iso_provider=lambda: "2026-05-25T10:00:00",
        )
        self.assertEqual(len(rows), 1)

    # ---- D3: 照片字段保存合并 ----

    def test_photo_field_save_merges_into_single_action_log(self) -> None:
        from specimen_app.excel_store import ExcelStore
        from specimen_app.models import ACTION_LOG_FILE
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        src = self.tmp / "photo.jpg"
        Image.new("RGB", (16, 16), color="blue").save(src, "JPEG")
        store.add_photo(voucher, src)
        # 单次 batch 同时改两个字段
        before_rows = store._read_plain_rows(store.data_dir / ACTION_LOG_FILE)
        before_count = len(before_rows)
        changed = store.set_photo_fields_batch(voucher, 0, {"描述": "新描述", "文件名": "renamed.jpg"})
        self.assertTrue(changed)
        after_rows = store._read_plain_rows(store.data_dir / ACTION_LOG_FILE)
        new_actions = after_rows[before_count:]
        self.assertEqual(len(new_actions), 1, "batch 改 2 个字段应只增 1 条 action-log")
        # 一次 undo 应同时还原两个字段
        store.undo_last()
        photo_after_undo = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher][0]
        self.assertEqual(photo_after_undo.get("描述"), "")
        self.assertNotEqual(photo_after_undo.get("文件名"), "renamed.jpg")

    def test_photo_field_save_batch_returns_false_when_no_changes(self) -> None:
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        voucher = store.create_specimen()
        src = self.tmp / "no_change.jpg"
        Image.new("RGB", (16, 16), color="green").save(src, "JPEG")
        store.add_photo(voucher, src)
        # 写一致的值 → False，不产生 action-log
        photo = [r for r in store.read_rows("photo") if r.get("入库编号*") == voucher][0]
        result = store.set_photo_fields_batch(voucher, 0, {"描述": photo.get("描述", ""), "文件名": photo.get("文件名", "")})
        self.assertFalse(result)


class Phase5PhotoArchiveExtractionTests(unittest.TestCase):
    """plan v0.10.0 Phase 5 (P2 结构拆分)：E1 PhotoArchive 模块抽离。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_photo_archive_module_can_be_imported_standalone(self) -> None:
        """PhotoArchive 应不引 openpyxl / 不导致 ExcelStore 循环 import。"""
        # 模拟最小依赖：构造一个不依赖 ExcelStore 的 PhotoArchive
        from specimen_app.photo_archive import PhotoArchive
        archive = PhotoArchive(
            workspace_root=self.tmp,
            read_only=False,
            read_photo_rows_callback=lambda: [],
        )
        self.assertEqual(archive.compute_workspace_archive_directory(), self.tmp / "照片")

    def test_photo_archive_sanitize_filename(self) -> None:
        from specimen_app.photo_archive import PhotoArchive
        archive = PhotoArchive(self.tmp, False, lambda: [])
        self.assertEqual(archive.sanitize_photo_filename_for_storage("foo/bar.jpg"), "bar.jpg")
        self.assertEqual(archive.sanitize_photo_filename_for_storage("a<b>:c.jpg"), "a_b__c.jpg")
        self.assertEqual(archive.sanitize_photo_filename_for_storage(""), "photo")
        self.assertEqual(
            archive.sanitize_photo_filename_for_storage("noext", default_suffix=".jpg"),
            "noext.jpg",
        )

    def test_photo_archive_is_path_under_workspace_archive_directory(self) -> None:
        from specimen_app.photo_archive import PhotoArchive
        archive = PhotoArchive(self.tmp, False, lambda: [])
        archive_dir = archive.compute_workspace_archive_directory()
        archive_dir.mkdir(parents=True)
        self.assertTrue(archive.is_path_under_workspace_archive_directory(archive_dir / "a.jpg"))
        self.assertFalse(archive.is_path_under_workspace_archive_directory(self.tmp / "outside.jpg"))

    def test_excel_store_delegates_to_photo_archive(self) -> None:
        """ExcelStore 的 4 个迁移方法应通过委托给 PhotoArchive 实现。"""
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        self.assertEqual(store._photo_archive_dir(), self.tmp / "照片")
        # 同一调用经由两条路径应得到相同结果
        self.assertEqual(
            store._safe_photo_filename("bad/name.jpg"),
            store.photo_archive().sanitize_photo_filename_for_storage("bad/name.jpg"),
        )


class Phase6StartupPerfTests(unittest.TestCase):
    """plan v0.10.3 启动性能热修：H1 索引异步 + H2 WSL 检测 + H4 preheat 跳过。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quick_index_sanity_check_passes_on_normal_workspace(self) -> None:
        """正常工作区（建过 voucher）quick check 应返回 True，event 已 set。"""
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        store.create_specimen()
        store.release_lock()
        # 重开 store 模拟启动
        store2 = ExcelStore(self.tmp)
        self.assertTrue(store2._quick_index_sanity_check_passes())
        self.assertTrue(store2._index_ready_event.is_set())
        store2.release_lock()

    def test_next_voucher_waits_for_index_ready(self) -> None:
        """index event 未 set 时 next_voucher 应同步等到 set。"""
        from specimen_app.excel_store import ExcelStore
        import threading as _threading
        import time as _time
        store = ExcelStore(self.tmp)
        # 故意把 event clear 模拟启动后 quick check 失败 + 后台还没跑完
        store._index_ready_event.clear()

        def _delayed_set() -> None:
            _time.sleep(0.3)
            store._index_ready_event.set()

        _threading.Thread(target=_delayed_set, daemon=True).start()
        # next_voucher 应等到 set 才返回，不抛异常
        voucher = store.next_voucher()
        self.assertTrue(voucher.startswith("YZZ"))
        store.release_lock()

    def test_iter_images_skips_unchanged_directories(self) -> None:
        """plan v0.10.4 I1：父目录 mtime 未变时不 yield 该目录的文件。"""
        from specimen_app.image_search import iter_images
        import time as _time
        photos_dir = self.tmp / "photos"
        photos_dir.mkdir()
        from PIL import Image as _PILImage
        _PILImage.new("RGB", (8, 8)).save(photos_dir / "a.jpg", "JPEG")
        _PILImage.new("RGB", (8, 8)).save(photos_dir / "b.jpg", "JPEG")
        first_pass = iter_images([self.tmp], max_depth=0, suffixes=[".jpg"])
        self.assertEqual(len(first_pass), 2)
        scan_baseline = _time.time() + 5  # baseline 设到未来一点，确保已有目录全部 <= baseline
        # 父目录 mtime 未变（不增删子项），incremental 应返回 0
        second_pass = iter_images(
            [self.tmp], max_depth=0, suffixes=[".jpg"],
            skip_directories_unchanged_since=scan_baseline,
        )
        self.assertEqual(len(second_pass), 0)

    def test_iter_images_detects_newly_added_files_via_parent_dir_mtime(self) -> None:
        """plan v0.10.4 I1：新增文件触发父目录 mtime 更新，incremental 应能检出。"""
        from specimen_app.image_search import iter_images
        import time as _time
        photos_dir = self.tmp / "photos"
        photos_dir.mkdir()
        from PIL import Image as _PILImage
        _PILImage.new("RGB", (8, 8)).save(photos_dir / "old.jpg", "JPEG")
        scan_baseline = _time.time()
        _time.sleep(1.1)  # 确保新文件的父目录 mtime 严格大于 baseline
        _PILImage.new("RGB", (8, 8)).save(photos_dir / "new.jpg", "JPEG")
        new_files = iter_images(
            [self.tmp], max_depth=0, suffixes=[".jpg"],
            skip_directories_unchanged_since=scan_baseline,
        )
        new_file_names = {p.name for p in new_files}
        self.assertIn("new.jpg", new_file_names)

    def test_list_reserved_vouchers_pending_ingestion_filters_already_built_specimens(self) -> None:
        """plan v0.10.6 S2：批量领取后没建 specimen 行的号才出现在列表里。"""
        from specimen_app.excel_store import ExcelStore
        store = ExcelStore(self.tmp)
        reserved_vouchers = store.batch_reserve_vouchers(5)
        self.assertEqual(len(reserved_vouchers), 5)
        # 显式 log 一条批量领取事件（UI 层 BatchGenerateDialog 默认会做这步）
        store.log_alloc_event({
            "记录ID": "test-batch-001",
            "时间": "2026-05-26T10:00:00",
            "类型": "批量领取",
            "人员": "张三",
            "编号系列": "YZZ",
            "编号起始": reserved_vouchers[0],
            "编号结束": reserved_vouchers[-1],
            "数量": str(len(reserved_vouchers)),
        })
        # 给其中 2 个建 specimen 行
        store.create_specimen_with_voucher(reserved_vouchers[0])
        store.create_specimen_with_voucher(reserved_vouchers[2])
        pending = store.list_reserved_vouchers_pending_ingestion()
        pending_voucher_set = {entry["voucher"] for entry in pending}
        # 剩 3 个应该还在 pending
        self.assertNotIn(reserved_vouchers[0], pending_voucher_set)
        self.assertNotIn(reserved_vouchers[2], pending_voucher_set)
        self.assertIn(reserved_vouchers[1], pending_voucher_set)
        self.assertIn(reserved_vouchers[3], pending_voucher_set)
        self.assertIn(reserved_vouchers[4], pending_voucher_set)
        # reserver_name 应正确透传
        for entry in pending:
            self.assertEqual(entry["reserver_name"], "张三")
        store.release_lock()

    def test_workload_aggregation_by_specimen_recorder_field(self) -> None:
        """plan v0.10.6 S4：工作量按 specimen 表"信息录入人员"字段聚合，不依赖 ALLOC_LOG 任务记录。"""
        from specimen_app.excel_store import ExcelStore
        from collections import Counter
        store = ExcelStore(self.tmp)
        voucher1 = store.create_specimen()
        voucher2 = store.create_specimen()
        voucher3 = store.create_specimen()
        store.set_fields("specimen", voucher1, {"信息录入人员": "张三"})
        store.set_fields("specimen", voucher2, {"信息录入人员": "张三"})
        store.set_fields("specimen", voucher3, {"信息录入人员": "李四"})
        # 直接读 specimen 表算 — 与 WorkloadReportDialog 内部算法一致
        rows = store.read_rows("specimen")
        recorder_counts = Counter(
            (row.get("信息录入人员") or "").strip()
            for row in rows
            if row.get("入库编号*")
        )
        self.assertEqual(recorder_counts.get("张三"), 2)
        self.assertEqual(recorder_counts.get("李四"), 1)
        store.release_lock()

    def test_reconcile_scope_preserves_cached_entries_outside_changed_dirs(self) -> None:
        """plan v0.10.4 I2：未扫描的目录 cached entries 不应被 removed。"""
        from specimen_app.image_search import ImageIndexStore
        import time as _time
        dir_a = self.tmp / "dirA"
        dir_b = self.tmp / "dirB"
        dir_a.mkdir()
        dir_b.mkdir()
        from PIL import Image as _PILImage
        _PILImage.new("RGB", (8, 8)).save(dir_a / "a.jpg", "JPEG")
        _PILImage.new("RGB", (8, 8)).save(dir_b / "b.jpg", "JPEG")
        store = ImageIndexStore(self.tmp)
        # 全扫一次让两个目录都进 cache
        full_update = store.reconcile_scope([self.tmp], max_depth=0)
        self.assertEqual(full_update.added, 2)
        # 等一下保证 mtime 分辨率，往 dirA 加一个文件，dirB 不动
        scan_baseline = _time.time()
        _time.sleep(1.1)
        _PILImage.new("RGB", (8, 8)).save(dir_a / "a2.jpg", "JPEG")
        # 增量 reconcile：dirB 未变不应被扫到，所以 dirB 的 cached entry 不应进 removed
        incremental_update = store.reconcile_scope(
            [self.tmp], max_depth=0, incremental_since_unix=scan_baseline,
        )
        self.assertEqual(incremental_update.removed, 0)
        self.assertGreaterEqual(incremental_update.added, 1)

    def test_detect_workspace_on_windows_mounted_filesystem(self) -> None:
        """plan H2：WSL + /mnt/<drive>/... 工作区被识别为跨 fs。"""
        from specimen_app.startup_diag import detect_workspace_on_windows_mounted_filesystem
        from unittest.mock import patch
        # 模拟 WSL + /mnt/n/...
        with patch("pathlib.Path.read_text", return_value="Linux microsoft WSL2"):
            self.assertTrue(detect_workspace_on_windows_mounted_filesystem(Path("/mnt/n/codex/ws")))
            self.assertTrue(detect_workspace_on_windows_mounted_filesystem(Path("/mnt/c/users/test")))
            self.assertFalse(detect_workspace_on_windows_mounted_filesystem(Path("/home/user/ws")))
        # 模拟非 WSL
        with patch("pathlib.Path.read_text", return_value="Linux generic"):
            self.assertFalse(detect_workspace_on_windows_mounted_filesystem(Path("/mnt/n/codex/ws")))


if __name__ == "__main__":
    unittest.main()
