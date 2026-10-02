"""table_backend_sqlite.py — SqliteBackend：一个工作区一个库，xlsx 的每张表原样映射成一张 SQLite 表。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 1 节。
  * 表名 = xlsx 文件名去掉 .xlsx；一个文件多张 sheet 时 "文件名__sheet名"（修改记录__修改汇总）。
  * 列名 = 现在的中文表头，一字不改；值一律 TEXT；隐藏列 _seq 记行序，读出时按它排。
  * meta 表：schema_version、version:<key>（每表变更计数，写一次 +1）、exported:<key>（上次生成
    Excel 时的计数）。文件级 key（"修改记录.xlsx"）的 version 随它任一 sheet 一起 +1，store 用它判缓存失效。
  * 每个写动作一个事务（BEGIN IMMEDIATE … COMMIT），replace_many 多张表要么全成要么全不成。
  * 本地盘 WAL + NORMAL；网络/挂载盘 network_safe=True → DELETE + FULL（SQLite 官方：WAL 在网络
    文件系统上不安全）。
只依赖 stdlib，不得 import PyQt5 / openpyxl。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .table_backend import Row, split_table_key

SCHEMA_VERSION = 1
_SEQ = "_seq"


def _q(name: str) -> str:
    """SQL 标识符引号（中文列名 / 含 * 的列名都靠它）。"""
    return '"' + str(name).replace('"', '""') + '"'


class SqliteBackend:
    def __init__(
        self,
        db_path: Path | str,
        *,
        fit_row: Callable[[Row, list[str]], Row],
        to_string: Callable[[object], str],
        network_safe: bool = False,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self._fit_row = fit_row
        self._to_string = to_string
        self._read_only = bool(read_only)
        self._lock = threading.RLock()
        if self._read_only:
            # 只读副本：mode=ro 打开，不改 journal_mode、不建 meta、关库不 checkpoint —— 零副作用
            self._conn = sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=10, check_same_thread=False, isolation_level=None
            )
            self._conn.execute("PRAGMA busy_timeout=10000")
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=10, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA journal_mode=" + ("DELETE" if network_safe else "WAL"))
        self._conn.execute("PRAGMA synchronous=" + ("FULL" if network_safe else "NORMAL"))
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def table_name(key: str) -> str:
        file_name, sheet = split_table_key(key)
        base = file_name[:-5] if file_name.lower().endswith(".xlsx") else file_name
        return f"{base}__{sheet}" if sheet else base

    def _table_exists(self, table: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def _sheet_tables_of(self, file_name: str) -> list[str]:
        base = self.table_name(file_name)
        like = base.replace("%", "\\%").replace("_", "\\_") + "\\_\\_%"
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\'", (like,)
        ).fetchall()
        return [r[0] for r in rows]

    def _columns(self, table: str) -> list[str]:
        rows = self._conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
        return [r[1] for r in rows if r[1] != _SEQ]

    @staticmethod
    def _check_headers(headers: Iterable[str]) -> list[str]:
        cleaned = [str(h) for h in headers]
        if any(not h.strip() for h in cleaned):
            raise ValueError("表头含空白列名，无法建 SQLite 表")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("表头含重复列名，无法建 SQLite 表")
        if _SEQ in cleaned:
            raise ValueError(f"列名 {_SEQ} 为内部保留")
        return cleaned

    def _bump(self, key: str) -> None:
        keys = [key]
        file_name, sheet = split_table_key(key)
        if sheet is not None:
            keys.append(file_name)
        for k in keys:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
                (f"version:{k}",),
            )

    def _create(self, table: str, headers: list[str]) -> None:
        cols = ", ".join(f"{_q(h)} TEXT" for h in headers)
        self._conn.execute(f"CREATE TABLE {_q(table)} ({_SEQ} INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")

    def _insert(self, table: str, headers: list[str], rows: Iterable[Row]) -> None:
        sql = (
            f"INSERT INTO {_q(table)} (" + ", ".join(_q(h) for h in headers) + ") VALUES ("
            + ", ".join("?" for _ in headers) + ")"
        )
        payload = []
        for row in rows:
            fitted = self._fit_row(row, headers)
            values = [self._to_string(fitted.get(h, "")) for h in headers]
            if all(v == "" for v in values):
                continue  # 与 xlsx 读回语义一致：全空行不存在
            payload.append(values)
        if payload:
            self._conn.executemany(sql, payload)

    def _row_from(self, cols: list[str], raw: tuple) -> Row:
        row: Row = {}
        for col, value in zip(cols, raw):
            if value is None:
                continue
            text = self._to_string(value)
            if text != "":
                row[col] = text
        return row

    # ------------------------------------------------------------------
    # TableBackend
    # ------------------------------------------------------------------
    def exists(self, key: str) -> bool:
        with self._lock:
            file_name, sheet = split_table_key(key)
            if sheet is None and self._sheet_tables_of(file_name):
                return True
            return self._table_exists(self.table_name(key))

    def headers(self, key: str) -> list[str]:
        with self._lock:
            table = self.table_name(key)
            return self._columns(table) if self._table_exists(table) else []

    def read_rows(self, key: str, fallback_headers: list[str] | None = None) -> list[Row]:
        with self._lock:
            table = self.table_name(key)
            if not self._table_exists(table):
                return []
            cols = self._columns(table)
            if not cols:
                return []
            sql = "SELECT " + ", ".join(_q(c) for c in cols) + f" FROM {_q(table)} ORDER BY {_SEQ}"
            out: list[Row] = []
            for raw in self._conn.execute(sql):
                row = self._row_from(cols, raw)
                if row:
                    out.append(row)
            return out

    def read_many(self, items: list[tuple[str, list[str] | None]]) -> list[list[Row]]:
        return [self.read_rows(key, fallback) for key, fallback in items]

    def replace_rows(self, key: str, headers: list[str], rows: Iterable[Row]) -> None:
        self.replace_many([(key, list(headers), list(rows))])

    def replace_many(self, items: list[tuple[str, list[str], list[Row]]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for key, headers, rows in items:
                    clean = self._check_headers(headers)
                    table = self.table_name(key)
                    self._conn.execute(f"DROP TABLE IF EXISTS {_q(table)}")
                    self._create(table, clean)
                    self._insert(table, clean, rows)
                    self._bump(key)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_rows(self, key: str, headers: list[str], rows: Iterable[Row]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            table = self.table_name(key)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                clean = self._check_headers(headers)
                if not self._table_exists(table):
                    self._create(table, clean)
                existing = self._columns(table)
                for h in clean:
                    if h not in existing:
                        self._conn.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {_q(h)} TEXT")
                self._insert(table, clean, rows)
                self._bump(key)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def stream_columns(self, key: str, wanted_columns: set[str]) -> Iterator[dict[str, str]]:
        with self._lock:
            table = self.table_name(key)
            if not self._table_exists(table):
                return
            cols = [c for c in self._columns(table) if c in wanted_columns]
            if not cols:
                return
            sql = "SELECT " + ", ".join(_q(c) for c in cols) + f" FROM {_q(table)} ORDER BY {_SEQ}"
            rows = self._conn.execute(sql).fetchall()
        for raw in rows:
            row = self._row_from(cols, raw)
            if row:
                yield row

    def version_token(self, key: str) -> int:
        return self.table_version(key)

    def close(self) -> None:
        with self._lock:
            if not self._read_only:
                self.checkpoint()
            self._conn.close()

    # ------------------------------------------------------------------
    # meta / 维护
    # ------------------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return None if row is None else str(row[0])

    def meta_set(self, key: str, value: object) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def table_version(self, key: str) -> int:
        return int(self.meta_get(f"version:{key}") or 0)

    def exported_version(self, key: str) -> int:
        return int(self.meta_get(f"exported:{key}") or 0)

    def mark_exported(self, key: str, version: int) -> None:
        self.meta_set(f"exported:{key}", int(version))

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def checkpoint(self) -> None:
        if self._read_only:
            return
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def quick_check(self) -> bool:
        with self._lock:
            try:
                return self._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            except sqlite3.Error:
                return False
