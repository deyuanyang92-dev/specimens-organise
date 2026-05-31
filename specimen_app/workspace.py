from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .app_settings import load_settings
from .models import CLASSIFICATION_FILE, INDEX_FILE, PHOTO_FILE, SPECIMEN_FILE, WORKSPACE_CONFIG_FILE


GENERATED_DIR_NAMES = {"build", "dist", "releases"}
DESKTOP_DIR_NAMES = {"desktop", "桌面"}


def is_generated_workspace_path(path: Path | str) -> bool:
    try:
        parts = Path(path).resolve().parts
    except OSError:
        parts = Path(path).parts
    return any(part.lower() in GENERATED_DIR_NAMES for part in parts)


def is_desktop_path(path: Path | str) -> bool:
    """Return True for the user's Desktop directory.

    The app writes workspace metadata, Excel files, SQLite journals, photos,
    and update releases under the workspace root. Using Desktop as the root
    pollutes the user's desktop with internal files, so treat it as unsafe.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    if resolved.name.lower() not in DESKTOP_DIR_NAMES:
        return False
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    if resolved == home:
        return False
    # Covers ~/Desktop, ~/桌面, and cloud-redirected paths like
    # ~/OneDrive/Desktop. A project folder outside the user's home is not
    # rejected just because it happens to be named "Desktop".
    return home == resolved or home in resolved.parents


def is_unsafe_workspace_root(path: Path | str) -> bool:
    """判断路径是否"范围过大、不该作为工作区"：文件系统根 / 盘符根 / 用户主目录 / 桌面。

    把这类目录当工作区，会让全工作区扫描（图片索引等）遍历海量文件，可能拖垮整机。
    桌面还会被工作区内部文件污染。
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    # 文件系统根 / 盘符根：自身的 parent 等于自身（如 "/"、"C:\\"）。
    if resolved.parent == resolved:
        return True
    try:
        if resolved == Path.home().resolve():
            return True
    except (OSError, RuntimeError):
        pass
    if is_desktop_path(resolved):
        return True
    return False


def has_workspace_templates(path: Path | str) -> bool:
    root = Path(path)
    return (root / "字段模版" / "表格信息预设字段.xlsx").exists()


def has_workspace_data(path: Path | str) -> bool:
    root = Path(path)
    data_dir = root / "数据"
    return any(
        (data_dir / file_name).exists()
        for file_name in [WORKSPACE_CONFIG_FILE, SPECIMEN_FILE, PHOTO_FILE, CLASSIFICATION_FILE, INDEX_FILE]
    )


def is_workspace(path: Path | str) -> bool:
    return (
        not is_generated_workspace_path(path)
        and not is_unsafe_workspace_root(path)
        and (has_workspace_data(path) or has_workspace_templates(path))
    )


def default_workspace() -> Path | None:
    candidates: list[Path] = []
    settings = load_settings()
    if settings.last_workspace:
        # 旧：last_workspace 仍与其他候选一起遍历。新：若有效则直接返回，跳过后续 I/O 扫描。
        try:
            p = Path(settings.last_workspace).resolve()
            if not is_generated_workspace_path(p) and not is_unsafe_workspace_root(p) and has_workspace_data(p):
                return p
        except OSError:
            pass
        candidates.append(Path(settings.last_workspace))
    candidates.append(Path.cwd())
    executable_parent = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    candidates.extend([executable_parent, executable_parent.parent, executable_parent.parent.parent])
    for recent in settings.recent_workspaces:
        candidates.append(Path(recent))

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_generated_workspace_path(resolved):
            continue
        if is_unsafe_workspace_root(resolved):
            continue
        if has_workspace_data(resolved):
            return resolved
    return None


def initialize_workspace(target: Path | str, template_source: Path | str | None = None) -> None:
    root = Path(target).resolve()
    if is_unsafe_workspace_root(root):
        raise ValueError(f"不能把桌面、用户主目录、盘符根或文件系统根作为工作区：{root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "数据").mkdir(exist_ok=True)
    if (root / "字段模版").exists():
        return
    if template_source:
        source = Path(template_source).resolve() / "字段模版"
        if source.exists():
            shutil.copytree(source, root / "字段模版")
