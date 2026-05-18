"""运行环境检测(规范化软件设计 2026-05 新增,启动卡死优化用)。

提供:
- `total_ram_mb()` 总 RAM(MB);失败返 None。
- `is_low_memory()` 总 RAM < 3GB 判低内存机。
- `is_wsl()` 在 WSL 上(检 /proc/version 或 uname.release)。
- `is_frozen()` 在 PyInstaller 打包态(sys.frozen)。
- `env_snapshot()` 拼一行环境快照字符串(供 startup_diag 落 log)。

任何检测失败一律返 None / False,不抛异常(启动期诊断不能挂启动)。
"""

from __future__ import annotations

import os
import platform
import sys
from functools import lru_cache
from typing import Optional


# 总 RAM 阈值:< 3GB 算"低内存机器"。
# - 2GB 机(用户案例)显然命中
# - 3GB 是 Win10 最低官方推荐内存,跑桌面应用也吃紧
# - >= 4GB 用默认配置即可
_LOW_MEMORY_THRESHOLD_MB = 3000


@lru_cache(maxsize=1)
def total_ram_mb() -> Optional[int]:
    """返回总物理 RAM(MB);失败返 None。"""
    try:
        import psutil  # 运行时依赖,requirements.txt 已加 psutil>=5.9
        return int(psutil.virtual_memory().total // (1024 * 1024))
    except Exception:
        # psutil 缺失 / 出错 时回落到 /proc/meminfo (Linux) 或返 None (其他平台)
        if sys.platform.startswith("linux"):
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return kb // 1024
            except Exception:
                pass
        return None


@lru_cache(maxsize=1)
def is_low_memory() -> bool:
    """总 RAM < 3GB 判低内存。失败 (未知 RAM) 保守返 False (即按正常配置跑)。"""
    ram = total_ram_mb()
    if ram is None:
        return False
    return ram < _LOW_MEMORY_THRESHOLD_MB


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    """检测是否在 WSL (Windows Subsystem for Linux) 内运行。

    优先读 /proc/version(WSL 内核名含 "microsoft" / "WSL");兜底用 platform.uname()。
    """
    try:
        if sys.platform.startswith("linux"):
            release = platform.uname().release.lower()
            if "microsoft" in release or "wsl" in release:
                return True
            try:
                with open("/proc/version", "r") as f:
                    txt = f.read().lower()
                    if "microsoft" in txt or "wsl" in txt:
                        return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def is_frozen() -> bool:
    """PyInstaller 打包态(sys.frozen)。"""
    return bool(getattr(sys, "frozen", False))


def is_fast_profile(profile: Optional[str] = None) -> bool:
    """high / extra_high 档位走"快路径":预 import、立 start、预热。

    规范化软件设计 2026-05 K 章新增:64GB 机用户选高档位不该承担为 2GB 机做的省内存
    延迟/lazy 开销。本 helper 让代码各处判断"要不要跳过省内存路径"。

    profile=None 时从 settings 读;失败 / 低档返 False (保持现有省内存路径)。
    """
    if profile is None:
        try:
            from .app_settings import load_settings
            profile = load_settings().memory_profile
        except Exception:
            return False
    return profile in ("high", "extra_high")


def memory_profile_params(profile: str) -> dict:
    """规范化软件设计 2026-05 新增:把 profile key 映射为各组件实际参数。

    返回 dict:
    - ``thumb_cache_bytes`` (int): ThumbnailCache memory_limit_bytes
    - ``thumb_workers`` (int): ThumbnailWorker max_workers
    - ``row_cache_maxsize`` (int): ExcelStore._row_cache_maxsize

    profile == "auto" 时按 is_low_memory() 内部分流(< 3GB → low 级,否则中)。
    未知 profile fallback "auto"。
    """
    if profile == "auto":
        if is_low_memory():
            return {"thumb_cache_bytes": 16 << 20, "thumb_workers": 1, "row_cache_maxsize": 4}
        return {"thumb_cache_bytes": 32 << 20, "thumb_workers": 2, "row_cache_maxsize": 6}
    table = {
        "extra_low":  {"thumb_cache_bytes":   8 << 20, "thumb_workers": 1, "row_cache_maxsize":  3},
        "low":        {"thumb_cache_bytes":  16 << 20, "thumb_workers": 1, "row_cache_maxsize":  4},
        "high":       {"thumb_cache_bytes": 128 << 20, "thumb_workers": 4, "row_cache_maxsize": 12},
        "extra_high": {"thumb_cache_bytes": 256 << 20, "thumb_workers": 4, "row_cache_maxsize": 20},
    }
    return table.get(profile, memory_profile_params("auto"))


def current_rss_mb() -> Optional[int]:
    """返回当前进程 RSS (MB);失败返 None。用于状态栏实时显示。

    psutil 不可用时回落到 resource.getrusage (Linux/Mac) 的 ru_maxrss(高水位线,
    不是实时 RSS,但仍有参考意义)。
    """
    try:
        import psutil
        return int(psutil.Process().memory_info().rss // (1024 * 1024))
    except Exception:
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: KB;macOS: bytes。简单按 Linux 处理(返回偏低于实际 RSS 也可接受)。
        if sys.platform == "darwin":
            return int(peak // (1024 * 1024))
        return int(peak // 1024)
    except Exception:
        return None


def env_snapshot() -> str:
    """拼一行环境快照字符串,供启动诊断落 log 头部。"""
    ram = total_ram_mb()
    ram_text = f"{ram}MB" if ram is not None else "?"
    return (
        f"Python={sys.version.split()[0]} "
        f"Platform={platform.platform()} "
        f"RAM={ram_text} "
        f"WSL={is_wsl()} "
        f"Frozen={is_frozen()} "
        f"QtPlatform={os.environ.get('QT_QPA_PLATFORM', 'default')}"
    )
