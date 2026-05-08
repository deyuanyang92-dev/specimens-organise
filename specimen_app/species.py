from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook


@dataclass(frozen=True)
class SpeciesMatch:
    chinese_name: str
    latin_name: str
    family_name: str
    family_latin: str


class SpeciesMatcher:
    """Live matcher backed by the preset Excel file.

    The file is reloaded when its mtime changes so users can update the preset
    workbook while the application is running.
    """

    def __init__(self, preset_path: Path):
        self.preset_path = preset_path
        self._mtime: float | None = None
        self._rows: list[SpeciesMatch] = []

    def matches(self, query: str, limit: int = 20) -> list[SpeciesMatch]:
        self._reload_if_needed()
        text = query.strip().lower()
        if not text:
            return []

        def rank(item: SpeciesMatch) -> tuple[int, int, str]:
            name = item.chinese_name.lower()
            if name == text:
                return (0, len(name), name)
            if name.startswith(text):
                return (1, len(name), name)
            if text in name:
                return (2, len(name), name)
            latin = item.latin_name.lower()
            if latin.startswith(text):
                return (3, len(latin), name)
            if text in latin:
                return (4, len(latin), name)
            return (99, len(name), name)

        matched = [row for row in self._rows if rank(row)[0] < 99]
        return sorted(matched, key=rank)[:limit]

    def all_rows(self) -> Iterable[SpeciesMatch]:
        self._reload_if_needed()
        return list(self._rows)

    def _reload_if_needed(self) -> None:
        if not self.preset_path.exists():
            self._rows = []
            self._mtime = None
            return
        mtime = self.preset_path.stat().st_mtime
        if self._mtime == mtime:
            return
        self._mtime = mtime
        self._rows = self._load_rows()

    def _load_rows(self) -> list[SpeciesMatch]:
        wb = load_workbook(self.preset_path, read_only=True, data_only=True)
        try:
            ws = wb.active
            header = [str(cell.value or "").strip() for cell in next(ws.iter_rows(max_row=1))]
            mapping = {name: idx for idx, name in enumerate(header)}
            required = ["物种中文名", "物种拉丁名", "科中文名", "科拉丁名"]
            if any(name not in mapping for name in required):
                return []
            rows: list[SpeciesMatch] = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                chinese = _clean(row[mapping["物种中文名"]])
                if not chinese:
                    continue
                rows.append(
                    SpeciesMatch(
                        chinese_name=chinese,
                        latin_name=_clean(row[mapping["物种拉丁名"]]),
                        family_name=_clean(row[mapping["科中文名"]]),
                        family_latin=_clean(row[mapping["科拉丁名"]]),
                    )
                )
            return rows
        finally:
            wb.close()


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
