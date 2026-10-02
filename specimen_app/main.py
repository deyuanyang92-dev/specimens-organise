from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _run_check_only(channel: str) -> int:
    """D20 ``--check-only``: 不开 GUI,打印当前/最新版本号,按结果返回退出码。

    返回码:
    - 0 = 已是最新
    - 1 = 有新版可下
    - 2 = 网络 / 解析错误
    """
    from . import __version__
    try:
        from .updater import check_latest_release, is_newer
        release = check_latest_release(channel=channel)
    except Exception as exc:
        print(f"[update-check] 错误：{exc}", file=sys.stderr)
        return 2
    if release is None:
        print(f"[update-check] channel={channel} 无可用 release。current=v{__version__}")
        return 0
    if is_newer(release.version, __version__):
        print(f"[update-check] 发现新版 v{release.version}（当前 v{__version__}）")
        return 1
    print(f"[update-check] 已是最新 v{__version__}（GitHub 最新 v{release.version}）")
    return 0


def _run_smoke() -> int:
    """打包自检：不开窗口、不碰工作区，只验证运行时（Qt 插件、DLL、全部模块）能加载。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt5.QtWidgets import QApplication

        from . import __version__
        from . import ui  # noqa: F401  全部界面模块
        from .excel_store import ExcelStore  # noqa: F401
        from .image_cache import ThumbnailCache  # noqa: F401

        app = QApplication.instance() or QApplication(["smoke"])
        app.processEvents()
        print(f"[smoke] ok v{__version__}")
        return 0
    except Exception as exc:  # noqa: BLE001
        from .boot_guard import write_startup_failure

        log = write_startup_failure(exc)
        if sys.stderr is not None:
            print(f"[smoke] FAILED: {exc!r} log={log}", file=sys.stderr)
        return 3


def main() -> None:
    parser = argparse.ArgumentParser(description="标本入库管理桌面软件")
    parser.add_argument("--workspace", default=None, help="工作区目录")
    parser.add_argument(
        "--check-only", action="store_true",
        help="(D20) 仅检查 GitHub 最新版本号,不开 GUI。退出码 0=最新 / 1=有更新 / 2=网络错。",
    )
    parser.add_argument(
        "--update-channel", default=None,
        help="(D18) 临时指定本次 --check-only 用的 channel: stable / prerelease。",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="(2026-10-02) 自检：加载全部模块 + 建 QApplication + 建主窗口类后退出，退出码 0=正常。发布流程用它试跑打包好的 exe。",
    )
    args = parser.parse_args()
    if args.smoke:
        sys.exit(_run_smoke())

    if args.check_only:
        # 不读 settings 也行,但优先用用户选的 channel 保一致。
        channel = args.update_channel
        if not channel:
            try:
                from .app_settings import load_settings
                channel = load_settings().auto_update_channel or "stable"
            except Exception:
                channel = "stable"
        sys.exit(_run_check_only(channel))

    from .ui import run_app
    from .workspace import default_workspace
    workspace = Path(args.workspace) if args.workspace else default_workspace()
    if workspace is None:
        # 旧文案提到"在弹出的对话框中选择"——那是窗口构建前的裸 QFileDialog。
        # 现改为：先打开主窗口，再在窗口内提示选择/新建工作区。
        print("未找到上次使用的工作区，程序将打开主窗口并提示选择或新建工作区。", file=sys.stderr)
    run_app(workspace)


if __name__ == "__main__":
    main()
