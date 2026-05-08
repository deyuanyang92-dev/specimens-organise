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
        print("未找到工作区，请使用 --workspace 指定目录，或在弹出的对话框中选择。", file=sys.stderr)
    run_app(workspace)


if __name__ == "__main__":
    main()
