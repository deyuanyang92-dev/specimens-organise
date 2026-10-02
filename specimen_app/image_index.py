"""image_index.py — 图片文件索引：目录扫描 + SQLite 持久化（无 Qt，不做匹配/排序）。

2026-10-01 从 image_search.py 拆出（"构建索引单独一个模块"）。匹配/排序在 image_match.py，
对外门面仍是 image_search.py（旧 import 路径全部保留）。

索引 v2 为什么快
----------------
旧 v1 每个文件除了 ``entries`` 一行，还把 stem 每个 token 的所有前缀写进 ``tokens`` 表
（每行都带完整路径）：20k 张照片 = 68.7 万行、541 MB、全量建索引 43.7 s（本机实测）。
v2 只存 ``entries`` 一行/文件（20k 张 ≈ 2 MB），匹配改在 Python 里按候选行打分
（image_match.rank_entries），候选行由 SQL 用"查询第一段子串 + 后缀"预筛。
扫描改用 ``os.scandir`` 递归：Windows 上 ``DirEntry.stat()`` 不再额外发一次系统调用。

旧库兼容：打开时发现 ``tokens`` 表 → 直接 DROP（entries 原样保留，**不重扫**），
下次 reconcile 时 VACUUM 回收空间（在工作线程里）。

保持不变的红线：索引只读照片目录，绝不改动任何照片文件；缓存目录
``数据/图片搜索索引缓存`` 本身被排除在扫描范围之外。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .image_match import natural_sort_key
from .local_cache import local_cache_dir, read_only_sqlite_uri


SUPPORTED_IMAGE_SUFFIX_ORDER = (
    ".tif",
    ".tiff",
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".bmp",
    ".webp",
    ".gif",
    ".jp2",
    ".j2k",
)
SUPPORTED_IMAGE_SUFFIXES = set(SUPPORTED_IMAGE_SUFFIX_ORDER)
TIF_IMAGE_SUFFIXES = {".tif", ".tiff"}
JPG_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif"}
TIF_JPG_IMAGE_SUFFIXES = TIF_IMAGE_SUFFIXES | JPG_IMAGE_SUFFIXES
IMAGE_TYPE_SUFFIXES = {
    "tif": TIF_IMAGE_SUFFIXES,
    "jpg": JPG_IMAGE_SUFFIXES,
    "tif_jpg": TIF_JPG_IMAGE_SUFFIXES,
    "all": SUPPORTED_IMAGE_SUFFIXES,
}
EXCLUDED_DIR_NAMES = {"build", "dist", "releases", "__pycache__", ".git", ".agents"}
EXCLUDED_SYSTEM_DIRS = {"proc", "sys", "dev", "run", "snap", "boot", "lib", "lib64", "sbin", "bin", "usr"}
EXCLUDED_PATH_PARTS = {("数据", "数据版本"), ("数据", "缩略图缓存"), ("数据", "图片搜索索引缓存")}
IMAGE_INDEX_CACHE_DIR_NAME = "图片搜索索引缓存"
IMAGE_INDEX_SQLITE_FILE_NAME = "image_search.sqlite3"
IMAGE_INDEX_SCHEMA_VERSION = 2
LAST_SCAN_SAFETY_MARGIN_SECONDS = 2.0  # 见 reconcile_scope 里的说明
_INSERT_BATCH = 2000


@dataclass(frozen=True)
class ImageIndexEntry:
    path: Path
    file_name: str
    stem: str
    suffix: str


@dataclass(frozen=True)
class ImageIndexUpdate:
    scanned: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    cancelled: bool = False

    @property
    def modified(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass(frozen=True)
class ScannedFile:
    """扫描结果：路径 + 扫描时顺手拿到的 stat（Windows 上零额外系统调用）。"""

    path: Path
    size: int
    mtime_ns: int


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------

def is_supported_image(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES


def normalize_suffixes(suffixes: Iterable[str]) -> set[str]:
    return {
        suffix if suffix.startswith(".") else f".{suffix}"
        for suffix in (item.lower().strip() for item in suffixes)
        if suffix
    }


def suffixes_for_image_type(image_type: str) -> set[str]:
    return set(IMAGE_TYPE_SUFFIXES.get(image_type, TIF_IMAGE_SUFFIXES))


def image_file_filter() -> str:
    suffix_patterns = " ".join(f"*{suffix}" for suffix in SUPPORTED_IMAGE_SUFFIX_ORDER)
    return f"图片文件 ({suffix_patterns});;所有文件 (*.*)"


def is_excluded_path(path: Path | str, root: Path | str) -> bool:
    workspace = Path(root).resolve()
    candidate = Path(path)
    try:
        parts = candidate.relative_to(workspace).parts
    except ValueError:
        try:
            parts = candidate.resolve().relative_to(workspace).parts
        except ValueError:
            return False
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    for excluded in EXCLUDED_PATH_PARTS:
        for index in range(0, len(parts) - len(excluded) + 1):
            if tuple(parts[index : index + len(excluded)]) == excluded:
                return True
    return False


def _dedupe_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _entry_from_path(path: Path) -> ImageIndexEntry:
    return ImageIndexEntry(path=path, file_name=path.name, stem=path.stem, suffix=path.suffix.lower())


def scan_image_files(
    roots: list[Path | str],
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    name_pattern: re.Pattern[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    skip_directories_unchanged_since: float | None = None,
) -> list[ScannedFile]:
    """``os.scandir`` 递归遍历 ``roots``，返回匹配后缀的图片及其 stat。

    语义与旧 ``iter_images``（os.walk 版）完全一致：
      - 不跟随目录符号链接；文件条目跟随（与 os.walk 相同）；
      - ``max_depth > 0`` 时，相对根目录深度 > max_depth 的目录整体跳过（含其文件）；
      - 根为 ``/`` 时额外剔除系统目录；``EXCLUDED_DIR_NAMES`` / ``is_excluded_path`` 剔除；
      - ``skip_directories_unchanged_since``：目录 mtime <= 该时间 → 不产出该目录的直接
        子文件，但仍递归子目录（plan v0.10.4 I1 增量规则，照片基本只读，可接受漏判内容修改）；
      - 结果按文件名自然排序；跨根去重（normcase(abspath)）。
    """
    allowed_suffixes = normalize_suffixes(suffixes) if suffixes is not None else SUPPORTED_IMAGE_SUFFIXES
    seen: set[str] = set()
    results: list[ScannedFile] = []
    for root in roots:
        if should_stop and should_stop():
            break
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            continue
        is_root_fs = root_path == Path("/")
        stack: list[tuple[Path, int]] = [(root_path, 0)]
        while stack:
            if should_stop and should_stop():
                return results
            current_path, depth = stack.pop()
            if max_depth > 0 and depth > max_depth:
                continue
            emit_files = True
            if skip_directories_unchanged_since is not None:
                try:
                    directory_mtime = current_path.stat().st_mtime
                except OSError:
                    directory_mtime = float("inf")  # 取不到 mtime 时保守全扫
                if directory_mtime <= skip_directories_unchanged_since:
                    emit_files = False
            child_dirs: list[Path] = []
            try:
                with os.scandir(current_path) as iterator:
                    for entry in iterator:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                name = entry.name
                                if is_root_fs and name in EXCLUDED_SYSTEM_DIRS:
                                    continue
                                if name in EXCLUDED_DIR_NAMES:
                                    continue
                                child = current_path / name
                                if is_excluded_path(child, root_path):
                                    continue
                                child_dirs.append(child)
                                continue
                            if not emit_files:
                                continue
                            name = entry.name
                            dot = name.rfind(".")
                            suffix = name[dot:].lower() if dot >= 0 else ""
                            if suffix not in allowed_suffixes:
                                continue
                            if name_pattern is not None and not name_pattern.match(name[:dot] if dot >= 0 else name):
                                continue
                            if not entry.is_file():
                                continue
                            stat = entry.stat()
                        except OSError:
                            continue
                        path = current_path / name
                        key = _dedupe_key(path)
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append(ScannedFile(path, int(stat.st_size), int(stat.st_mtime_ns)))
            except OSError:
                continue
            # 逆序压栈 → 子目录按名称顺序出栈（仅影响遍历顺序，结果最终统一排序）
            for child in sorted(child_dirs, reverse=True):
                stack.append((child, depth + 1))
    results.sort(key=lambda item: natural_sort_key(item.path.name))
    return results


def iter_images(
    roots: list[Path | str],
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    name_pattern: re.Pattern[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    skip_directories_unchanged_since: float | None = None,
) -> list[Path]:
    """兼容旧 API：只要路径列表。实现已换成 ``scan_image_files``（os.scandir）。"""
    return [
        item.path
        for item in scan_image_files(
            roots,
            max_depth=max_depth,
            suffixes=suffixes,
            name_pattern=name_pattern,
            should_stop=should_stop,
            skip_directories_unchanged_since=skip_directories_unchanged_since,
        )
    ]


def iter_workspace_images(root: Path | str) -> list[Path]:
    workspace = Path(root).resolve()
    results: list[Path] = []
    for current, dir_names, file_names in os.walk(workspace):
        current_path = Path(current)
        dir_names[:] = [
            directory
            for directory in dir_names
            if directory not in EXCLUDED_DIR_NAMES and not is_excluded_path(current_path / directory, workspace)
        ]
        for file_name in file_names:
            path = current_path / file_name
            if not is_supported_image(path):
                continue
            if is_excluded_path(path, workspace):
                continue
            results.append(path)
    return sorted(results, key=lambda item: item.as_posix().lower())


# ---------------------------------------------------------------------------
# 作用域（roots + 深度）
# ---------------------------------------------------------------------------

def image_index_key(roots: list[Path | str], max_depth: int) -> tuple[tuple[str, ...], int]:
    # 旧：Path(root).resolve()（网络盘一次往返，且主线程常调）。现：纯字符串 abspath。
    return (tuple(os.path.abspath(str(root)) for root in roots), max_depth)


def effective_image_search_depth(roots: list[Path | str], max_depth: int = 0) -> int:
    if any(os.path.abspath(str(r)) == os.path.abspath("/") for r in roots) and max_depth == 0:
        return 4
    if any(os.path.abspath(str(r)) == os.path.abspath("/") for r in roots):
        return min(max_depth, 4)
    return max_depth


def image_search_depth(
    workspace: Path, extra_roots: list[Path | str] | None, roots: list[Path | str], max_depth: int = 0
) -> int:
    depth = effective_image_search_depth(roots, max_depth)
    if extra_roots is None and not (workspace / "照片").is_dir() and depth == 0:
        return 4
    return depth


def image_search_roots(workspace: Path, extra_roots: list[Path | str] | None = None) -> list[Path]:
    roots: list[Path] = []
    if extra_roots:
        candidates = [Path(os.path.abspath(str(root))) for root in extra_roots]
    else:
        photo_dir = workspace / "照片"
        candidates = [photo_dir if photo_dir.is_dir() else workspace]
    for candidate in candidates:
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def path_in_index_scope(path: Path, roots: list[Path], max_depth: int) -> bool:
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if max_depth > 0 and len(relative.parts) > max_depth:
            continue
        if is_excluded_path(path, root):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# SQLite 持久化
# ---------------------------------------------------------------------------

class ImageIndexStore:
    """磁盘索引（每工作区一个 SQLite），大作用域不进应用内存。"""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(os.path.abspath(str(workspace)))
        # 旧：self.workspace / "数据" / 图片搜索索引缓存 / image_search.sqlite3（在工作区里，网络盘上每次都走 SMB）
        # 现：本机 app 配置目录下的 cache/image_index/<工作区指纹>/（见 local_cache.py）
        self.path = local_cache_dir(self.workspace, "image_index") / IMAGE_INDEX_SQLITE_FILE_NAME

    # -- 连接 / 结构 ---------------------------------------------------------

    def _connect_read_only(self) -> sqlite3.Connection:
        """只读查询专用（UI 线程会调 has_scope / last_scan）：mode=ro，不建库、不建表、不迁移。

        2026-10-02 用户升级 0.10.32 后"一打开就未响应"：旧 tokens 表可达数百 MB，_connect 里的
        v1→v2 迁移（DROP tokens）在 GUI 线程上要几秒。迁移改为只在 reconcile（工作线程）里做。
        """
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        conn = sqlite3.connect(read_only_sqlite_uri(self.path), uri=True, timeout=10)
        conn.create_function("py_lower", 1, _py_lower, deterministic=True)
        return conn

    @contextmanager
    def _read_connection(self):
        conn = self._connect_read_only()
        try:
            yield conn
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scopes (
                scope_key TEXT PRIMARY KEY,
                roots_json TEXT NOT NULL,
                max_depth INTEGER NOT NULL,
                last_scan REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS entries (
                scope_key TEXT NOT NULL,
                path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                stem TEXT NOT NULL,
                suffix TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                PRIMARY KEY (scope_key, path)
            );
            CREATE INDEX IF NOT EXISTS idx_image_entries_scope_name
                ON entries (scope_key, file_name);
            """
        )
        # Python 的 lower() 处理非 ASCII 大写（SQLite 内置 lower 只管 ASCII）
        conn.create_function("py_lower", 1, _py_lower, deterministic=True)
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """v1 → v2：丢掉 tokens 表（entries 不动、不重扫），记下需要 VACUUM。"""
        has_tokens = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tokens'"
        ).fetchone()
        if has_tokens:
            conn.executescript(
                """
                DROP INDEX IF EXISTS idx_image_tokens_lookup;
                DROP TABLE IF EXISTS tokens;
                """
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('needs_vacuum', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(IMAGE_INDEX_SCHEMA_VERSION),),
        )
        conn.commit()

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _discard_legacy_workspace_index(self) -> None:
        """2026-10-02：索引已搬到本机，工作区里旧的 数据/图片搜索索引缓存/（可达数百 MB）直接删掉，
        不再在网络盘上做任何迁移/VACUUM。只在 reconcile（工作线程）里调，失败忽略。"""
        legacy_dir = self.workspace / "数据" / IMAGE_INDEX_CACHE_DIR_NAME
        if not legacy_dir.is_dir():
            return
        for name in (IMAGE_INDEX_SQLITE_FILE_NAME, IMAGE_INDEX_SQLITE_FILE_NAME + "-wal", IMAGE_INDEX_SQLITE_FILE_NAME + "-shm", IMAGE_INDEX_SQLITE_FILE_NAME + "-journal"):
            try:
                (legacy_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            for stale in legacy_dir.glob("*.json"):
                stale.unlink(missing_ok=True)
            legacy_dir.rmdir()
        except OSError:
            pass

    def _vacuum_if_needed(self) -> None:
        """v1 → v2 迁移后回收 tokens 表占的空间；只在 reconcile（工作线程）里调用。"""
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='needs_vacuum'").fetchone()
                if row and str(row[0]) == "1":
                    conn.execute("VACUUM")
                    conn.execute("UPDATE meta SET value='0' WHERE key='needs_vacuum'")
                    conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return

    # -- 作用域元数据 -------------------------------------------------------

    @staticmethod
    def scope_key(roots: list[Path | str], max_depth: int) -> str:
        key = image_index_key(roots, max_depth)
        payload = json.dumps({"roots": key[0], "max_depth": key[1]}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8", errors="surrogatepass")).hexdigest()

    def has_scope(self, roots: list[Path | str], max_depth: int) -> bool:
        if not self.path.exists():
            return False
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._read_connection() as conn:
                return conn.execute("SELECT 1 FROM scopes WHERE scope_key = ?", (scope_key,)).fetchone() is not None
        except (sqlite3.Error, OSError):
            return False

    def get_scope_last_scan_timestamp(self, roots: list[Path | str], max_depth: int = 0) -> float | None:
        """plan v0.10.4 I3：该 scope 上次完成 reconcile 的时间戳；None = 从未扫描。纯只读。"""
        if not self.path.exists():
            return None
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._read_connection() as conn:
                row = conn.execute("SELECT last_scan FROM scopes WHERE scope_key = ?", (scope_key,)).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    # -- 写入 ---------------------------------------------------------------

    def reconcile_scope(
        self,
        roots: list[Path | str],
        max_depth: int = 0,
        should_stop: Callable[[], bool] | None = None,
        incremental_since_unix: float | None = None,
    ) -> ImageIndexUpdate:
        """扫描 + 与库里 diff（新增/删除/改动）。

        ``incremental_since_unix=T``（plan v0.10.4 I2）：目录 mtime <= T 的目录不产出文件；
        removed 只在"本次实际扫到的父目录"范围内判定，未扫目录的旧条目仍视为有效。
        """
        scan_started = time.time()
        scanned = scan_image_files(
            roots,
            max_depth=max_depth,
            suffixes=SUPPORTED_IMAGE_SUFFIXES,
            should_stop=should_stop,
            skip_directories_unchanged_since=incremental_since_unix,
        )
        if should_stop and should_stop():
            return ImageIndexUpdate(cancelled=True)
        current_by_path: dict[str, tuple[ImageIndexEntry, int, int]] = {}
        for item in scanned:
            current_by_path[str(item.path)] = (_entry_from_path(item.path), item.size, item.mtime_ns)
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                old_rows = conn.execute(
                    "SELECT path, size, mtime_ns FROM entries WHERE scope_key = ?", (scope_key,)
                ).fetchall()
                existing = {str(path): (int(size), int(mtime_ns)) for path, size, mtime_ns in old_rows}
                if incremental_since_unix is not None:
                    changed_directories = {str(Path(p).parent) for p in current_by_path}
                    relevant_existing_paths = {p for p in existing if str(Path(p).parent) in changed_directories}
                    removed = relevant_existing_paths.difference(current_by_path)
                else:
                    removed = set(existing).difference(current_by_path)
                added = set(current_by_path).difference(existing)
                changed = {
                    path
                    for path in set(existing).intersection(current_by_path)
                    if existing[path] != current_by_path[path][1:]
                }
                if should_stop and should_stop():
                    return ImageIndexUpdate(cancelled=True)
                conn.executemany(
                    "DELETE FROM entries WHERE scope_key = ? AND path = ?",
                    [(scope_key, path) for path in removed | changed],
                )
                self._insert_entries(
                    conn,
                    scope_key,
                    (current_by_path[path] for path in sorted(added | changed)),
                )
                roots_json = json.dumps(
                    [str(Path(root).resolve()) for root in roots], ensure_ascii=False, separators=(",", ":")
                )
                conn.execute(
                    """
                    INSERT INTO scopes(scope_key, roots_json, max_depth, last_scan)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        roots_json=excluded.roots_json,
                        max_depth=excluded.max_depth,
                        last_scan=excluded.last_scan
                    """,
                    # 旧：last_scan = time.time()（扫描结束时刻）。两个漏洞：
                    #   1. 扫描进行中被加进"已扫过目录"的文件，其目录 mtime < 结束时刻 → 永远漏掉；
                    #   2. 目录 mtime 走内核粗粒度时钟（ext4 可落后 time.time() 几 ms；FAT/exFAT 2 s），
                    #      紧跟扫描之后新增的文件 mtime 可能 <= last_scan → 增量扫跳过（v2 索引变快后实测复现）。
                    # 新：记"扫描开始时刻 - 2 s"。代价只是下次增量多看一眼这 2 s 内动过的目录。
                    (scope_key, roots_json, max_depth, scan_started - LAST_SCAN_SAFETY_MARGIN_SECONDS),
                )
            self._vacuum_if_needed()
            self._discard_legacy_workspace_index()
            return ImageIndexUpdate(
                scanned=len(current_by_path),
                added=len(added),
                removed=len(removed),
                changed=len(changed),
            )
        except sqlite3.Error:
            return ImageIndexUpdate(cancelled=True)

    def upsert_paths(self, roots: list[Path | str], paths: Iterable[Path | str], max_depth: int = 0) -> int:
        """把刚关联的照片直接补进已有索引（不重扫）。返回新增条数。"""
        if not self.has_scope(roots, max_depth):
            return 0
        scope_key = self.scope_key(roots, max_depth)
        resolved_roots = [Path(root).resolve() for root in roots]
        count = 0
        try:
            with self._connection() as conn:
                for raw_path in paths:
                    path = Path(raw_path).resolve()
                    if not path.is_file() or not path_in_index_scope(path, resolved_roots, max_depth):
                        continue
                    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                        continue
                    stat = path.stat()
                    prior = conn.execute(
                        "SELECT size, mtime_ns FROM entries WHERE scope_key = ? AND path = ?",
                        (scope_key, str(path)),
                    ).fetchone()
                    if prior == (int(stat.st_size), int(stat.st_mtime_ns)):
                        continue
                    conn.execute("DELETE FROM entries WHERE scope_key = ? AND path = ?", (scope_key, str(path)))
                    self._insert_entries(
                        conn, scope_key, [(_entry_from_path(path), int(stat.st_size), int(stat.st_mtime_ns))]
                    )
                    if prior is None:
                        count += 1
            return count
        except (OSError, sqlite3.Error):
            return 0

    def clear_scope(self, roots: list[Path | str], max_depth: int = 0) -> None:
        if not self.path.exists():
            return
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM entries WHERE scope_key = ?", (scope_key,))
                conn.execute("DELETE FROM scopes WHERE scope_key = ?", (scope_key,))
        except sqlite3.Error:
            return

    @staticmethod
    def _insert_entries(
        conn: sqlite3.Connection,
        scope_key: str,
        items: Iterable[tuple[ImageIndexEntry, int, int]],
    ) -> None:
        batch: list[tuple[str, str, str, str, str, int, int]] = []
        for entry, size, mtime_ns in items:
            batch.append((scope_key, str(entry.path), entry.file_name, entry.stem, entry.suffix, size, mtime_ns))
            if len(batch) >= _INSERT_BATCH:
                conn.executemany(
                    "INSERT OR REPLACE INTO entries(scope_key, path, file_name, stem, suffix, size, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO entries(scope_key, path, file_name, stem, suffix, size, mtime_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )

    # -- 读取 ---------------------------------------------------------------

    def entries(self, roots: list[Path | str], max_depth: int = 0) -> list[ImageIndexEntry]:
        if not self.path.exists():
            return []
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._read_connection() as conn:
                rows = conn.execute(
                    "SELECT path, file_name, stem, suffix FROM entries WHERE scope_key = ?", (scope_key,)
                ).fetchall()
        except (sqlite3.Error, OSError):
            return []
        return [self._row_entry(row) for row in rows]

    def iter_candidates(
        self,
        roots: list[Path | str],
        needle: str,
        suffixes: Iterable[str] | None = None,
        max_depth: int = 0,
    ) -> Iterator[ImageIndexEntry]:
        """按"stem 含 needle 子串 + 后缀"流式返回候选行；打分排序交给 image_match。"""
        if not self.path.exists():
            return
        needle = str(needle or "").strip().lower()
        if not needle:
            return
        scope_key = self.scope_key(roots, max_depth)
        allowed = sorted(normalize_suffixes(suffixes)) if suffixes is not None else []
        sql = "SELECT path, file_name, stem, suffix FROM entries WHERE scope_key = ? AND instr(py_lower(stem), ?) > 0"
        params: list[object] = [scope_key, needle]
        if allowed:
            sql += " AND suffix IN (" + ",".join("?" for _ in allowed) + ")"
            params.extend(allowed)
        try:
            conn = self._connect_read_only()
        except (sqlite3.Error, OSError):
            return
        try:
            cursor = conn.execute(sql, params)
            while True:
                rows = cursor.fetchmany(_INSERT_BATCH)
                if not rows:
                    break
                for row in rows:
                    yield self._row_entry(row)
        except sqlite3.Error:
            return
        finally:
            conn.close()

    @staticmethod
    def _row_entry(row: tuple[str, str, str, str]) -> ImageIndexEntry:
        return ImageIndexEntry(path=Path(row[0]), file_name=row[1], stem=row[2], suffix=row[3])


def _py_lower(value: object) -> str:
    return str(value or "").lower()
