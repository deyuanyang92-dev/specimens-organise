from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classification_fields import (
    CLASSIFICATION_COLUMNS,
    CLASSIFICATION_SUMMARY_FIELDS,
    REQUIRED_CLASSIFICATION_COLUMNS,
)


SPECIMEN_FILE = "标本信息.xlsx"
PHOTO_FILE = "照片信息.xlsx"
CLASSIFICATION_FILE = "分类信息.xlsx"  # 也供 server_sync.preview_aggregate 引用
INDEX_FILE = "编号索引.xlsx"
CHANGE_LOG_FILE = "修改记录.xlsx"
ACTION_LOG_FILE = "操作记录.xlsx"
DATA_VERSION_LOG_FILE = "数据版本记录.xlsx"
ALLOC_LOG_FILE = "编号分发记录.xlsx"
WORKSPACE_CONFIG_FILE = "工作区配置.json"
DATA_VERSION_DIR = "数据版本"
CURRENT_DATA_SCHEMA_VERSION = "1.1.2"

SPECIMEN_HEADERS = [
    "入库编号*",
    "管内编号*",
    "保存方式",
    "采集日期",
    "采集地点缩写*",
    "入库日期",
    "标本存放位置",
    "信息录入人员",
    "核对人员",
    "备注",
]

PHOTO_HEADERS = [
    "入库编号*",
    "文件名",
    "相对路径",
    "绝对路径",
    "描述",
    "来源工作区根路径",
    "原始文件名",
    "原始路径",
    "文件SHA256",
    "文件大小",
    "归档时间",
    "归档状态",
]

CLASSIFICATION_HEADERS = list(CLASSIFICATION_COLUMNS)

INDEX_HEADERS = ["入库编号", "record_id", "创建时间", "来源工作区", "来源记录ID", "记录指纹"]

CHANGE_LOG_HEADERS = [
    "入库编号",
    "信息类别",
    "字段名",
    "旧值",
    "新值",
    "修改时间",
    "操作类型",
]

CHANGE_SUMMARY_HEADERS = [
    "入库编号",
    "创建时间",
    "第一次修改时间",
    "第二次修改时间",
    "最近修改时间",
    "修改次数",
]

ACTION_LOG_HEADERS = [
    "操作ID",
    "时间",
    "操作类型",
    "入库编号",
    "信息类别",
    "字段名",
    "旧值JSON",
    "新值JSON",
    "是否撤销",
]

DATA_VERSION_LOG_HEADERS = [
    "版本ID",
    "时间",
    "操作类型",
    "软件版本",
    "数据结构版本",
    "操作者",
    "摘要",
    "快照路径",
]

ALLOC_LOG_HEADERS = [
    "记录ID",
    "时间",
    "类型",        # 批量领取 / 任务开始 / 任务结束
    "人员",
    "用途",
    "备注",
    "编号系列",
    "编号起始",
    "编号结束",
    "数量",
    "关联任务ID",  # 任务结束时指向对应的任务开始记录ID
]

SPECIMEN_REQUIRED = ["入库编号*", "管内编号*", "采集地点缩写*"]
CLASSIFICATION_REQUIRED = list(REQUIRED_CLASSIFICATION_COLUMNS)

SAVE_METHOD_OPTIONS = ["9E", "7E", "79", "RE", "FE"]

# 录入加速：一批标本里往往相同的标本信息字段。用于"沿用上条"和"多选批量设置"。
# 旧：("标本存放位置", "信息录入人员", "核对人员", "保存方式") —— 按需求补入 "备注"，
# 录入阶段同批标本备注常相同，沿用/批量设置都带上减少重复输入。
CARRY_OVER_SPECIMEN_FIELDS = ("标本存放位置", "信息录入人员", "核对人员", "保存方式", "备注")

# 入库汇总视图：把分散在多个 Excel 的字段汇总成一张宽表（纯内存视图，不改任何文件结构）。
# PHOTO_COUNT_COLUMN 是计算列；SPECIMEN_HEADERS 与 CLASSIFICATION_HEADERS 都含"备注"，
# 汇总表里 classification 的"备注"用 CLASSIFICATION_NOTE_DISPLAY 消歧（仅显示名，回写时映射回真实列名）。
PHOTO_COUNT_COLUMN = "照片数"
CLASSIFICATION_NOTE_DISPLAY = "分类备注"
# 照片聚合列：一个入库编号对应多张照片，下列每格 = 该编号所有照片对应值的拼接（list 值）。
PHOTO_FILENAME_COLUMN = "照片文件名"
PHOTO_PATH_COLUMN = "照片绝对路径"
PHOTO_DESC_COLUMN = "照片描述"
PHOTO_AGGREGATE_COLUMNS = [PHOTO_FILENAME_COLUMN, PHOTO_PATH_COLUMN, PHOTO_DESC_COLUMN]

# 汇总表列顺序：入库编号* + 标本其余列 + 分类其余列（备注消歧）+ 照片数 + 照片聚合列。
SUMMARY_COLUMNS = (
    ["入库编号*"]
    + [col for col in SPECIMEN_HEADERS if col != "入库编号*"]
    + [
        CLASSIFICATION_NOTE_DISPLAY if col == "备注" else col
        for col in CLASSIFICATION_HEADERS
        if col != "入库编号*"
    ]
    + [PHOTO_COUNT_COLUMN]
    + PHOTO_AGGREGATE_COLUMNS
)

# 汇总列 -> (category, excel_field)。category="readonly" 表示不可编辑（主键 / 计算列）。
# 可编辑列回写时按 category 调 ExcelStore.set_fields(category, voucher, {excel_field: value})。
SUMMARY_COLUMN_SOURCE: dict[str, tuple[str, str]] = {"入库编号*": ("readonly", "入库编号*")}
for _col in SPECIMEN_HEADERS:
    if _col != "入库编号*":
        SUMMARY_COLUMN_SOURCE[_col] = ("specimen", _col)
for _col in CLASSIFICATION_HEADERS:
    if _col == "入库编号*":
        continue
    _display = CLASSIFICATION_NOTE_DISPLAY if _col == "备注" else _col
    SUMMARY_COLUMN_SOURCE[_display] = ("classification", _col)
SUMMARY_COLUMN_SOURCE[PHOTO_COUNT_COLUMN] = ("readonly", PHOTO_COUNT_COLUMN)
for _col in PHOTO_AGGREGATE_COLUMNS:
    SUMMARY_COLUMN_SOURCE[_col] = ("readonly", _col)  # 照片聚合列只读，不回写
del _col, _display

# 入库汇总对话框默认显示的列（其余列默认隐藏，用户可在表头右键切换）。
SUMMARY_DEFAULT_VISIBLE_COLUMNS = [
    "入库编号*",
    "管内编号*",
    "保存方式",
    "采集日期",
    "种名*",
    "科*",
    PHOTO_COUNT_COLUMN,
]

CATEGORY_FILES = {
    "specimen": SPECIMEN_FILE,
    "photo": PHOTO_FILE,
    "classification": CLASSIFICATION_FILE,
}

CATEGORY_HEADERS = {
    "specimen": SPECIMEN_HEADERS,
    "photo": PHOTO_HEADERS,
    "classification": CLASSIFICATION_HEADERS,
}

DISPLAY_CATEGORY_NAMES = {
    "specimen": "标本信息",
    "photo": "照片信息",
    "classification": "分类信息",
}


class WorkspaceError(RuntimeError):
    """Base class for workspace-level failures."""


class WorkspaceLockedError(WorkspaceError):
    """Raised when the workspace lock is held by another process."""


class WorkspaceNotInitializedError(WorkspaceError):
    """Raised when a selected directory has not been initialized as a workspace."""


class DuplicateVoucherError(WorkspaceError):
    """Raised when duplicate voucher numbers are found."""


class ImportConflictError(WorkspaceError):
    """Raised when an import has blocking voucher conflicts."""

    def __init__(self, message: str, report_path: Path | None = None):
        super().__init__(message)
        self.report_path = report_path


# 规范化软件设计 2026-05 P1 优化:frozen dataclass 加 slots=True 省 __dict__ overhead。
# Python 3.10+ 原生支持;3.13 项目内可用。5000 凭证 × StatusFlags ~ 省 750KB。
@dataclass(frozen=True, slots=True)
class StatusFlags:
    specimen_complete: bool
    has_photo: bool
    classification_complete: bool

    def label(self) -> str:
        return "".join("√" if value else "×" for value in (
            self.specimen_complete,
            self.has_photo,
            self.classification_complete,
        ))


@dataclass(frozen=True, slots=True)  # P1 优化:slots 省 __dict__ overhead
class ImportResult:
    imported: int
    skipped: int
    photos_imported: int
    report_path: Path | None = None
    # M4 跨 voucher 同 SHA256 照片审核：当 import_workspace 的 photo_duplicate_policy
    # 为 "report"/"skip" 时，命中"中心已存在同 SHA256 但属于不同 voucher"的源 photo 行
    # 记录到这里。"report" 模式下源照片不会被写入 `照片信息.xlsx`，由调用方（aggregate_incoming）
    # 收集到 duplicates/ 报告里，主管在 UI 审核后再决定是否补登。
    # 默认行为（"import"）不写入此列表，向后兼容所有现有调用。
    duplicate_candidates: list[dict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    action_type: str
    voucher: str
    description: str


# 多人协作 — 收件箱聚合返回结构（M1 / M4 / S3 字段）
# conflicted / errored / duplicates 元素结构见 server_sync.aggregate_incoming 文档。
# - conflicted:    list of (subdir_name, message, report_path | None)
# - errored:       list of (subdir_name, message)
# - duplicates:    list of (subdir_name, candidates_list, report_path | None)
#                  candidates_list 元素见 ImportResult.duplicate_candidates 注释。
# - name_conflicts (S3): list of (original_filename, entries_list)
#                  entries_list 元素 = {"subdir":..., "voucher":..., "sha256":..., "archived_filename":...}
#                  仅含跨子目录"原始文件名相同但 SHA256 不同"的项。物理层已通过加后缀 _2 解决，
#                  这是告知用户"发生过同名不同内容"的事后报告。
@dataclass(frozen=True, slots=True)
class AggregateReport:
    processed: list[str] = field(default_factory=list)
    conflicted: list[tuple] = field(default_factory=list)
    errored: list[tuple] = field(default_factory=list)
    duplicates: list[tuple] = field(default_factory=list)
    name_conflicts: list[tuple] = field(default_factory=list)
    total_imported: int = 0
    total_photos: int = 0
    snapshot_path: Path | None = None
    name_conflicts_report_path: Path | None = None


# 多人协作 — 合并预览（S7 dry-run 结构，纯只读扫描结果）
@dataclass(frozen=True, slots=True)
class AggregatePreview:
    """合并前的只读预览。每个候选子目录的预测结果。"""
    # 每个候选子目录的预测：(subdir_name, predicted_outcome, voucher_count, note)
    # predicted_outcome ∈ {"new", "skipped", "conflict", "segment_violation", "unreadable"}
    candidates: list[tuple] = field(default_factory=list)
    total_candidates: int = 0
    predicted_new_vouchers: int = 0
    predicted_skipped_vouchers: int = 0
    predicted_conflicts: int = 0
    predicted_photos: int = 0
    predicted_name_conflicts: int = 0
    predicted_cross_voucher_duplicates: int = 0


Row = dict[str, Any]
