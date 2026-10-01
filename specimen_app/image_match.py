"""image_match.py — 图片文件名匹配与相关度排序（纯函数：无 Qt、无 sqlite、无 I/O）。

2026-10-01 从 image_search.py 拆出。用户报障：输入 ``GDLZ-LZC-OWC``，结果没按相关度排、
卡片「核心编号」只剩 ``gdlz-lzc``。根因（旧 ``ImageIndexStore._verify_entry`` /
``ImageSearchIndex._verify_positions``）：stem token 在 query token 之后只要剩下的全是
数字就判"不匹配"——本意是 ``WenSC004`` 不能命中 ``WenSC0042``，但用户敲站位码 ``OWC``
时 ``OWC001`` 也被同一条规则拒掉，于是三段全部失配 → 逐段退化到 ``gdlz-lzc`` →
整个断面的照片都命中，再按文件名排序，ONFC 排在 OWC 前面。

新规则（``token_match_level``）：
  * query token 以数字结尾（已是完整编号）→ 剩余部分不得再是数字（保留旧防线）；
  * query token 以字母结尾（站位码/前缀）→ 剩余部分是数字 = "编号延续"，算高分匹配。

相关度分层（分数越高越靠前，同分按文件名自然排序）：
  100 exact      所有 query token 从文件名开头起逐段完全相等
   95 prefix     从开头起逐段匹配，末段为编号延续（OWC → OWC001）
   93 prefix     从开头起逐段匹配，含普通前缀（OW → OWC001）         ［仅模糊］
   90 inner      逐段连续匹配，但不在文件名开头（图200-A-…）          ［仅模糊］
   85 any_order  每个 query token 都能前缀命中某个 stem token，不要求连续  ［仅模糊］
   60 contains   整个查询串作为子串出现（sampleP001middle）            ［仅模糊］
30–55 partial    以上全部落空时，逐段去掉末尾 token 重试（原"渐进退化"）  ［仅模糊］
                 只取能命中的最长前缀，分数 < 60，并把实际匹配到的前缀回报给 UI

模式：``fuzzy``（模糊，默认，旧行为的超集）/ ``exact``（精准：只保留 100/95）。
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol


IDENTIFIER_SEPARATOR_RE = re.compile(r"[-_]+")
NATURAL_SORT_RE = re.compile(r"(\d+)")

MATCH_MODE_FUZZY = "fuzzy"
MATCH_MODE_EXACT = "exact"
MATCH_MODES = (MATCH_MODE_FUZZY, MATCH_MODE_EXACT)
_MATCH_MODE_ALIASES = {
    "fuzzy": MATCH_MODE_FUZZY,
    "模糊": MATCH_MODE_FUZZY,
    "模糊匹配": MATCH_MODE_FUZZY,
    "exact": MATCH_MODE_EXACT,
    "strict": MATCH_MODE_EXACT,
    "精准": MATCH_MODE_EXACT,
    "精确": MATCH_MODE_EXACT,
    "精准匹配": MATCH_MODE_EXACT,
}

SCORE_EXACT = 100
SCORE_PREFIX_NUMBERED = 95
SCORE_PREFIX_GENERIC = 93
SCORE_INNER = 90
SCORE_ANY_ORDER = 85
SCORE_CONTAINS = 60
SCORE_PARTIAL_MAX = 55
SCORE_PARTIAL_MIN = 30

KIND_EXACT = "exact"
KIND_PREFIX = "prefix"
KIND_INNER = "inner"
KIND_ANY_ORDER = "any_order"
KIND_CONTAINS = "contains"
KIND_PARTIAL = "partial"

_STOP_CHECK_EVERY = 512


class _NamedEntry(Protocol):
    file_name: str
    stem: str


@dataclass(frozen=True)
class RankedEntry:
    """一条排好序的命中：``entry`` 是索引条目（ImageIndexEntry 或任何带 file_name/stem 的对象）。"""

    entry: object
    score: int
    kind: str
    matched_query: str


def normalize_match_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return _MATCH_MODE_ALIASES.get(text, MATCH_MODE_FUZZY)


def tokenize(text: object) -> list[str]:
    return [token.strip().lower() for token in IDENTIFIER_SEPARATOR_RE.split(str(text or "")) if token.strip()]


def natural_sort_key(value: str) -> list[tuple[int, object]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in NATURAL_SORT_RE.split(value)
        if part
    ]


def token_match_level(query_token: str, stem_token: str) -> int:
    """0 = 不匹配；1 = 普通前缀；2 = 编号延续（字母结尾的 query + 纯数字余量）；3 = 完全相等。"""
    if not query_token or not stem_token:
        return 0
    if stem_token == query_token:
        return 3
    if not stem_token.startswith(query_token):
        return 0
    remainder = stem_token[len(query_token):]
    if remainder.isdigit():
        # 旧规则：query 自身以数字结尾（已是完整编号）时，不允许数字继续延长（SC004 ≠ SC0042）
        return 0 if query_token[-1].isdigit() else 2
    return 1


def _best_window_level(stem_tokens: list[str], query_tokens: list[str], anchored: bool) -> int:
    """query_tokens 作为连续窗口落在 stem_tokens 上的最好匹配等级（取窗口内各段的最小等级）。"""
    count = len(query_tokens)
    if count == 0 or count > len(stem_tokens):
        return 0
    best = 0
    starts = range(1) if anchored else range(len(stem_tokens) - count + 1)
    for start in starts:
        level = 3
        for offset, query_token in enumerate(query_tokens):
            current = token_match_level(query_token, stem_tokens[start + offset])
            if current == 0:
                level = 0
                break
            if current < level:
                level = current
        if level > best:
            best = level
            if best == 3:
                break
    return best


def _any_order_match(stem_tokens: list[str], query_tokens: list[str]) -> bool:
    used: set[int] = set()
    for query_token in query_tokens:
        found = False
        for index, stem_token in enumerate(stem_tokens):
            if index in used:
                continue
            if token_match_level(query_token, stem_token) > 0:
                used.add(index)
                found = True
                break
        if not found:
            return False
    return True


def _contains_needles(query: str) -> tuple[str, ...]:
    raw = query.strip().lower()
    if not raw:
        return ()
    normalized = IDENTIFIER_SEPARATOR_RE.sub("-", raw)
    return tuple(dict.fromkeys([raw, normalized]))


def _contains_haystacks(file_name: str, stem: str) -> tuple[str, ...]:
    raw_name = file_name.lower()
    raw_stem = stem.lower()
    return tuple(
        dict.fromkeys(
            [raw_name, raw_stem, IDENTIFIER_SEPARATOR_RE.sub("-", raw_name), IDENTIFIER_SEPARATOR_RE.sub("-", raw_stem)]
        )
    )


def _contains_with_digit_guard(needle: str, haystack: str) -> bool:
    """子串命中，但数字不许被"延长"：query 以数字结尾时后一个字符不能是数字，以数字开头时前一个字符不能是数字。"""
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        tail_ok = not (needle[-1].isdigit() and end < len(haystack) and haystack[end].isdigit())
        head_ok = not (needle[0].isdigit() and index > 0 and haystack[index - 1].isdigit())
        if tail_ok and head_ok:
            return True
        start = index + 1


def score_full_query(stem_tokens: list[str], query_tokens: list[str], mode: str) -> tuple[int, str] | None:
    """整个查询（全部 token）对一个 stem 的命中分层；None = 未命中。"""
    anchored_level = _best_window_level(stem_tokens, query_tokens, anchored=True)
    if anchored_level == 3:
        return SCORE_EXACT, KIND_EXACT
    if anchored_level == 2:
        return SCORE_PREFIX_NUMBERED, KIND_PREFIX
    if mode == MATCH_MODE_EXACT:
        return None
    if anchored_level == 1:
        return SCORE_PREFIX_GENERIC, KIND_PREFIX
    if _best_window_level(stem_tokens, query_tokens, anchored=False) > 0:
        return SCORE_INNER, KIND_INNER
    if len(query_tokens) > 1 and _any_order_match(stem_tokens, query_tokens):
        return SCORE_ANY_ORDER, KIND_ANY_ORDER
    return None


def score_contains(query: str, file_name: str, stem: str) -> int | None:
    needles = _contains_needles(query)
    if not needles:
        return None
    haystacks = _contains_haystacks(file_name, stem)
    for needle in needles:
        for haystack in haystacks:
            if _contains_with_digit_guard(needle, haystack):
                return SCORE_CONTAINS
    return None


def partial_score(matched_tokens: int, total_tokens: int) -> int:
    if total_tokens <= 0:
        return SCORE_PARTIAL_MIN
    span = SCORE_PARTIAL_MAX - SCORE_PARTIAL_MIN
    return SCORE_PARTIAL_MIN + round(span * matched_tokens / total_tokens)


def candidate_needle(query: str) -> str:
    """SQL 预筛用的子串：query 的第一个 token（所有分层的命中都必然包含它）。"""
    tokens = tokenize(query)
    if tokens:
        return tokens[0]
    return query.strip().lower()


def rank_entries(
    entries: Iterable[_NamedEntry],
    query: str,
    mode: str = MATCH_MODE_FUZZY,
    limit: int = 50,
    should_stop: Callable[[], bool] | None = None,
) -> list[RankedEntry]:
    """对候选条目打分并取前 ``limit`` 条。

    选取顺序（渐进退化，与旧版一致但分数真实）：
      1. 全查询命中（100/95/93/90/85）与子串命中（60）合并，按分数排序；
      2. 一个都没有 → 逐段去掉末尾 token，取能命中的最长前缀（partial，<60）。
    精准模式只做第 1 步里的 100/95。
    """
    mode = normalize_match_mode(mode)
    query = str(query or "").strip()
    query_tokens = tokenize(query)
    if not query_tokens or limit <= 0:
        return []
    total = len(query_tokens)
    # partial 回报给 UI 的前缀保留用户输入的大小写/原文（只截段，不改写）
    typed_parts = [part for part in IDENTIFIER_SEPARATOR_RE.split(query) if part.strip()]
    full_hits: list[RankedEntry] = []
    partial_hits: dict[int, list[RankedEntry]] = {}
    best_partial = 0
    for position, entry in enumerate(entries):
        if should_stop and position % _STOP_CHECK_EVERY == 0 and should_stop():
            return []
        stem_tokens = tokenize(entry.stem)
        scored = score_full_query(stem_tokens, query_tokens, mode)
        if scored is not None:
            full_hits.append(RankedEntry(entry, scored[0], scored[1], query))
            continue
        if mode == MATCH_MODE_EXACT:
            continue
        contains = score_contains(query, entry.file_name, entry.stem)
        if contains is not None:
            full_hits.append(RankedEntry(entry, contains, KIND_CONTAINS, query))
            continue
        if full_hits or total <= 1:
            continue  # 已有真命中就不再找退化命中；单段查询无可退化
        # 从 n-1 段往下试到当前已知的最长退化长度（含）；更短的不再找（渐进退化只取最长命中）
        for count in range(total - 1, max(best_partial, 1) - 1, -1):
            if _best_window_level(stem_tokens, query_tokens[:count], anchored=False) > 0:
                if len(typed_parts) == total:
                    matched_query = "-".join(part.strip() for part in typed_parts[:count])
                else:
                    matched_query = "-".join(query_tokens[:count])
                partial_hits.setdefault(count, []).append(
                    RankedEntry(entry, partial_score(count, total), KIND_PARTIAL, matched_query)
                )
                if count > best_partial:
                    best_partial = count
                break
    if full_hits:
        pool = full_hits
    elif best_partial:
        pool = partial_hits.get(best_partial, [])
    else:
        return []
    return heapq.nsmallest(limit, pool, key=lambda item: (-item.score, natural_sort_key(item.entry.file_name)))


def consecutive_match_level(stem_tokens: list[str], query_tokens: list[str], anchored: bool = False) -> int:
    """公开版 ``_best_window_level``：供内存索引 ``ImageSearchIndex`` 复用同一套 token 规则。"""
    return _best_window_level(stem_tokens, query_tokens, anchored)
