"""excel_mirror.py — 从 SQLite 真相源生成 Excel 镜像（文件名、表头、行序与旧 xlsx 完全一致）。

设计：docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md 第 2.2 节。
  * 脏表判定：meta 里 version:<key> > exported:<key>。
  * 按文件分组写（修改记录.xlsx 两张 sheet 一次 replace_many）；文件内任一张 sheet 脏就整本重写。
  * 写成功才 mark_exported；版本号在读行**之前**取——读与标之间若有新写入，表仍算脏，下次再导，不丢。
  * 某个文件写失败（Excel 正开着 → PermissionError、磁盘满 → OSError）只记进 failed，其它文件继续；
    数据库不受影响，该表保持"未导出"。
纯函数式，不依赖 PyQt5 / ExcelStore。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .table_backend import Row, TableBackend, split_table_key


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)   # 成功重写的 xlsx 文件名
    failed: dict[str, str] = field(default_factory=dict)  # 文件名 → 原因

    @property
    def ok(self) -> bool:
        return not self.failed


def dirty_keys(sqlite_backend, table_keys: tuple[str, ...] | list[str]) -> list[str]:
    return [key for key in table_keys if sqlite_backend.table_version(key) > sqlite_backend.exported_version(key)]


def expand_to_whole_files(keys: list[str], table_keys: tuple[str, ...] | list[str]) -> list[str]:
    """某文件任一张 sheet 要导 → 该文件所有受管 sheet 一起导（整本新建，不走"保留未知 sheet"的慢路径）。"""
    files = {split_table_key(k)[0] for k in keys}
    return [k for k in table_keys if split_table_key(k)[0] in files]


def export(
    sqlite_backend,
    xlsx_backend: TableBackend,
    keys: list[str],
    headers_for: Callable[[str], list[str]],
) -> ExportResult:
    result = ExportResult()
    by_file: dict[str, list[str]] = {}
    for key in keys:
        by_file.setdefault(split_table_key(key)[0], []).append(key)
    for file_name, file_keys in by_file.items():
        versions: dict[str, int] = {}
        items: list[tuple[str, list[str], list[Row]]] = []
        try:
            for key in file_keys:
                versions[key] = sqlite_backend.table_version(key)  # 先取版本，再读行（见模块注释）
                headers = sqlite_backend.headers(key) or list(headers_for(key))
                items.append((key, headers, sqlite_backend.read_rows(key, headers)))
            xlsx_backend.replace_many(items)
        except Exception as exc:  # noqa: BLE001  —— PermissionError / OSError / 其它都不许冒泡
            result.failed[file_name] = f"{type(exc).__name__}: {exc}"
            continue
        for key, version in versions.items():
            sqlite_backend.mark_exported(key, version)
        result.written.append(file_name)
    return result
