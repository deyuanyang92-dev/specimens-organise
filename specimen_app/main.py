from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ui import run_app
from .workspace import default_workspace


def main() -> None:
    parser = argparse.ArgumentParser(description="标本入库管理桌面软件")
    parser.add_argument("--workspace", default=None, help="工作区目录")
    args = parser.parse_args()
    workspace = Path(args.workspace) if args.workspace else default_workspace()
    if workspace is None:
        # 旧文案提到"在弹出的对话框中选择"——那是窗口构建前的裸 QFileDialog。
        # 现改为：先打开主窗口，再在窗口内提示选择/新建工作区。
        print("未找到上次使用的工作区，程序将打开主窗口并提示选择或新建工作区。", file=sys.stderr)
    run_app(workspace)


if __name__ == "__main__":
    main()
