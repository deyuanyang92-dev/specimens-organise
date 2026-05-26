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

# v0.10.8 fix: GitHub Actions Windows runner defaults stdout to cp1252 → UnicodeEncodeError
# when PyInstaller subprocess prints paths containing CJK (bundle dir is "标本入库管理_vX.Y.Z").
# Reconfigure stdout/stderr to UTF-8 before any print or subprocess capture.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

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


# v0.10.8 重新启用增量更新基础设施（v0.8.2 曾删，但用户反馈"为何不能像 VSCode 一样
# 只下小数据"——重新加回，产物：setup_v*.zip（全量主分发）+ app_v*.zip（仅代码 ~5MB）+
# update_manifest_*.json（含 runtime_hash 供 updater 端比对复用本地 runtime）。
# 老用户首次升级仍全量；后续若 Python+PyQt5 版本不变 → 仅下 app_v*.zip 几 MB。


# v0.10.8 partition_bundle: 把 PyInstaller --onedir 产物拆 app/runtime 两部分。
# app: root exe + _internal/specimen_app/** + .update_meta.json (含 runtime_hash)
# runtime: 其他 _internal/** (Python / PyQt5 / 资源等，~60MB，版本间复用率高)
# runtime_hash = 对 runtime 文件清单 + 各文件 sha256 算总 hash，runtime 不变则 hash 同。
APP_INTERNAL_SPECIMEN_APP_SUBPATH = "_internal/specimen_app"


def _list_runtime_relative_paths(bundle_dir: Path) -> list[Path]:
    """返回 bundle_dir 内 "runtime" 部分（_internal/** 但排除 specimen_app/）的相对路径列表，sorted。"""
    runtime_paths: list[Path] = []
    internal_root = bundle_dir / "_internal"
    if not internal_root.is_dir():
        return runtime_paths
    specimen_app_dir = (bundle_dir / APP_INTERNAL_SPECIMEN_APP_SUBPATH).resolve()
    for item in sorted(internal_root.rglob("*")):
        if not item.is_file():
            continue
        try:
            item_resolved = item.resolve()
            item_resolved.relative_to(specimen_app_dir)
            continue  # 在 specimen_app/ 子树里，属于 app 部分
        except ValueError:
            pass
        runtime_paths.append(item.relative_to(bundle_dir))
    return runtime_paths


def _list_app_relative_paths(bundle_dir: Path) -> list[Path]:
    """返回 bundle_dir 内 "app" 部分（root exe + _internal/specimen_app/**）的相对路径列表，sorted。"""
    app_paths: list[Path] = []
    # root 层除 _internal 目录之外的所有文件（exe / .update_meta.json / 其他散文件）
    for item in sorted(bundle_dir.iterdir()):
        if item.is_file():
            app_paths.append(item.relative_to(bundle_dir))
    # _internal/specimen_app/** 全部文件
    specimen_app_dir = bundle_dir / APP_INTERNAL_SPECIMEN_APP_SUBPATH
    if specimen_app_dir.is_dir():
        for item in sorted(specimen_app_dir.rglob("*")):
            if item.is_file():
                app_paths.append(item.relative_to(bundle_dir))
    return app_paths


def _compute_runtime_hash(bundle_dir: Path) -> str:
    """对 runtime 文件清单 + 各文件 sha256 算总 hash。runtime 文件不变则 hash 不变。"""
    aggregator = hashlib.sha256()
    for relative_path in _list_runtime_relative_paths(bundle_dir):
        aggregator.update(relative_path.as_posix().encode("utf-8"))
        aggregator.update(b"\0")
        aggregator.update(sha256(bundle_dir / relative_path).encode("ascii"))
        aggregator.update(b"\0")
    return aggregator.hexdigest()[:12]


def _write_update_meta(bundle_dir: Path, version: str, runtime_hash: str, app_relative_paths: list[Path]) -> None:
    """把 .update_meta.json 写到 bundle_dir 根，updater 用来识别 app-only zip。"""
    meta = {
        "version": version,
        "runtime_hash": runtime_hash,
        "app_files": [p.as_posix() for p in app_relative_paths],
    }
    (bundle_dir / ".update_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_app_only_zip(bundle_dir: Path, release_dir: Path, version: str, platform_tag: str) -> tuple[Path, str]:
    """打包 app-only zip：只含 root exe + _internal/specimen_app/** + .update_meta.json。

    zip 内根目录与 setup_*.zip 一致（``标本入库管理_v{version}/``），方便 updater 解压后识别。
    返回 (zip_path, sha256_hex)。
    """
    versioned_name = bundle_dir.name
    zip_name = f"app_v{version}_{platform_tag}.zip"
    zip_path = release_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    # app 部分包含：root 层全部文件（已含 .update_meta.json） + _internal/specimen_app/**
    app_relative_paths = _list_app_relative_paths(bundle_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path in app_relative_paths:
            arc_name = Path(versioned_name) / relative_path
            archive.write(bundle_dir / relative_path, arc_name.as_posix())
    zip_digest = sha256(zip_path)
    (release_dir / f"{zip_name}.sha256").write_text(
        f"{zip_digest}  {zip_name}\n", encoding="utf-8"
    )
    return zip_path, zip_digest


def _write_update_manifest(release_dir: Path, version: str, platform_tag: str, *, setup_zip_name: str,
                           setup_zip_sha256: str, app_zip_name: str, app_zip_sha256: str,
                           runtime_hash: str) -> Path:
    """update_manifest_{plat}.json：updater 端先拉这个判增量是否可用。

    包含两套下载选项：
    - setup_zip: 全量包，老用户/无匹配 runtime 时走
    - app_zip + runtime_hash: 本地 release 目录中若有相同 runtime_hash 的旧版本，
      复制其 _internal/ 后只下 app_zip 覆盖 specimen_app/ → 真增量
    """
    manifest = {
        "version": version,
        "platform": platform_tag,
        "runtime_hash": runtime_hash,
        "setup_zip": {"name": setup_zip_name, "sha256": setup_zip_sha256},
        "app_zip": {"name": app_zip_name, "sha256": app_zip_sha256},
    }
    manifest_name = f"update_manifest_{platform_tag}.json"
    manifest_path = release_dir / manifest_name
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


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

    # v0.10.8 重启用增量更新：先算 runtime_hash + 写 .update_meta.json，再打 setup zip
    # （含 meta） + app-only zip + manifest。setup_*.zip 仍是主分发，老用户/无匹配 runtime
    # 走它；updater 端可选拉 manifest 走 app_v*.zip 增量。
    runtime_hash = _compute_runtime_hash(versioned_dir)
    app_relative_paths = _list_app_relative_paths(versioned_dir)
    # 先写 meta（让 setup zip 也含），再算 app_relative_paths 时 meta 也算 app 一员
    _write_update_meta(versioned_dir, version, runtime_hash, app_relative_paths)
    # 重算 app_relative_paths 含新写的 .update_meta.json
    app_relative_paths = _list_app_relative_paths(versioned_dir)

    # 完整 zip：主分发包，命名纯 ASCII setup_ 前缀。
    zip_name = f"setup_v{version}_{platform_tag}.zip"
    zip_path = release_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(versioned_dir.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(release_dir))
    zip_digest = sha256(zip_path)
    (release_dir / f"{zip_name}.sha256").write_text(
        f"{zip_digest}  {zip_name}\n", encoding="utf-8"
    )

    # v0.10.8 app-only zip + manifest（增量更新基础设施）
    app_zip_path, app_zip_digest = _build_app_only_zip(versioned_dir, release_dir, version, platform_tag)
    manifest_path = _write_update_manifest(
        release_dir, version, platform_tag,
        setup_zip_name=zip_name, setup_zip_sha256=zip_digest,
        app_zip_name=app_zip_path.name, app_zip_sha256=app_zip_digest,
        runtime_hash=runtime_hash,
    )
    # ASCII-only stdout: Windows GitHub Actions runner default cp1252 can't encode CJK
    print(f"[release] full setup ({zip_path.stat().st_size // 1024 // 1024} MB): {zip_name}")
    print(f"[release] app-only ({app_zip_path.stat().st_size // 1024 // 1024} MB): {app_zip_path.name}")
    print(f"[release] runtime_hash={runtime_hash}  manifest={manifest_path.name}")

    # sha256.txt 保留原有 exe 摘要行（向后兼容），并追加完整 zip 摘要行。
    (release_dir / "sha256.txt").write_text(
        f"{digest}  {versioned_exe.name}\n{zip_digest}  {zip_name}\n", encoding="utf-8"
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
