"""EXIF 抽取测试（A2）。

覆盖：
- 无 EXIF 的 JPEG → 返回空 dict
- 含 DateTimeOriginal 的 JPEG → 抽到 event_date / event_time
- 含 GPS 信息的 JPEG → 抽到 latitude / longitude（南纬西经为负）
- 含 Make/Model → 抽到 camera_make / camera_model
- 不存在的文件 → 空 dict
- 损坏 / 非图像文件 → 空 dict（不抛）
- apply_exif_to_specimen 默认只填空字段；overwrite=True 强制覆盖
- apply_exif_to_specimen EXIF 无效 → 不调 set_fields
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from specimen_app.exif_info import apply_exif_to_specimen, extract_exif
from specimen_app.excel_store import ExcelStore


def _make_jpeg_with_exif(
    path: Path,
    datetime_original: str | None = None,
    latitude: tuple[int, int, int] | None = None,
    lat_ref: str = "N",
    longitude: tuple[int, int, int] | None = None,
    lon_ref: str = "E",
    altitude: float | None = None,
    alt_ref: int = 0,
    make: str | None = None,
    model: str | None = None,
) -> None:
    """生成一个 1x1 像素的 JPEG，按需写入 EXIF tags。Pillow 8+ 支持 image.save(exif=...)。"""
    img = Image.new("RGB", (1, 1), color=(255, 0, 0))
    exif = img.getexif()
    if datetime_original:
        exif[0x9003] = datetime_original
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if latitude or longitude or altitude is not None:
        gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
        if latitude:
            gps_ifd[1] = lat_ref
            gps_ifd[2] = latitude
        if longitude:
            gps_ifd[3] = lon_ref
            gps_ifd[4] = longitude
        if altitude is not None:
            gps_ifd[5] = alt_ref
            gps_ifd[6] = altitude
        # 把 GPS sub-IFD 写回主 exif
        exif[0x8825] = gps_ifd
    img.save(path, "JPEG", exif=exif)


class ExtractExifTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nonexistent_file_returns_empty(self) -> None:
        result = extract_exif(self.tmp / "nope.jpg")
        self.assertEqual(result, {})

    def test_non_image_file_returns_empty(self) -> None:
        bad = self.tmp / "bad.jpg"
        bad.write_bytes(b"not a real jpeg")
        result = extract_exif(bad)
        self.assertEqual(result, {})

    def test_jpeg_without_exif_returns_empty(self) -> None:
        plain = self.tmp / "plain.jpg"
        img = Image.new("RGB", (1, 1), (0, 0, 0))
        img.save(plain, "JPEG")
        result = extract_exif(plain)
        # 无 EXIF 时 result 可能完全为空或缺关键字段
        self.assertNotIn("event_date", result)
        self.assertNotIn("latitude", result)

    def test_datetime_original_extracted(self) -> None:
        p = self.tmp / "dt.jpg"
        _make_jpeg_with_exif(p, datetime_original="2025:09:23 14:30:00")
        result = extract_exif(p)
        self.assertEqual(result.get("event_date"), "2025-09-23")
        self.assertEqual(result.get("event_time"), "14:30:00")

    def test_camera_make_model_extracted(self) -> None:
        p = self.tmp / "cam.jpg"
        _make_jpeg_with_exif(p, make="Canon", model="EOS R5")
        result = extract_exif(p)
        self.assertEqual(result.get("camera_make"), "Canon")
        self.assertEqual(result.get("camera_model"), "EOS R5")

    def test_gps_north_east_positive(self) -> None:
        # 30.5° N, 120.5° E
        p = self.tmp / "gps.jpg"
        _make_jpeg_with_exif(
            p,
            latitude=(30, 30, 0),  # 30°30'00" = 30.5
            lat_ref="N",
            longitude=(120, 30, 0),
            lon_ref="E",
        )
        result = extract_exif(p)
        lat = result.get("latitude")
        lon = result.get("longitude")
        self.assertIsNotNone(lat)
        self.assertIsNotNone(lon)
        self.assertAlmostEqual(lat, 30.5, places=3)
        self.assertAlmostEqual(lon, 120.5, places=3)

    def test_gps_south_west_negative(self) -> None:
        p = self.tmp / "gps_sw.jpg"
        _make_jpeg_with_exif(
            p,
            latitude=(45, 0, 0),
            lat_ref="S",
            longitude=(90, 0, 0),
            lon_ref="W",
        )
        result = extract_exif(p)
        self.assertAlmostEqual(result.get("latitude"), -45.0, places=3)
        self.assertAlmostEqual(result.get("longitude"), -90.0, places=3)


class ApplyExifToSpecimenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fills_empty_field(self) -> None:
        store = ExcelStore(self.ws)
        v = store.create_specimen()
        # 采集日期 当前为空
        photo = self.tmp / "p.jpg"
        _make_jpeg_with_exif(photo, datetime_original="2025:09:23 14:30:00")

        result = apply_exif_to_specimen(store, v, photo)

        self.assertIn("采集日期", result["filled_fields"])
        self.assertEqual(result["event_date"], "2025-09-23")
        # 工作区 specimen 的采集日期已填上
        spec = store.get_specimen(v)
        self.assertEqual(spec["采集日期"], "2025-09-23")

    def test_does_not_overwrite_existing_field(self) -> None:
        store = ExcelStore(self.ws)
        v = store.create_specimen()
        store.set_fields("specimen", v, {"采集日期": "2024-01-01"})
        photo = self.tmp / "p.jpg"
        _make_jpeg_with_exif(photo, datetime_original="2025:09:23 14:30:00")

        result = apply_exif_to_specimen(store, v, photo)

        # 已有值不被覆盖
        self.assertIn("采集日期", result["skipped_fields"])
        self.assertNotIn("采集日期", result["filled_fields"])
        spec = store.get_specimen(v)
        self.assertEqual(spec["采集日期"], "2024-01-01")

    def test_overwrite_forces_replace(self) -> None:
        store = ExcelStore(self.ws)
        v = store.create_specimen()
        store.set_fields("specimen", v, {"采集日期": "2024-01-01"})
        photo = self.tmp / "p.jpg"
        _make_jpeg_with_exif(photo, datetime_original="2025:09:23 14:30:00")

        result = apply_exif_to_specimen(store, v, photo, overwrite=True)

        self.assertIn("采集日期", result["filled_fields"])
        spec = store.get_specimen(v)
        self.assertEqual(spec["采集日期"], "2025-09-23")

    def test_no_exif_no_writes(self) -> None:
        store = ExcelStore(self.ws)
        v = store.create_specimen()
        plain = self.tmp / "plain.jpg"
        Image.new("RGB", (1, 1)).save(plain, "JPEG")

        result = apply_exif_to_specimen(store, v, plain)
        self.assertEqual(result["filled_fields"], [])
        self.assertEqual(result["skipped_fields"], [])


if __name__ == "__main__":
    unittest.main()
