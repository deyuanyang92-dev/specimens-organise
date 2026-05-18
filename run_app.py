"""标本入库管理 启动入口。

规范化软件设计 2026-05 起:
- 顶部检测 WSL,设软件渲染环境变量,避开 MESA ZINK GPU 探测的 5-10s 卡顿。
- 环境变量必须在 import PyQt5 / QApplication 之前设置,否则 Qt 已选定平台插件无法改。
"""

import os
import platform
import sys

# WSL 检测 + Qt 软件渲染环境变量(必须在 import PyQt5 前设置)。
# - WSL 上 Qt 默认尝试 GPU 渲染 (MESA ZINK / GLX),失败回落软件渲染但中间有 5-10s 卡顿。
# - 显式声明 software 跳过 GPU 探测,启动稳定 + 快。
# - 物理 Windows / 物理 Linux 不命中此分支,沿用 Qt 默认。
try:
    _release = platform.uname().release.lower()
    _is_wsl = "microsoft" in _release or "wsl" in _release
except Exception:
    _is_wsl = False
if _is_wsl:
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")  # (留作 WebEngine 兼容,本期未引入)

try:
    from specimen_app.main import main
except ImportError as exc:
    print(f"\n[错误] 缺少依赖库：{exc}")
    print("请运行：  pip install -r requirements.txt")
    print("或直接下载打包好的 EXE（无需安装 Python）：")
    print("  https://github.com/deyuanyang92-dev/specimens-organise/releases")
    sys.exit(1)


if __name__ == "__main__":
    main()
