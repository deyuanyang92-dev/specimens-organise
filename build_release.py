from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from specimen_app import __version__


APP_NAME = "标本入库管理"

IS_WINDOWS = sys.platform == "win32"


def exe_suffix() -> str:
    return ".exe" if IS_WINDOWS else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(version: str, project_root: Path) -> Path:
    release_dir = project_root / "releases" / f"v{version}"
    release_dir.mkdir(parents=True, exist_ok=True)

    suffix = exe_suffix()
    versioned_name = f"{APP_NAME}_v{version}"
    work_path = project_root / "build" / f"pyinstaller_v{version.replace('.', '_')}"
    spec_path = project_root / "build" / "specs"
    spec_path.mkdir(parents=True, exist_ok=True)

    # Generate icon file for PyInstaller
    icon_dir = project_root / "build" / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_ico = icon_dir / "app_icon.ico"
    try:
        from specimen_app.icon import create_app_icon
        img = create_app_icon()
        img.save(str(icon_ico), "ICO")
        print(f"图标已生成: {icon_ico}")
    except Exception as exc:
        print(f"警告：图标生成失败 ({exc})，将使用默认图标")
        icon_ico = None

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        versioned_name,
        "--distpath",
        str(release_dir),
        "--workpath",
        str(work_path),
        "--specpath",
        str(spec_path),
        "--hidden-import",
        "tifffile",
    ]
    if icon_ico and icon_ico.exists():
        command.extend(["--icon", str(icon_ico)])
    command.append("run_app.py")
    # 原代码保留说明：这里曾多出一个独立的 "]"，会导致 build_release.py 语法错误。
    subprocess.run(command, cwd=project_root, check=True)

    versioned_dir = release_dir / versioned_name
    versioned_exe = versioned_dir / f"{APP_NAME}_v{version}{suffix}"

    stable_dir = project_root / "dist" / APP_NAME
    stable_exe = stable_dir / f"{APP_NAME}_v{version}{suffix}"
    stable_updated = False
    stable_error = ""
    try:
        if stable_dir.exists():
            shutil.rmtree(stable_dir)
        shutil.copytree(versioned_dir, stable_dir)
        if not IS_WINDOWS:
            stable_exe.chmod(stable_exe.stat().st_mode | 0o111)
        stable_updated = True
    except OSError as exc:
        stable_error = str(exc)

    digest = sha256(versioned_exe)
    (release_dir / "sha256.txt").write_text(f"{digest}  {versioned_exe.name}\n", encoding="utf-8")

    build_info = {
        "app_name": APP_NAME,
        "version": version,
        "built_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "source": str(project_root),
        "exe": versioned_exe.name,
        "stable_exe": str(stable_exe),
        "stable_updated": stable_updated,
        "stable_error": stable_error,
        "sha256": digest,
    }
    (release_dir / "build_info.json").write_text(json.dumps(build_info, ensure_ascii=False, indent=2), encoding="utf-8")

    notes = release_dir / "release_notes.md"
    if not notes.exists():
        notes.write_text(
            f"# {APP_NAME} v{version}\n\n"
            "## 安全修复\n\n"
            "- 修复路径遍历漏洞，防止恶意 Excel 中的相对路径指向工作区外的文件。\n"
            "- 修复版本管理器可执行文件启动验证，仅允许受信任目录内的程序。\n"
            "- 修复快照路径验证，防止数据恢复时读取版本目录外的文件。\n"
            "- 限制外部打开仅允许图片文件格式。\n"
            "\n"
            "## 性能优化\n\n"
            "- 增加 Excel 数据内存缓存，避免重复解析文件；列表刷新从 O(N) 降为 O(1)。\n"
            "- 标本状态批量计算，一次读取代替逐条查询。\n"
            "- 合并修改日志写入，减少文件 I/O 次数。\n"
            "- 大 TIFF 缩略图使用 stride 下采样，500MB+ 图片不再 OOM 崩溃。\n"
            "\n"
            "## 跨平台支持\n\n"
            "- 支持 Linux 桌面运行和打包。\n"
            "- 标记为可选依赖，未安装时仍可通过按钮添加照片。\n"
            "- 版本管理器跨平台识别可执行文件（Windows .exe / Linux 可执行文件 / AppImage）。\n",
            encoding="utf-8",
        )
    return versioned_exe


def main() -> None:
    os_label = "Windows" if IS_WINDOWS else "Linux"
    parser = argparse.ArgumentParser(description=f"构建 {APP_NAME} {os_label} release")
    parser.add_argument("--version", default=__version__, help="发布版本号，默认读取 specimen_app.__version__")
    parser.add_argument("--project-root", default=".", help="项目根目录")
    args = parser.parse_args()
    exe = build_release(args.version, Path(args.project_root).resolve())
    print(exe)


if __name__ == "__main__":
    main()
