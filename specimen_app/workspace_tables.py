"""workspace_tables.py — 跨工作区只读读取器："有 sqlite 读 sqlite，否则读 xlsx"。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 3 节。
服务器同步预览 / 导入合并 / 聚合 / 任务包 读"别的工作区"的表时统一走这里：
对方工作区若是 SQLite 模式且还没生成 Excel 镜像，xlsx 不存在也能读到数据。
只读：sqlite 以 mode=ro 打开，不建库、不建表、不迁移；xlsx 用 table_backend 的流式读。
不依赖 ExcelStore / PyQt5。
"""
from __future__ import annotations

from pathlib import Path

from .models import SQLITE_DATA_FILE
from .table_backend import Row, split_table_key, xlsx_headers, xlsx_read_rows
from .table_backend_sqlite import SqliteBackend


def _to_string(value: object) -> str:
    return "" if value is None else str(value).strip()


def _fit(row: Row, headers: list[str]) -> Row:
    return {h: _to_string(row.get(h, "")) for h in headers}


def sqlite_file_for(table_path: Path | str) -> Path | None:
    """<工作区>/数据/<表>.xlsx（或 数据/ 目录本身）→ 同目录下的 标本数据.sqlite；不存在返回 None。"""
    p = Path(table_path)
    data_dir = p if p.is_dir() else p.parent
    candidate = data_dir / SQLITE_DATA_FILE
    return candidate if candidate.is_file() else None


def _key_for(table_path: Path, sheet: str | None) -> str:
    name = Path(table_path).name
    return f"{name}::{sheet}" if sheet else name


def table_exists(table_path: Path | str, sheet: str | None = None) -> bool:
    table_path = Path(table_path)
    db = sqlite_file_for(table_path)
    if db is None:
        return table_path.exists()
    backend = SqliteBackend(db, fit_row=_fit, to_string=_to_string, read_only=True)
    try:
        return backend.exists(_key_for(table_path, sheet))
    except Exception:
        return False
    finally:
        backend.close()


def read_table(
    table_path: Path | str,
    fallback_headers: list[str] | None = None,
    column_aliases: dict[str, str] | None = None,
    sheet: str | None = None,
) -> tuple[list[str], list[Row]]:
    """返回 (表头, 稀疏行)。sqlite 优先；都没有 → ([], [])。"""
    table_path = Path(table_path)
    db = sqlite_file_for(table_path)
    if db is not None:
        backend = SqliteBackend(db, fit_row=_fit, to_string=_to_string, read_only=True)
        try:
            key = _key_for(table_path, sheet)
            return backend.headers(key), backend.read_rows(key, fallback_headers)
        except Exception:
            return [], []
        finally:
            backend.close()
    aliases = dict(column_aliases or {})
    try:
        return xlsx_headers(table_path, _to_string, sheet), xlsx_read_rows(
            table_path, _to_string, aliases, fallback_headers, sheet
        )
    except Exception:
        return [], []


def read_table_raw(table_path: Path | str, sheet: str | None = None) -> tuple[list[str], list[list]]:
    """server_sync 的旧形状：(表头列表, 行=按表头顺序的值列表)。"""
    headers, rows = read_table(table_path, sheet=sheet)
    if not headers:
        return [], []
    return headers, [[row.get(h, "") for h in headers] for row in rows]


def column_values(table_path: Path | str, column: str) -> list[str]:
    headers, rows = read_table(table_path)
    if column not in headers:
        return []
    return [_to_string(row.get(column, "")) for row in rows if _to_string(row.get(column, ""))]
