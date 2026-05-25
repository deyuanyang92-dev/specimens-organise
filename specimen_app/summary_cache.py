"""plan D2：入库汇总派生 SQLite 缓存（v0.10.0 最小实现版）。

设计目标
---------
`ExcelStore.summary_records()` 是一张宽表 left-join：每次调用都重新读 specimen /
classification / photo 三张 xlsx 并 in-memory 聚合。中型工作区 (1000+ 入库编号)
每次开「入库汇总」对话框都阻塞 1-3s。

D2 把汇总结果缓存到 ``数据/summary_cache.sqlite``，行级匹配 SUMMARY_COLUMNS。
失效策略（v0.10.0 最小实现）：

- 主表 xlsx 写入 → ``mark_cache_as_invalid()``（设 meta.last_updated = 0）
- ``undo_last`` / ``redo_last`` → ``mark_cache_as_invalid()``
- 读时若 cache 失效 / 不存在 / 解析失败 → fallback 调 ``summary_records()``，并把
  结果同步回 SQLite

后续 (v0.11.0) 可在此基础上加：
- 后台 QThread 增量重建（避免 fallback 时阻塞主线程）
- WAL 多读者
- 文件级 mtime 监听（捕外部 Excel 编辑）

约束
----
- 仅用 stdlib ``sqlite3`` + ``json``，无新依赖
- 不引 PyQt5（保 ``excel_store.py`` stdlib-only 契约时，本模块同样保持中立，
  让 SpecimenWindow 在 UI 层启停可选的后台线程）
- 缓存为派生数据：缺失 / 损坏时一律按"未缓存"处理，永不影响主数据
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable


_SUMMARY_CACHE_FILENAME = "summary_cache.sqlite"

_SCHEMA_VERSION = 1  # 缓存 schema 版本，列变化时 +1，老 db 直接重建

_META_SCHEMA_VERSION_KEY = "schema_version"
_META_LAST_UPDATED_KEY = "last_updated_iso"
_META_ROW_COUNT_KEY = "row_count"


class InventorySummaryCacheDatabase:
    """plan D2：单文件 SQLite 缓存，封装入库汇总宽表的读写。

    线程模型：每个方法调用内部新建一个 ``sqlite3.Connection``，不跨线程共享连接对象
    （SQLite 单连接非线程安全；WAL 模式下多连接读安全）。``_lock`` 仅用于本进程内
    并发 rebuild 互斥，避免重复写。
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_path = self._cache_dir / _SUMMARY_CACHE_FILENAME
        self._rebuild_lock = threading.Lock()

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def _open_connection(self) -> sqlite3.Connection:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._cache_path, timeout=5.0, isolation_level=None)
        # WAL 模式让多读者并发安全（未来加后台线程时不用改）
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS summary_rows (voucher TEXT PRIMARY KEY, json_payload TEXT NOT NULL)"
        )
        existing_version = self._read_meta_value(connection, _META_SCHEMA_VERSION_KEY)
        if existing_version != str(_SCHEMA_VERSION):
            # schema 升级：直接清表重建，缓存无权威性
            connection.execute("DELETE FROM summary_rows")
            self._write_meta_value(connection, _META_SCHEMA_VERSION_KEY, str(_SCHEMA_VERSION))
            self._write_meta_value(connection, _META_LAST_UPDATED_KEY, "")
            self._write_meta_value(connection, _META_ROW_COUNT_KEY, "0")

    def _read_meta_value(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _write_meta_value(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def current_cache_freshness_timestamp(self) -> str:
        """返回最近一次成功写入缓存的 ISO 时间戳；从未写入 / cache 失效 / 缺文件均返回空串。"""
        if not self._cache_path.exists():
            return ""
        try:
            with self._open_connection() as connection:
                self._ensure_schema(connection)
                return self._read_meta_value(connection, _META_LAST_UPDATED_KEY) or ""
        except sqlite3.DatabaseError:
            return ""

    def mark_cache_as_invalid(self) -> None:
        """把 ``meta.last_updated_iso`` 设为空串，下次读必走 fallback 重建。

        不直接删 SQLite 文件，原因：
          - 文件 IO 比单行 UPDATE 慢
          - 后台 rebuilder 未来可以在失效状态下提前读旧数据再异步刷新
          - 删文件会让并发读连接报错
        """
        if not self._cache_path.exists():
            return
        try:
            with self._open_connection() as connection:
                self._ensure_schema(connection)
                self._write_meta_value(connection, _META_LAST_UPDATED_KEY, "")
        except sqlite3.DatabaseError:
            # 缓存损坏，直接干掉文件让下次重建
            try:
                self._cache_path.unlink(missing_ok=True)
            except OSError:
                pass

    def read_all_summary_rows_or_fallback(
        self,
        fallback_provider: Callable[[], list[dict[str, Any]]],
        now_iso_provider: Callable[[], str],
    ) -> list[dict[str, Any]]:
        """汇总对话框的统一读入口。

        逻辑：
          1. cache 存在且 ``last_updated_iso`` 非空 → 直接读 summary_rows 表
          2. 否则调 ``fallback_provider()``（一般是 ``store.summary_records``），把
             结果同步回 SQLite，再返回。

        ``now_iso_provider`` 一般传 ``lambda: store._now()``，避免直接 import datetime
        让本模块与 store 的时间格式保持一致。
        """
        last_updated_iso = self.current_cache_freshness_timestamp()
        if last_updated_iso:
            try:
                return self._read_all_rows_from_database()
            except sqlite3.DatabaseError:
                pass  # cache 损坏 → 走 fallback
        records = list(fallback_provider())
        try:
            self.rebuild_cache_from_records(records, now_iso_provider())
        except sqlite3.DatabaseError:
            pass  # 写缓存失败不应阻塞返回结果
        return records

    def _read_all_rows_from_database(self) -> list[dict[str, Any]]:
        with self._open_connection() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT json_payload FROM summary_rows"
            ).fetchall()
        return [json.loads(row["json_payload"]) for row in rows]

    def rebuild_cache_from_records(
        self,
        records: Iterable[dict[str, Any]],
        now_iso: str,
    ) -> int:
        """把整套 summary records 写回 SQLite，并刷新 meta。返回写入行数。

        全量重写，不做 diff。中型工作区（5000 入库编号）整体 JSON 序列化 + 单事务
        executemany ~50ms，远快于读三张 xlsx 重新聚合的代价。
        """
        materialized_records = list(records)
        with self._rebuild_lock, self._open_connection() as connection:
            self._ensure_schema(connection)
            connection.execute("BEGIN")
            try:
                connection.execute("DELETE FROM summary_rows")
                connection.executemany(
                    "INSERT INTO summary_rows (voucher, json_payload) VALUES (?, ?)",
                    (
                        (
                            str(record.get("入库编号*") or record.get("入库编号") or ""),
                            json.dumps(record, ensure_ascii=False, default=str),
                        )
                        for record in materialized_records
                    ),
                )
                self._write_meta_value(connection, _META_LAST_UPDATED_KEY, now_iso)
                self._write_meta_value(connection, _META_ROW_COUNT_KEY, str(len(materialized_records)))
                connection.execute("COMMIT")
            except sqlite3.DatabaseError:
                connection.execute("ROLLBACK")
                raise
        return len(materialized_records)
