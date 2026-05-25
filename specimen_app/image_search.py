from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .models import Row


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
IDENTIFIER_SEPARATOR_RE = re.compile(r"[-_]+")
NATURAL_SORT_RE = re.compile(r"(\d+)")
IMAGE_INDEX_CACHE_LIMIT = 3
IMAGE_INDEX_CACHE_DIR_NAME = "图片搜索索引缓存"
IMAGE_INDEX_SQLITE_FILE_NAME = "image_search.sqlite3"
_IMAGE_INDEX_CACHE: OrderedDict[tuple[tuple[str, ...], int], list["ImageIndexEntry"]] = OrderedDict()
_IMAGE_INDEX_LOCK = threading.RLock()
_SEARCH_INDEX_CACHE: OrderedDict[tuple[tuple[str, ...], int], "ImageSearchIndex"] = OrderedDict()
_SEARCH_INDEX_CACHE_LIMIT = 3
_SEARCH_INDEX_LOCK = threading.RLock()


@dataclass(frozen=True)
class ImageSearchResult:
    path: Path
    relative_path: str
    file_name: str
    score: int
    matched_keywords: tuple[str, ...]
    is_linked: bool = False
    linked_vouchers: list[str] | None = None


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


class ImageSearchIndex:
    """Token-based inverted index for fast image search.

    Builds a prefix index from filename stems so that queries with any number
    of segments (e.g. "QD-C", "CK", "SC008") can find matching files in O(1)
    lookups rather than O(n) linear scans.
    """

    def __init__(self) -> None:
        self._entries: list[ImageIndexEntry] = []
        self._token_index: dict[str, set[int]] = {}
        self._source_key: tuple[tuple[str, ...], int] | None = None

    @property
    def entries(self) -> list[ImageIndexEntry]:
        return list(self._entries)

    @property
    def source_key(self) -> tuple[tuple[str, ...], int] | None:
        return self._source_key

    def build(
        self,
        entries: list[ImageIndexEntry],
        source_key: tuple[tuple[str, ...], int] | None = None,
    ) -> None:
        self._entries = list(entries)
        self._source_key = source_key
        self._token_index.clear()
        for idx, entry in enumerate(self._entries):
            tokens = self._tokenize(entry.stem)
            for token in tokens:
                for prefix in self._prefixes(token):
                    self._token_index.setdefault(prefix, set()).add(idx)

    def search(self, query: str, limit: int = 100) -> list[int]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        candidates: set[int] | None = None
        for token in query_tokens:
            if token in self._token_index:
                matches = self._token_index[token]
            else:
                return []
            if candidates is None:
                candidates = set(matches)
            else:
                candidates &= matches
            if not candidates:
                return []

        if candidates is None:
            return []

        result = [idx for idx in candidates if self._verify_positions(idx, query_tokens)]
        return sorted(result, key=lambda i: natural_sort_key(self._entries[i].file_name))[:limit]

    def contains_search(self, query: str, limit: int = 100) -> list[int]:
        needles = self._contains_needles(query)
        if not needles:
            return []
        result = []
        for idx, entry in enumerate(self._entries):
            haystacks = self._contains_haystacks(entry)
            if any(needle in haystack for needle in needles for haystack in haystacks):
                result.append(idx)
        return sorted(result, key=lambda i: natural_sort_key(self._entries[i].file_name))[:limit]

    def _verify_positions(self, entry_idx: int, query_tokens: list[str]) -> bool:
        stem_tokens = self._tokenize(self._entries[entry_idx].stem)
        for start in range(len(stem_tokens) - len(query_tokens) + 1):
            match = True
            for i, qt in enumerate(query_tokens):
                st = stem_tokens[start + i]
                if not st.startswith(qt):
                    match = False
                    break
                # Reject when stem token extends query with digits only
                # (e.g. "WenSC004" should NOT match "WenSC0042")
                remainder = st[len(qt):]
                if remainder and remainder.isdigit():
                    match = False
                    break
            if match:
                return True
        return False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [t.lower() for t in IDENTIFIER_SEPARATOR_RE.split(text) if t]

    @staticmethod
    def _contains_needles(text: str) -> tuple[str, ...]:
        raw = str(text or "").strip().lower()
        if not raw:
            return ()
        normalized = IDENTIFIER_SEPARATOR_RE.sub("-", raw)
        return tuple(dict.fromkeys([raw, normalized]))

    @staticmethod
    def _contains_haystacks(entry: ImageIndexEntry) -> tuple[str, ...]:
        raw_name = entry.file_name.lower()
        raw_stem = entry.stem.lower()
        normalized_name = IDENTIFIER_SEPARATOR_RE.sub("-", raw_name)
        normalized_stem = IDENTIFIER_SEPARATOR_RE.sub("-", raw_stem)
        return tuple(dict.fromkeys([raw_name, raw_stem, normalized_name, normalized_stem]))

    @staticmethod
    def _prefixes(token: str) -> list[str]:
        return [token[: i + 1] for i in range(len(token))]

    def to_dict(self) -> dict[str, list[int]]:
        return {k: sorted(v) for k, v in self._token_index.items()}

    @classmethod
    def from_dict(
        cls,
        entries: list[ImageIndexEntry],
        data: dict[str, list[int]],
        source_key: tuple[tuple[str, ...], int] | None = None,
    ) -> "ImageSearchIndex":
        index = cls()
        index._entries = list(entries)
        index._source_key = source_key
        index._token_index = {k: set(v) for k, v in data.items()}
        return index


class ImageIndexStore:
    """Disk-backed index which keeps large search scopes out of application RAM."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / "数据" / IMAGE_INDEX_CACHE_DIR_NAME / IMAGE_INDEX_SQLITE_FILE_NAME

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
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
            CREATE TABLE IF NOT EXISTS tokens (
                scope_key TEXT NOT NULL,
                token TEXT NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY (scope_key, token, path)
            );
            CREATE INDEX IF NOT EXISTS idx_image_tokens_lookup
                ON tokens (scope_key, token, path);
            CREATE INDEX IF NOT EXISTS idx_image_entries_scope_name
                ON entries (scope_key, file_name);
            """
        )
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def scope_key(roots: list[Path | str], max_depth: int) -> str:
        key = _image_index_key(roots, max_depth)
        payload = json.dumps({"roots": key[0], "max_depth": key[1]}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8", errors="surrogatepass")).hexdigest()

    def has_scope(self, roots: list[Path | str], max_depth: int) -> bool:
        if not self.path.exists():
            return False
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                return conn.execute(
                    "SELECT 1 FROM scopes WHERE scope_key = ?", (scope_key,)
                ).fetchone() is not None
        except sqlite3.Error:
            return False

    def get_scope_last_scan_timestamp(
        self,
        roots: list[Path | str],
        max_depth: int = 0,
    ) -> float | None:
        """plan v0.10.4 I3：读取该 scope 上次完成 reconcile 的时间戳。

        scopes 表已有 ``last_scan`` 列 (v0.10.4 之前已存在)，本方法只读，不触发任何 walk。
        返回 None 代表"从未扫描过"——调用方应走全扫；返回 float 时调用方可决定
        是否短路 / 走 incremental。
        """
        if not self.path.exists():
            return None
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT last_scan FROM scopes WHERE scope_key = ?", (scope_key,)
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    def reconcile_scope(
        self,
        roots: list[Path | str],
        max_depth: int = 0,
        should_stop: Callable[[], bool] | None = None,
        incremental_since_unix: float | None = None,
    ) -> ImageIndexUpdate:
        """plan v0.10.4 I2：增量模式扫描 + 收窄 diff 范围。

        ``incremental_since_unix=None`` 时与旧逻辑等价：iter_images 全扫，
        diff 在整套 cached entries 上计算。

        ``incremental_since_unix=T`` 时：
          - iter_images 跳过 mtime <= T 的目录（不 yield 它的文件）
          - changed_directories = 实际 yield 出来的文件的 parent dir 集合
          - removed 只在 changed_directories 范围内算（未扫描的目录其 cached 条目仍有效）
        """
        paths = iter_images(
            roots,
            max_depth=max_depth,
            suffixes=SUPPORTED_IMAGE_SUFFIXES,
            should_stop=should_stop,
            skip_directories_unchanged_since=incremental_since_unix,
        )
        if should_stop and should_stop():
            return ImageIndexUpdate(cancelled=True)
        current: dict[str, tuple[ImageIndexEntry, int, int]] = {}
        for path in paths:
            if should_stop and should_stop():
                return ImageIndexUpdate(cancelled=True)
            try:
                stat = path.stat()
            except OSError:
                continue
            current[_dedupe_key(path)] = (_entry_from_path(path), int(stat.st_size), int(stat.st_mtime_ns))
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                old_rows = conn.execute(
                    "SELECT path, size, mtime_ns FROM entries WHERE scope_key = ?", (scope_key,)
                ).fetchall()
                existing = {str(path): (int(size), int(mtime_ns)) for path, size, mtime_ns in old_rows}
                current_by_path = {
                    str(entry.path): (entry, size, mtime_ns)
                    for entry, size, mtime_ns in current.values()
                }
                # plan v0.10.4 I2: 增量模式下 removed 仅在"本次扫到的父目录"范围内算
                # 否则未扫的目录（mtime 未变）会被误判为整体消失
                if incremental_since_unix is not None:
                    changed_directories = {str(Path(p).parent) for p in current_by_path}
                    relevant_existing_paths = {
                        p for p in existing if str(Path(p).parent) in changed_directories
                    }
                    removed = relevant_existing_paths.difference(current_by_path)
                else:
                    removed = set(existing).difference(current_by_path)
                added = set(current_by_path).difference(existing)
                changed = {
                    path
                    for path in set(existing).intersection(current_by_path)
                    if existing[path] != current_by_path[path][1:]
                }
                for path in removed | changed:
                    conn.execute("DELETE FROM tokens WHERE scope_key = ? AND path = ?", (scope_key, path))
                    conn.execute("DELETE FROM entries WHERE scope_key = ? AND path = ?", (scope_key, path))
                for path in added | changed:
                    entry, size, mtime_ns = current_by_path[path]
                    self._insert_entry(conn, scope_key, entry, size, mtime_ns)
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
                    (scope_key, roots_json, max_depth, time.time()),
                )
            return ImageIndexUpdate(
                scanned=len(current_by_path),
                added=len(added),
                removed=len(removed),
                changed=len(changed),
            )
        except sqlite3.Error:
            return ImageIndexUpdate(cancelled=True)

    def upsert_paths(
        self,
        roots: list[Path | str],
        paths: Iterable[Path | str],
        max_depth: int = 0,
    ) -> int:
        if not self.has_scope(roots, max_depth):
            return 0
        scope_key = self.scope_key(roots, max_depth)
        count = 0
        try:
            with self._connection() as conn:
                for raw_path in paths:
                    path = Path(raw_path).resolve()
                    if not path.is_file() or not _path_in_index_scope(path, [Path(root).resolve() for root in roots], max_depth):
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
                    conn.execute("DELETE FROM tokens WHERE scope_key = ? AND path = ?", (scope_key, str(path)))
                    conn.execute("DELETE FROM entries WHERE scope_key = ? AND path = ?", (scope_key, str(path)))
                    self._insert_entry(conn, scope_key, _entry_from_path(path), int(stat.st_size), int(stat.st_mtime_ns))
                    if prior is None:
                        count += 1
            return count
        except (OSError, sqlite3.Error):
            return 0

    def entries(self, roots: list[Path | str], max_depth: int = 0) -> list[ImageIndexEntry]:
        if not self.path.exists():
            return []
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                rows = conn.execute(
                    "SELECT path, file_name, stem, suffix FROM entries WHERE scope_key = ?", (scope_key,)
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            ImageIndexEntry(path=Path(path), file_name=str(name), stem=str(stem), suffix=str(suffix))
            for path, name, stem, suffix in rows
        ]

    def search_entries(
        self,
        roots: list[Path | str],
        query: str,
        limit: int,
        max_depth: int = 0,
    ) -> tuple[list[ImageIndexEntry], str, int]:
        query_tokens = ImageSearchIndex._tokenize(query)
        if not query_tokens or not self.path.exists():
            return [], query, 100
        scope_key = self.scope_key(roots, max_depth)
        candidate_limit = max(2000, limit * 20)
        try:
            with self._connection() as conn:
                current_tokens = list(query_tokens)
                while current_tokens:
                    rows = self._query_tokens(conn, scope_key, current_tokens, candidate_limit)
                    entries = [self._row_entry(row) for row in rows]
                    entries = [entry for entry in entries if self._verify_entry(entry, current_tokens)]
                    if entries:
                        entries.sort(key=lambda entry: natural_sort_key(entry.file_name))
                        matched_query = query if current_tokens == query_tokens else "-".join(current_tokens)
                        return entries[: limit * 3], matched_query, 100
                    if len(current_tokens) <= 1:
                        break
                    current_tokens.pop()
                needle = query.lower().strip()
                like = f"%{needle}%"
                rows = conn.execute(
                    """
                    SELECT path, file_name, stem, suffix FROM entries
                    WHERE scope_key = ? AND (lower(file_name) LIKE ? OR lower(stem) LIKE ?)
                    ORDER BY lower(file_name), file_name LIMIT ?
                    """,
                    (scope_key, like, like, candidate_limit),
                ).fetchall()
        except sqlite3.Error:
            return [], query, 100
        entries = [self._row_entry(row) for row in rows]
        entries.sort(key=lambda entry: natural_sort_key(entry.file_name))
        return entries[: limit * 3], query, 60

    def clear_scope(self, roots: list[Path | str], max_depth: int = 0) -> None:
        if not self.path.exists():
            return
        scope_key = self.scope_key(roots, max_depth)
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM tokens WHERE scope_key = ?", (scope_key,))
                conn.execute("DELETE FROM entries WHERE scope_key = ?", (scope_key,))
                conn.execute("DELETE FROM scopes WHERE scope_key = ?", (scope_key,))
        except sqlite3.Error:
            return

    @staticmethod
    def _insert_entry(
        conn: sqlite3.Connection, scope_key: str, entry: ImageIndexEntry, size: int, mtime_ns: int
    ) -> None:
        path = str(entry.path)
        conn.execute(
            "INSERT INTO entries(scope_key, path, file_name, stem, suffix, size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope_key, path, entry.file_name, entry.stem, entry.suffix, size, mtime_ns),
        )
        prefixes = {
            prefix
            for token in ImageSearchIndex._tokenize(entry.stem)
            for prefix in ImageSearchIndex._prefixes(token)
        }
        conn.executemany(
            "INSERT OR IGNORE INTO tokens(scope_key, token, path) VALUES (?, ?, ?)",
            [(scope_key, prefix, path) for prefix in prefixes],
        )

    @staticmethod
    def _row_entry(row: tuple[str, str, str, str]) -> ImageIndexEntry:
        return ImageIndexEntry(path=Path(row[0]), file_name=row[1], stem=row[2], suffix=row[3])

    @staticmethod
    def _verify_entry(entry: ImageIndexEntry, query_tokens: list[str]) -> bool:
        stem_tokens = ImageSearchIndex._tokenize(entry.stem)
        for start in range(len(stem_tokens) - len(query_tokens) + 1):
            if all(
                stem_tokens[start + offset].startswith(token)
                and not stem_tokens[start + offset][len(token):].isdigit()
                for offset, token in enumerate(query_tokens)
            ):
                return True
        return False

    @staticmethod
    def _query_tokens(
        conn: sqlite3.Connection, scope_key: str, tokens: list[str], limit: int
    ) -> list[tuple[str, str, str, str]]:
        joins = " ".join(
            f"JOIN tokens t{i} ON t{i}.scope_key = e.scope_key AND t{i}.path = e.path AND t{i}.token = ?"
            for i in range(len(tokens))
        )
        sql = (
            f"SELECT e.path, e.file_name, e.stem, e.suffix FROM entries e {joins} "
            "WHERE e.scope_key = ? ORDER BY lower(e.file_name), e.file_name LIMIT ?"
        )
        return conn.execute(sql, (*tokens, scope_key, limit)).fetchall()


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


def iter_images(
    roots: list[Path | str],
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    name_pattern: re.Pattern[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    skip_directories_unchanged_since: float | None = None,
) -> list[Path]:
    """遍历 ``roots`` 下匹配 ``suffixes`` 的图片文件。

    plan v0.10.4 I1：增量模式 ``skip_directories_unchanged_since``——
      - None → 全扫（首次 / 强制重建走此路径，行为不变）
      - 非 None → 走目录前对比 ``dir.stat().st_mtime``：
        · dir mtime > since → 该目录可能有新增/删除/重命名，yield 所有匹配文件
        · dir mtime <= since → 该目录子项未变（ext4/NTFS 子项增删 rename 才 bump dir mtime），
          不 yield 该层文件；**仍 recurse 进 subdirs**（subdir mtime 可能 > since）

    工作区里"修改照片内容"极罕见（照片基本只读），所以"父目录 mtime 不变"≈
    "该目录直接子项无变化"。漏判文件内容修改是 acceptable trade-off。
    """
    allowed_suffixes = normalize_suffixes(suffixes) if suffixes is not None else SUPPORTED_IMAGE_SUFFIXES
    seen: set[str] = set()
    results: list[Path] = []
    for root in roots:
        if should_stop and should_stop():
            break
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            continue
        is_root_fs = root_path == Path("/")
        for current, dir_names, file_names in os.walk(root_path):
            if should_stop and should_stop():
                dir_names.clear()
                break
            current_path = Path(current)
            if max_depth > 0:
                depth = len(current_path.relative_to(root_path).parts)
                if depth > max_depth:
                    dir_names.clear()
                    continue
            if is_root_fs:
                dir_names[:] = [d for d in dir_names if d not in EXCLUDED_SYSTEM_DIRS]
            dir_names[:] = [
                d for d in dir_names
                if d not in EXCLUDED_DIR_NAMES and not is_excluded_path(current_path / d, root_path)
            ]
            # plan v0.10.4 I1: 目录 mtime 门控；未变目录跳过文件 yield 但保留递归
            if skip_directories_unchanged_since is not None:
                try:
                    directory_mtime = current_path.stat().st_mtime
                except OSError:
                    directory_mtime = float("inf")  # 取不到 mtime 时保守全扫
                if directory_mtime <= skip_directories_unchanged_since:
                    continue  # subdir 由 os.walk 自动递归处理；当前 dir 文件视为缓存仍有效
            for fn in file_names:
                if should_stop and should_stop():
                    break
                path = current_path / fn
                if path.suffix.lower() not in allowed_suffixes:
                    continue
                if name_pattern is not None and not name_pattern.match(path.stem):
                    continue
                dedupe_key = os.path.normcase(os.path.abspath(os.fspath(path)))
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    results.append(path)
    return sorted(results, key=lambda p: natural_sort_key(p.name))


def indexed_images(
    roots: list[Path | str],
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    force_rebuild: bool = False,
    cache_root: Path | str | None = None,
) -> list[Path]:
    return [
        entry.path
        for entry in indexed_image_entries(
            roots,
            max_depth=max_depth,
            suffixes=suffixes,
            should_stop=should_stop,
            force_rebuild=force_rebuild,
            cache_root=cache_root,
        )
    ]


def indexed_image_entries(
    roots: list[Path | str],
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    force_rebuild: bool = False,
    cache_root: Path | str | None = None,
) -> list[ImageIndexEntry]:
    allowed_suffixes = normalize_suffixes(suffixes) if suffixes is not None else SUPPORTED_IMAGE_SUFFIXES
    key = _image_index_key(roots, max_depth)
    with _IMAGE_INDEX_LOCK:
        if force_rebuild:
            _IMAGE_INDEX_CACHE.pop(key, None)
        elif key in _IMAGE_INDEX_CACHE:
            _IMAGE_INDEX_CACHE.move_to_end(key)
            return _filter_index_entries(_IMAGE_INDEX_CACHE[key], allowed_suffixes, should_stop)

    if cache_root is not None:
        store = ImageIndexStore(cache_root)
        if force_rebuild or not store.has_scope(roots, max_depth):
            update = store.reconcile_scope(roots, max_depth, should_stop)
            if update.cancelled:
                return []
        entries = store.entries(roots, max_depth)
        with _IMAGE_INDEX_LOCK:
            _remember_image_index(key, entries)
        return _filter_index_entries(entries, allowed_suffixes, should_stop)

    entries = _entries_from_paths(iter_images(
        roots,
        max_depth=max_depth,
        suffixes=SUPPORTED_IMAGE_SUFFIXES,
        should_stop=should_stop,
    ))
    if should_stop and should_stop():
        return []
    with _IMAGE_INDEX_LOCK:
        _remember_image_index(key, entries)
    return _filter_index_entries(entries, allowed_suffixes, should_stop)


def image_index_exists(
    root: Path | str,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> bool:
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, roots, max_depth)
    key = _image_index_key(roots, effective_depth)
    with _IMAGE_INDEX_LOCK:
        if key in _IMAGE_INDEX_CACHE:
            return True
    return ImageIndexStore(workspace).has_scope(roots, effective_depth)


def get_image_index_last_scan_timestamp(
    root: Path | str,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> float | None:
    """plan v0.10.4 I3：UI 层读取上次完成 reconcile 的时间，决定是否短路。

    纯只读，不触发任何 walk。``None`` = 从未扫描过 / 索引文件不存在。
    """
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, roots, max_depth)
    if not roots:
        return None
    return ImageIndexStore(workspace).get_scope_last_scan_timestamp(roots, effective_depth)


def reconcile_image_index(
    root: Path | str,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
    should_stop: Callable[[], bool] | None = None,
    incremental_since_unix: float | None = None,
    force_full_scan: bool = False,
) -> ImageIndexUpdate:
    """Check one search scope for external additions, deletes and renames.

    plan v0.10.4 I2：``incremental_since_unix`` 透传给 reconcile_scope，走目录级
    mtime 门控。本函数默认**自动增量**：如果该 scope 已有 ``last_scan`` 记录，
    把它当 incremental_since 用；首次扫描（无 last_scan）走全扫。
    ``force_full_scan=True`` 时绕过自动增量，全扫——「强制重建」按钮走这条路径。
    显式传 ``incremental_since_unix`` 时优先用调用方给的值。
    """
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, roots, max_depth)
    if not roots:
        return ImageIndexUpdate()
    store = ImageIndexStore(workspace)
    effective_incremental_since = incremental_since_unix
    if effective_incremental_since is None and not force_full_scan:
        effective_incremental_since = store.get_scope_last_scan_timestamp(roots, effective_depth)
    update = store.reconcile_scope(
        roots,
        effective_depth,
        should_stop,
        incremental_since_unix=effective_incremental_since,
    )
    if update.modified:
        key = _image_index_key(roots, effective_depth)
        with _IMAGE_INDEX_LOCK:
            _IMAGE_INDEX_CACHE.pop(key, None)
        with _SEARCH_INDEX_LOCK:
            _SEARCH_INDEX_CACHE.pop(key, None)
    return update


def append_images_to_index(
    root: Path | str,
    image_paths: Iterable[Path | str],
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> int:
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, roots, max_depth)
    key = _image_index_key(roots, effective_depth)
    count = ImageIndexStore(workspace).upsert_paths(roots, image_paths, effective_depth)
    if not count:
        return 0
    with _IMAGE_INDEX_LOCK:
        _IMAGE_INDEX_CACHE.pop(key, None)
    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_CACHE.pop(key, None)
    return count


def _image_index_key(roots: list[Path | str], max_depth: int) -> tuple[tuple[str, ...], int]:
    return (
        tuple(str(Path(root).resolve()) for root in roots),
        max_depth,
    )


def _remember_image_index(key: tuple[tuple[str, ...], int], entries: list[ImageIndexEntry]) -> None:
    _IMAGE_INDEX_CACHE[key] = entries
    _IMAGE_INDEX_CACHE.move_to_end(key)
    while len(_IMAGE_INDEX_CACHE) > IMAGE_INDEX_CACHE_LIMIT:
        _IMAGE_INDEX_CACHE.popitem(last=False)


def _filter_index_entries(
    entries: Iterable[ImageIndexEntry],
    allowed_suffixes: set[str],
    should_stop: Callable[[], bool] | None = None,
) -> list[ImageIndexEntry]:
    filtered: list[ImageIndexEntry] = []
    for entry in entries:
        if should_stop and should_stop():
            break
        if entry.suffix in allowed_suffixes:
            filtered.append(entry)
    return filtered


def _entry_from_path(path: Path) -> ImageIndexEntry:
    return ImageIndexEntry(
        path=path,
        file_name=path.name,
        stem=path.stem,
        suffix=path.suffix.lower(),
    )


def _entries_from_paths(paths: Iterable[Path]) -> list[ImageIndexEntry]:
    return [_entry_from_path(path) for path in paths]


def _path_in_index_scope(path: Path, roots: list[Path], max_depth: int) -> bool:
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


def _dedupe_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _image_index_disk_path(cache_root: Path | str, key: tuple[tuple[str, ...], int]) -> Path:
    """Location of pre-SQLite JSON caches, retained only for explicit cleanup."""
    payload = json.dumps({"roots": key[0], "max_depth": key[1]}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return Path(cache_root).resolve() / "数据" / IMAGE_INDEX_CACHE_DIR_NAME / f"{digest}.json"


def clear_image_index(
    root: Path | str | None = None,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> None:
    with _IMAGE_INDEX_LOCK:
        _IMAGE_INDEX_CACHE.clear()
    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_CACHE.clear()
    if root is None:
        return
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, roots, max_depth)
    ImageIndexStore(workspace).clear_scope(roots, effective_depth)
    old_path = _image_index_disk_path(workspace, _image_index_key(roots, effective_depth))
    try:
        old_path.unlink(missing_ok=True)
    except OSError:
        pass


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


def image_search_results(
    root: Path | str,
    voucher: str,
    specimen: Row | None,
    classification: Row | None,
    linked_paths: Iterable[Path],
    query: str = "",
    limit: int = 50,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
    suffixes: Iterable[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    search_index: ImageSearchIndex | None = None,
    force_rebuild: bool = False,
    path_to_vouchers: dict[str, list[str]] | None = None,
    canonical_photo_paths: dict[str, str] | None = None,
) -> list[ImageSearchResult]:
    workspace = Path(root).resolve()
    query = query.strip()
    if not query:
        return []

    all_roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, all_roots, max_depth)
    allowed_suffixes = suffixes if suffixes is not None else TIF_IMAGE_SUFFIXES

    key = _image_index_key(all_roots, effective_depth)
    index = search_index
    # 原代码直接复用界面启动时的索引；切换到整个工作区或自定义目录时会拿错范围，导致 A- 等新目录图片搜不到。
    if index is not None and index.source_key is not None and index.source_key != key:
        index = None
    if index is None:
        store = ImageIndexStore(workspace)
        if force_rebuild or not store.has_scope(all_roots, effective_depth):
            update = store.reconcile_scope(all_roots, effective_depth, should_stop)
            if update.cancelled:
                return []
        matched_entries, matched_query, matched_score = store.search_entries(
            all_roots, query, limit, effective_depth
        )
    else:
        matched_indices = index.search(query, limit=limit * 3)
        matched_query = query
        matched_score = 100
        if not matched_indices:
            # Progressive fallback: drop trailing tokens to find broader matches
            tokens = ImageSearchIndex._tokenize(query)
            while len(tokens) > 1 and not matched_indices:
                tokens.pop()
                matched_query = "-".join(tokens)
                matched_indices = index.search(matched_query, limit=limit * 3)
        if not matched_indices:
            # 保留原匹配语义：前缀无结果后再进行文件名包含匹配。
            matched_query = query
            matched_score = 60
            matched_indices = index.contains_search(query, limit=limit * 3)
        matched_entries = [index.entries[idx] for idx in matched_indices]
    if not matched_entries:
        return []

    linked = {str(path.resolve()) for path in linked_paths}
    canonical_by_path = {
        str(Path(path).resolve()): str(Path(canonical).resolve())
        for path, canonical in (canonical_photo_paths or {}).items()
    }
    matched_path_keys = {
        str(entry.path.resolve())
        for entry in matched_entries
        if entry.suffix in allowed_suffixes and entry.path.exists()
    }
    seen_canonical: set[str] = set()
    results: list[ImageSearchResult] = []
    for entry in matched_entries:
        if should_stop and should_stop():
            break
        if entry.suffix not in allowed_suffixes:
            continue
        path = entry.path
        if not path.exists():
            continue
        path_key = str(path.resolve())
        canonical_key = canonical_by_path.get(path_key, path_key)
        if path_key != canonical_key and canonical_key in matched_path_keys:
            continue
        if canonical_key in seen_canonical:
            continue
        seen_canonical.add(canonical_key)
        display_path = Path(canonical_key) if path_key != canonical_key and Path(canonical_key).exists() else path
        relative = relative_display(display_path, workspace)
        is_linked = canonical_key in linked or path_key in linked
        # 原代码：linked_vouchers 仅在 is_linked 为 True 时才查询，导致关联到
        # 其他标本的照片不显示入库编号。修复：无条件查询 path_to_vouchers，
        # 让用户看到所有关联到的入库编号（不仅是当前标本的）。
        vouchers = path_to_vouchers or {}
        linked_vouchers = (vouchers.get(canonical_key) or vouchers.get(path_key) or []) or None
        results.append(
            ImageSearchResult(
                path=display_path,
                relative_path=relative,
                file_name=display_path.name,
                score=matched_score,
                matched_keywords=(matched_query,),
                is_linked=is_linked,
                linked_vouchers=linked_vouchers,
            )
        )
        if len(results) >= limit:
            break
    return results


def _get_or_build_search_index(
    roots: list[Path],
    max_depth: int = 0,
    should_stop: Callable[[], bool] | None = None,
    force_rebuild: bool = False,
    cache_root: Path | str | None = None,
) -> ImageSearchIndex | None:
    """Compatibility API for callers requiring an in-memory index.

    Interactive UI searches query ImageIndexStore directly so large scopes are
    not materialised in RAM. Tests and callers passing an explicit index keep
    the historic object API.
    """
    key = _image_index_key(roots, max_depth)
    with _SEARCH_INDEX_LOCK:
        if force_rebuild:
            _SEARCH_INDEX_CACHE.pop(key, None)
        elif key in _SEARCH_INDEX_CACHE:
            _SEARCH_INDEX_CACHE.move_to_end(key)
            return _SEARCH_INDEX_CACHE[key]

    if cache_root is not None:
        store = ImageIndexStore(cache_root)
        if force_rebuild or not store.has_scope(roots, max_depth):
            update = store.reconcile_scope(roots, max_depth, should_stop)
            if update.cancelled:
                return None
        entries = store.entries(roots, max_depth)
    else:
        entries = _entries_from_paths(iter_images(
            roots,
            max_depth=max_depth,
            suffixes=SUPPORTED_IMAGE_SUFFIXES,
            should_stop=should_stop,
        ))
    if should_stop and should_stop():
        return None

    index = ImageSearchIndex()
    index.build(entries, source_key=key)
    with _SEARCH_INDEX_LOCK:
        _remember_search_index(key, index)
    return index


def _remember_search_index(
    key: tuple[tuple[str, ...], int], index: ImageSearchIndex
) -> None:
    _SEARCH_INDEX_CACHE[key] = index
    _SEARCH_INDEX_CACHE.move_to_end(key)
    while len(_SEARCH_INDEX_CACHE) > _SEARCH_INDEX_CACHE_LIMIT:
        _SEARCH_INDEX_CACHE.popitem(last=False)


def effective_image_search_depth(roots: list[Path | str], max_depth: int = 0) -> int:
    if any(Path(r).resolve() == Path("/") for r in roots) and max_depth == 0:
        return 4
    if any(Path(r).resolve() == Path("/") for r in roots):
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
        candidates = [Path(root).resolve() for root in extra_roots]
    else:
        photo_dir = workspace / "照片"
        candidates = [photo_dir if photo_dir.is_dir() else workspace]
    for candidate in candidates:
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def extract_core_identifier(value: str | object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in IDENTIFIER_SEPARATOR_RE.split(text) if part.strip()]
    if len(parts) < 3:
        return ""
    return "-".join(parts[:3])


def core_identifier_pattern(core_identifier: str) -> re.Pattern[str] | None:
    parts = [part.strip() for part in IDENTIFIER_SEPARATOR_RE.split(core_identifier) if part.strip()]
    if len(parts) < 3:
        return None
    pattern = "^" + r"[-_]".join(re.escape(part) for part in parts[:3]) + r"(?:[-_]|$)"
    return re.compile(pattern, re.IGNORECASE)


def natural_sort_key(value: str) -> list[tuple[int, object]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in NATURAL_SORT_RE.split(value)
        if part
    ]


def default_image_query(specimen: Row | None) -> str:
    tube_number = str((specimen or {}).get("管内编号*", "") or "").strip()
    return extract_core_identifier(tube_number)


def relative_display(path: Path | str, root: Path | str) -> str:
    file_path = Path(path).resolve()
    workspace = Path(root).resolve()
    try:
        return "./" + file_path.relative_to(workspace).as_posix()
    except ValueError:
        return file_path.as_posix()
