from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
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


# 增量更新：把 PyInstaller onedir 产物拆成"应用分区"和"运行时分区"。
# 应用分区每次发版都变（应用代码），运行时分区几乎不变（Python / PyQt5 等第三方）。
APP_META_FILE = ".update_meta.json"


def partition_bundle(versioned_dir: Path, app_pkg: str = "specimen_app") -> tuple[list[str], list[str]]:
    """把 bundle 目录下的文件分成 (应用分区, 运行时分区)，路径均为相对 bundle 目录的 posix 字符串。

    规则（确定、不依赖上一次构建）：
    - 应用分区 = 根目录的 exe（文件名以 APP_NAME 开头）
        ∪ ``_internal/{app_pkg}/**`` ∪ APP_META_FILE
        ∪ ``_internal/docs/**``（用户手册随应用一起更新，体积小，不进 runtime_hash —
           规范化软件设计 2026-05 新增）
    - 运行时分区 = 其余全部
    """
    app_files: list[str] = []
    runtime_files: list[str] = []
    for path in sorted(versioned_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(versioned_dir)
        rel_posix = rel.as_posix()
        parts = rel.parts
        is_app = (
            rel_posix == APP_META_FILE
            or (len(parts) == 1 and parts[0].startswith(APP_NAME))
            or (len(parts) >= 2 and parts[0] == "_internal" and parts[1] == app_pkg)
            or (len(parts) >= 2 and parts[0] == "_internal" and parts[1] == "docs")
        )
        (app_files if is_app else runtime_files).append(rel_posix)
    return app_files, runtime_files


def _zip_files(zip_path: Path, base_dir: Path, rel_files: list[str], arc_prefix: str) -> str:
    """把 base_dir 下的指定相对文件打进 zip，arcname 加 arc_prefix 前缀。返回 zip 的 sha256。"""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in rel_files:
            archive.write(base_dir / rel, f"{arc_prefix}/{rel}")
    return sha256(zip_path)


def build_release(version: str, project_root: Path, icon_path: Path | None = None) -> Path:
    release_dir = project_root / "releases" / f"v{version}"
    release_dir.mkdir(parents=True, exist_ok=True)

    suffix = exe_suffix()
    versioned_name = f"{APP_NAME}_v{version}"
    work_path = project_root / "build" / f"pyinstaller_v{version.replace('.', '_')}"
    spec_path = project_root / "build" / "specs"
    spec_path.mkdir(parents=True, exist_ok=True)

    # Generate or select icon file for PyInstaller.
    icon_file: Path | None = None
    if icon_path is not None:
        icon_file = icon_path if icon_path.is_absolute() else project_root / icon_path
        if not icon_file.exists():
            raise FileNotFoundError(f"指定图标不存在: {icon_file}")
        print(f"[icon] using specified icon: {icon_file}")
    else:
        # 未显式指定时：优先用预生成的默认图标变体（assets/），否则回退程序生成图标。
        from specimen_app.icon import DEFAULT_APP_ICON_VARIANT
        default_variant_ico = (
            project_root / "assets" / "icons" / "app-icon-variants"
            / DEFAULT_APP_ICON_VARIANT / f"{DEFAULT_APP_ICON_VARIANT}.ico"
        )
        if default_variant_ico.exists():
            icon_file = default_variant_ico
            print(f"[icon] using default variant: {icon_file}")
        else:
            icon_dir = project_root / "build" / "icons"
            icon_dir.mkdir(parents=True, exist_ok=True)
            icon_file = icon_dir / "app_icon.ico"
            try:
                from specimen_app.icon import create_app_icon
                img = create_app_icon()
                img.save(str(icon_file), "ICO")
                print(f"[icon] generated: {icon_file}")
            except Exception as exc:
                print(f"[icon] WARNING: generation failed ({exc}), no icon")
                icon_file = None

    # 把图标变体素材打进包，运行时「设置 → 应用图标」才能切换。
    icon_variants_dir = project_root / "assets" / "icons" / "app-icon-variants"

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
    if icon_file and icon_file.exists():
        command.extend(["--icon", str(icon_file)])
    if icon_variants_dir.is_dir():
        # PyInstaller --add-data 用 os.pathsep 分隔 src/dest（Win=";"，Linux=":"）。
        command.extend([
            "--add-data",
            f"{icon_variants_dir}{os.pathsep}assets/icons/app-icon-variants",
        ])
    # 兜底字段模版：打开缺 字段模版/ 的工作区时从这里补齐（种名/科名自动匹配依赖它）。
    bundled_templates = project_root / "specimen_app" / "字段模版"
    if bundled_templates.is_dir():
        command.extend([
            "--add-data",
            f"{bundled_templates}{os.pathsep}specimen_app/字段模版",
        ])
    # WoRMS 启动缓存：装机自带 ~15MB gz，离线环境开箱即可查 WoRMS 分类。
    # 缺失时不报错（开发态或精简发布版），运行时 ensure_bootstrap_cache() 会跳过。
    worms_bootstrap = project_root / "specimen_app" / "assets" / "worms_cache_bootstrap.sqlite.gz"
    if worms_bootstrap.is_file():
        command.extend([
            "--add-data",
            f"{worms_bootstrap}{os.pathsep}specimen_app/assets",
        ])
    # 用户手册（规范化软件设计 2026-05 新增）：docs/manual/*.md + 图片随包发布，
    # Help → 使用说明 由 specimen_app/help_dialog.py 的 QTextBrowser + markdown 库即时渲染。
    # 缺失时不报错；运行时 manual_root() 会返回 None，Help → 使用说明 弹兜底提示。
    docs_manual = project_root / "docs" / "manual"
    if docs_manual.is_dir():
        command.extend([
            "--add-data",
            f"{docs_manual}{os.pathsep}docs/manual",
        ])
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

    # 打包成可分发 zip，供应用内"检查更新"下载。
    # zip 内根目录即 onedir 文件夹（标本入库管理_v{version}/），与 release_manager
    # ._find_executable 的发现规则一致：解压到 releases/v{version}/ 后即可被识别。
    platform_tag = "windows" if IS_WINDOWS else ("macos" if sys.platform == "darwin" else "linux")

    # 增量更新：拆分前先做分区，并计算运行时分区的内容 hash。
    # .update_meta.json 须在打包前写进 bundle 目录（属应用分区），让本目录日后可作为运行时复用源。
    app_files, runtime_files = partition_bundle(versioned_dir)
    runtime_hash = hashlib.sha256(
        "\n".join(f"{rel}:{sha256(versioned_dir / rel)}" for rel in sorted(runtime_files)).encode("utf-8")
    ).hexdigest()[:12]
    app_files = sorted(app_files + [APP_META_FILE])  # 把 .update_meta.json 自身补进应用分区
    (versioned_dir / APP_META_FILE).write_text(
        json.dumps(
            {"version": version, "runtime_hash": runtime_hash, "app_files": app_files},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # 完整 zip：用户首次安装下载的包。命名改为纯 ASCII（旧：APP_NAME 前缀含中文，
    # GitHub Actions 上传后文件名中文部分丢失，在 Releases 页显示为 _v{ver}_{plat}.zip，
    # 用户看不懂；现改为 setup_ 前缀，意义明确、不含特殊字符）。
    zip_name = f"setup_v{version}_{platform_tag}.zip"
    zip_path = release_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(versioned_dir.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(release_dir))
    zip_digest = sha256(zip_path)

    # 增量更新用的拆分包：应用包（小、每版都变）+ 运行时包（大、按内容 hash 命名）。
    app_zip_name = f"app_v{version}_{platform_tag}.zip"
    runtime_zip_name = f"runtime_{platform_tag}_{runtime_hash}.zip"
    app_zip_digest = _zip_files(release_dir / app_zip_name, versioned_dir, app_files, versioned_name)
    runtime_zip_digest = _zip_files(release_dir / runtime_zip_name, versioned_dir, runtime_files, versioned_name)

    # 每个 zip 配一个独立的 .sha256（按文件名，避免多平台 CI 互相覆盖）。
    for name, dig in (
        (zip_name, zip_digest),
        (app_zip_name, app_zip_digest),
        (runtime_zip_name, runtime_zip_digest),
    ):
        (release_dir / f"{name}.sha256").write_text(f"{dig}  {name}\n", encoding="utf-8")

    # sha256.txt 保留原有 exe 摘要行（向后兼容），并追加完整 zip 摘要行。
    (release_dir / "sha256.txt").write_text(
        f"{digest}  {versioned_exe.name}\n{zip_digest}  {zip_name}\n", encoding="utf-8"
    )

    # update_manifest_{platform}.json：release 级资产，客户端据此决定增量/完整下载。
    # 按平台命名，避免多平台 CI 互相覆盖。
    manifest_name = f"update_manifest_{platform_tag}.json"
    (release_dir / manifest_name).write_text(
        json.dumps(
            {
                "version": version,
                "platform": platform_tag,
                "app_zip": app_zip_name,
                "app_sha256": app_zip_digest,
                "runtime_zip": runtime_zip_name,
                "runtime_sha256": runtime_zip_digest,
                "runtime_hash": runtime_hash,
                "app_files": app_files,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

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
        "icon": str(icon_file) if icon_file else "",
        "sha256": digest,
        "zip": zip_name,
        "zip_sha256": zip_digest,
        "app_zip": app_zip_name,
        "app_zip_sha256": app_zip_digest,
        "runtime_zip": runtime_zip_name,
        "runtime_zip_sha256": runtime_zip_digest,
        "runtime_hash": runtime_hash,
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
    parser.add_argument("--icon", default="", help="可选：指定 PyInstaller 使用的图标文件，例如 .ico 或 .icns")
    args = parser.parse_args()
    icon = Path(args.icon) if args.icon else None
    exe = build_release(args.version, Path(args.project_root).resolve(), icon)
    sys.stdout.buffer.write((str(exe) + "\n").encode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
