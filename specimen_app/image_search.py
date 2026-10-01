"""image_search.py — 图片检索门面（对外 API 不变；实现已拆成两个模块）。

2026-10-01 模块化（用户："把构建索引单独做成一个模块"）：
  * ``image_index.py``  目录扫描 + SQLite 索引（v2：一行/文件，无 tokens 表，os.scandir）
  * ``image_match.py``  token 规则 + 相关度分层 + 精准/模糊模式（纯函数）
  * 本文件            作用域解析、内存缓存、结果装配（关联/别名折叠），以及所有旧 import 名

旧行为与为何改（详见两个子模块的模块注释）：
  * 旧 ``ImageIndexStore.search_entries`` 用 tokens 表做 JOIN，再 ``_verify_entry`` 校验；
    "字母 query + 数字余量 = 不匹配"的规则把 OWC→OWC001 也拒掉，导致退化到 gdlz-lzc 并按
    文件名排序。现改为 SQL 预筛候选 + ``rank_entries`` 打分（见 image_match 分层表）。
  * 旧 tokens 表让 20k 文件的索引达 541 MB / 43 s；v2 只存 entries（≈2 MB），旧库打开时
    自动 DROP tokens、不重扫。
  * ``ImageSearchResult`` 新增 ``match_kind``（exact/prefix/inner/any_order/contains/partial），
    ``matched_keywords`` 对真命中就是用户输入原文，只有 partial 才是退化后的前缀。
  * 新增 ``match_mode`` 参数（fuzzy 默认 = 旧行为超集；exact = 只要从头逐段命中的）。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .models import Row
from .image_index import (  # noqa: F401  (re-exported for backward compatibility)
    EXCLUDED_DIR_NAMES,
    EXCLUDED_PATH_PARTS,
    EXCLUDED_SYSTEM_DIRS,
    IMAGE_INDEX_CACHE_DIR_NAME,
    IMAGE_INDEX_SCHEMA_VERSION,
    IMAGE_INDEX_SQLITE_FILE_NAME,
    IMAGE_TYPE_SUFFIXES,
    JPG_IMAGE_SUFFIXES,
    SUPPORTED_IMAGE_SUFFIXES,
    SUPPORTED_IMAGE_SUFFIX_ORDER,
    TIF_IMAGE_SUFFIXES,
    TIF_JPG_IMAGE_SUFFIXES,
    ImageIndexEntry,
    ImageIndexStore,
    ImageIndexUpdate,
    ScannedFile,
    _dedupe_key,
    _entry_from_path,
    effective_image_search_depth,
    image_file_filter,
    image_index_key,
    image_search_depth,
    image_search_roots,
    is_excluded_path,
    is_supported_image,
    iter_images,
    iter_workspace_images,
    normalize_suffixes,
    path_in_index_scope,
    scan_image_files,
    suffixes_for_image_type,
)
from .image_match import (  # noqa: F401  (re-exported for backward compatibility)
    IDENTIFIER_SEPARATOR_RE,
    KIND_PARTIAL,
    MATCH_MODES,
    MATCH_MODE_EXACT,
    MATCH_MODE_FUZZY,
    NATURAL_SORT_RE,
    RankedEntry,
    candidate_needle,
    consecutive_match_level,
    natural_sort_key,
    normalize_match_mode,
    rank_entries,
    token_match_level,
    tokenize,
)

# 旧私有名（其他模块/测试可能引用）
_image_index_key = image_index_key
_path_in_index_scope = path_in_index_scope

IMAGE_INDEX_CACHE_LIMIT = 3
_IMAGE_INDEX_CACHE: OrderedDict[tuple[tuple[str, ...], int], list[ImageIndexEntry]] = OrderedDict()
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
    match_kind: str = ""  # image_match.KIND_*；"" = 旧调用方未填


class ImageSearchIndex:
    """内存版 token 前缀倒排索引（兼容 API；交互式检索走 ImageIndexStore，不把大作用域装进内存）。

    token 规则与磁盘路径共用 ``image_match``：旧 ``_verify_positions`` 的"数字余量即拒绝"
    已改为 ``token_match_level``（OWC 可命中 OWC001，SC004 仍不命中 SC0042）。
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
            for token in self._tokenize(entry.stem):
                for prefix in self._prefixes(token):
                    self._token_index.setdefault(prefix, set()).add(idx)

    def search(self, query: str, limit: int = 100) -> list[int]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []
        candidates: set[int] | None = None
        for token in query_tokens:
            matches = self._token_index.get(token)
            if not matches:
                return []
            candidates = set(matches) if candidates is None else candidates & matches
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
        # 旧：逐段 startswith + "余量全数字即拒绝"。现：image_match.token_match_level。
        return consecutive_match_level(self._tokenize(self._entries[entry_idx].stem), query_tokens) > 0

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return tokenize(text)

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


# ---------------------------------------------------------------------------
# 内存缓存 + 作用域级 API（与旧版相同）
# ---------------------------------------------------------------------------

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
    key = image_index_key(roots, max_depth)
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

    entries = _entries_from_paths(
        iter_images(roots, max_depth=max_depth, suffixes=SUPPORTED_IMAGE_SUFFIXES, should_stop=should_stop)
    )
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
    key = image_index_key(roots, effective_depth)
    with _IMAGE_INDEX_LOCK:
        if key in _IMAGE_INDEX_CACHE:
            return True
    return ImageIndexStore(workspace).has_scope(roots, effective_depth)


def get_image_index_last_scan_timestamp(
    root: Path | str,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> float | None:
    """plan v0.10.4 I3：UI 层读取上次完成 reconcile 的时间，决定是否短路。纯只读。"""
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
    """检查一个作用域的外部新增/删除/重命名（默认自动增量：有 last_scan 就按目录 mtime 门控）。"""
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
        key = image_index_key(roots, effective_depth)
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
    key = image_index_key(roots, effective_depth)
    count = ImageIndexStore(workspace).upsert_paths(roots, image_paths, effective_depth)
    if not count:
        return 0
    with _IMAGE_INDEX_LOCK:
        _IMAGE_INDEX_CACHE.pop(key, None)
    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_CACHE.pop(key, None)
    return count


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


def _entries_from_paths(paths: Iterable[Path]) -> list[ImageIndexEntry]:
    return [_entry_from_path(path) for path in paths]


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
    old_path = _image_index_disk_path(workspace, image_index_key(roots, effective_depth))
    try:
        old_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 检索：候选 → 打分排序 → 装配结果（关联状态 / 归档别名折叠）
# ---------------------------------------------------------------------------

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
    match_mode: str = MATCH_MODE_FUZZY,
) -> list[ImageSearchResult]:
    workspace = Path(root).resolve()
    query = query.strip()
    if not query:
        return []
    mode = normalize_match_mode(match_mode)

    all_roots = image_search_roots(workspace, extra_roots)
    effective_depth = image_search_depth(workspace, extra_roots, all_roots, max_depth)
    allowed_suffixes = set(suffixes) if suffixes is not None else TIF_IMAGE_SUFFIXES

    key = image_index_key(all_roots, effective_depth)
    index = search_index
    # 原代码直接复用界面启动时的索引；切换到整个工作区或自定义目录时会拿错范围，导致 A- 等新目录图片搜不到。
    if index is not None and index.source_key is not None and index.source_key != key:
        index = None
    candidate_limit = max(limit * 3, limit + 20)
    if index is None:
        store = ImageIndexStore(workspace)
        if force_rebuild or not store.has_scope(all_roots, effective_depth):
            update = store.reconcile_scope(all_roots, effective_depth, should_stop)
            if update.cancelled:
                return []
        # 旧：store.search_entries（tokens 表 JOIN + 渐进退化，取回 limit*3 条后才按后缀过滤，
        #     大量 JPG 会把 TIF 挤出候选）。现：SQL 按"首段子串 + 后缀"预筛，再统一打分。
        candidates = store.iter_candidates(all_roots, candidate_needle(query), allowed_suffixes, effective_depth)
        ranked = rank_entries(candidates, query, mode, limit=candidate_limit, should_stop=should_stop)
    else:
        # 旧：index.search → 逐段 pop 退化 → contains_search。现：同一套 rank_entries。
        candidates = [entry for entry in index.entries if entry.suffix in allowed_suffixes]
        ranked = rank_entries(candidates, query, mode, limit=candidate_limit, should_stop=should_stop)
    if not ranked:
        return []

    linked = {str(path.resolve()) for path in linked_paths}
    canonical_by_path = {
        str(Path(path).resolve()): str(Path(canonical).resolve())
        for path, canonical in (canonical_photo_paths or {}).items()
    }
    matched_path_keys = {str(item.entry.path.resolve()) for item in ranked if item.entry.path.exists()}
    seen_canonical: set[str] = set()
    results: list[ImageSearchResult] = []
    for item in ranked:
        if should_stop and should_stop():
            break
        entry = item.entry
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
                score=item.score,
                matched_keywords=(item.matched_query,),
                is_linked=is_linked,
                linked_vouchers=linked_vouchers,
                match_kind=item.kind,
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
    """Compatibility API for callers requiring an in-memory index."""
    key = image_index_key(roots, max_depth)
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
        entries = _entries_from_paths(
            iter_images(roots, max_depth=max_depth, suffixes=SUPPORTED_IMAGE_SUFFIXES, should_stop=should_stop)
        )
    if should_stop and should_stop():
        return None

    index = ImageSearchIndex()
    index.build(entries, source_key=key)
    with _SEARCH_INDEX_LOCK:
        _remember_search_index(key, index)
    return index


def _remember_search_index(key: tuple[tuple[str, ...], int], index: ImageSearchIndex) -> None:
    _SEARCH_INDEX_CACHE[key] = index
    _SEARCH_INDEX_CACHE.move_to_end(key)
    while len(_SEARCH_INDEX_CACHE) > _SEARCH_INDEX_CACHE_LIMIT:
        _SEARCH_INDEX_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# 核心编号（管内编号 → 默认查询词）
# ---------------------------------------------------------------------------

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
