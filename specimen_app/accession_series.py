from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AccessionSeries:
    name: str
    prefix: str
    digits: int = 6
    separator: str = "-"
    year_pos: str = "none"  # "none" | "before" | "after"
    next_counter: int = 1
    step: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccessionSeries:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def format_series_number(series: AccessionSeries, counter: int | None = None) -> str:
    """生成一个编号字符串，逻辑同 accession_number_tool.py:build_number()。"""
    year = datetime.now().year
    c = series.next_counter if counter is None else counter
    num = str(c).zfill(series.digits)
    sep = series.separator

    if series.year_pos == "before":
        parts = [str(year), series.prefix, num]
    elif series.year_pos == "after":
        parts = [series.prefix, str(year), num]
    else:
        parts = [series.prefix, num]

    if sep:
        return sep.join(parts)
    return "".join(parts)


def series_prefix_of(voucher: str) -> str:
    """从编号字符串提取前缀，用于按系列筛选。取第一个分隔符前的字母段。"""
    for sep in ("-", ".", "/", "_"):
        if sep in voucher:
            return voucher.split(sep)[0]
    # 无分隔符：取开头连续字母
    prefix = ""
    for ch in voucher:
        if ch.isalpha():
            prefix += ch
        else:
            break
    return prefix


# 内置预设——仅为格式模板，非各机构精确官方规范，用户可在此基础上调整。
BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "label": "大英自然历史博物馆 BMNH",
        "prefix": "BMNH", "digits": 6, "separator": ".", "year_pos": "none",
    },
    {
        "label": "美国自然历史博物馆 AMNH",
        "prefix": "AMNH", "digits": 6, "separator": "-", "year_pos": "none",
    },
    {
        "label": "中科院动物研究所 IZCAS",
        "prefix": "IZCAS", "digits": 6, "separator": "-", "year_pos": "none",
    },
    {
        "label": "史密森学会 USNM",
        "prefix": "USNM", "digits": 6, "separator": " ", "year_pos": "none",
    },
    {
        "label": "年份前置通用",
        "prefix": "PREFIX", "digits": 6, "separator": "-", "year_pos": "before",
    },
    {
        "label": "年份后置通用",
        "prefix": "PREFIX", "digits": 6, "separator": "-", "year_pos": "after",
    },
    {
        "label": "斜线分隔通用",
        "prefix": "PREFIX", "digits": 5, "separator": "/", "year_pos": "before",
    },
    {
        "label": "无分隔通用",
        "prefix": "PREFIX", "digits": 8, "separator": "", "year_pos": "none",
    },
]
