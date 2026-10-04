"""一键升级（v0.10.42）：下载安装器 → 关闭 → 静默安装 → 自动重启。

参考 VS Code / Notepad++ 等基于 Inno Setup 的 Windows 软件：升级 = 静默运行新版安装器。
好处是升级与用户手动双击安装器走**同一条**、已在 CI 上试跑过的代码路径，
不再自己做 zip 解压 + 目录链接切换（旧 updater_swap 路径，审计发现多个断点）。

流程：
  1. 下载 ``installer_v<ver>_windows.exe`` 到本机 ``<配置目录>/updates/``（不放工作区：工作区可能在 U 盘 / 网络盘）；
     **必须** 有 ``.sha256`` 且校验一致，否则拒绝（不装来路不明的文件）。
  2. 写一个 PowerShell 助手脚本并分离启动，主程序走正常关闭流程（保存 + 写正常退出标记）后退出。
  3. 助手等主程序进程结束 → ``安装器 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`` → 记录退出码 →
     重新打开 ``<安装目录>/current/*.exe``。安装失败时 current 仍指向旧版，重新打开的就是旧版。
  4. 下次启动读取结果文件：失败则告诉用户并给出安装日志位置。

本模块不 import PyQt5。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import updater
from .updater import UpdateError

RESULT_FILE = "last_update_result.json"


def installer_name(version: str) -> str:
    return f"installer_v{version}_windows.exe"


def find_inno_install_root(executable: Path | str | None = None) -> Path | None:
    """当前程序若由安装器安装（目录里有 Inno 卸载程序 unins000.exe 和 current/），返回安装根目录。"""
    exe = Path(executable or sys.executable)
    try:
        exe = exe.resolve()
    except OSError:
        pass
    for parent in list(exe.parents)[:6]:
        if (parent / "unins000.exe").exists() and (parent / "current").exists():
            return parent
    return None


def supports_installer_update(executable: Path | str | None = None) -> bool:
    return sys.platform == "win32" and getattr(sys, "frozen", False) and find_inno_install_root(executable) is not None


def updates_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "updates"


def download_installer(release, dest_dir: Path, progress_cb: Callable[[int], None] | None = None,
                       http_get=None, download_to=None) -> Path:
    """下载并校验安装器；已下载且校验一致则直接复用。缺校验文件或不一致 → UpdateError。"""
    http_get = http_get or updater._http_get
    download_to = download_to or updater._download_to
    name = installer_name(release.version)
    url = updater._asset_url(release.tag, name)
    updater._validate_url(url)
    sha_text = http_get(url + ".sha256").decode("utf-8", errors="replace")
    expected = updater._extract_expected_hash(sha_text, name)
    if not expected:
        raise UpdateError(f"新版本缺少校验文件（{name}.sha256），为安全起见不自动安装。")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if target.exists() and updater._file_sha256(target).lower() == expected.lower():
        if progress_cb:
            progress_cb(100)
        return target
    part = dest_dir / (name + ".part")
    try:
        download_to(url, part, progress_cb)
        actual = updater._file_sha256(part)
        if actual.lower() != expected.lower():
            raise UpdateError(f"安装器校验失败（sha256 不一致），已删除。\n期望：{expected}\n实际：{actual}")
        part.replace(target)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def write_helper_script(dest_dir: Path, *, pid: int, installer: Path, install_root: Path,
                        workspace: str = "", from_version: str = "", to_version: str = "") -> Path:
    """生成 PowerShell 助手脚本（UTF-8 带 BOM：Windows PowerShell 5.1 才能正确读中文路径）。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    log = dest_dir / f"install_{to_version or 'new'}.log"
    result = dest_dir / RESULT_FILE
    ws_args = f"@('--workspace', {_ps_quote(workspace)})" if workspace else "@()"
    script = f"""# 标本入库管理 一键升级助手（自动生成，可删除）
$ErrorActionPreference = 'Continue'
$appPid = {int(pid)}
$installer = {_ps_quote(installer)}
$root = {_ps_quote(install_root)}
$log = {_ps_quote(log)}
$result = {_ps_quote(result)}
$wsArgs = {ws_args}
# 1. 等主程序完全退出（最多 120 秒）
try {{ Wait-Process -Id $appPid -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}
Start-Sleep -Milliseconds 500
# 2. 静默安装（与手动双击安装器同一路径）
$rc = -1
try {{
    $p = Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', ('/LOG="' + $log + '"')) -Wait -PassThru
    $rc = $p.ExitCode
}} catch {{ $rc = -2 }}
$info = @{{ exit_code = $rc; from_version = {_ps_quote(from_version)}; to_version = {_ps_quote(to_version)}; log = $log; finished_at = (Get-Date).ToString('s') }}
$info | ConvertTo-Json | Set-Content -Path $result -Encoding UTF8
# 3. 重新打开（成功 = 新版；失败时 current 仍指向旧版 = 旧版）
$exe = Get-ChildItem -Path (Join-Path $root 'current') -Filter '*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($exe) {{
    if ($wsArgs.Count -gt 0) {{ Start-Process -FilePath $exe.FullName -WorkingDirectory $exe.DirectoryName -ArgumentList $wsArgs }}
    else {{ Start-Process -FilePath $exe.FullName -WorkingDirectory $exe.DirectoryName }}
}}
"""
    path = dest_dir / "run_update.ps1"
    path.write_text(script, encoding="utf-8-sig")
    return path


def launch_helper(script: Path) -> None:
    """分离启动助手：主程序退出后它继续运行，不弹黑窗。"""
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def read_and_clear_result(dest_dir: Path) -> dict | None:
    """读取上次升级结果（读完删除，只提示一次）。"""
    path = Path(dest_dir) / RESULT_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else None
