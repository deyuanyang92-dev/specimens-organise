from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 规范化软件设计 2026-05 P1 优化:openpyxl 改函数内 lazy import,启动期不触发加载。


@dataclass(frozen=True)
class SpeciesMatch:
    chinese_name: str
    latin_name: str
    family_name: str
    family_latin: str

    @property
    def genus_name(self) -> str:
        return self.latin_name.split(" ", 1)[0].strip()


@dataclass(frozen=True)
class FamilyMatch:
    family_name: str
    family_latin: str


class SpeciesMatcher:
    """Live matcher backed by the preset Excel file.

    The file is reloaded when its mtime changes so users can update the preset
    workbook while the application is running.
    """

    # 旧（§7）：
    # def __init__(self, preset_path: Path):
    #     self.preset_path = preset_path
    #     self._mtime: float | None = None
    #     self._rows: list[SpeciesMatch] = []
    def __init__(self, preset_path: Path, extra_paths: Iterable[Path] = ()):
        """preset_path = 软件自带预设（只读，随升级替换）；extra_paths = 工作区预设（用户记忆，优先）。

        2026-10-02：手动输入的新物种保存后写入工作区 字段模版/表格信息预设字段.xlsx（remember_species），
        这里把它与自带库合并；同一中文名以工作区为准（可更正自带库）。
        """
        self.preset_path = preset_path
        self.extra_paths: list[Path] = [Path(x) for x in extra_paths]
        self._mtime: object = None
        self._rows: list[SpeciesMatch] = []

    def knows_species(self, chinese_name: str) -> bool:
        """中文种名是否已在（合并后的）预设里——决定保存时要不要记住。"""
        self._reload_if_needed()
        text = (chinese_name or "").strip().lower()
        return bool(text) and any(row.chinese_name.lower() == text for row in self._rows)

    def matches(self, query: str, limit: int = 20) -> list[SpeciesMatch]:
        return self.species_matches(query, limit=limit)

    def species_matches(self, query: str, limit: int = 50) -> list[SpeciesMatch]:
        self._reload_if_needed()
        text = query.strip().lower()
        if not text:
            return []

        rows = _dedupe_species(self._rows)
        matched = [row for row in rows if _rank_species(row, text)[0] < 99]
        return sorted(matched, key=lambda row: _rank_species(row, text))[:limit]

    def family_matches(self, query: str, limit: int = 50) -> list[FamilyMatch]:
        self._reload_if_needed()
        text = query.strip().lower()
        if not text:
            return []

        rows = _dedupe_families(self._rows)
        matched = [row for row in rows if _rank_family(row, text)[0] < 99]
        return sorted(matched, key=lambda row: _rank_family(row, text))[:limit]

    def resolve_unique_species(self, query: str) -> SpeciesMatch | None:
        self._reload_if_needed()
        text = query.strip().lower()
        if not text:
            return None
        rows = _dedupe_species(self._rows)

        exact = [row for row in rows if row.chinese_name.lower() == text or row.latin_name.lower() == text]
        if len(exact) == 1:
            return exact[0]

        prefix = [
            row for row in rows
            if row.chinese_name.lower().startswith(text) or row.latin_name.lower().startswith(text)
        ]
        if len(prefix) == 1:
            return prefix[0]
        return None

    def resolve_unique_family(self, query: str) -> FamilyMatch | None:
        self._reload_if_needed()
        text = query.strip().lower()
        if not text:
            return None
        rows = _dedupe_families(self._rows)

        exact = [row for row in rows if row.family_name.lower() == text or row.family_latin.lower() == text]
        if len(exact) == 1:
            return exact[0]

        prefix = [
            row for row in rows
            if row.family_name.lower().startswith(text) or row.family_latin.lower().startswith(text)
        ]
        if len(prefix) == 1:
            return prefix[0]
        return None

    def all_rows(self) -> Iterable[SpeciesMatch]:
        self._reload_if_needed()
        return list(self._rows)

    # 旧（§7）：只看自带预设一个文件
    # def _reload_if_needed(self) -> None:
    #     if not self.preset_path.exists():
    #         self._rows = []
    #         self._mtime = None
    #         return
    #     mtime = self.preset_path.stat().st_mtime
    #     if self._mtime == mtime:
    #         return
    #     self._mtime = mtime
    #     self._rows = self._load_rows()
    def _reload_if_needed(self) -> None:
        paths = [*self.extra_paths, self.preset_path]
        stamp = tuple(_mtime_or_none(path) for path in paths)
        if self._mtime == stamp:
            return
        self._mtime = stamp
        user_rows: list[SpeciesMatch] = []
        for path in self.extra_paths:
            if path.exists():
                user_rows.extend(_load_preset_rows(path))
        user_names = {row.chinese_name.lower() for row in user_rows}
        bundled = _load_preset_rows(self.preset_path) if self.preset_path.exists() else []
        self._rows = user_rows + [row for row in bundled if row.chinese_name.lower() not in user_names]

    def _load_rows(self) -> list[SpeciesMatch]:
        return _load_preset_rows(self.preset_path)


PRESET_HEADERS = ["物种中文名", "物种拉丁名", "科中文名", "科拉丁名"]


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def remember_species(preset_path: Path, chinese: str, latin: str, family: str, family_latin: str) -> bool:
    """把一个新物种追加到（工作区）预设表。已存在 / 中文名为空 / 文件损坏 → False，且不改动文件。

    原子写：tmp → replace。文件不存在就新建（含表头）。
    """
    chinese = _clean(chinese)
    if not chinese:
        return False
    from openpyxl import Workbook, load_workbook  # lazy

    preset_path = Path(preset_path)
    if preset_path.exists():
        try:
            wb = load_workbook(preset_path)
        except Exception:
            return False  # 损坏或被占用：宁可不记，也不覆盖用户文件
        ws = wb.active
        header = [_clean(cell.value) for cell in ws[1]]
        if any(name not in header for name in PRESET_HEADERS):
            wb.close()
            return False
        idx = {name: header.index(name) for name in PRESET_HEADERS}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if _clean(row[idx["物种中文名"]]).lower() == chinese.lower():
                wb.close()
                return False
        new_row = [None] * len(header)
        for name, value in zip(PRESET_HEADERS, (chinese, latin, family, family_latin)):
            new_row[idx[name]] = _clean(value) or None
        ws.append(new_row)
    else:
        preset_path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.append(PRESET_HEADERS)
        ws.append([_clean(chinese), _clean(latin) or None, _clean(family) or None, _clean(family_latin) or None])
    import os
    import threading

    tmp = preset_path.with_name(f"{preset_path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        wb.save(tmp)
        tmp.replace(preset_path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    finally:
        wb.close()
    return True


def _load_preset_rows(preset_path: Path) -> list[SpeciesMatch]:
    """读一本预设表（四列表头）。损坏 / 缺列 → []。（原 SpeciesMatcher._load_rows 主体，搬出以便多文件合并）"""
    # 旧：无异常保护，openpyxl 读取错误会向上抛，导致 _finish_initial_load 崩溃。
    # 现：文件损坏 / 读取异常一律返回 []，由调用方的 all_rows() 触发空预设警告。
    try:
        from openpyxl import load_workbook  # lazy, P1 优化
        wb = load_workbook(preset_path, read_only=True, data_only=True)
    except Exception:
        return []
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
    except Exception:
        return []
    finally:
        wb.close()


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe_species(rows: Iterable[SpeciesMatch]) -> list[SpeciesMatch]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[SpeciesMatch] = []
    for row in rows:
        key = (
            row.chinese_name.lower(),
            row.latin_name.lower(),
            row.family_name.lower(),
            row.family_latin.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _dedupe_families(rows: Iterable[SpeciesMatch]) -> list[FamilyMatch]:
    seen: set[tuple[str, str]] = set()
    unique: list[FamilyMatch] = []
    for row in rows:
        family_name = row.family_name.strip()
        family_latin = row.family_latin.strip()
        if not family_name and not family_latin:
            continue
        key = (family_name.lower(), family_latin.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(FamilyMatch(family_name=family_name, family_latin=family_latin))
    return unique


def _rank_species(item: SpeciesMatch, text: str) -> tuple[int, int, str]:
    chinese = item.chinese_name.lower()
    latin = item.latin_name.lower()
    if chinese == text:
        return (0, len(chinese), chinese)
    if latin == text:
        return (1, len(latin), chinese)
    if chinese.startswith(text):
        return (2, len(chinese), chinese)
    if latin.startswith(text):
        return (3, len(latin), chinese)
    if text in chinese:
        return (4, len(chinese), chinese)
    if text in latin:
        return (5, len(latin), chinese)
    return (99, len(chinese), chinese)


def _rank_family(item: FamilyMatch, text: str) -> tuple[int, int, str]:
    family = item.family_name.lower()
    latin = item.family_latin.lower()
    if family == text:
        return (0, len(family), family)
    if latin == text:
        return (1, len(latin), family)
    if family.startswith(text):
        return (2, len(family), family)
    if latin.startswith(text):
        return (3, len(latin), family)
    if text in family:
        return (4, len(family), family)
    if text in latin:
        return (5, len(latin), family)
    return (99, len(family), family)
