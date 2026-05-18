"""崩溃日志 + 异常退出诊断（E1）。

设计要点：
- **不影响应用启动**：所有 IO 失败都被静默吞，仅尝试输出到 stderr
- **跨平台**：路径用 `app_config_dir()`（macOS 走 `~/.specimen_inventory/`，Windows 走 `%APPDATA%\标本入库管理\`）
- **零第三方依赖**：仅用 stdlib (sys / threading / traceback / platform)
- **不在主线程兜底**：threading.excepthook 也覆盖（Py3.8+），覆盖 QThread 漏抓的异常

写日志格式（人类可读 + 报告可复制）：
```
========== specimen-organise crash report ==========
Time:     2026-05-16 14:30:00
Version:  v0.5.0
Platform: linux-5.15-WSL2 / Python 3.13.0
PID:      12345
Exception:
  Traceback (most recent call last):
    File "ui.py", line 1234, in foo
      ...
  ValueError: bad data
========== end report ==========
```

启动检测：
- `mark_app_started()`：删 last_exit_clean marker 文件；返回上次是否 clean exit
- `mark_app_exiting_clean()`：closeEvent 末尾 touch marker → 表示这次是 graceful exit
- 启动时若 marker 不存在 → 上次异常退出（kill / crash / power off）→ UI 弹提示让用户查看 crash_*.log
"""

from __future__ import annotations

import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path


_CRASH_LOG_NAME_PATTERN = "crash_{ts}.log"
_LAST_EXIT_MARKER = "last_exit_clean"
_MAX_CRASH_LOGS = 20  # 保留最近 20 个 crash log，更老的被新写入自动清理


def _config_dir() -> Path | None:
    """安全拿到 app_config_dir；任何失败返回 None，所有写入会跳过。"""
    try:
        from .app_settings import app_config_dir
        return app_config_dir()
    except Exception:
        return None


def write_crash_log(
    exc_type: type,
    exc_value: BaseException,
    tb,
    context_note: str = "",
) -> Path | None:
    """把未捕异常写到 `<app_config_dir>/crash_<时间戳>.log`。

    失败时不抛异常（避免在 crash 处理中再 crash），尽量打到 stderr 兜底。
    返回 log 路径或 None。
    """
    try:
        cfg = _config_dir()
        if cfg is None:
            return None
        cfg.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = cfg / _CRASH_LOG_NAME_PATTERN.format(ts=ts)

        # 应用版本（容错读取）
        try:
            from . import __version__ as app_version
        except Exception:
            app_version = "unknown"

        header_lines = [
            "========== specimen-organise crash report ==========",
            f"Time:     {datetime.now().isoformat(timespec='seconds')}",
            f"Version:  v{app_version}",
            f"Platform: {platform.platform()}",
            f"Python:   {platform.python_version()}",
            f"PID:      {_pid()}",
        ]
        if context_note:
            header_lines.append(f"Context:  {context_note}")
        header_lines.extend(
            [
                "Exception:",
                "",
            ]
        )
        tb_lines = traceback.format_exception(exc_type, exc_value, tb)
        body = "".join(tb_lines).rstrip("\n")
        footer = "\n========== end report ==========\n"

        text = "\n".join(header_lines) + body + footer
        log_path.write_text(text, encoding="utf-8")

        _trim_old_crash_logs(cfg)
        # 同步打 stderr 让控制台用户也能立即看到
        try:
            sys.stderr.write(f"\n[crash] wrote {log_path}\n")
            sys.stderr.write(text)
        except Exception:
            pass
        return log_path
    except Exception:  # noqa: BLE001 - 兜底任何二级异常
        try:
            sys.stderr.write(f"[crash] failed to write log: {exc_type.__name__}: {exc_value}\n")
        except Exception:
            pass
        return None


def _pid() -> int:
    try:
        import os

        return os.getpid()
    except Exception:
        return -1


def _trim_old_crash_logs(cfg: Path, keep: int = _MAX_CRASH_LOGS) -> None:
    """保留最新 keep 个 crash_*.log，删更老的（按 mtime 倒序）。"""
    try:
        logs = sorted(
            cfg.glob("crash_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in logs[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def install_excepthook() -> None:
    """安装全局未捕异常 hook。

    覆盖 `sys.excepthook` 和 `threading.excepthook`（Py3.8+），让主线程和子线程的
    未捕异常都被记录到 crash_*.log。原 hook 仍被调用（PyQt 默认 hook 会弹错误对话框）。
    """
    previous = sys.excepthook

    def _main_hook(exc_type, exc_value, tb) -> None:
        write_crash_log(exc_type, exc_value, tb, context_note="main_thread")
        try:
            previous(exc_type, exc_value, tb)
        except Exception:
            pass

    sys.excepthook = _main_hook

    # threading.excepthook 在 Py3.8+ 可用；老版本静默跳过
    try:
        prev_thread_hook = threading.excepthook

        def _thread_hook(args) -> None:
            write_crash_log(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                context_note=f"thread:{getattr(args.thread, 'name', '?')}",
            )
            try:
                prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook
    except AttributeError:
        pass


def mark_app_started() -> bool:
    """启动时检测上次是否 clean exit。

    返回 True 表示上次正常退出；False 表示上次异常退出（kill / crash / 断电）。
    无论结果如何，**调用后会删除 marker**（开始下一轮）— 由 mark_app_exiting_clean 重建。
    """
    cfg = _config_dir()
    if cfg is None:
        return True  # 无法检测则假设 ok，不打扰用户
    marker = cfg / _LAST_EXIT_MARKER
    was_clean = marker.exists()
    try:
        if was_clean:
            marker.unlink()
    except OSError:
        pass
    return was_clean


def mark_app_exiting_clean() -> None:
    """closeEvent 末尾调，touch last_exit_clean marker → 下次启动 mark_app_started() 返回 True。"""
    cfg = _config_dir()
    if cfg is None:
        return
    try:
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / _LAST_EXIT_MARKER).touch()
    except OSError:
        pass


def list_recent_crash_logs(limit: int = 5) -> list[Path]:
    """返回最近 `limit` 个 crash log 路径（按 mtime 倒序）。"""
    cfg = _config_dir()
    if cfg is None or not cfg.exists():
        return []
    try:
        return sorted(
            cfg.glob("crash_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
    except OSError:
        return []
