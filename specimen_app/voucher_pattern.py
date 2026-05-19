"""入库编号规则推断 (规范化软件设计 2026-05 Phase 5)。

用户输入 ≥2 个 voucher 样本 → 推断 prefix + 数字位宽 + 增量 → 一键批量生成。

例:
    ['YZZ000001','YZZ000002'] → {prefix:'YZZ', width:6, start:1, step:1, next:3}
    ['QD-LSD-001','QD-LSD-002'] → {prefix:'QD-LSD-', width:3, start:1, step:1, next:3}
    ['A100','A102'] → {prefix:'A', width:3, start:100, step:2, next:104}
"""

from __future__ import annotations

import re
from typing import Optional


_TAIL_DIGITS_RE = re.compile(r"^(.*?)(\d+)$")


def _split_tail(s: str) -> Optional[tuple[str, str]]:
    """拆 'YZZ000001' → ('YZZ', '000001');无尾数字返 None。"""
    m = _TAIL_DIGITS_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def _longest_common_prefix(items: list[str]) -> str:
    if not items:
        return ""
    p = items[0]
    for s in items[1:]:
        # 截至 p 与 s 的最长公共前缀
        i = 0
        max_i = min(len(p), len(s))
        while i < max_i and p[i] == s[i]:
            i += 1
        p = p[:i]
        if not p:
            return ""
    return p


def infer_pattern(samples: list[str]) -> Optional[dict]:
    """从样本推断 voucher 规则。

    返回 dict (失败返 None):
        prefix: 公共前缀字符串
        width: 数字位宽 (用于 zero-pad)
        start: 第一个数字
        step:  增量 (默认 1, 若样本递增不均匀则 None)
        next:  下一个该用的数字 (max + step)
        examples: list[str] 用此规则前几项 (校验用)

    边界:
    - 少于 2 个样本 → None
    - 任一样本无尾数字 → None
    - 公共前缀为空 → None
    """
    cleaned = [s.strip() for s in samples if s and s.strip()]
    if len(cleaned) < 2:
        return None

    splits = [_split_tail(s) for s in cleaned]
    if any(sp is None for sp in splits):
        return None

    # 找最长公共前缀 (head 部分)
    heads = [sp[0] for sp in splits]
    common_prefix = _longest_common_prefix(heads)
    if not common_prefix:
        return None

    # 剩余部分是 "差异前缀 + 数字尾" — 把 head 公共部分剥掉,看剩余是不是纯数字结尾
    # 但用户样本可能是 ['YZZ000001','YZZ000002'] head=YZZ 共, 完全对齐。
    # 复杂情况 ['QD-LSD-SC001-1-R-250923', ...] 跳过 — 推不出按返 None。
    digits_list = []
    widths = []
    for sp in splits:
        head, digits = sp
        # 严格:head 必须完全等于公共前缀(否则混入其他段,推不出)
        if head != common_prefix:
            return None
        digits_list.append(int(digits))
        widths.append(len(digits))

    if not digits_list:
        return None

    # 位宽:全部样本数字位宽一致才用,否则取最大
    width = max(widths)
    if len(set(widths)) > 1:
        # 位宽不一致:可能用户混了 1, 10, 100 — 推断按最大位宽 zero-pad
        pass

    # 排序后计 step
    sorted_d = sorted(set(digits_list))
    if len(sorted_d) < 2:
        return None
    diffs = [sorted_d[i + 1] - sorted_d[i] for i in range(len(sorted_d) - 1)]
    step = diffs[0] if all(d == diffs[0] for d in diffs) else None
    if step is None or step <= 0:
        step = 1  # 推不出均匀 step,默认 1

    next_n = max(digits_list) + step
    return {
        "prefix": common_prefix,
        "width": width,
        "start": min(digits_list),
        "step": step,
        "next": next_n,
        "examples": [_format(common_prefix, width, n) for n in (sorted_d[:3] + [next_n])],
    }


def _format(prefix: str, width: int, n: int) -> str:
    return f"{prefix}{n:0{width}d}"


def generate_batch(pattern: dict, count: int, start_at: Optional[int] = None) -> list[str]:
    """按推断的 pattern 批量生成 count 个 voucher。

    start_at=None 时从 pattern['next'] 起;否则从指定数字起。
    """
    if not pattern or count <= 0:
        return []
    prefix = pattern["prefix"]
    width = pattern["width"]
    step = pattern.get("step") or 1
    n0 = pattern["next"] if start_at is None else int(start_at)
    return [_format(prefix, width, n0 + i * step) for i in range(count)]
