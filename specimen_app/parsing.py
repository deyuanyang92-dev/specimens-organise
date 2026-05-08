from __future__ import annotations

import re
from datetime import date

VOUCHER_RE = re.compile(r"^YZZ(\d{6})$")


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
    parts = [part.strip() for part in str(tube_number or "").split("-") if part.strip()]
    if len(parts) < 2:
        return ""
    return "-".join(parts[:2])


def extract_bottle_label(tube_number: str) -> str:
    parts = [part.strip() for part in str(tube_number or "").split("-") if part.strip()]
    if len(parts) >= 3:
        return "-".join(parts[:3])
    return str(tube_number or "").strip()


def extract_collection_date(tube_number: str) -> str:
    text = str(tube_number or "")
    candidates = re.findall(r"(?<!\d)(\d{8}|\d{6})(?!\d)", text)
    for raw in reversed(candidates):
        parsed = _parse_date_digits(raw)
        if parsed:
            return parsed.isoformat()
    return ""


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


# Photo filename patterns for auto-filling specimen info
_TUBE_PATTERN = re.compile(r"([A-Z]{2,4})[-_]([A-Z]{2,4})[-_]([A-Za-z]*\d{2,6})", re.IGNORECASE)
_PHOTO_DATE_RE = re.compile(r"(?<!\d)(\d{8}|\d{6})(?!\d)")


def extract_tube_from_filename(filename: str) -> str:
    """Extract tube number pattern (e.g. QD-CK-SC008) from a photo filename."""
    match = _TUBE_PATTERN.search(str(filename or ""))
    if match:
        return "-".join(match.groups())
    return ""


def extract_photo_date(filename: str) -> str:
    """Extract date (YYYYMMDD or YYMMDD) from a photo filename."""
    candidates = _PHOTO_DATE_RE.findall(str(filename or ""))
    for raw in reversed(candidates):
        parsed = _parse_date_digits(raw)
        if parsed:
            return parsed.isoformat()
    return ""
