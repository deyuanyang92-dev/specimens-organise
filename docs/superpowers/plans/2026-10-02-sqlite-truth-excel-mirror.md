# SQLite 真相源 + Excel 镜像 — 实施计划（第 1、2 段）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 运行期只读写 `数据/标本数据.sqlite`，Excel 文件按时机生成为镜像；老工作区自动转换；没转过的工作区走今天的 xlsx 逻辑一字不改。

**Architecture:** `ExcelStore` 业务逻辑不动，只把它底下的表格读写原语收口成 `TableBackend` 接口（`XlsxBackend` = 现有代码搬入，`SqliteBackend` = 新写）。独立模块负责转换（`workspace_convert.py`）、镜像生成（`excel_mirror.py`）、跨工作区读取（`workspace_tables.py`）。UI 只加开关、菜单项、状态栏指示和三个时机钩子。

**Tech Stack:** Python 3.11+（打包）/3.13（开发），PyQt5 5.15，openpyxl 3.1，stdlib sqlite3；测试 unittest（`python -m unittest discover -s tests`，GUI 用 `QT_QPA_PLATFORM=offscreen`）。

**Spec:** `docs/superpowers/specs/2026-10-02-sqlite-truth-excel-mirror-design.md`

## Global Constraints

- xlsx 文件名、表头（中文列名）、行序、"值一律字符串"的契约不变；`XlsxBackend` 写出的文件读回内容必须与旧 `_write_plain_rows` 逐格相等。
- 所有写文件走 `tmp = path.with_suffix(f".{os.getpid()}.tmp")` → 校验 → `tmp.replace(path)`；xlsx 写后必须调 `_verify_workbook_file_can_be_reopened`。
- 改旧行为必须保留 `# 旧：…` 注释说明原行为与兼容决策（仓库 CLAUDE.md 规则）。
- `excel_store.py` / 新后端模块只用 stdlib + openpyxl，不得 import PyQt5。
- 只读副本（`read_only=True`）零副作用：不建文件、不转换、不导出。
- 转换过程源 xlsx 从头到尾不被修改；任何失败留在旧模式、不留半截文件。
- `CURRENT_DATA_SCHEMA_VERSION` 仅在第 2 段最后一个任务改为 `"1.2.0"`；sqlite 工作区配置写 `"storage_backend": "sqlite"`。
- 每个任务：失败测试 → 实现 → 全量 `python -m unittest discover -s tests` 绿 → `python -m ruff check specimen_app tests --select=F821` → commit（Conventional Commits，中文主题，末尾 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`）。
- 第 1 段结束发 v0.10.34（用户零感知）；第 2 段结束发 v0.11.0；发版说明必须写"所有机器先升级再打开共享工作区"。

## Review Focus

1. 转换时 xlsx 表头含空白单元格或重复列名 → 必须整表拒绝转换并留在旧模式（SQLite 列名不能空/重复），不能静默丢列。→ Task 6 测试 `test_convert_refuses_duplicate_or_blank_headers`。
2. Excel 正被用户在 Excel 里打开（Windows 文件锁）时生成镜像 → `tmp.replace` 抛 `PermissionError`：数据库不受影响，该表保持"未导出"，状态栏提示，程序不崩。→ Task 7 测试 `test_export_keeps_dirty_when_target_locked`。
3. 转换中途断电：`标本数据.sqlite` 已改名但配置 JSON 还没写 → 下次打开发现 sqlite 存在而配置缺 `storage_backend`：以 sqlite 为准补写配置（幂等），不重做转换。→ Task 5 测试 `test_open_repairs_config_when_sqlite_exists_without_flag`。
4. 工作区在只读介质 / 被别的进程锁住 → 不转换、不导出、照旧模式打开，不抛错。→ Task 6 测试 `test_convert_skipped_for_read_only_store`。
5. 大表（50k 行）关窗时生成镜像耗时数秒 → 关窗流程显示"正在生成 Excel…"并等待完成，绝不因超时强杀写线程丢文件。→ Task 10 测试 `test_close_waits_for_mirror_export`（用 10k 行合成表）。

---

## 文件结构

| 文件 | 职责 | 段 |
|---|---|---|
| `specimen_app/table_backend.py`（新） | `TableBackend` Protocol、`split_table_key`、`XlsxBackend`（现有 openpyxl 原语搬入） | 1 |
| `specimen_app/excel_store.py`（改） | 四个原语委托后端；两 sheet 修改记录、增量 append、mtime 缓存改走后端；第 2 段加模式选择/转换/镜像/降级入口 | 1,2 |
| `specimen_app/table_backend_sqlite.py`（新） | `SqliteBackend` + `meta` 表（schema 版本、每表变更计数、导出计数） | 2 |
| `specimen_app/workspace_convert.py`（新） | xlsx → sqlite 转换与校验 | 2 |
| `specimen_app/excel_mirror.py`（新） | 从 sqlite 生成 xlsx 镜像（脏表 / 全量），导出计数 | 2 |
| `specimen_app/workspace_tables.py`（新） | 跨工作区只读读取器："有 sqlite 读 sqlite，否则读 xlsx" | 2 |
| `specimen_app/models.py`（改） | `SQLITE_DATA_FILE`、`MANAGED_TABLE_KEYS`、`CURRENT_DATA_SCHEMA_VERSION` | 2 |
| `specimen_app/server_sync.py`（改） | 两处 xlsx 读改走 `workspace_tables` | 2 |
| `specimen_app/app_settings.py`（改） | `auto_excel_mirror: bool = False` | 2 |
| `specimen_app/ui.py`（改） | 「自动写入 Excel」开关、工具菜单两项、状态栏指示、关窗/切换/保存钩子、空闲计时器 | 2 |
| `tests/test_table_backend.py`（新） | 两个后端的契约测试（参数化） | 1,2 |
| `tests/test_workspace_convert.py`（新） | 模式选择 / 转换 / 失败 / 幂等 / 只读 | 2 |
| `tests/test_excel_mirror.py`（新） | 脏表导出、锁文件、全量、快照含 sqlite | 2 |
| `tests/test_sqlite_mode_ui.py`（新） | offscreen 真实窗口：三个时机 + 开关 + 状态栏 | 2 |
| `docs/adr/0001-sqlite-truth-excel-mirror.md`（新） | 难以回退的决策记录 | 2 |

---

## 第 1 段：抽后端接口（用户零感知）

### Task 1: `table_backend.py` — 接口 + `XlsxBackend`

**Files:**
- Create: `specimen_app/table_backend.py`
- Test: `tests/test_table_backend.py`

**Interfaces:**
- Produces:
  - `SHEET_SEP = "::"`; `split_table_key(key: str) -> tuple[str, str | None]`（`"修改记录.xlsx::修改汇总"` → `("修改记录.xlsx", "修改汇总")`）
  - `class TableBackend(Protocol)`：`exists(key) -> bool`、`headers(key) -> list[str]`、`read_rows(key, fallback_headers=None) -> list[Row]`（sparse：只含非空值）、`replace_rows(key, headers, rows) -> None`、`replace_many(items: list[tuple[str, list[str], list[Row]]]) -> None`、`append_rows(key, headers, rows) -> None`、`stream_columns(key, wanted_columns: set[str]) -> Iterator[dict[str, str]]`、`version_token(key) -> float | int`、`close() -> None`
  - `class XlsxBackend(data_dir: Path, *, fit_row: Callable[[Row, list[str]], Row], to_string: Callable[[object], str], verify_file: Callable[[Path], None] | None, column_aliases: dict[str, str])`
  - 模块函数 `xlsx_read_rows(path, to_string, column_aliases, fallback_headers=None, sheet=None) -> list[Row]` 与 `xlsx_headers(path, to_string, sheet=None) -> list[str]`（供 store 读外部工作区文件、供 `workspace_tables` 复用）

- [ ] **Step 1: 写失败测试**（`tests/test_table_backend.py`：`SplitKeyTests` + `XlsxBackendContractTests`——replace→read 稀疏且有序、append 追加/建文件、sheet key 共用一本 workbook、alias 归一、version_token、stream_columns、无 tmp 残留、模块读取器读外部路径；代码见 Task 1 Step 1 原文）
- [ ] **Step 2: 跑测试确认失败** — `python -m unittest tests.test_table_backend` → `ModuleNotFoundError: specimen_app.table_backend`
- [ ] **Step 3: 实现 `specimen_app/table_backend.py`**：`XlsxBackend` 的每个方法体从 `excel_store.py` 原样搬入——`read_rows` ← `_read_plain_rows`（4773，含 COLUMN_ALIASES 归一）/`_rows_from_sheet`（4823）；`replace_many` ← `_write_plain_rows`（4844：新 Workbook、Sheet1、表头+行、原子保存+校验）与 `_write_changes_and_summary`（3900：load 整本、`_replace_sheet`、原子保存）；`append_rows` ← `_append_row_incremental`（4308）/`_append_index_row`（4408）的 load+append+save 及"失败全量重写"兜底；`headers` ← `_headers`（4760）；`stream_columns` ← `_stream_columns`（645）；`version_token` = `path.stat().st_mtime`（缺文件 0.0）。`_atomic_save(wb, path)`：tmp→verify→replace，finally 清 tmp。
- [ ] **Step 4: 跑测试** — `python -m unittest tests.test_table_backend -v` → 全部 PASS
- [ ] **Step 5: Commit** — `refactor(store): 抽出 TableBackend 接口 + XlsxBackend（原语原样搬入，第 1 段/1）`

### Task 2: `ExcelStore` 四个原语委托 `XlsxBackend`；两 sheet 日志、增量 append、mtime 缓存改走后端

**Files:**
- Modify: `specimen_app/excel_store.py`（`__init__` ~第 200 行；`_read_plain_rows` 4773；`_write_plain_rows` 4844；`_headers` 4760；`_read_sheet_rows` 4811；`_rows_from_sheet` 4823；`_replace_sheet` 4866；`_open_workbook` 4874；`_stream_columns` 645；`_write_changes_and_summary` 3900；`_ensure_summary_row` 4003；`_append_index_row` 4408；`_append_row_incremental` 4308；`_ensure_workbook` 3319；`_ensure_change_log` 3349；3955–3985 修改明细/汇总整表替换；`_cached_rows` 2985；`_ensure_index_voucher_set` 1897；`_ensure_summary_voucher_set` 3987；`_voided` 缓存 3820；`_invalidate_cache` 3035；`_record_action` 4098 兜底不动）
- Test: 现有全量测试 + `tests/test_table_backend.py` 新增 `StoreUsesBackendTests`

**Interfaces:**
- Consumes: Task 1 全部。
- Produces: `ExcelStore._backend: TableBackend`；`ExcelStore._table_key_for(path: Path) -> str | None`（`数据/` 内的文件返回文件名，否则 None）；`ExcelStore.storage_backend_name -> str`（第 1 段恒为 `"xlsx"`）；模块常量 `CHANGE_DETAIL_KEY = f"{CHANGE_LOG_FILE}::修改明细"`、`CHANGE_SUMMARY_KEY = f"{CHANGE_LOG_FILE}::修改汇总"`。

- [ ] **Step 1: 写失败测试**（`StoreUsesBackendTests`：`test_store_routes_reads_and_writes_through_backend`——`storage_backend_name == "xlsx"`，`set_fields` 后 monkeypatch `_backend.read_rows` 记录 key 并命中 `SPECIMEN_FILE`，`修改明细` 能从后端读到该字段；`test_table_key_for_external_path_is_none`）
- [ ] **Step 2: 确认失败** — `AttributeError: storage_backend_name`
- [ ] **Step 3: 实现（逐处）**
  1. `__init__`：`self._rw_lock` 之后创建 `self._backend = XlsxBackend(self.data_dir, fit_row=self._fit_headers, to_string=self._string, verify_file=self._verify_workbook_file_can_be_reopened, column_aliases=COLUMN_ALIASES)`；加 `storage_backend_name` 属性与 `_table_key_for`。
  2. `_read_plain_rows(path, fallback)`：受管路径 → `self._backend.read_rows(key, fallback)`；外部路径 → `xlsx_read_rows(path, self._string, COLUMN_ALIASES, fallback)`。保留 `# 旧：` 说明。
  3. `_write_plain_rows(path, headers, rows)`：受管 → `replace_rows`；外部（冲突报告等）→ 临时 `XlsxBackend(path.parent, …).replace_rows(path.name, …)`；末尾 `_invalidate_cache(path.name)` 不变。
  4. `_headers` / `_read_sheet_rows` / `_stream_columns` → 委托。
  5. `_write_changes_and_summary`：读两 sheet 改 `self._backend.read_rows(CHANGE_DETAIL_KEY/…)`，写改 `replace_many([...两张...])`；逻辑不动。3955–3985 两个整表替换 → `replace_rows`。
  6. `_ensure_summary_row` / `_append_index_row` / `_append_row_incremental` 的 load+append+save 段 → `self._backend.append_rows(...)`（兜底在后端内）；缓存 `mtime` → `version_token`。
  7. `_ensure_workbook`：`not path.exists()` → `not self._backend.exists(key)` → `replace_rows(key, headers, [])`；补列逻辑不动。`_ensure_change_log` → `replace_many` 两张空表。
  8. `_cached_rows` / `_ensure_index_voucher_set` / `_ensure_summary_voucher_set` / `_voided` 缓存：`stat().st_mtime` → `self._backend.version_token(file_key)`。
  9. `_rows_from_sheet` / `_replace_sheet` / `_open_workbook`：删除，`grep` 确认零引用。
- [ ] **Step 4: 全量测试** — `python -m unittest discover -s tests` → 全绿（任何一条红都说明搬运不等价，回 Task 1 对照原函数）
- [ ] **Step 5: 等价性抽查**（一次性脚本，不入库）：`测试数据集/` 副本上用改前（`git stash`）/改后各跑一遍相同操作序列（`create_specimen`、`set_fields`、`add_photo`、`undo_last`），openpyxl 逐格比较 9 本 xlsx（忽略 docProps 时间戳），结果写进 commit message。
- [ ] **Step 6: Commit** — `refactor(store): ExcelStore 读写原语全部委托 XlsxBackend，行为逐格等价（第 1 段/2）`

### Task 3: 第 1 段收尾发版 v0.10.34

- [ ] `specimen_app/__init__.py` → `"0.10.34"`；CLAUDE.md「Core modules」加 `table_backend.py` 一行。
- [ ] 全量绿；ruff 绿。
- [ ] 分支 `feature/table-backend-phase1` → commit → `merge --no-ff` 进 main（`Merge … (v0.10.34)`）→ `git push origin main`。tag 由用户决定。

---

## 第 2 段：SqliteBackend、自动转换、Excel 镜像、界面

### Task 4: `table_backend_sqlite.py` — `SqliteBackend`

**Files:** Create `specimen_app/table_backend_sqlite.py`；Modify `specimen_app/models.py`（`SQLITE_DATA_FILE = "标本数据.sqlite"`、`MANAGED_TABLE_KEYS`）；Test `tests/test_table_backend.py`（契约测试基类化，两个后端各跑一遍）

**Interfaces:**
- `SqliteBackend(db_path, *, fit_row, to_string, network_safe=False)` 实现 `TableBackend`；额外 `meta_get/meta_set`、`table_version(key) -> int`、`exported_version(key)`/`mark_exported(key, version)`、`checkpoint()`、`quick_check() -> bool`、`table_name(key) -> str`（`标本信息.xlsx` → `标本信息`；`修改记录.xlsx::修改汇总` → `修改记录__修改汇总`）。
- 表：`"_seq" INTEGER PRIMARY KEY AUTOINCREMENT` + 每个表头一列 TEXT；`read_rows` 按 `_seq` 排序、只含非空值；`replace_many` 一个 `BEGIN IMMEDIATE` 事务内 DROP/CREATE/INSERT 全部表并 bump 版本，任一失败 ROLLBACK；`append_rows` 建表/补列后 INSERT；表头空白或重复 → `ValueError`。
- 连接：`check_same_thread=False`、`busy_timeout=10000`；`network_safe=True` → `journal_mode=DELETE` + `synchronous=FULL`，否则 WAL + NORMAL。`meta` 表：`schema_version`、`version:<key>`、`exported:<key>`。
- `MANAGED_TABLE_KEYS = (SPECIMEN_FILE, PHOTO_FILE, CLASSIFICATION_FILE, INDEX_FILE, CHANGE_DETAIL_KEY, CHANGE_SUMMARY_KEY, ALLOC_LOG_FILE, DATA_VERSION_LOG_FILE)`；`操作记录.xlsx`（兜底）、`入库人员.xlsx`、冲突报告不受管。

- [ ] 失败测试：契约测试两后端 + `test_version_increments_per_write_and_export_marks`、`test_rejects_blank_or_duplicate_headers`、`test_replace_many_is_atomic`
- [ ] 实现 → 两后端全绿 → Commit `feat(store): SqliteBackend（11 表映射 + meta 变更计数 + 网络盘保守日志）（第 2 段/1）`

### Task 5: 模式选择、新工作区默认 SQLite、`backend=` 参数、close 导出钩子位

**Files:** `specimen_app/excel_store.py`（`__init__`、`_has_workspace_seed_files` 549、`ensure_files` 521、`close` 281、`_table_key_for`、`storage_backend_name`）；`tests/test_workspace_convert.py`（新，`ModeSelectionTests`）

**Interfaces:**
- `ExcelStore(..., backend: str | None = None)`：`"xlsx"`/`"sqlite"`/`None`=自动（有 sqlite 文件 → sqlite；无种子文件的新工作区 → sqlite；只有 xlsx → 可写则转换（Task 6），只读 → xlsx）。
- `storage_backend_name` → `"sqlite"`/`"xlsx"`；`sqlite_path` 属性；sqlite 模式 `_table_key_for` 对非受管文件返回 None（继续直接读写 xlsx）。
- `ensure_files()` sqlite 模式：受管表 `exists(key) or replace_rows(key, headers, [])`；`操作记录.xlsx`、`入库人员` 仍 `_ensure_workbook`。
- `close(export_excel: bool = True)`：sqlite 且非只读且 `export_excel` → `export_excel_mirror()`（Task 7 实现；本任务放占位返回 `[]`，Task 7 必须删除占位）。
- 配置：`self.config["storage_backend"] = "sqlite"`；缺标记但 sqlite 文件存在 → 补写（幂等）。
- 模块级 `DEFAULT_NEW_WORKSPACE_BACKEND = "sqlite"`。

- [ ] 失败测试：`test_new_workspace_defaults_to_sqlite`（close 后镜像 xlsx 出现）、`test_backend_kwarg_forces_xlsx`、`test_reopen_detects_sqlite`、`test_open_repairs_config_when_sqlite_exists_without_flag`、`test_read_only_open_of_sqlite_workspace_has_zero_side_effects`
- [ ] 实现 → 本任务两个测试文件绿；全量跑一遍，把因"新工作区默认 sqlite"而失败的旧测试列进 commit message（交 Task 11 处理）→ Commit `feat(store): 模式识别 + 新工作区默认 SQLite + backend= 参数（第 2 段/2）`

### Task 6: `workspace_convert.py` — 旧 → 新自动转换

**Files:** Create `specimen_app/workspace_convert.py`；Modify `excel_store.py`（`__init__` 在 `_load_or_create_config` 之后、`ensure_files` 之前调用 `_select_backend_and_maybe_convert()`）；Test `tests/test_workspace_convert.py`

**Interfaces:**
- `convert_workspace_to_sqlite(data_dir, *, xlsx_backend, make_sqlite_backend, snapshot, write_config, table_keys, headers_for) -> ConversionReport(converted, reason, tables: dict[str,int], snapshot_path)`
- 步骤：快照 → 读 9 本 xlsx → 写临时库 `标本数据.sqlite.converting` → 逐表读回逐格比对（表头、值、行序、行数）→ `os.replace` 为 `标本数据.sqlite` → `write_config({"storage_backend": "sqlite", "data_schema_version": "1.2.0"})` → 每表 `mark_exported(key, table_version(key))`。任何异常：删 `.converting`，返回 `converted=False`，调用方留在 xlsx 模式。

- [ ] 失败测试：`test_convert_moves_all_tables_and_keeps_xlsx_untouched`（xlsx mtime/size 前后相等、逐表逐格相等、配置 1.2.0、先快照）、`test_convert_refuses_duplicate_or_blank_headers`、`test_convert_failure_leaves_no_partial_files`、`test_convert_skipped_for_read_only_store`、`test_convert_is_idempotent_on_reopen`、`test_undo_still_works_after_convert`
- [ ] 实现 → 绿 → Commit `feat(store): 旧工作区自动转换为 SQLite（快照→导入→校验→原子替换，源 xlsx 不动）（第 2 段/3）`

### Task 7: `excel_mirror.py` — 镜像生成 + 快照含 sqlite

**Files:** Create `specimen_app/excel_mirror.py`；Modify `excel_store.py`（`export_excel_mirror`、`excel_mirror_pending`、`create_data_snapshot` 2160、`restore_data_snapshot` 2567、`verify_snapshot_integrity`）；Test `tests/test_excel_mirror.py`

**Interfaces:**
- `dirty_keys(sqlite_backend, table_keys) -> list[str]`（`table_version > exported_version`）
- `export(sqlite_backend, xlsx_backend, keys, headers_for) -> ExportResult(written: list[str], failed: dict[str, str])`：按文件分组 `replace_many`（修改记录两 sheet 一次写）；写成功才 `mark_exported`；`PermissionError`/`OSError` 记 `failed`，继续其它表。
- `ExcelStore.export_excel_mirror(all_tables=False) -> ExportResult`；`excel_mirror_pending() -> int`；xlsx 模式返回空/0。
- `create_data_snapshot`：sqlite 模式先 `export_excel_mirror()`，`checkpoint()`，拷贝 `.xlsx/.json/.sqlite`；`restore_data_snapshot` 拷回 `.sqlite` 并重开后端。

- [ ] 失败测试：`test_export_only_dirty_tables`、`test_export_all`、`test_export_keeps_dirty_when_target_locked`（`patch("os.replace", side_effect=PermissionError)`）、`test_snapshot_contains_sqlite_and_fresh_xlsx`、`test_restore_snapshot_brings_back_sqlite`、`test_close_exports_dirty_and_marks_clean`
- [ ] 实现 → 绿 → Commit `feat(store): Excel 镜像导出（脏表/全量/锁文件不崩）+ 快照含 sqlite（第 2 段/4）`

### Task 8: 降级 `downgrade_to_xlsx_workspace()`

- `export_excel_mirror(all_tables=True)`（有 `failed` → `RuntimeError`，不降级）→ 逐表比对 xlsx 读回 == sqlite 读回 → 关后端 → `标本数据.sqlite` 改名 `标本数据.sqlite.bak-YYYYmmdd-HHMMSS` → 配置 `data_schema_version="1.1.3"`、删 `storage_backend` → 切 `XlsxBackend` → 返回 bak 路径。
- 测试：往返相等 + bak 存在；导出失败不降级。
- Commit `feat(store): 导出 Excel 并降级工作区（可回退旧版）（第 2 段/5）`

### Task 9: `workspace_tables.py` 跨工作区读取器 + `server_sync` / 导入合并接线

- `read_table(workspace_root, key, fallback_headers=None) -> tuple[list[str], list[Row]]`：有 sqlite → `sqlite3.connect("file:…?mode=ro", uri=True)` 只读；否则 `xlsx_read_rows`。
- `server_sync._read_source_vouchers`/`_read_xlsx_rows_safe` 改调它（旧函数名保留为薄包装）；`ExcelStore._read_plain_rows` 外部路径分支：其 `数据/` 下有 sqlite 则读 sqlite 对应表。
- 测试：源工作区 sqlite 模式且未导出镜像时，同步预览/导入合并仍读到数据。
- Commit `feat(sync): 跨工作区读取器——有 sqlite 读 sqlite，否则读 xlsx（第 2 段/6）`

### Task 10: 界面：开关、菜单、状态栏、三个时机

**Files:** `app_settings.py`（`auto_excel_mirror: bool = False`，仿 `auto_save_enabled` 第 77/188/269/318 行）、`ui.py`、`tests/test_sqlite_mode_ui.py`

- 工具栏 `_auto_excel_action`（checkable，文案「Excel 自动写入：开/关」，放 `_auto_save_action` 之后）；`toggled` → 存设置，勾选时立即 `_schedule_excel_mirror()`。
- 工具菜单（第 2516 行之后）：「导出 Excel（立即生成）」→ 排空后 worker 内 `export_excel_mirror(all_tables=True)`；「导出 Excel 并降级工作区…」→ 管理员密码 → `downgrade_to_xlsx_workspace()` → 重新 `_load_workspace_into_window`。
- 状态栏右侧 `_excel_sync_label`：`_on_store_op_done` 后刷新："Excel 已同步" / "Excel 落后 N 张表"；xlsx 模式隐藏。
- 空闲计时 `_excel_mirror_timer`（singleShot 5000 ms）：`_on_store_op_done` 若开关勾选则 `start()`；超时且 worker 空闲 → `enqueue("excel_mirror", store.export_excel_mirror)`。
- 时机钩子：`closeEvent` 在 `_drain_store_worker` 之后、`request_stop` 之前同步 `export_excel_mirror()`（状态栏"正在生成 Excel…"）；`_load_workspace_into_window` 同；`_save_panel`/`_save_all_panels` 排空后 enqueue。`store.close()` 处传 `export_excel=False`。
- `_open_data_in_excel` 警告文案："Excel 文件是软件生成的副本；在 Excel 里修改不会导入软件，下次生成会被覆盖。"
- 测试（offscreen，仿 `tests/test_store_worker_saves.py`）：`test_close_exports_excel`、`test_close_waits_for_mirror_export`（10k 行）、`test_save_button_exports`、`test_auto_toggle_exports_after_idle`（计时器改 200 ms）、`test_status_label_reflects_pending`。
- Commit `feat(ui): Excel 自动写入开关 + 导出/降级菜单 + 状态栏同步指示 + 关窗/切换/保存时生成（第 2 段/7）`

### Task 11: 旧测试适配、文档、版本 0.11.0、发版

- Task 5 记录的失败清单逐条处理：读 xlsx 前加 `store.export_excel_mirror()`，或本意是 xlsx 行为则 `ExcelStore(..., backend="xlsx")`；每处留一行注释。
- `models.CURRENT_DATA_SCHEMA_VERSION = "1.2.0"`；`_assert_supported_data_schema` 文案加"降级请用「工具 → 导出 Excel 并降级工作区」"。
- `docs/adr/0001-sqlite-truth-excel-mirror.md`（背景/决策/后果/回退，≤40 行）；CLAUDE.md「Data storage」改写；`docs/manual` 加「Excel 文件现在是镜像」；发版说明草稿。
- `__version__ = "0.11.0"`；全量绿；ruff 绿；merge → push；tag 由用户决定。
- 独立复审：`superpowers:requesting-code-review` 让另一模型审整个分支，修完再发。

---

## 第 3 段（本计划不含，另立计划）

汇总缓存按变更计数失效；`入库人员` 并入；`transaction.jsonl` 在 sqlite 模式下改为纯事务；行级 `update_row` 优化（现为整表替换 ~15 ms/1500 行，够用）。
