#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
馆藏入库编号分配工具 (Python CLI 版)
自由配置前缀 · 位数 · 分隔符 · 年份 · 步长，兼容各大博物馆编号规范
"""

import json
import csv
import os
import sys
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import List, Optional


# ─────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────

@dataclass
class Config:
    prefix: str = "YZZ"
    digits: int = 6
    separator: str = "-"
    year_pos: str = "none"   # "none" | "before" | "after"
    counter: int = 1
    step: int = 1


@dataclass
class HistoryEntry:
    number: str
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


# ─────────────────────────────────────────
# 预设模板
# ─────────────────────────────────────────

PRESETS = [
    {"name": "YZZ 馆藏标准",    "prefix": "YZZ", "digits": 6, "separator": "-", "year_pos": "none"},
    {"name": "MBM 博物馆",      "prefix": "MBM", "digits": 5, "separator": "-", "year_pos": "none"},
    {"name": "年份前置",        "prefix": "BW",  "digits": 6, "separator": "-", "year_pos": "before"},
    {"name": "年份后置",        "prefix": "NH",  "digits": 6, "separator": "-", "year_pos": "after"},
    {"name": "大英博物馆风格",  "prefix": "BM",  "digits": 7, "separator": ".", "year_pos": "none"},
    {"name": "故宫无分隔",      "prefix": "GG",  "digits": 8, "separator": "", "year_pos": "none"},
    {"name": "斜线分隔",        "prefix": "ART", "digits": 5, "separator": "/", "year_pos": "before"},
]

# 年份位置说明
YEAR_POS_LABELS = {
    "none":   "不含年份",
    "before": "年份在前  (如 2025-YZZ-000001)",
    "after":  "年份在后  (如 YZZ-2025-000001)",
}

# 分隔符说明
SEP_OPTIONS = {
    "-":  "横线  -",
    ".":  "点   .",
    "/":  "斜线  /",
    "_":  "下划线 _",
    "":   "无分隔",
}

# 持久化文件路径（与脚本同目录）
_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(_BASE_DIR, "acc_config.json")
HISTORY_FILE   = os.path.join(_BASE_DIR, "acc_history.json")
MAX_HISTORY    = 500


# ─────────────────────────────────────────
# 核心逻辑
# ─────────────────────────────────────────

def build_number(config: Config, counter: Optional[int] = None) -> str:
    """根据配置生成一个编号。counter 若为 None 则使用 config.counter。"""
    year = datetime.now().year
    c    = config.counter if counter is None else counter
    num  = str(c).zfill(config.digits)
    sep  = config.separator

    if config.year_pos == "before":
        return f"{year}{sep}{config.prefix}{sep}{num}"
    elif config.year_pos == "after":
        return f"{config.prefix}{sep}{year}{sep}{num}"
    else:
        return f"{config.prefix}{sep}{num}"


def generate_one(config: Config) -> tuple[str, Config]:
    """生成单个编号并递增计数器，返回 (编号, 更新后的 config)。"""
    number = build_number(config)
    config.counter += config.step
    return number, config


def generate_batch(config: Config, batch_size: int) -> tuple[List[str], Config]:
    """批量生成编号并递增计数器，返回 (编号列表, 更新后的 config)。"""
    numbers: List[str] = []
    c = config.counter
    for _ in range(batch_size):
        numbers.append(build_number(config, counter=c))
        c += config.step
    config.counter = c
    return numbers, config


# ─────────────────────────────────────────
# 持久化
# ─────────────────────────────────────────

def load_config() -> Config:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(**data)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return Config()


def save_config(config: Config) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(asdict(config), f, ensure_ascii=False, indent=2)


def load_history() -> List[HistoryEntry]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [HistoryEntry(**item) for item in data]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def save_history(history: List[HistoryEntry]) -> None:
    trimmed = history[:MAX_HISTORY]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in trimmed], f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# 导出
# ─────────────────────────────────────────

def export_csv(history: List[HistoryEntry], prefix: str) -> str:
    filename = f"accession_{prefix}.csv"
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["编号", "生成时间"])
        for entry in history:
            writer.writerow([entry.number, entry.timestamp])
    return filename


def export_txt(history: List[HistoryEntry], prefix: str) -> str:
    filename = f"accession_{prefix}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(e.number for e in history))
    return filename


# ─────────────────────────────────────────
# CLI 辅助函数
# ─────────────────────────────────────────

def hr(char: str = "─", width: int = 50) -> None:
    print(char * width)


def header(title: str) -> None:
    print()
    hr()
    print(f"  {title}")
    hr()


def prompt_int(msg: str, default: int, min_val: int = 1, max_val: int = 10_000) -> int:
    while True:
        raw = input(f"{msg} [{default}]: ").strip()
        if raw == "":
            return default
        try:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  ⚠ 请输入 {min_val}~{max_val} 之间的整数")
        except ValueError:
            print("  ⚠ 无效输入，请输入整数")


def prompt_str(msg: str, default: str, max_len: int = 10) -> str:
    while True:
        raw = input(f"{msg} [{default}]: ").strip().upper()
        if raw == "":
            return default
        if 1 <= len(raw) <= max_len:
            return raw
        print(f"  ⚠ 长度需在 1~{max_len} 字符之间")


def prompt_choice(msg: str, options: dict, default: str) -> str:
    """options: {key: label}"""
    print(msg)
    keys = list(options.keys())
    for i, (k, label) in enumerate(options.items(), 1):
        marker = "●" if k == default else " "
        print(f"  {marker} {i}. {label}")
    while True:
        raw = input(f"  选择 [默认 {keys.index(default) + 1}]: ").strip()
        if raw == "":
            return default
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        except ValueError:
            pass
        print("  ⚠ 无效选择")


def pause() -> None:
    input("\n  按 Enter 继续...")


# ─────────────────────────────────────────
# 菜单功能
# ─────────────────────────────────────────

def show_status(config: Config, history: List[HistoryEntry]) -> None:
    preview = build_number(config)
    print(f"\n  当前预览: {preview}")
    print(f"  计数器:  {config.counter}  步长: {config.step}  历史条数: {len(history)}")


def menu_configure(config: Config) -> Config:
    header("⚙ 编号结构配置")
    config.prefix    = prompt_str("前缀 (Prefix，最多10字符)", config.prefix)
    config.digits    = prompt_int("数字位数 (3~12)", config.digits, min_val=3, max_val=12)
    config.separator = prompt_choice("分隔符", SEP_OPTIONS, config.separator)
    config.year_pos  = prompt_choice("年份位置", YEAR_POS_LABELS, config.year_pos)
    config.step      = prompt_int("步长 (每次递增, 1~100)", config.step, min_val=1, max_val=100)
    config.counter   = prompt_int("当前计数器起始值", config.counter, min_val=1, max_val=999_999_999)
    print(f"\n  ✓ 配置已更新，预览: {build_number(config)}")
    return config


def menu_generate_one(config: Config, history: List[HistoryEntry]):
    header("➕ 生成单个编号")
    number, config = generate_one(config)
    entry = HistoryEntry(number=number)
    history.insert(0, entry)
    print(f"\n  生成编号: {number}")
    print(f"  时间戳:  {entry.timestamp}")
    print(f"  计数器已推进至: {config.counter}")
    return config, history


def menu_generate_batch(config: Config, history: List[HistoryEntry]):
    header("📦 批量生成编号")
    size = prompt_int("生成数量 (1~1000)", 10, min_val=1, max_val=1000)
    numbers, config = generate_batch(config, size)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_entries = [HistoryEntry(number=n, timestamp=now) for n in numbers]
    history = new_entries + history

    print(f"\n  已生成 {size} 个编号：")
    for n in numbers[:20]:
        print(f"    {n}")
    if len(numbers) > 20:
        print(f"    ... 共 {len(numbers)} 条（仅显示前 20）")
    print(f"\n  计数器已推进至: {config.counter}")

    # 可选：保存到文件
    save_opt = input("\n  是否立即导出为文件？(c=CSV / t=TXT / 回车跳过): ").strip().lower()
    if save_opt == "c":
        fname = export_csv(history, config.prefix)
        print(f"  ✓ 已导出: {fname}")
    elif save_opt == "t":
        fname = export_txt(history, config.prefix)
        print(f"  ✓ 已导出: {fname}")

    return config, history


def menu_history(history: List[HistoryEntry], config: Config) -> List[HistoryEntry]:
    header(f"📋 历史记录 (共 {len(history)} 条)")
    if not history:
        print("  暂无记录")
        pause()
        return history

    page_size = 20
    total_pages = max(1, (len(history) + page_size - 1) // page_size)
    page = 0

    while True:
        start = page * page_size
        end   = min(start + page_size, len(history))
        print(f"\n  第 {page + 1}/{total_pages} 页  (第 {start + 1}~{end} 条)")
        print(f"  {'编号':<30} 生成时间")
        hr("-", 50)
        for entry in history[start:end]:
            print(f"  {entry.number:<30} {entry.timestamp}")
        hr("-", 50)

        print("  [n] 下一页  [p] 上一页  [c] 导出CSV  [t] 导出TXT  [x] 清空历史  [q] 返回")
        cmd = input("  > ").strip().lower()
        if cmd == "n" and page < total_pages - 1:
            page += 1
        elif cmd == "p" and page > 0:
            page -= 1
        elif cmd == "c":
            fname = export_csv(history, config.prefix)
            print(f"  ✓ 已导出: {fname}")
        elif cmd == "t":
            fname = export_txt(history, config.prefix)
            print(f"  ✓ 已导出: {fname}")
        elif cmd == "x":
            confirm = input("  确认清空所有历史？(yes/no): ").strip().lower()
            if confirm == "yes":
                history = []
                print("  ✓ 历史已清空")
                break
        elif cmd == "q":
            break

    return history


def menu_presets(config: Config) -> Config:
    header("🏛 预设模板")
    print(f"  {'序号':<4} {'名称':<16} 示例编号")
    hr("-", 50)
    for i, p in enumerate(PRESETS, 1):
        # 构建示例编号（counter=1）
        tmp = Config(
            prefix=p["prefix"], digits=p["digits"],
            separator=p["separator"], year_pos=p["year_pos"],
            counter=1, step=1
        )
        example = build_number(tmp)
        print(f"  {i:<4} {p['name']:<16} {example}")
    hr("-", 50)

    raw = input("  选择预设编号 (回车取消): ").strip()
    if not raw:
        return config
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(PRESETS):
            p = PRESETS[idx]
            config.prefix    = p["prefix"]
            config.digits    = p["digits"]
            config.separator = p["separator"]
            config.year_pos  = p["year_pos"]
            print(f"  ✓ 已应用预设「{p['name']}」，预览: {build_number(config)}")
        else:
            print("  ⚠ 序号超出范围")
    except ValueError:
        print("  ⚠ 无效输入")
    return config


def menu_reset(config: Config, history: List[HistoryEntry]):
    confirm = input("  确认重置计数器并清空历史？(yes/no): ").strip().lower()
    if confirm == "yes":
        config.counter = 1
        history = []
        print("  ✓ 已重置")
    return config, history


# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────

def main() -> None:
    print("\n" + "═" * 50)
    print("  馆藏入库编号分配工具  v1.0 (Python CLI)")
    print("  自由配置前缀 · 位数 · 分隔符 · 年份 · 步长")
    print("═" * 50)

    config  = load_config()
    history = load_history()

    while True:
        show_status(config, history)
        print()
        print("  1. ⚙  编号结构配置")
        print("  2. ➕  生成单个编号")
        print("  3. 📦  批量生成编号")
        print("  4. 📋  查看历史记录")
        print("  5. 🏛  预设模板")
        print("  6. 🔄  重置 (计数器归1 + 清空历史)")
        print("  0. 退出")
        hr()

        cmd = input("  请选择: ").strip()

        if cmd == "1":
            config = menu_configure(config)
        elif cmd == "2":
            config, history = menu_generate_one(config, history)
        elif cmd == "3":
            config, history = menu_generate_batch(config, history)
        elif cmd == "4":
            history = menu_history(history, config)
        elif cmd == "5":
            config = menu_presets(config)
        elif cmd == "6":
            config, history = menu_reset(config, history)
        elif cmd == "0":
            break
        else:
            print("  ⚠ 无效选择，请输入 0~6")
            continue

        # 每次操作后自动保存
        save_config(config)
        save_history(history)

        if cmd in ("2", "3"):
            pause()

    print("\n  ✓ 配置和历史已保存，再见！\n")


if __name__ == "__main__":
    main()
