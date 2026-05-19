"""Detect how the running app was installed.

Mirrors Claude Code's `claude update` mind-model: the right upgrade strategy
depends on how the app got onto the user's machine in the first place.

`installation_kind()` returns one of:
- ``frozen-current``  PyInstaller bundle launched through the stable
  ``current/`` junction/symlink (D1). Full auto-upgrade flow available.
- ``frozen-direct``   PyInstaller bundle launched directly from a
  versioned ``releases/v*/`` directory. Shows D10 banner inviting the
  user to enable ``current/`` for seamless upgrades.
- ``source``          Running from the source tree via ``python run_app.py``.
  In-app rebuild (D6) is available; auto-upgrade is not.
- ``appimage``        Linux AppImage single-file launch. Upgrade path is
  "replace .AppImage file".
- ``system-package``  Installed via a system package manager
  (apt / snap / brew / MSI in Program Files). Built-in upgrade is disabled;
  the user is told to use their package manager.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePath, PureWindowsPath
from typing import Literal

InstallKind = Literal[
    "frozen-current",
    "frozen-direct",
    "source",
    "appimage",
    "system-package",
]

# Path prefixes that imply system-managed install. Heuristic — v0.9 can probe
# dpkg/rpm/brew directly. Order matters only for readability.
SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/usr/",
    "/opt/",
    "/snap/",
    "C:\\Program Files\\",
    "C:\\Program Files (x86)\\",
)

# The junction/symlink directory name used by D1 stable-entry pattern.
CURRENT_LINK_NAME = "current"


def _exe_path() -> PurePath:
    """Return sys.executable as a PurePath that handles Windows backslashes
    even when the test runs on POSIX (and vice versa).
    """
    raw = sys.executable
    return PureWindowsPath(raw) if "\\" in raw else Path(raw)


def installation_kind() -> InstallKind:
    """Return the install kind for the currently running process."""
    if not getattr(sys, "frozen", False):
        return "source"

    exe = _exe_path()

    if exe.suffix.lower() == ".appimage" or os.environ.get("APPIMAGE"):
        return "appimage"

    raw = str(exe)
    if any(raw.startswith(prefix) for prefix in SYSTEM_PATH_PREFIXES):
        return "system-package"

    if exe.parent.name == CURRENT_LINK_NAME:
        return "frozen-current"

    return "frozen-direct"


def is_frozen_via_current_link() -> bool:
    return installation_kind() == "frozen-current"


def is_built_in_upgrade_supported(kind: InstallKind | None = None) -> bool:
    """True when built-in upgrade flow should be exposed in the UI.

    system-package: no — delegate to the package manager.
    source: no built-in self-upgrade, but the dev-rebuild menu item still
    appears (handled separately).
    All other kinds: yes.
    """
    if kind is None:
        kind = installation_kind()
    return kind not in ("system-package",)


def kind_description(kind: InstallKind) -> str:
    return {
        "frozen-current": "通过 current/ 稳定链接启动（推荐）",
        "frozen-direct": "直接从 releases/v*/ 启动",
        "source": "源码运行（开发模式）",
        "appimage": "AppImage 单文件",
        "system-package": "系统包管理器安装",
    }[kind]


def upgrade_advice(kind: InstallKind) -> str:
    return {
        "frozen-current": "内置自动升级可用。",
        "frozen-direct": "建议启用 current/ 稳定链接以获得无缝升级。",
        "source": "源码模式：用 git pull 更新代码；或在 升级 → 高级 → 本地重新打包 出新 exe。",
        "appimage": "AppImage 模式：升级会替换当前 .AppImage 文件。",
        "system-package": "通过系统包管理器升级（apt / snap / brew），内置升级已禁用。",
    }[kind]
