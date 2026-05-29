"""Tier B: SQLite 操作日志（替代 操作记录.xlsx）。

旧：_record_action 用 openpyxl 追加行 (~100ms/次)；undo_last 全量读写 xlsx O(n) (~200-500ms)。
新：INSERT ~1ms；undo/redo = SELECT+UPDATE ~2ms，完全与行数无关。

约束（同 summary_cache.py）：
- 仅用 stdlib sqlite3 + json
- 不引 PyQt5
- 缺失/损坏时按"未初始化"处理，由 ExcelStore fallback 到 xlsx 路径（可选）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


_ACTION_LOG_DB_FILE = "操作记录.sqlite"
_SCHEMA_VERSION = 1


class ActionLogDatabase:
    """操作日志 SQLite 后端。

    每次调用在本进程内复用同一个 sqlite3.Connection（check_same_thread=False）。
    WAL 模式 + exclusive write = 安全。不跨进程共享（ExcelStore 已有进程锁文件）。
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._db_path = self._data_dir / _ACTION_LOG_DB_FILE
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=5.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass
            self._ensure_schema(conn)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS action_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                op_id     TEXT NOT NULL DEFAULT '',
                ts        TEXT NOT NULL DEFAULT '',
                op_type   TEXT NOT NULL DEFAULT '',
                voucher   TEXT NOT NULL DEFAULT '',
                category  TEXT NOT NULL DEFAULT '',
                field     TEXT NOT NULL DEFAULT '',
                old_json  TEXT NOT NULL DEFAULT 'null',
                new_json  TEXT NOT NULL DEFAULT 'null',
                is_undone INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_active ON action_log(is_undone, id DESC)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def insert_action(self, row: dict[str, Any]) -> None:
        """插入一条操作记录。row 键名与 ACTION_LOG_HEADERS 一致。"""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO action_log
               (op_id, ts, op_type, voucher, category, field, old_json, new_json, is_undone)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                row.get("操作ID", ""),
                row.get("时间", ""),
                row.get("操作类型", ""),
                row.get("入库编号", ""),
                row.get("信息类别", ""),
                row.get("字段名", ""),
                row.get("旧值JSON", "null"),
                row.get("新值JSON", "null"),
            ),
        )
        conn.commit()

    def mark_undone(self, action_id: int) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE action_log SET is_undone=1 WHERE id=?", (action_id,))
        conn.commit()

    def mark_redone(self, action_id: int) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE action_log SET is_undone=0 WHERE id=?", (action_id,))
        conn.commit()

    def trim_to_depth(self, depth: int) -> None:
        """删除超出 undo_depth 限制的最老记录（redo 候选也一并清除）。"""
        conn = self._get_conn()
        conn.execute(
            """DELETE FROM action_log
               WHERE id <= (
                   SELECT COALESCE(MIN(id), 0) FROM (
                       SELECT id FROM action_log ORDER BY id DESC LIMIT ?
                   )
               )""",
            (depth,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_last_active(self, undo_depth: int, voided: set[str]) -> dict | None:
        """返回 undo 目标：最后 undo_depth 条记录中最近的非撤销非注销行。

        旧 xlsx 逻辑：rows[-depth:] 取最后 depth 条（含已撤销），再过滤，取[-1]（最新）。
        SQLite 等价：在最后 depth 条里找 is_undone=0 且非注销的最大 id。
        """
        conn = self._get_conn()
        if voided:
            placeholders = ",".join("?" * len(voided))
            sql = f"""
                SELECT * FROM (
                    SELECT * FROM action_log ORDER BY id DESC LIMIT ?
                ) WHERE is_undone=0 AND voucher NOT IN ({placeholders})
                ORDER BY id DESC LIMIT 1
            """
            row = conn.execute(sql, (undo_depth, *sorted(voided))).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM action_log ORDER BY id DESC LIMIT ?
                   ) WHERE is_undone=0
                   ORDER BY id DESC LIMIT 1""",
                (undo_depth,),
            ).fetchone()
        return dict(row) if row else None

    def get_next_redoable(self, voided: set[str]) -> dict | None:
        """返回紧跟在 undo 栈顶之后的第一条已撤销操作（可重做）。"""
        conn = self._get_conn()
        # 找 undo 栈顶（最近一条 active 行的 id），然后取其后第一条 undone 行
        if voided:
            placeholders = ",".join("?" * len(voided))
            last_active_sql = f"""
                SELECT COALESCE(MAX(id), 0) FROM action_log
                WHERE is_undone=0 AND voucher NOT IN ({placeholders})
            """
            last_active_id = conn.execute(
                last_active_sql, tuple(sorted(voided))
            ).fetchone()[0]
            redo_sql = f"""
                SELECT * FROM action_log
                WHERE is_undone=1 AND id > ? AND voucher NOT IN ({placeholders})
                ORDER BY id ASC LIMIT 1
            """
            row = conn.execute(redo_sql, (last_active_id, *sorted(voided))).fetchone()
        else:
            last_active_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM action_log WHERE is_undone=0"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT * FROM action_log WHERE is_undone=1 AND id > ? ORDER BY id ASC LIMIT 1",
                (last_active_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_all(self) -> list[dict]:
        """返回全部记录（用于导出 / 兼容统计）。"""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM action_log ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def count_active_and_undone(self, voided: set[str]) -> tuple[int, int]:
        """返回 (可撤回数, 可重做数)，用于 UI 计数显示。"""
        conn = self._get_conn()
        if voided:
            placeholders = ",".join("?" * len(voided))
            active = conn.execute(
                f"SELECT COUNT(*) FROM action_log WHERE is_undone=0 AND voucher NOT IN ({placeholders})",
                tuple(sorted(voided)),
            ).fetchone()[0]
            undone = conn.execute(
                f"SELECT COUNT(*) FROM action_log WHERE is_undone=1 AND voucher NOT IN ({placeholders})",
                tuple(sorted(voided)),
            ).fetchone()[0]
        else:
            active = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE is_undone=0"
            ).fetchone()[0]
            undone = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE is_undone=1"
            ).fetchone()[0]
        return active, undone

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------

    def migrate_from_rows(self, rows: list[dict]) -> None:
        """从 xlsx 行列表批量导入（迁移时一次性调用）。"""
        conn = self._get_conn()
        for row in rows:
            conn.execute(
                """INSERT OR IGNORE INTO action_log
                   (op_id, ts, op_type, voucher, category, field, old_json, new_json, is_undone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.get("操作ID", ""),
                    row.get("时间", ""),
                    row.get("操作类型", ""),
                    row.get("入库编号", ""),
                    row.get("信息类别", ""),
                    row.get("字段名", ""),
                    row.get("旧值JSON", "null"),
                    row.get("新值JSON", "null"),
                    1 if row.get("是否撤销", "") == "是" else 0,
                ),
            )
        conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def exists(self) -> bool:
        return self._db_path.exists()

    def to_xlsx_rows(self) -> list[dict]:
        """把 SQLite 记录转为 ACTION_LOG_HEADERS 格式的 dict 列表（用于导出）。"""
        rows = self.get_all()
        result = []
        for r in rows:
            result.append({
                "操作ID":   r.get("op_id", ""),
                "时间":     r.get("ts", ""),
                "操作类型": r.get("op_type", ""),
                "入库编号": r.get("voucher", ""),
                "信息类别": r.get("category", ""),
                "字段名":   r.get("field", ""),
                "旧值JSON": r.get("old_json", "null"),
                "新值JSON": r.get("new_json", "null"),
                "是否撤销": "是" if r.get("is_undone") else "",
            })
        return result
