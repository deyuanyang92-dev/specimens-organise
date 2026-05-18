"""WoRMS 全库抓取 — 独立进程 daemon（W1）。

可作为独立 Python 进程跑：

    python -m specimen_app.worms_crawler_daemon \\
        --resume-state ~/.specimen_inventory/worms_crawl_state.json \\
        --pid-file    ~/.specimen_inventory/worms_crawler.pid

UI 用 `subprocess.Popen` 启动（POSIX `start_new_session=True` / Windows
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`），主应用关闭后 daemon 继续跑。
利用 `worms_client.crawl_full_rest` 的 resume_state 机制天然断点续传。

进程间通信：纯文件协议
- **PID 文件**：daemon 启动时写入自身 PID，结束时（atexit / 信号）删除。
  UI 读 PID 文件检测 daemon 是否在跑（再调 `os.kill(pid, 0)` 确认）。
- **state 文件**：crawl_full_rest 已有 autosave 机制（每 5000 条 + 完成清除）。
  UI 用 QTimer 1-2s 轮询读 `imported` 字段显示进度。
- **停止**：UI 给 daemon 发 SIGTERM（POSIX）或 `taskkill /pid`（Windows）。
  daemon 收到 SIGTERM → should_stop 返回 True → crawl_full_rest 优雅停 + 当前
  state 已 autosave，下次启动自动续传。

设计原则：
- 零第三方依赖（仅 stdlib）
- 独立进程不依赖 PyQt5（worms_client 也是纯 stdlib）
- 失败/异常都 graceful 退出，保证 PID 文件清理
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys
import time
from pathlib import Path


# 退出码约定（UI 可据此判断 daemon 结果）
EXIT_OK = 0
EXIT_ALREADY_RUNNING = 1
EXIT_IMPORT_FAILED = 2
EXIT_CRASHED = 3
EXIT_CANCELLED = 4


def _is_pid_alive(pid: int) -> bool:
    """跨平台检查进程是否存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 在另一台机器或权限不足 — 视为存活（避免误判 stale 后双启动）
        return True
    except OSError:
        return False


def _read_pid(pid_path: Path) -> int | None:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def daemon_running(pid_path: Path) -> int | None:
    """供 UI 调：检查 daemon 是否在跑，返回 PID（活着）或 None（未跑 / pid 文件 stale）。"""
    if not pid_path.exists():
        return None
    pid = _read_pid(pid_path)
    if pid is None:
        return None
    if _is_pid_alive(pid):
        return pid
    # PID 文件残留但进程已死 → 清理
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_signal_handlers(stop_flag: dict) -> None:
    """SIGTERM / SIGINT 触发 stop_flag → crawl_full_rest 的 should_stop 返回 True。"""
    def _handle(signum, frame) -> None:
        stop_flag["stop"] = True

    try:
        signal.signal(signal.SIGTERM, _handle)
    except (ValueError, OSError):
        pass  # 某些环境（如 Windows + 主线程之外）不能注册
    try:
        signal.signal(signal.SIGINT, _handle)
    except (ValueError, OSError):
        pass


def run_daemon(
    resume_state: Path,
    pid_file: Path | None,
    rate_limit_qps: float = 2.0,
    cache_db_path: Path | None = None,
) -> int:
    """daemon 主流程。可被测试直接调（不经过 argparse）。

    返回退出码 EXIT_*。
    """
    if pid_file is not None:
        # 防双启动:同步检查 + 写 PID
        # 规范化软件设计 2026-05 P1 审查修复:
        # 旧:daemon_running 检查 + write_text 两步,中间窗口期可双进程同时通过 check。
        # 现:用 os.open(O_CREAT | O_EXCL) 原子创建 PID 文件,失败说明已有 daemon。
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # PID 文件已在,检查里面的 PID 是否仍活
            existing = daemon_running(pid_file)
            if existing is not None:
                print(
                    f"[worms-daemon] 已有 daemon 在跑(pid={existing}),退出",
                    file=sys.stderr,
                )
                return EXIT_ALREADY_RUNNING
            # 旧 PID 已死,删 stale + 重试一次原子创建
            try:
                pid_file.unlink()
                fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except (FileExistsError, OSError) as exc:
                print(f"[worms-daemon] PID 文件竞争失败:{exc}", file=sys.stderr)
                return EXIT_ALREADY_RUNNING
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        atexit.register(_remove_pid_file, pid_file)

    # 信号处理
    stop_flag = {"stop": False}
    _setup_signal_handlers(stop_flag)

    # 延迟 import，让 PID 文件管理在 import 失败时也能清理（atexit 兜底）
    try:
        from .worms_client import crawl_full_rest, WormsError
    except ImportError as exc:
        print(f"[worms-daemon] 无法导入 crawl_full_rest：{exc}", file=sys.stderr)
        return EXIT_IMPORT_FAILED

    def _should_stop() -> bool:
        return stop_flag["stop"]

    def _progress(imported: int, name_or_status: str) -> None:
        # 控制台输出（用户 stdin/stdout 重定向到日志可看）。
        # state 文件由 crawl_full_rest 内部 autosave 每 5000 条 + 完成清除。
        print(
            f"[worms-daemon] imported={imported} now={name_or_status}",
            file=sys.stderr,
        )

    print(
        f"[worms-daemon] 启动，pid={os.getpid()} resume={resume_state}",
        file=sys.stderr,
    )
    start_ts = time.time()
    try:
        count = crawl_full_rest(
            progress_cb=_progress,
            rate_limit_qps=rate_limit_qps,
            resume_state_path=resume_state,
            should_stop=_should_stop,
        )
        dur = time.time() - start_ts
        print(
            f"[worms-daemon] 完成 — 本次会话新写 {count} 条，耗时 {dur:.0f}s",
            file=sys.stderr,
        )
        return EXIT_OK
    except InterruptedError:
        print(
            "[worms-daemon] 已取消（状态已保存，下次启动会自动续传）",
            file=sys.stderr,
        )
        return EXIT_CANCELLED
    except WormsError as exc:
        print(f"[worms-daemon] WoRMS 错误：{exc}", file=sys.stderr)
        return EXIT_CRASHED
    except Exception as exc:  # noqa: BLE001
        print(
            f"[worms-daemon] 崩溃：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        # 同时写到 crash log（如果可用）
        try:
            from .crash_log import write_crash_log
            write_crash_log(type(exc), exc, exc.__traceback__, context_note="worms-daemon")
        except Exception:
            pass
        return EXIT_CRASHED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WoRMS REST 全库抓取 daemon")
    parser.add_argument(
        "--resume-state", required=True,
        help="断点续传 state JSON 文件路径（crawl_full_rest 已用过的路径）",
    )
    parser.add_argument(
        "--pid-file", default=None,
        help="PID 锁文件路径；同一个 pid-file 防止双启动",
    )
    parser.add_argument(
        "--rate-limit-qps", type=float, default=2.0,
        help="HTTP 请求速率上限（默认 2 req/s）",
    )
    args = parser.parse_args(argv)

    return run_daemon(
        resume_state=Path(args.resume_state).expanduser(),
        pid_file=Path(args.pid_file).expanduser() if args.pid_file else None,
        rate_limit_qps=args.rate_limit_qps,
    )


def spawn_detached(
    resume_state: Path,
    pid_file: Path,
    rate_limit_qps: float = 2.0,
) -> int:
    """UI 调用：启动一个 detached daemon 子进程。返回子进程 PID。

    跨平台 detach：
    - POSIX (Linux/WSL/macOS)：`start_new_session=True` 让子进程脱离父进程组
    - Windows：`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` flags
    """
    import subprocess

    cmd = [
        sys.executable,
        "-m", "specimen_app.worms_crawler_daemon",
        "--resume-state", str(resume_state),
        "--pid-file", str(pid_file),
        "--rate-limit-qps", str(rate_limit_qps),
    ]
    kwargs: dict = {
        # daemon 不需要 stdin；输出重定向到 daemon 自己的日志文件
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


def request_stop_daemon(pid_file: Path) -> bool:
    """UI 调用：给跑中的 daemon 发 SIGTERM 让它优雅停止。

    返回 True 表示已发送信号；False 表示 daemon 不在跑。
    Windows 上发 SIGTERM 不被处理 → 用 taskkill 退化方案。
    """
    pid = daemon_running(pid_file)
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            # Windows: SIGTERM 不会被 Python signal handler 处理;用 taskkill。
            # 规范化软件设计 2026-05 P1 审查修复:
            # 旧:taskkill /PID /T 立即终止进程树,daemon 无机会保存 state.json,可能留半写状态。
            # 现:先发送 Ctrl-Break(SIGBREAK 等价)给 daemon 触发优雅退出 → 等 3s →
            #     仍存活才 /F 强杀。最大程度保留 state 完整性。
            import subprocess
            # CTRL_BREAK_EVENT 仅对同 console group 进程有效;daemon 用 DETACHED_PROCESS 启动时
            # 不在我们 console group, CTRL_BREAK 不奏效 → 直接 taskkill 优雅(不带 /F)再等。
            subprocess.run(
                ["taskkill", "/PID", str(pid)],  # 不带 /F:发 WM_CLOSE,让 daemon 优雅退出
                check=False, capture_output=True,
            )
            # 等 3s,看 daemon 是否自行退出
            import time as _t
            for _ in range(30):
                if daemon_running(pid_file) is None:
                    return True
                _t.sleep(0.1)
            # 超时 → /F 强杀 + /T 进程树
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False, capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
