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

# 规范化软件设计 2026-05 新增:内存档位选项。
# 5 档对应不同的缩略图缓存 / 缩略图并发 / Excel _row_cache LRU 上限,详见 env_detect.memory_profile_params。
MEMORY_PROFILE_OPTIONS: dict[str, str] = {
    "extra_low": "极低 (≤ 1GB 机器,8MB 缓存,1 并发)",
    "low": "低 (1-3GB,16MB 缓存,1 并发)",
    "auto": "自动 (按 RAM 检测,默认)",
    "high": "高 (8-16GB,128MB 缓存,4 并发)",
    "extra_high": "极高 (16GB+ 工作站,256MB 缓存,4 并发,适合超大汇总)",
}


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
    cursor_style: str = "default"  # 趣味光标样式 key（见 cursors.CURSOR_STYLE_OPTIONS）；default=系统箭头
    app_icon_variant: str = "specimen_blue"  # 应用图标变体 key（见 icon.APP_ICON_VARIANTS）
    auto_save_enabled: bool = True  # 录入是否自动保存（输入停 0.5s 自动写）；关时靠手动「保存」按钮
    # 规范化软件设计 2026-05 新增：工具栏布局 / 辅助工具栏可见性 / 快捷键自定义
    # toolbar_layout: 主/辅栏的 action_id 顺序列表；空 dict / 缺 key 时回落到 TOOLBAR_DEFAULT_LAYOUT。
    # action_id 详见 ui.TOOLBAR_ACTIONS。
    toolbar_layout: dict = field(default_factory=dict)
    aux_toolbar_visible: bool = False  # 辅助工具栏默认隐藏（视图菜单可勾选打开）
    # custom_shortcuts: {action_id: keyseq_str}；空 dict 全用默认。
    # 可绑定的 action_id 详见 ui.SHORTCUTABLE_ACTIONS。
    custom_shortcuts: dict = field(default_factory=dict)
    # 规范化软件设计 2026-05 新增:内存档位 (extra_low/low/auto/high/extra_high)
    # 控制 ThumbnailCache 大小 + ThumbnailWorker 并发 + _row_cache LRU 上限。
    # 默认 "auto" 按 RAM 检测。详见 MEMORY_PROFILE_OPTIONS。
    memory_profile: str = "auto"
    # 入库人员管理 2026-05 新增:全局团队成员库。
    # 每个 dict 含 name/pinyin/role/starred/pinned/default_purpose/note/created_at/last_used_at/color_hint
    # 工作区 `数据/入库人员.xlsx` 是真权威,settings 这层是本地缓存,双向同步。
    # 字段详见 persons_store.TeamMember。
    team_members: list = field(default_factory=list)
    # 当前选中的录入员姓名 (状态栏下拉记忆;空 = 未设置)。
    current_recorder: str = ""


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
    # 趣味光标样式：旧 settings.json 无此键 -> 默认箭头；非法值也回退默认。
    from .cursors import CURSOR_STYLE_OPTIONS  # 局部 import：cursors 只依赖 PyQt，无循环
    cursor_style = str(data.get("cursor_style", "default"))
    if cursor_style not in CURSOR_STYLE_OPTIONS:
        cursor_style = "default"
    # 应用图标变体：旧 settings.json 无此键 -> 默认变体；非法值也回退默认。
    from .icon import APP_ICON_VARIANTS, DEFAULT_APP_ICON_VARIANT
    app_icon_variant = str(data.get("app_icon_variant", DEFAULT_APP_ICON_VARIANT))
    if app_icon_variant not in APP_ICON_VARIANTS:
        app_icon_variant = DEFAULT_APP_ICON_VARIANT
    # 自动保存：旧 settings.json 无此键 -> 默认 True（保持原有"一直自动保存"行为）。
    auto_save_enabled = data.get("auto_save_enabled", True)
    if not isinstance(auto_save_enabled, bool):
        auto_save_enabled = True
    # 工具栏布局（规范化软件设计 2026-05 新增）：dict 失败 -> 空 dict，启动时回落默认。
    raw_layout = data.get("toolbar_layout", {})
    toolbar_layout: dict = {}
    if isinstance(raw_layout, dict):
        for key in ("main", "aux"):
            raw_list = raw_layout.get(key, [])
            if isinstance(raw_list, list):
                toolbar_layout[key] = [str(x) for x in raw_list if isinstance(x, str)]
    aux_toolbar_visible = data.get("aux_toolbar_visible", False)
    if not isinstance(aux_toolbar_visible, bool):
        aux_toolbar_visible = False
    raw_shortcuts = data.get("custom_shortcuts", {})
    custom_shortcuts: dict = {}
    if isinstance(raw_shortcuts, dict):
        for k, v in raw_shortcuts.items():
            if isinstance(k, str) and isinstance(v, str):
                custom_shortcuts[k] = v
    # 内存档位:旧 settings.json 无此键 -> "auto";非法值也回落 "auto"。
    memory_profile = str(data.get("memory_profile", "auto"))
    if memory_profile not in MEMORY_PROFILE_OPTIONS:
        memory_profile = "auto"
    # 入库人员管理 2026-05:旧 settings.json 无此键 -> 空 list。
    raw_members = data.get("team_members", [])
    team_members: list = []
    if isinstance(raw_members, list):
        for m in raw_members:
            if isinstance(m, dict) and m.get("name"):
                team_members.append(m)
    current_recorder = str(data.get("current_recorder", ""))
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
        cursor_style=cursor_style,
        app_icon_variant=app_icon_variant,
        auto_save_enabled=auto_save_enabled,
        toolbar_layout=toolbar_layout,
        aux_toolbar_visible=aux_toolbar_visible,
        custom_shortcuts=custom_shortcuts,
        memory_profile=memory_profile,
        team_members=team_members,
        current_recorder=current_recorder,
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
        "cursor_style": settings.cursor_style,
        "app_icon_variant": settings.app_icon_variant,
        "auto_save_enabled": settings.auto_save_enabled,
        "toolbar_layout": settings.toolbar_layout,
        "aux_toolbar_visible": settings.aux_toolbar_visible,
        "custom_shortcuts": settings.custom_shortcuts,
        "memory_profile": settings.memory_profile,
        "team_members": settings.team_members,
        "current_recorder": settings.current_recorder,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def remember_workspace(workspace: Path | str) -> None:
    resolved = str(Path(workspace).resolve())
    settings = load_settings()
    settings.last_workspace = resolved
    settings.recent_workspaces = [resolved] + [item for item in settings.recent_workspaces if item != resolved]
    save_settings(settings)
