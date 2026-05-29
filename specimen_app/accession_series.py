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

    def is_custom(self) -> bool:
        """前缀为空 → 完全自定义系列：不自动生成编号，新增时手动输入。

        用户可建一个前缀/流水号留空的系列，表示"这批编号我自己手输"。
        激活这种系列后点「＋ 新增编号」会弹手动输入，而非自增。
        """
        return not (self.prefix or "").strip()


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

    # 过滤空段:前缀留空(完全自定义系列)时不产生悬空分隔符。
    parts = [p for p in parts if p]
    if sep:
        return sep.join(parts)
    return "".join(parts)


def extract_series_counter(voucher: str, series: "AccessionSeries") -> int | None:
    """从格式化编号字符串中提取流水号整数。失败返回 None。
    支持 year_pos = none / before / after 三种格式。
    供 ExcelStore._sync_all_series_counters() 使用。
    """
    import re
    sep = series.separator or ""
    prefix = series.prefix
    if series.year_pos == "before":
        # 格式：{YYYY}{sep}{prefix}{sep}{counter}
        pat = r"^\d{4}" + re.escape(sep) + re.escape(prefix) + re.escape(sep) + r"(\d+)$"
        m = re.match(pat, voucher)
        return int(m.group(1)) if m else None
    elif series.year_pos == "after":
        # 格式：{prefix}{sep}{YYYY}{sep}{counter}
        start = prefix + sep
        if not voucher.startswith(start):
            return None
        remainder = voucher[len(start):]
        parts = remainder.split(sep, 1) if sep else [remainder]
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                return None
        return None
    else:  # year_pos == "none"：{prefix}{sep}{counter}
        start = prefix + sep
        if not voucher.startswith(start):
            return None
        try:
            return int(voucher[len(start):])
        except ValueError:
            return None


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
