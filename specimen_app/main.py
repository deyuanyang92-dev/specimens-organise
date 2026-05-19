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
    args = parser.parse_args()

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
