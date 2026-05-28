"""启动分阶段诊断：记录每个启动阶段的耗时 + 进程峰值内存，用于定位启动卡顿/死机阶段。

用户报告"启动很容易电脑死机"但无法说清卡在哪一步。此模块在启动各阶段打点，输出到
stderr（`[startup]` 前缀）并追加到 app 配置目录下的 `startup_diagnostics.log`（限大小，
**不写进 workspace 数据目录**，保持工作区可移植）。下次死机时，日志最后一行即卡死阶段。

纯标准库实现，打点失败绝不影响启动流程。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .app_settings import app_config_dir

_LOG_NAME = "startup_diagnostics.log"
_MAX_LOG_BYTES = 256 * 1024  # 超限即截断重写，避免日志无限增长

_start_time = time.monotonic()
_last_time = _start_time


def _peak_rss_mb() -> float | None:
    """当前进程的峰值 RSS（MB）；取不到返回 None。

    用峰值而非瞬时值：某阶段若把内存推高，峰值会留下痕迹，正是死机排查所需。
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if ok:
                return counters.PeakWorkingSetSize / (1024 * 1024)
            return None
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux 的 ru_maxrss 单位是 KB，macOS 是字节。
        if sys.platform == "darwin":
            return peak / (1024 * 1024)
        return peak / 1024
    except Exception:
        return None


def _append_log(line: str) -> None:
    try:
        path = app_config_dir() / _LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            path.write_text("", encoding="utf-8")  # 截断重写，保留最近一次启动
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass  # 诊断日志写入失败绝不能影响启动


def detect_workspace_on_windows_mounted_filesystem(workspace_root: Path) -> bool:
    """plan v0.10.3 H2：识别 WSL 下工作区是否落在 Windows-mounted 文件系统上。

    判定条件（同时满足）：
      - 运行环境是 WSL（``/proc/version`` 含 ``"microsoft"`` 或 ``"WSL"``）
      - 工作区路径以 ``/mnt/<单字符盘符>/`` 开头（如 ``/mnt/c/``、``/mnt/n/``）

    跨 9P → NTFS 的 IO 比 ext4 慢 10-100×，启动阶段的多张 xlsx open 容易累积到秒级，
    用户体感"超级卡"。检测命中时 UI 层会跳过 ``_preheat_caches``，并在 stderr 打提示
    建议把工作区迁到 WSL 本地（``~/...``）。
    """
    try:
        proc_version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    if "microsoft" not in proc_version and "wsl" not in proc_version:
        return False
    # 旧：先 Path.resolve()；在 Windows 测试进程里 Path("/mnt/n/...") 会被规范化成
    # Windows 盘符路径，导致模拟 WSL 的 /mnt/<drive> 判断失效。先保留原始字符串，
    # 真实非 /mnt 路径再 resolve，不影响 WSL 运行时的兼容语义。
    workspace_path_string = str(workspace_root).replace("\\", "/")
    if not workspace_path_string.startswith("/mnt/"):
        try:
            workspace_path_string = str(Path(workspace_root).resolve()).replace("\\", "/")
        except (OSError, ValueError):
            return False
    # /mnt/c/, /mnt/n/, /mnt/d/ 等单字符盘符挂载
    if not workspace_path_string.startswith("/mnt/"):
        return False
    parts = workspace_path_string.split("/", 3)
    # parts == ["", "mnt", "<drive_letter>", "..."] for /mnt/c/...
    if len(parts) >= 3 and len(parts[2]) == 1 and parts[2].isalpha():
        return True
    return False


def mark(stage: str) -> None:
    """记录一个启动阶段完成：自上个 mark 的耗时 + 累计耗时 + 进程峰值 RSS。"""
    global _last_time
    now = time.monotonic()
    delta = now - _last_time
    total = now - _start_time
    _last_time = now
    rss = _peak_rss_mb()
    rss_text = f"peak {rss:.0f}MB" if rss is not None else "peak RSS?"
    line = f"[startup] +{delta:6.2f}s  total {total:6.2f}s  {rss_text}  {stage}"
    print(line, file=sys.stderr)
    _append_log(line)
