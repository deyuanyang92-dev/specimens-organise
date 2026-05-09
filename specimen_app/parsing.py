from __future__ import annotations

import re
from datetime import date

VOUCHER_RE = re.compile(r"^YZZ(\d{6})$")
_DATE_SEG_RE = re.compile(r"^\d{8}$|^\d{6}$")
_PHOTO_SEQ_SEG_RE = re.compile(r"^\d{1,2}$")


def parse_voucher_serial(value: object) -> int | None:
    match = VOUCHER_RE.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1))


def format_voucher(serial: int) -> str:
    if serial < 1 or serial > 999999:
        raise ValueError("入库编号序号必须在 1 到 999999 之间")
    return f"YZZ{serial:06d}"


def extract_location_code(tube_number: str) -> str:
    # 原代码：这里单独 split("-") 取前两段；现在统一复用 _parse_tube_info，避免各函数规则不一致。
    parts = _parse_tube_info(tube_number)["core_parts"]
    return "-".join(parts[:2]) if len(parts) >= 2 else ""


def extract_bottle_label(tube_number: str) -> str:
    # 原代码：这里单独 split("-") 取前三段；现在统一复用 _parse_tube_info，兼容 "--" 和照片序号段。
    parts = _parse_tube_info(tube_number)["core_parts"]
    return "-".join(parts[:3]) if len(parts) >= 3 else "-".join(parts)


def extract_collection_date(tube_number: str) -> str:
    # 原代码：这里用正则扫描整串日期；现在由 _parse_tube_info 统一定位日期段。
    return str(_parse_tube_info(tube_number)["collection_date"])


def extract_photo_seq(tube_number: str) -> int:
    return int(_parse_tube_info(tube_number)["photo_seq"])


def derive_specimen_fields_from_tube_number(tube_number: str) -> dict[str, str]:
    """Derive specimen fields from a manually entered tube number."""
    # 原代码只在 ExcelStore.set_fields 中单独派生采集日期和采集地点缩写。
    # 现在统一在这里派生，恢复旧版本对保存方式的自动识别，并让照片文件名填充复用同一规则。
    updates: dict[str, str] = {}
    location = extract_location_code(tube_number)
    collection_date = extract_collection_date(tube_number)
    save_method = extract_save_method_from_tube_number(tube_number)
    if location:
        updates["采集地点缩写*"] = location
    if collection_date:
        updates["采集日期"] = collection_date
    if save_method:
        updates["保存方式"] = save_method
    return updates


def _parse_tube_info(tube_number: str) -> dict[str, object]:
    parts = [part.strip() for part in str(tube_number or "").split("-") if part.strip()]
    date_idx: int | None = None
    collection_date = ""
    for idx, segment in enumerate(parts):
        if not _DATE_SEG_RE.match(segment):
            continue
        parsed = _parse_date_digits(segment)
        if parsed:
            date_idx = idx
            collection_date = parsed.isoformat()
            break

    photo_seq = 1
    if date_idx is None:
        core_parts = parts
    else:
        core_end = date_idx
        if date_idx > 0 and _PHOTO_SEQ_SEG_RE.match(parts[date_idx - 1]):
            photo_seq = int(parts[date_idx - 1])
            core_end = date_idx - 1
        core_parts = parts[:core_end]

    return {
        "core_parts": core_parts,
        "collection_date": collection_date,
        "photo_seq": photo_seq,
    }


def _parse_date_digits(raw: str) -> date | None:
    try:
        if len(raw) == 8:
            year = int(raw[:4])
            month = int(raw[4:6])
            day = int(raw[6:8])
        elif len(raw) == 6:
            year = 2000 + int(raw[:2])
            month = int(raw[2:4])
            day = int(raw[4:6])
        else:
            return None
        return date(year, month, day)
    except ValueError:
        return None


# 原规则要求第二段地点码为 2-4 位字母：([A-Z]{2,4})。
# 现在兼容 GXRG-A-BZC001.tif 这类旧录入文件名，允许第二段为 1-4 位，并继续保留旧格式 QD-CK-SC008。
_TUBE_CORE_RE = re.compile(r"([A-Z]{2,4})[_-]([A-Z]{1,4})[_-]([A-Za-z]*\d{2,6})", re.IGNORECASE)
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(\d{8}|\d{6})(?!\d)")
_FILENAME_SEGMENT_RE = re.compile(r"[-_]+")
_SAVE_METHOD_ALIASES = {
    "9E": "9E",
    "7E": "7E",
    "79": "79",
    "RE": "RE",
    "FE": "FE",
    "R": "RE",
    "F": "FE",
}


def extract_tube_from_filename(filename: str) -> str:
    """Extract tube number pattern (e.g. QD-CK-SC008) from a photo filename."""
    match = _TUBE_CORE_RE.search(str(filename or ""))
    if match:
        return "-".join(match.groups())
    return ""


def extract_specimen_tube_from_filename(filename: str) -> str:
    """Extract the specimen tube string from a photo filename.

    This keeps the stable tube core plus any separated segments up to the first
    valid date, so a name like QD-CK-SC008-2-20240315-a.jpg becomes
    QD-CK-SC008-2-20240315.  When no date is present, it conservatively returns
    only the core identifier used by older versions.
    """
    match = _TUBE_CORE_RE.search(str(filename or ""))
    if not match:
        return ""
    tail_parts = _tail_segments_after_tube_core(filename, match)
    tube_parts = [part.upper() if index < 2 else part for index, part in enumerate(match.groups())]
    found_date = False
    for segment in tail_parts:
        tube_parts.append(segment)
        if _DATE_SEG_RE.match(segment) and _parse_date_digits(segment):
            found_date = True
            break
    if not found_date:
        return "-".join(tube_parts[:3])
    return "-".join(tube_parts)


def extract_save_method_from_filename(filename: str) -> str:
    """Extract a specimen preservation method code from filename segments."""
    # 原代码只给照片文件名使用；保留这个入口，内部改为复用管内编号保存方式解析，避免两套规则不一致。
    return extract_save_method_from_tube_number(filename)


def extract_save_method_from_tube_number(tube_number: str) -> str:
    """Extract a specimen preservation method code from tube-number tail segments."""
    text = str(tube_number or "")
    match = _TUBE_CORE_RE.search(text)
    if not match:
        return ""
    for segment in _tail_segments_after_tube_core(text, match):
        if _DATE_SEG_RE.match(segment) and _parse_date_digits(segment):
            # 原照片文件名逻辑遇到日期就停止；管内编号需要兼容保存方式在日期前后出现，所以这里只跳过日期段。
            continue
        value = _SAVE_METHOD_ALIASES.get(segment.upper())
        if value:
            return value
    return ""


def _tail_segments_after_tube_core(filename: str, match: re.Match[str]) -> list[str]:
    text = str(filename or "")
    stem = text.rsplit(".", 1)[0]
    tail = stem[match.end():]
    return [segment.strip() for segment in _FILENAME_SEGMENT_RE.split(tail) if segment.strip()]


def extract_photo_seq_from_filename(filename: str) -> int:
    text = str(filename or "")
    match = _TUBE_CORE_RE.search(text)
    if not match:
        return 1
    tail = re.sub(r"^[_-]+", "", text[match.end():])
    found = re.match(r"^(\d{1,2})(?:[_-]|$)", tail)
    if found and not re.match(r"^\d{6,8}", tail):
        return int(found.group(1))
    return 1


def extract_photo_date(filename: str) -> str:
    """Extract date (YYYYMMDD or YYMMDD) from a photo filename."""
    candidates = _FILENAME_DATE_RE.findall(str(filename or ""))
    for raw in reversed(candidates):
        parsed = _parse_date_digits(raw)
        if parsed:
            return parsed.isoformat()
    return ""
