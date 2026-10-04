"""启动保护（2026-10-02）：打包版 (--windowed) 启动失败时必须留痕 + 弹窗。

旧行为：PyInstaller --windowed 下 sys.stdout/stderr 为 None，run_app.py 的
``print("[错误] 缺少依赖库…")`` 什么也不输出，随后 sys.exit(1) —— 用户看到的就是
"双击没反应"。Qt / DLL 层的硬崩溃（access violation）也不经过 Python 异常钩子，
同样无日志。

本模块只用标准库，必须能在 import PyQt5 之前工作。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR_NAME = "标本入库管理"  # 与 app_settings.APP_DIR_NAME 一致；此处不 import app_settings，避免启动链依赖
FAULT_LOG_NAME = "boot_fault.log"
_fault_file = None  # 保持文件句柄存活，faulthandler 需要


def boot_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".specimen_inventory"


def enable_fault_log() -> Path | None:
    """把 C 层崩溃（段错误 / access violation）的 Python 栈写到 boot_fault.log。失败不抛。"""
    global _fault_file
    try:
        d = boot_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / FAULT_LOG_NAME
        _fault_file = open(path, "a", encoding="utf-8")
        _fault_file.write(f"\n===== boot {datetime.now().isoformat(timespec='seconds')} pid={os.getpid()} "
                          f"frozen={bool(getattr(sys, 'frozen', False))} exe={sys.executable} =====\n")
        _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
        return path
    except Exception:
        return None


def write_startup_failure(exc: BaseException) -> Path | None:
    """启动阶段未捕获异常 → startup_failure_<ts>.log。失败不抛。"""
    try:
        d = boot_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"startup_failure_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        path.write_text(
            f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Executable: {sys.executable}\nFrozen: {bool(getattr(sys, 'frozen', False))}\n"
            f"Python: {sys.version}\nPlatform: {sys.platform}\n\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        return path
    except Exception:
        return None


def show_fatal(message: str) -> None:
    """无控制台也能让用户看到：stderr 存在就打印；Windows 再弹系统对话框（不依赖 Qt）。"""
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass
    if sys.platform == "win32":
        _message_box(message)


def _message_box(message: str) -> None:
    """Windows 系统错误框（不依赖 Qt）。单独成函数：测试里替换掉，否则 CI 上会弹框卡死。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "标本入库管理 无法启动", 0x10)
    except Exception:
        pass


def report_startup_failure(exc: BaseException) -> None:
    log = write_startup_failure(exc)
    where = f"\n\n详细日志已保存：\n{log}" if log else ""
    show_fatal(f"启动失败：{type(exc).__name__}: {exc}{where}\n\n请把这个日志文件发给维护者。")
