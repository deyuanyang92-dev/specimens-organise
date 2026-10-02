"""workspace_convert.py — 旧工作区（只有 xlsx）→ SQLite 真相源 的转换与校验。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 2.1 节。
纯函数式：只拿两个后端 + 回调工作，不碰 ExcelStore 内部；不依赖 PyQt5。

保险（用户要求"稳健、不丢数据"）：
  1. 源 xlsx 从头到尾只读，不改名、不删除；
  2. 写到临时库 `标本数据.sqlite.converting`，逐表读回与 xlsx 逐格比对（表头、值、行序、行数）；
  3. 任一步失败 → 删临时库，返回 converted=False 与原因，调用方留在旧模式；
  4. 比对通过才 os.replace 成正式库名；表头空白/重复（SQLite 建不了表）也算失败。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .table_backend import TableBackend

CONVERTING_SUFFIX = ".converting"


@dataclass(frozen=True)
class ConversionReport:
    converted: bool
    reason: str = ""
    tables: dict[str, int] = field(default_factory=dict)
    snapshot_path: Path | None = None


def convert_tables(
    xlsx_backend: TableBackend,
    sqlite_backend,
    table_keys: tuple[str, ...],
    headers_for: Callable[[str], list[str]],
    column_aliases: dict[str, str],
) -> dict[str, int]:
    """把每张受管表从 xlsx 后端搬进 sqlite 后端并逐格校验；返回 {key: 行数}。失败抛异常。"""
    counts: dict[str, int] = {}
    for key in table_keys:
        default_headers = list(headers_for(key))
        raw_headers = xlsx_backend.headers(key)
        # 旧列名（COLUMN_ALIASES 的 key）归一为新列名；read_rows 读出的行本来就是新列名
        headers = [column_aliases.get(h, h) for h in raw_headers if h] or default_headers
        rows = xlsx_backend.read_rows(key, default_headers)
        sqlite_backend.replace_rows(key, headers, rows)
        back_headers = sqlite_backend.headers(key)
        if back_headers != headers:
            raise ValueError(f"{key}：表头读回不一致 {back_headers!r} != {headers!r}")
        back_rows = sqlite_backend.read_rows(key)
        if back_rows != rows:
            raise ValueError(f"{key}：数据读回不一致（xlsx {len(rows)} 行，sqlite {len(back_rows)} 行）")
        counts[key] = len(rows)
    return counts


def convert_workspace(
    sqlite_path: Path,
    *,
    xlsx_backend: TableBackend,
    make_sqlite_backend: Callable[[Path], object],
    table_keys: tuple[str, ...],
    headers_for: Callable[[str], list[str]],
    column_aliases: dict[str, str],
    snapshot: Callable[[], Path | None],
) -> ConversionReport:
    """完整流程：快照 → 临时库 → 转换+校验 → 标记已导出 → 原子改名。配置写入由调用方做（它持有 config）。"""
    sqlite_path = Path(sqlite_path)
    tmp_path = sqlite_path.with_name(sqlite_path.name + CONVERTING_SUFFIX)
    if sqlite_path.exists():
        return ConversionReport(converted=False, reason="标本数据.sqlite 已存在，无需转换")
    snapshot_path: Path | None = None
    try:
        snapshot_path = snapshot()
    except Exception as exc:  # noqa: BLE001
        return ConversionReport(converted=False, reason=f"转换前快照失败：{exc}")
    for stale in (tmp_path, Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm"), Path(str(tmp_path) + "-journal")):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    backend = None
    try:
        backend = make_sqlite_backend(tmp_path)
        counts = convert_tables(xlsx_backend, backend, table_keys, headers_for, column_aliases)
        for key in table_keys:
            backend.mark_exported(key, backend.table_version(key))  # 此刻 Excel == 数据库
        backend.close()
        backend = None
        os.replace(tmp_path, sqlite_path)
        return ConversionReport(converted=True, tables=counts, snapshot_path=snapshot_path)
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        for leftover in (tmp_path, Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm"), Path(str(tmp_path) + "-journal")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        return ConversionReport(converted=False, reason=reason, snapshot_path=snapshot_path)
