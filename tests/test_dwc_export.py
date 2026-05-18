"""Darwin Core Archive 导出测试（A1）。

覆盖：
- 空工作区导出仍是合法 archive（occurrence.txt 仅含表头）
- 单 voucher 导出：字段映射正确（catalogNumber / fieldNumber / scientificName 等）
- 中文字段 UTF-8 输出
- 照片 → multimedia.txt：coreid 关联正确、accessURI 是 file:// URI、fileFormat MIME
- meta.xml 结构正确：core 指向 occurrence.txt、extension 指向 multimedia.txt
- eml.xml 含必要 dataset 元数据
- 重复导出到已存在目标 → FileExistsError
- 拒绝静默覆盖
"""

from __future__ import annotations

import csv
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from specimen_app.dwc_export import (
    DWC_TERM_BASE,
    AC_TERM_BASE,
    export_dwc_archive,
)
from specimen_app.excel_store import ExcelStore


class DwcExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_zip_text(self, zf: zipfile.ZipFile, name: str) -> str:
        return zf.read(name).decode("utf-8")

    def _read_zip_tsv(self, zf: zipfile.ZipFile, name: str) -> list[list[str]]:
        text = self._read_zip_text(zf, name)
        return list(csv.reader(io.StringIO(text), delimiter="\t"))

    # ─────────────────────────── 基本结构 ───────────────────────────

    def test_empty_workspace_still_produces_valid_archive(self) -> None:
        store = ExcelStore(self.workspace)
        dest = self.tmp / "out.zip"
        result = export_dwc_archive(store, dest)
        self.assertEqual(result, dest.resolve())

        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            self.assertIn("occurrence.txt", names)
            self.assertIn("multimedia.txt", names)
            self.assertIn("meta.xml", names)
            self.assertIn("eml.xml", names)
            # occurrence 仅表头
            rows = self._read_zip_tsv(zf, "occurrence.txt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "occurrenceID")

    def test_dest_existing_raises(self) -> None:
        store = ExcelStore(self.workspace)
        dest = self.tmp / "out.zip"
        dest.write_bytes(b"placeholder")
        with self.assertRaises(FileExistsError):
            export_dwc_archive(store, dest)

    # ─────────────────────────── 字段映射 ───────────────────────────

    def test_specimen_maps_to_dwc_terms(self) -> None:
        store = ExcelStore(self.workspace)
        voucher = store.create_specimen()
        # 先设管内编号（会触发 derive_specimen_fields_from_tube_number 自动推导
        # 保存方式 / 采集日期 / 采集地点）
        store.set_fields("specimen", voucher, {"管内编号*": "QD-LSD-SC001-1-R-250923"})
        # 再覆盖保存方式 / 采集日期 / 录入员等字段为测试期望值（不能合并到上面一次调用，
        # 否则会被自动推导覆盖）
        store.set_fields(
            "specimen",
            voucher,
            {
                "保存方式": "9E",
                "采集日期": "2025-09-23",
                "采集地点缩写*": "QD",
                "信息录入人员": "张三",
                "核对人员": "李四",
                "备注": "test note",
            },
        )
        store.set_fields(
            "classification",
            voucher,
            {
                "种名*": "珠江川纽虫",
                "种拉丁": "Amniclineus zhujiangensis",
                "属名": "Amniclineus",
                "科*": "纵沟纽虫科",
                "科拉丁": "Lineidae",
                "目": "Heteronemertea",
                "纲": "Pilidiophora",
                "门": "Nemertea",
            },
        )

        dest = self.tmp / "out.zip"
        export_dwc_archive(store, dest)

        with zipfile.ZipFile(dest) as zf:
            rows = self._read_zip_tsv(zf, "occurrence.txt")
        self.assertEqual(len(rows), 2)  # header + 1 record
        header = rows[0]
        record = dict(zip(header, rows[1]))

        # 主键
        self.assertEqual(record["occurrenceID"], voucher)
        self.assertEqual(record["catalogNumber"], voucher)
        # 标本字段
        self.assertEqual(record["fieldNumber"], "QD-LSD-SC001-1-R-250923")
        self.assertEqual(record["preparations"], "9E")
        self.assertEqual(record["eventDate"], "2025-09-23")
        self.assertEqual(record["verbatimLocality"], "QD")
        self.assertEqual(record["recordedBy"], "张三")
        self.assertEqual(record["identifiedBy"], "李四")
        self.assertEqual(record["occurrenceRemarks"], "test note")
        # 分类字段
        self.assertEqual(record["scientificName"], "Amniclineus zhujiangensis")
        self.assertEqual(record["vernacularName"], "珠江川纽虫")
        self.assertEqual(record["genus"], "Amniclineus")
        self.assertEqual(record["family"], "Lineidae")
        self.assertEqual(record["order"], "Heteronemertea")
        self.assertEqual(record["class"], "Pilidiophora")
        self.assertEqual(record["phylum"], "Nemertea")

    def test_chinese_utf8_roundtrip(self) -> None:
        store = ExcelStore(self.workspace)
        voucher = store.create_specimen()
        store.set_fields("specimen", voucher, {"采集地点缩写*": "青岛", "信息录入人员": "张三"})

        dest = self.tmp / "out.zip"
        export_dwc_archive(store, dest)

        with zipfile.ZipFile(dest) as zf:
            raw_bytes = zf.read("occurrence.txt")
        text = raw_bytes.decode("utf-8")
        self.assertIn("青岛", text)
        self.assertIn("张三", text)

    # ─────────────────────────── multimedia ───────────────────────────

    def test_photo_maps_to_multimedia(self) -> None:
        store = ExcelStore(self.workspace)
        voucher = store.create_specimen()
        photo_src = self.tmp / "src.jpg"
        photo_src.write_bytes(b"fake-jpg-bytes-for-test")
        store.add_photo(voucher, photo_src, allow_outside=True)

        dest = self.tmp / "out.zip"
        export_dwc_archive(store, dest)

        with zipfile.ZipFile(dest) as zf:
            rows = self._read_zip_tsv(zf, "multimedia.txt")
        self.assertEqual(len(rows), 2)
        header = rows[0]
        rec = dict(zip(header, rows[1]))
        self.assertEqual(rec["coreid"], voucher)
        self.assertEqual(rec["identifier"], "src.jpg")
        self.assertTrue(rec["accessURI"].startswith("file://"))
        self.assertEqual(rec["fileFormat"], "image/jpeg")
        self.assertEqual(rec["hashFunction"], "SHA-256")
        self.assertTrue(len(rec["hashValue"]) == 64)  # SHA-256 hex digest

    # ─────────────────────────── meta.xml ───────────────────────────

    def test_meta_xml_structure(self) -> None:
        store = ExcelStore(self.workspace)
        store.create_specimen()
        dest = self.tmp / "out.zip"
        export_dwc_archive(store, dest)

        with zipfile.ZipFile(dest) as zf:
            meta = zf.read("meta.xml").decode("utf-8")

        # 关键结构断言（不解 XML namespace，纯字符串）
        self.assertIn("<core ", meta)
        self.assertIn("<extension ", meta)
        self.assertIn("occurrence.txt", meta)
        self.assertIn("multimedia.txt", meta)
        self.assertIn(DWC_TERM_BASE + "Occurrence", meta)
        self.assertIn(AC_TERM_BASE + "Multimedia", meta)
        self.assertIn(DWC_TERM_BASE + "catalogNumber", meta)
        self.assertIn(DWC_TERM_BASE + "scientificName", meta)
        # 解析 XML 合法
        try:
            ET.fromstring(meta)
        except ET.ParseError as exc:
            self.fail(f"meta.xml XML 解析失败：{exc}")

    def test_eml_xml_contains_dataset_metadata(self) -> None:
        store = ExcelStore(self.workspace)
        store.create_specimen()
        dest = self.tmp / "out.zip"
        export_dwc_archive(store, dest, dataset_title="测试数据集", dataset_creator="张三")

        with zipfile.ZipFile(dest) as zf:
            eml = zf.read("eml.xml").decode("utf-8")
        self.assertIn("测试数据集", eml)
        self.assertIn("张三", eml)
        self.assertIn("<pubDate>", eml)
        try:
            ET.fromstring(eml)
        except ET.ParseError as exc:
            self.fail(f"eml.xml XML 解析失败：{exc}")


if __name__ == "__main__":
    unittest.main()
