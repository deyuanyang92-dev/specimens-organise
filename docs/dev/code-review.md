# 代码评审：架构、命名、可读性（发表级体检）

面向"确保前后端达到发表级别"的一次深入评审。结论先行：**这是一个成熟、工程化良好的桌面应用**，
分层清晰、数据兼容性约束严谨、命名整体优秀。距离"发表级"主要差两类东西：(1) `ui.py` 体量过大；
(2) 公共 API 文档与少量类型/异常处理细节。本轮已修复其中低风险项（见末尾"本轮已修复"），
高风险的结构重构给出**明确建议但不在本轮执行**。

## 1. 模块结构与职责分离 —— 整体良好

分层清晰：

- **数据层** `excel_store.py`：Excel I/O、CRUD、撤销/重做、工作区锁、导入冲突检测、快照。
- **领域模型** `models.py`、`classification_fields.py`、`species.py`、`parsing.py`。
- **服务层** `image_search.py`、`image_cache.py`、`workspace.py`、`app_settings.py`、
  `updater.py`、`release_manager.py`、`batch_export.py`。
- **UI 层** `ui.py`。

### 主要问题：`ui.py` 是 5300+ 行的 god object

`ui.py` 单文件包含主窗口、8 个对话框类（`ImageSearchDialog`、`IngestSummaryDialog`、
`VersionManagerDialog`、`BatchSpecimenFieldsDialog`、`SettingsDialog`、`PhotoBatchDialog`、
`ActionLogDialog`、`PhotoFilenameFillDialog`）、多个控件/工作线程类（`PhotoGraphicsView`、
`GridPhotoCell`、`ThumbnailWorker`、`IndexBuildWorker`、`ImageSearchWorker`、
`UpdateCheckWorker`、`UpdateDownloadWorker` 等），200+ 私有方法。

**影响**：新人上手慢、代码审查困难、单元测试难以覆盖 UI。

### 是否需要重构？建议：值得拆，但**增量拆、本轮不做**

- ui.py 确实应该拆。推荐目标布局：
  - `specimen_app/dialogs/` —— 每个对话框一个文件（`ingest_summary_dialog.py` 约 1000 行、
    `image_search_dialog.py`、`version_manager_dialog.py`、`settings_dialog.py`、…），
    `dialogs/__init__.py` 统一 re-export。
  - `specimen_app/widgets/` —— `photo_graphics_view.py`、`grid_photo_cell.py`、
    `field_value_completer.py`。
  - `specimen_app/workers/` —— `ThumbnailWorker` / `IndexBuildWorker` / `ImageSearchWorker` /
    `UpdateCheckWorker` / `UpdateDownloadWorker`。
  - `ui.py` 仅保留 `run_app()` 与主窗口 `SpecimenWindow`（目标 ≤ 1500 行）。
- **为什么不在本轮做**：大爆炸式拆分风险高 —— 项目有"数据兼容性强制"硬约束，且**没有 UI
  自动化测试覆盖**，一次性搬动 5000 行极易引入回归。
- **推荐做法**：分步增量拆分，每次只搬一个独立对话框/控件 + 跑一次手动冒烟，逐步收敛。
  推荐顺序（依赖从少到多）：`workers/` → `widgets/` → 各 `dialogs/` → 最后收尾主窗口。
- 可选：引入 `app_facade.py` 服务层，让 UI 依赖一个高层门面而非直接 import
  `ExcelStore` / `image_search` 的 16+ 个符号，降低耦合、便于测试。

## 2. 函数 / 方法命名 —— 优秀

命名自解释、动宾清晰，例如 `derive_specimen_fields_from_tube_number()`、
`classification_values_from_species_match()`、`is_supported_image()`、`_lock_is_stale()`。
**不建议为改而改**。少数信号槽 `_on_text_changed()` 在多个对话框里语义略泛，可在后续拆分
对话框时顺带改成 `_on_voucher_input_changed()` 之类，非必须。

PyQt 重写方法（`wheelEvent` / `resizeEvent` / `dragEnterEvent` …）保持 camelCase 是 Qt 要求，
**正确，无需改**。

## 3. 参数命名与签名 —— 良好

类型注解基本齐全。已修复项：`classification_fields.py` 的
`classification_values_from_species_match(match: Any)` → `match: SpeciesMatch`（`FamilyMatch` 同理）。
布尔参数（如 `force_rebuild`、`auto_derive_specimen_fields`）属真正的开关量，可接受。

## 4. 一致性 —— 优秀

snake_case / CamelCase / UPPER_CASE 全项目一致；私有 `_` 前缀使用规范；
中文仅用于用户可见文本与字段名，代码标识符全英文 —— 符合既定约定。

## 5. 可读性 —— 良好，有缺口

- **优点**：关键模块有 docstring，`CLAUDE.md` 架构总览完整，历史重构以 `#` 注释保留上下文。
- **缺口（本轮已补部分）**：`excel_store.py` 多个公共 API（`list_vouchers` /
  `workspace_overview` / `get_specimen` / `get_classification` / `get_photos` / `set_fields` /
  `close`）原先无 docstring —— 数据层是整个应用的地基，已补上用途/参数/返回结构说明。
- **建议（未做）**：`image_cache.py` 的若干 magic number 可加注释；历史重构的中文 `#` 注释
  可逐步沉淀为 `docs/adr/` 决策记录。

## 6. 代码异味

- **全局缓存**（`image_search.py` 的 `_IMAGE_INDEX_CACHE` / `_SEARCH_INDEX_CACHE`、
  `image_cache.py` 的 TIFF 懒加载守卫）：均有锁保护、有容量上限，属合理设计。
- **静默吞异常**：`ui.py:closeEvent` 原有 `except Exception: pass` —— 已修复为窄化
  `OSError` 容忍 + 其余打 stderr，不再静默吞掉真正的 bug。
- **UI ↔ 数据层耦合**：`ui.py` 直接 import 数据/服务层 16+ 符号，见第 1 节 `app_facade` 建议。

## 优先级清单

| 优先级 | 项 | 状态 |
|---|---|---|
| 必做（发表级） | `excel_store.py` 公共 API 补 docstring | ✅ 本轮已修 |
| 必做（发表级） | `classification_fields.py` 类型注解收紧 | ✅ 本轮已修 |
| 必做（发表级） | `closeEvent` 去掉静默吞异常 | ✅ 本轮已修 |
| 高（结构性） | 拆分 `ui.py` god object | ⏳ 建议增量拆分，单列后续工作 |
| 中 | 引入 `app_facade.py` 降耦合 | ⏳ 可选，随拆分一并做 |
| 低 | magic number 注释、ADR 沉淀、信号槽命名细化 | ⏳ nice-to-have |

## 本轮已修复（随启动死机修复一并提交）

- `excel_store.py`：`list_vouchers` / `workspace_overview` / `get_specimen` /
  `get_classification` / `get_photos` / `set_fields` / `close` 补 docstring。
- `classification_fields.py`：`match: Any` → `SpeciesMatch` / `FamilyMatch`（`TYPE_CHECKING` 导入）。
- `ui.py`：`closeEvent` 的 `except Exception: pass` → 窄化 + 记 stderr。

## 总评

工程基础扎实，数据安全意识强。**核心阻碍是 `ui.py` 体量**，建议作为独立、增量的后续工作推进
（不要一次性大爆炸式重构）。其余发表级细节本轮已补齐。
