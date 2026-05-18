from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
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
# 新增格式和通用兜底检索后，旧磁盘索引需要失效重建。
IMAGE_INDEX_DISK_VERSION = 4
IMAGE_INDEX_CACHE_DIR_NAME = "图片搜索索引缓存"
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
) -> list[Path]:
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

    if cache_root is not None and not force_rebuild:
        disk_entries = _load_image_index_from_disk(cache_root, key)
        if disk_entries is not None:
            with _IMAGE_INDEX_LOCK:
                _remember_image_index(key, disk_entries)
            return _filter_index_entries(disk_entries, allowed_suffixes, should_stop)

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
    if cache_root is not None:
        _save_image_index_to_disk(cache_root, key, entries)
    return _filter_index_entries(entries, allowed_suffixes, should_stop)


def image_index_exists(
    root: Path | str,
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> bool:
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = effective_image_search_depth(roots, max_depth)
    key = _image_index_key(roots, effective_depth)
    with _IMAGE_INDEX_LOCK:
        if key in _IMAGE_INDEX_CACHE:
            return True
    return _image_index_disk_version_matches(_image_index_disk_path(workspace, key))


def append_images_to_index(
    root: Path | str,
    image_paths: Iterable[Path | str],
    extra_roots: list[Path | str] | None = None,
    max_depth: int = 0,
) -> int:
    workspace = Path(root).resolve()
    roots = image_search_roots(workspace, extra_roots)
    effective_depth = effective_image_search_depth(roots, max_depth)
    key = _image_index_key(roots, effective_depth)

    with _IMAGE_INDEX_LOCK:
        existing_entries = _IMAGE_INDEX_CACHE.get(key)
    if existing_entries is None:
        existing_entries = _load_image_index_from_disk(workspace, key)
    if existing_entries is None:
        return 0

    existing_by_key = {_dedupe_key(entry.path) for entry in existing_entries}
    additions: list[ImageIndexEntry] = []
    for raw_path in image_paths:
        path = Path(raw_path).resolve()
        if not _path_in_index_scope(path, roots, effective_depth):
            continue
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        dedupe_key = _dedupe_key(path)
        if dedupe_key in existing_by_key:
            continue
        existing_by_key.add(dedupe_key)
        additions.append(_entry_from_path(path))

    if not additions:
        return 0

    updated = sorted([*existing_entries, *additions], key=lambda entry: natural_sort_key(entry.file_name))
    with _IMAGE_INDEX_LOCK:
        _remember_image_index(key, updated)
    _save_image_index_to_disk(workspace, key, updated)
    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_CACHE.pop(key, None)
    return len(additions)


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
    payload = json.dumps({"roots": key[0], "max_depth": key[1]}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return Path(cache_root).resolve() / "数据" / IMAGE_INDEX_CACHE_DIR_NAME / f"{digest}.json"


def _image_index_disk_version_matches(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            prefix = handle.read(128)
    except OSError:
        return False
    return f'"version":{IMAGE_INDEX_DISK_VERSION}' in prefix.replace(" ", "")


def _load_image_index_from_disk(cache_root: Path | str, key: tuple[tuple[str, ...], int]) -> list[ImageIndexEntry] | None:
    path = _image_index_disk_path(cache_root, key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    try:
        if payload.get("version") != IMAGE_INDEX_DISK_VERSION:
            return None
        if tuple(payload.get("roots", ())) != key[0] or int(payload.get("max_depth", -1)) != key[1]:
            return None
        entries = []
        for item in payload.get("entries", []):
            raw_entry_path = str(item.get("path", ""))
            if not raw_entry_path:
                continue
            entry_path = Path(raw_entry_path)
            entries.append(
                ImageIndexEntry(
                    path=entry_path,
                    file_name=str(item.get("file_name") or entry_path.name),
                    stem=str(item.get("stem") or entry_path.stem),
                    suffix=str(item.get("suffix") or entry_path.suffix.lower()).lower(),
                )
            )
        return entries
    except (TypeError, ValueError):
        return None


def _save_image_index_to_disk(cache_root: Path | str, key: tuple[tuple[str, ...], int], entries: list[ImageIndexEntry]) -> None:
    path = _image_index_disk_path(cache_root, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # P1 审查修复:tmp 后缀加 uuid4 防同 PID 短期内多写竞争。
        import uuid as _uuid
        tmp = path.with_suffix(f".{os.getpid()}.{_uuid.uuid4().hex[:8]}.tmp")
        payload = {
            "version": IMAGE_INDEX_DISK_VERSION,
            "roots": list(key[0]),
            "max_depth": key[1],
            "entries": [
                {
                    "path": str(entry.path),
                    "file_name": entry.file_name,
                    "stem": entry.stem,
                    "suffix": entry.suffix,
                }
                for entry in entries
            ],
        }
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)
    except OSError:
        return


def clear_image_index() -> None:
    with _IMAGE_INDEX_LOCK:
        _IMAGE_INDEX_CACHE.clear()
    with _SEARCH_INDEX_LOCK:
        _SEARCH_INDEX_CACHE.clear()


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
) -> list[ImageSearchResult]:
    workspace = Path(root).resolve()
    query = query.strip()
    if not query:
        return []

    all_roots = image_search_roots(workspace, extra_roots)
    effective_depth = effective_image_search_depth(all_roots, max_depth)
    allowed_suffixes = suffixes if suffixes is not None else TIF_IMAGE_SUFFIXES

    key = _image_index_key(all_roots, effective_depth)
    index = search_index
    # 原代码直接复用界面启动时的索引；切换到整个工作区或自定义目录时会拿错范围，导致 A- 等新目录图片搜不到。
    if index is not None and index.source_key is not None and index.source_key != key:
        index = None
    if index is None or force_rebuild:
        index = _get_or_build_search_index(
            all_roots,
            max_depth=effective_depth,
            should_stop=should_stop,
            force_rebuild=force_rebuild,
            cache_root=workspace,
        )
    if index is None:
        return []

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
        # 原代码只支持按分隔符 token 前缀检索；这里保留原逻辑，找不到时再按完整文件名做通用包含匹配。
        matched_query = query
        matched_score = 60
        matched_indices = index.contains_search(query, limit=limit * 3)
    if not matched_indices:
        return []

    linked = {path.resolve() for path in linked_paths}
    results: list[ImageSearchResult] = []
    for idx in matched_indices:
        if should_stop and should_stop():
            break
        entry = index.entries[idx]
        if entry.suffix not in allowed_suffixes:
            continue
        path = entry.path
        if not path.exists():
            continue
        relative = relative_display(path, workspace)
        is_linked = path.resolve() in linked
        # 原代码：linked_vouchers 仅在 is_linked 为 True 时才查询，导致关联到
        # 其他标本的照片不显示入库编号。修复：无条件查询 path_to_vouchers，
        # 让用户看到所有关联到的入库编号（不仅是当前标本的）。
        linked_vouchers = (path_to_vouchers or {}).get(str(path.resolve()), []) or None
        results.append(
            ImageSearchResult(
                path=path,
                relative_path=relative,
                file_name=entry.file_name,
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
    """Return a cached ImageSearchIndex, building it if necessary."""
    key = _image_index_key(roots, max_depth)
    with _SEARCH_INDEX_LOCK:
        if force_rebuild:
            _SEARCH_INDEX_CACHE.pop(key, None)
        elif key in _SEARCH_INDEX_CACHE:
            _SEARCH_INDEX_CACHE.move_to_end(key)
            return _SEARCH_INDEX_CACHE[key]

    if cache_root is not None and not force_rebuild:
        disk_index = _load_search_index_from_disk(cache_root, key)
        if disk_index is not None:
            with _SEARCH_INDEX_LOCK:
                _remember_search_index(key, disk_index)
            return disk_index

    entries = indexed_image_entries(
        roots,
        max_depth=max_depth,
        suffixes=SUPPORTED_IMAGE_SUFFIXES,
        should_stop=should_stop,
        force_rebuild=force_rebuild,
        cache_root=cache_root,
    )
    if should_stop and should_stop():
        return None

    index = ImageSearchIndex()
    index.build(entries, source_key=key)
    with _SEARCH_INDEX_LOCK:
        _remember_search_index(key, index)
    if cache_root is not None:
        _save_search_index_to_disk(cache_root, key, index)
    return index


def _remember_search_index(
    key: tuple[tuple[str, ...], int], index: ImageSearchIndex
) -> None:
    _SEARCH_INDEX_CACHE[key] = index
    _SEARCH_INDEX_CACHE.move_to_end(key)
    while len(_SEARCH_INDEX_CACHE) > _SEARCH_INDEX_CACHE_LIMIT:
        _SEARCH_INDEX_CACHE.popitem(last=False)


def _save_search_index_to_disk(
    cache_root: Path | str,
    key: tuple[tuple[str, ...], int],
    index: ImageSearchIndex,
) -> None:
    path = _image_index_disk_path(cache_root, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # P1 审查修复:tmp 后缀加 uuid4 防同 PID 短期内多写竞争。
        import uuid as _uuid
        tmp = path.with_suffix(f".{os.getpid()}.{_uuid.uuid4().hex[:8]}.tmp")
        payload = {
            "version": IMAGE_INDEX_DISK_VERSION,
            "roots": list(key[0]),
            "max_depth": key[1],
            "entries": [
                {
                    "path": str(entry.path),
                    "file_name": entry.file_name,
                    "stem": entry.stem,
                    "suffix": entry.suffix,
                }
                for entry in index.entries
            ],
            "token_index": index.to_dict(),
        }
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)
    except OSError:
        return


def _load_search_index_from_disk(
    cache_root: Path | str,
    key: tuple[tuple[str, ...], int],
) -> ImageSearchIndex | None:
    path = _image_index_disk_path(cache_root, key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    token_data = payload.get("token_index")
    if not token_data or payload.get("version") != IMAGE_INDEX_DISK_VERSION:
        return None
    if tuple(payload.get("roots", ())) != key[0] or int(payload.get("max_depth", -1)) != key[1]:
        return None
    entries = []
    for item in payload.get("entries", []):
        raw_entry_path = str(item.get("path", ""))
        if not raw_entry_path:
            continue
        entry_path = Path(raw_entry_path)
        entries.append(
            ImageIndexEntry(
                path=entry_path,
                file_name=str(item.get("file_name") or entry_path.name),
                stem=str(item.get("stem") or entry_path.stem),
                suffix=str(item.get("suffix") or entry_path.suffix.lower()).lower(),
            )
        )
    return ImageSearchIndex.from_dict(entries, token_data, source_key=key)


def effective_image_search_depth(roots: list[Path | str], max_depth: int = 0) -> int:
    if any(Path(r).resolve() == Path("/") for r in roots) and max_depth == 0:
        return 4
    if any(Path(r).resolve() == Path("/") for r in roots):
        return min(max_depth, 4)
    return max_depth


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
