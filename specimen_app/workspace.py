from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .app_settings import load_settings
from .models import CLASSIFICATION_FILE, INDEX_FILE, PHOTO_FILE, SPECIMEN_FILE, WORKSPACE_CONFIG_FILE


GENERATED_DIR_NAMES = {"build", "dist", "releases"}


def is_generated_workspace_path(path: Path | str) -> bool:
    try:
        parts = Path(path).resolve().parts
    except OSError:
        parts = Path(path).parts
    return any(part.lower() in GENERATED_DIR_NAMES for part in parts)


def is_unsafe_workspace_root(path: Path | str) -> bool:
    """判断路径是否"范围过大、不该作为工作区"：文件系统根 / 盘符根 / 用户主目录。

    把这类目录当工作区，会让全工作区扫描（图片索引等）遍历海量文件，可能拖垮整机。
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
    return not is_generated_workspace_path(path) and (has_workspace_data(path) or has_workspace_templates(path))


def default_workspace() -> Path | None:
    candidates: list[Path] = []
    settings = load_settings()
    if settings.last_workspace:
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
        if has_workspace_data(resolved):
            return resolved
    return None


def initialize_workspace(target: Path | str, template_source: Path | str | None = None) -> None:
    root = Path(target).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "数据").mkdir(exist_ok=True)
    if (root / "字段模版").exists():
        return
    if template_source:
        source = Path(template_source).resolve() / "字段模版"
        if source.exists():
            shutil.copytree(source, root / "字段模版")
