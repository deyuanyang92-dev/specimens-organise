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

PHOTO_MANAGEMENT_OPTIONS = {
    "copy_with_absolute": "复制到工作区照片库，并记录绝对路径",
    "absolute_only": "仅记录绝对路径，不复制",
    "copy_to_custom_library": "复制到自定义照片库，并记录绝对路径",
}

DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT = "Ctrl+Alt+F"


@dataclass
class AppSettings:
    last_workspace: str = ""
    recent_workspaces: list[str] = field(default_factory=list)
    preview_quality: str = "standard"
    photo_management_mode: str = "copy_with_absolute"
    photo_library_path: str = ""
    search_paths: list[str] = field(default_factory=list)
    show_grid_filenames: bool = True
    photo_filename_fill_shortcut: str = DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT
    window_geometry: str = ""
    splitter_sizes: list = field(default_factory=list)
    check_updates_on_startup: bool = False  # 启动时是否后台检查 GitHub 更新
    last_update_check: str = ""  # 上次检查更新的 ISO 时间戳，用于限频
    carry_over_specimen_fields: bool = True  # 新增标本时是否沿用上一条的标本信息字段
    summary_visible_columns: list[str] = field(default_factory=list)  # 入库汇总宽表可见列（空=用默认集）
    ui_font_size: int = 0  # 全局界面字体大小（pt）；0=未设置，用系统默认。范围 7–24
    image_viewer_path: str = ""  # 自定义图片查看器程序路径；空=用系统默认程序打开原图


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
    photo_management_mode = str(data.get("photo_management_mode", "copy_with_absolute"))
    if photo_management_mode not in PHOTO_MANAGEMENT_OPTIONS:
        photo_management_mode = "copy_with_absolute"
    check_updates_on_startup = data.get("check_updates_on_startup", False)
    if not isinstance(check_updates_on_startup, bool):
        check_updates_on_startup = False
    carry_over_specimen_fields = data.get("carry_over_specimen_fields", True)
    if not isinstance(carry_over_specimen_fields, bool):
        carry_over_specimen_fields = True
    raw_summary_columns = data.get("summary_visible_columns", [])
    if isinstance(raw_summary_columns, list):
        summary_visible_columns = [str(item) for item in raw_summary_columns if item]
    else:
        summary_visible_columns = []
    ui_font_size = data.get("ui_font_size", 0)
    if not isinstance(ui_font_size, int) or isinstance(ui_font_size, bool):
        ui_font_size = 0
    elif ui_font_size > 0:
        ui_font_size = max(7, min(24, ui_font_size))  # 钳制到合理范围
    else:
        ui_font_size = 0
    image_viewer_path = data.get("image_viewer_path", "")
    if not isinstance(image_viewer_path, str):
        image_viewer_path = ""
    return AppSettings(
        last_workspace=str(data.get("last_workspace", "")),
        recent_workspaces=[str(item) for item in data.get("recent_workspaces", []) if item],
        preview_quality=str(data.get("preview_quality", "standard")),
        photo_management_mode=photo_management_mode,
        photo_library_path=str(data.get("photo_library_path", "")),
        search_paths=[str(item) for item in data.get("search_paths", []) if item],
        show_grid_filenames=show_grid_filenames,
        photo_filename_fill_shortcut=photo_filename_fill_shortcut.strip(),
        window_geometry=str(data.get("window_geometry", "")),
        splitter_sizes=splitter_sizes,
        check_updates_on_startup=check_updates_on_startup,
        last_update_check=str(data.get("last_update_check", "")),
        carry_over_specimen_fields=carry_over_specimen_fields,
        summary_visible_columns=summary_visible_columns,
        ui_font_size=ui_font_size,
        image_viewer_path=image_viewer_path,
    )


def save_settings(settings: AppSettings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_workspace": settings.last_workspace,
        "recent_workspaces": settings.recent_workspaces[:10],
        "preview_quality": settings.preview_quality,
        "photo_management_mode": settings.photo_management_mode,
        "photo_library_path": settings.photo_library_path,
        "search_paths": settings.search_paths[:20],
        "show_grid_filenames": settings.show_grid_filenames,
        "photo_filename_fill_shortcut": settings.photo_filename_fill_shortcut or DEFAULT_PHOTO_FILENAME_FILL_SHORTCUT,
        "window_geometry": settings.window_geometry,
        "splitter_sizes": settings.splitter_sizes,
        "check_updates_on_startup": settings.check_updates_on_startup,
        "last_update_check": settings.last_update_check,
        "carry_over_specimen_fields": settings.carry_over_specimen_fields,
        "summary_visible_columns": settings.summary_visible_columns,
        "ui_font_size": settings.ui_font_size,
        "image_viewer_path": settings.image_viewer_path,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def remember_workspace(workspace: Path | str) -> None:
    resolved = str(Path(workspace).resolve())
    settings = load_settings()
    settings.last_workspace = resolved
    settings.recent_workspaces = [resolved] + [item for item in settings.recent_workspaces if item != resolved]
    save_settings(settings)
