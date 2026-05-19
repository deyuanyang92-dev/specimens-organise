"""Generate and launch the external swap script that repoints ``current/``
to a new bundle directory after the running PID has exited.

Cannot do the swap from inside the running process on Windows because the
running exe holds open ``python3X.dll`` / Qt / ``_internal/`` files. The
script approach mirrors Firefox's ``updater.exe`` and Squirrel's
``Update.exe`` pattern in a much lighter form (~30 lines of shell, no
extra binary to ship).

Public API:

- :func:`write_swap_script_windows`
- :func:`write_swap_script_linux`
- :func:`launch_swap_detached`
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Maximum seconds the helper waits for the running PID to disappear before
# giving up. The user-facing app should not normally take more than 2s to
# exit; we leave a generous safety margin for slow disks / NAS workspaces.
_WAIT_FOR_EXIT_SECONDS = 30

_WINDOWS_TEMPLATE = r"""@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
set PID=%1
set NEW_BUNDLE=%~2
set CURRENT_LINK=%~3
set NEW_EXE=%~4
set WORKSPACE=%~5

REM Wait for the launching process to exit (max {wait_seconds}s).
for /L %%i in (1,1,{wait_seconds}) do (
  tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
  if errorlevel 1 goto :swap
  timeout /t 1 /nobreak >nul
)
echo Timed out waiting for PID %PID% to exit.
exit /b 1

:swap
REM Remove any existing junction/directory at CURRENT_LINK and recreate it.
if exist "%CURRENT_LINK%" (
  rmdir "%CURRENT_LINK%" 2>nul
  if exist "%CURRENT_LINK%" rmdir /S /Q "%CURRENT_LINK%"
)
mklink /J "%CURRENT_LINK%" "%NEW_BUNDLE%"
if errorlevel 1 (
  echo Failed to create junction at %CURRENT_LINK%
  exit /b 2
)

REM Launch the new exe through the stable current/ path. The workspace arg
REM is optional — only pass when non-empty so default-workspace discovery
REM still runs when WORKSPACE is blank.
if "%WORKSPACE%"=="" (
  start "" "%CURRENT_LINK%\%NEW_EXE%"
) else (
  start "" "%CURRENT_LINK%\%NEW_EXE%" --workspace "%WORKSPACE%"
)
exit /b 0
"""

_LINUX_TEMPLATE = r"""#!/usr/bin/env bash
set -eu

PID="$1"
NEW_BUNDLE="$2"
CURRENT_LINK="$3"
NEW_EXE="$4"
WORKSPACE="${{5:-}}"

# Wait for the launching process to exit (max {wait_seconds}s).
for _ in $(seq 1 {wait_seconds}); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

# Atomic symlink swap. mv -T treats CURRENT_LINK as a file even when
# pre-existing, which is exactly what we want for a symlink rotation.
TMP_LINK="${{CURRENT_LINK}}.new.$$"
ln -sfn "$NEW_BUNDLE" "$TMP_LINK"
mv -Tf "$TMP_LINK" "$CURRENT_LINK"

# Re-arm the exec bit (the bundle move-rename may have lost it on some
# filesystems) and launch detached.
chmod +x "$CURRENT_LINK/$NEW_EXE" 2>/dev/null || true

if [ -z "$WORKSPACE" ]; then
  nohup "$CURRENT_LINK/$NEW_EXE" >/dev/null 2>&1 &
else
  nohup "$CURRENT_LINK/$NEW_EXE" --workspace "$WORKSPACE" >/dev/null 2>&1 &
fi
"""


def write_swap_script_windows(
    *,
    dest_dir: Path | None = None,
    wait_seconds: int = _WAIT_FOR_EXIT_SECONDS,
) -> Path:
    """Materialize the Windows ``swap.bat`` and return its path."""
    dest_dir = Path(dest_dir) if dest_dir else Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"specimen_swap_{time.time_ns()}_{os.getpid()}.bat"
    path.write_text(
        _WINDOWS_TEMPLATE.format(wait_seconds=wait_seconds),
        encoding="utf-8",
    )
    return path


def write_swap_script_linux(
    *,
    dest_dir: Path | None = None,
    wait_seconds: int = _WAIT_FOR_EXIT_SECONDS,
) -> Path:
    """Materialize the Linux ``swap.sh`` and return its path. Sets +x."""
    dest_dir = Path(dest_dir) if dest_dir else Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"specimen_swap_{time.time_ns()}_{os.getpid()}.sh"
    path.write_text(
        _LINUX_TEMPLATE.format(wait_seconds=wait_seconds),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def launch_swap_detached(
    *,
    pid: int,
    new_bundle: Path,
    current_link: Path,
    new_exe_name: str,
    workspace: Path | None,
    dest_dir: Path | None = None,
) -> Path:
    """Generate the platform-appropriate swap script and launch it as a
    fully-detached child process. Returns the script path (mainly for
    test inspection — the caller should immediately ``QApplication.quit()``).

    ``new_bundle``: absolute path of the freshly-extracted bundle dir.
    ``current_link``: where the ``current`` junction/symlink should land.
    ``new_exe_name``: bare filename (no path) of the exe inside the bundle.
    ``workspace``: optional ``--workspace`` argument to forward on relaunch.
    """
    new_bundle = Path(new_bundle).resolve()
    current_link = Path(current_link).resolve()
    ws_arg = str(workspace) if workspace else ""

    if sys.platform == "win32":
        script = write_swap_script_windows(dest_dir=dest_dir)
        args = [
            "cmd", "/c", "start", "", "/B",
            str(script),
            str(pid),
            str(new_bundle),
            str(current_link),
            new_exe_name,
            ws_arg,
        ]
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS keeps the helper alive
        # after we quit. CREATE_NO_WINDOW hides the console flash.
        creationflags = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(
            args,
            close_fds=True,
            creationflags=creationflags,
        )
    else:
        script = write_swap_script_linux(dest_dir=dest_dir)
        args = [
            str(script),
            str(pid),
            str(new_bundle),
            str(current_link),
            new_exe_name,
            ws_arg,
        ]
        subprocess.Popen(
            args,
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return script


def quote_for_log(args: list[str]) -> str:
    """Helper for crash-log writes that want a copyable swap command."""
    return " ".join(shlex.quote(a) for a in args)
