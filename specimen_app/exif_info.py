"""相机 EXIF 元数据抽取（A2）。

野外标本拍照的 EXIF 含 GPS 坐标 / 拍摄时间 / 设备型号等关键采集元数据 — 对应 DwC
中的 eventDate / decimalLatitude / decimalLongitude。本模块**只读地**从 JPEG/TIFF
照片中抽取这些信息，UI 在用户主动触发时把空字段回填到 specimen 表（不强制覆盖
已有值，避免数据丢失）。

Pillow 不是核心依赖（image_cache.py 已 lazy import），但 PIL.Image 在本项目通过
PyInstaller 已绑入。本模块同样 lazy import + 异常 graceful（无 PIL / 无 EXIF /
照片损坏都返回空 dict 不抛异常）。

DwC 字段映射：
- DateTimeOriginal (0x9003) → eventDate（采集日期）
- GPSInfo (0x8825) → decimalLatitude / decimalLongitude + coordinateUncertaintyInMeters
- Make + Model (0x010F + 0x0110) → georeferenceProtocol / recordedByID 的辅助信息

注：DwC 的 eventDate 是"采集事件"日期；EXIF 的 DateTimeOriginal 是"拍照"日期。
绝大多数野外场景两者一致；极少数（实验室补拍）会差几天 — 由用户判断是否回填。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


# EXIF 标准 tag ID（来自 EXIF 2.32 规范）
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_GPS_INFO = 0x8825
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110

# GPS sub-IFD tags
_GPS_LATITUDE_REF = 1
_GPS_LATITUDE = 2
_GPS_LONGITUDE_REF = 3
_GPS_LONGITUDE = 4
_GPS_ALTITUDE_REF = 5
_GPS_ALTITUDE = 6


def extract_exif(image_path: Path | str) -> dict[str, Any]:
    """从照片抽取 DwC-相关 EXIF 元数据。任何失败返回空 dict（不抛异常）。

    返回字段：
    - event_date: str  ISO 日期 "YYYY-MM-DD"（来自 EXIF DateTimeOriginal）
    - event_time: str  ISO 时间 "HH:MM:SS"
    - latitude:   float  十进制度（南纬 / 西经为负）
    - longitude:  float
    - altitude:   float  米（海平面为 0；下方为负）
    - camera_make: str
    - camera_model: str

    缺失任一字段则该键不在返回值里（调用方用 `.get()` 取）。
    """
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return {}
    try:
        from PIL import Image  # lazy import，避免 import 时强依赖
    except ImportError:
        return {}
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return {}
            result: dict[str, Any] = {}

            # 拍摄时间
            dto = exif.get(_TAG_DATETIME_ORIGINAL)
            if dto:
                # EXIF 标准格式："YYYY:MM:DD HH:MM:SS"
                try:
                    dt = datetime.strptime(str(dto).strip(), "%Y:%m:%d %H:%M:%S")
                    result["event_date"] = dt.date().isoformat()
                    result["event_time"] = dt.time().isoformat(timespec="seconds")
                except (ValueError, TypeError):
                    pass

            # 设备信息
            # 规范化软件设计 2026-05 P1 审查修复:EXIF 字段可能是 bytes (旧相机/编码异常),
            # str(b'xxx') 会返字面量 "b'xxx'" 而非解码内容。先 isinstance 判断 + decode。
            def _safe_str(v) -> str:
                if isinstance(v, bytes):
                    return v.decode("utf-8", errors="ignore").strip().rstrip("\x00")
                return str(v).strip().rstrip("\x00")

            make = exif.get(_TAG_MAKE)
            if make:
                result["camera_make"] = _safe_str(make)
            model = exif.get(_TAG_MODEL)
            if model:
                result["camera_model"] = _safe_str(model)

            # GPS sub-IFD
            gps_info = exif.get_ifd(_TAG_GPS_INFO)
            if gps_info:
                try:
                    lat = _parse_gps_coord(
                        gps_info.get(_GPS_LATITUDE),
                        gps_info.get(_GPS_LATITUDE_REF),
                    )
                    lon = _parse_gps_coord(
                        gps_info.get(_GPS_LONGITUDE),
                        gps_info.get(_GPS_LONGITUDE_REF),
                    )
                    if lat is not None:
                        result["latitude"] = lat
                    if lon is not None:
                        result["longitude"] = lon
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
                try:
                    alt = gps_info.get(_GPS_ALTITUDE)
                    if alt is not None:
                        alt_value = float(alt)
                        # 海平面下用 ref=1 标记（DwC 约定海面下为负）
                        if gps_info.get(_GPS_ALTITUDE_REF) == 1:
                            alt_value = -alt_value
                        result["altitude"] = alt_value
                except (TypeError, ValueError):
                    pass

            return result
    except Exception:  # noqa: BLE001 - 任何 PIL/IO 异常都视为"无 EXIF"
        return {}


def _parse_gps_coord(coord: Any, ref: Any) -> float | None:
    """EXIF GPS 坐标 = (degrees, minutes, seconds) 三元组 + N/S/E/W 半球符号 →
    十进制度。无效返回 None。
    """
    if not coord:
        return None
    try:
        d, m, s = coord
        deg = float(d) + float(m) / 60.0 + float(s) / 3600.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", errors="ignore")
    ref_str = str(ref or "").strip().upper()
    if ref_str in ("S", "W"):
        deg = -deg
    return deg


def apply_exif_to_specimen(
    store,  # ExcelStore 实例 — 避免循环 import
    voucher: str,
    photo_path: Path | str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """从指定照片读 EXIF，回填到 voucher 的 specimen 行（仅空字段，除非 overwrite=True）。

    返回回填摘要：{"event_date": "...", "filled_fields": [...], "skipped_fields": [...]}。
    EXIF 抽不到任何信息 → 返回 `{"filled_fields": [], "skipped_fields": []}`，不调 set_fields。

    设计：默认只填空字段。用户已手填的值绝不被 EXIF 覆盖（除非显式 overwrite=True）。
    """
    exif = extract_exif(photo_path)
    if not exif:
        return {"filled_fields": [], "skipped_fields": []}
    specimen = store.get_specimen(voucher) or {}
    updates: dict[str, str] = {}
    filled: list[str] = []
    skipped: list[str] = []

    event_date = exif.get("event_date")
    if event_date:
        current = str(specimen.get("采集日期", "") or "").strip()
        if not current or overwrite:
            updates["采集日期"] = event_date
            filled.append("采集日期")
        else:
            skipped.append("采集日期")

    # GPS 没有专门字段对应（待 A3 字段补全），暂用"备注"追加。仅在用户许可时（overwrite）
    # 才动备注，避免覆盖已有重要内容。
    if overwrite and ("latitude" in exif or "longitude" in exif):
        gps_str = (
            f"GPS: {exif.get('latitude', '?'):.6f}, {exif.get('longitude', '?'):.6f}"
            if isinstance(exif.get("latitude"), float) and isinstance(exif.get("longitude"), float)
            else ""
        )
        if gps_str:
            current_note = str(specimen.get("备注", "") or "").strip()
            updates["备注"] = (current_note + "\n" + gps_str).strip() if current_note else gps_str
            filled.append("备注(GPS)")

    if updates:
        store.set_fields("specimen", voucher, updates)

    result = {"filled_fields": filled, "skipped_fields": skipped}
    if event_date:
        result["event_date"] = event_date
    if "latitude" in exif:
        result["latitude"] = exif["latitude"]
    if "longitude" in exif:
        result["longitude"] = exif["longitude"]
    if "camera_make" in exif:
        result["camera_make"] = exif["camera_make"]
    if "camera_model" in exif:
        result["camera_model"] = exif["camera_model"]
    return result
