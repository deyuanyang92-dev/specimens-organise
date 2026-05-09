from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR_NAME = "标本入库管理"
SETTINGS_FILE = "settings.json"


PREVIEW_QUALITY_OPTIONS = {
    "compressed": "压缩预览（800×600，省内存）",
    "standard": "标准预览（1600×1200）",
    "original": "原始质量（不压缩）",
}

PREVIEW_QUALITY_SIZES = {
    "compressed": (800, 600),
    "standard": (1600, 1200),
    "original": None,
}

DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT = "Ctrl+Alt+F"


@dataclass
class AppSettings:
    last_workspace: str = ""
    recent_workspaces: list[str] = field(default_factory=list)
    preview_quality: str = "standard"
    search_paths: list[str] = field(default_factory=list)
    show_grid_filenames: bool = True
    photo_filename_fill_shortcut: str = DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT
    window_geometry: str = ""
    splitter_sizes: list = field(default_factory=list)


def app_config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".specimen_inventory"


def settings_path() -> Path:
    return app_config_dir() / SETTINGS_FILE


def load_settings() -> AppSettings:
    path = settings_path()
    if not path.exists():
        return AppSettings()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    show_grid_filenames = data.get("show_grid_filenames", True)
    if not isinstance(show_grid_filenames, bool):
        show_grid_filenames = True
    photo_filename_fill_shortcut = data.get("photo_filename_fill_shortcut", DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT)
    if not isinstance(photo_filename_fill_shortcut, str) or not photo_filename_fill_shortcut.strip():
        photo_filename_fill_shortcut = DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT
    raw_sizes = data.get("splitter_sizes", [])
    if raw_sizes and isinstance(raw_sizes[0], list):
        splitter_sizes = raw_sizes
    else:
        splitter_sizes = []
    return AppSettings(
        last_workspace=str(data.get("last_workspace", "")),
        recent_workspaces=[str(item) for item in data.get("recent_workspaces", []) if item],
        preview_quality=str(data.get("preview_quality", "standard")),
        search_paths=[str(item) for item in data.get("search_paths", []) if item],
        show_grid_filenames=show_grid_filenames,
        photo_filename_fill_shortcut=photo_filename_fill_shortcut.strip(),
        window_geometry=str(data.get("window_geometry", "")),
        splitter_sizes=splitter_sizes,
    )


def save_settings(settings: AppSettings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_workspace": settings.last_workspace,
        "recent_workspaces": settings.recent_workspaces[:10],
        "preview_quality": settings.preview_quality,
        "search_paths": settings.search_paths[:20],
        "show_grid_filenames": settings.show_grid_filenames,
        "photo_filename_fill_shortcut": settings.photo_filename_fill_shortcut or DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT,
        "window_geometry": settings.window_geometry,
        "splitter_sizes": settings.splitter_sizes,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def remember_workspace(workspace: Path | str) -> None:
    resolved = str(Path(workspace).resolve())
    settings = load_settings()
    settings.last_workspace = resolved
    settings.recent_workspaces = [resolved] + [item for item in settings.recent_workspaces if item != resolved]
    save_settings(settings)
