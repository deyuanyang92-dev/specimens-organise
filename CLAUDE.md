# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

标本入库管理 (Specimen Inventory Management) — a PyQt5 desktop app for managing biological specimen records stored as Excel files. Target platforms: Windows and Linux.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app (auto-discovers last-used workspace or current directory)
python run_app.py
python run_app.py --workspace "/path/to/workspace"

# Run full test suite
python -m unittest discover -s tests

# Run a single test
python -m unittest tests.test_core.CoreTests.test_create_vouchers_increment

# Build Windows/Linux release (requires PyInstaller)
python build_release.py --version 0.2.5

# Windows: one-click build (also runs build_release.py)
build.bat
```

## Architecture

Entry point: `run_app.py` → `specimen_app/main.py:main()` → `specimen_app/ui.py:run_app()`.

### Core modules

- **`models.py`** — All constants: Excel file names, headers (imported from `classification_fields.py`), required fields, save method options, category mappings, `CURRENT_DATA_SCHEMA_VERSION`. Also the inventory-summary view constants: `SUMMARY_COLUMNS` (flat wide-table column order = specimen fields + classification fields + `照片数`; classification `备注` disambiguated as `分类备注` via `CLASSIFICATION_NOTE_DISPLAY`), `SUMMARY_COLUMN_SOURCE` (display col → `(category, excel_field)`, `category="readonly"` marks 入库编号*/照片数), `SUMMARY_DEFAULT_VISIBLE_COLUMNS`. Dataclasses: `StatusFlags`, `ImportResult`, `ActionResult`, `Row` alias. Custom exceptions: `WorkspaceError`, `WorkspaceLockedError`, `WorkspaceNotInitializedError`, `DuplicateVoucherError`, `ImportConflictError`.
- **`classification_fields.py`** — Classification column definitions and autofill mappings. `CLASSIFICATION_COLUMNS` defines Excel write order; `SPECIES_LOOKUP_INPUT_COLUMNS` / `FAMILY_LOOKUP_INPUT_COLUMNS` dictate which fields trigger taxonomy preset searches. `classification_values_from_species_match()` and `classification_values_from_family_match()` convert `SpeciesMatch`/`FamilyMatch` results into classification form values (e.g., `SpeciesMatch` → `{种名*: "...", 种拉丁: "...", 属名: "...", 科*: "...", 科拉丁: "..."}`).
- **`excel_store.py`** — `ExcelStore` is the data layer backing the entire app. Reads/writes all Excel files via openpyxl. Handles CRUD, undo/redo (action-log based, default 200 steps), workspace import with fingerprint-based conflict detection, data snapshots (`create_data_snapshot` / `restore_data_snapshot`), and file locking (`数据/.workspace.lock`). Blocks numpy import during openpyxl usage to prevent library conflicts. Uses in-memory cache (`_row_cache`) to avoid re-parsing files on every read. `summary_records()` left-joins specimen + classification + photo counts/filenames into one flat dict per voucher (keyed by `SUMMARY_COLUMNS`) — a pure in-memory view backing the inventory-summary wide table, changes nothing on disk. Photos from outside the workspace are copied into `照片/` with content-hash deduplication; same-name photos get numbered archive names (`same.jpg` → `same_2.jpg`); hash-prefixed legacy archives are migrated on open.
- **`ui.py`** — `run_app()` creates the `QApplication` and `SpecimenApp(QMainWindow)`. Main window (~1500+ lines): voucher list with status flags (√/× for specimen/photo/classification), photo preview with zoom/pan/grid (2/4/6/8), species autocomplete, image search dialog, version manager, settings. Background photo loading via `QThread`. `IngestSummaryDialog` (toolbar "入库汇总") is a unified wide-table view over `store.summary_records()`: every voucher's specimen + classification fields + `照片数` in one editable, sortable, per-column-filterable table (header right-click toggles column visibility, persisted to `summary_visible_columns`; left-click sorts), a right-side read-only detail panel + photo grid, and Excel/CSV export of the current filtered+visible columns. Editable cells write back via `store.set_fields()` (undoable), gated by the same `_require_password()` as photo ops. `ImageSearchDialog` result cards carry full-name tooltips and a "查看详情" context action since card labels elide long filenames.
- **`parsing.py`** — Voucher number formatting (`YZZ000001` pattern), tube number parsing (extracts location codes, dates, bottle labels from strings like `QD-LSD-SC001-1-R-250923`). Also exports `extract_tube_from_filename()` and `extract_photo_date()` for auto-filling specimen info from photo filenames via `_TUBE_PATTERN` and `_PHOTO_DATE_RE` regexes.
- **`workspace.py`** — Workspace discovery (`default_workspace()` checks last-used, CWD, executable parent), validation (rejects `build/`, `dist/`, `releases/` directories via `is_generated_workspace_path`; rejects filesystem/drive root and home dir via `is_unsafe_workspace_root` — those are too large and would make full-workspace scans walk huge trees), initialization (copies `字段模版/` from template).
- **`species.py`** — `SpeciesMatcher` loads species presets from `字段模版/表格信息预设字段.xlsx` (columns: 物种中文名, 物种拉丁名, 科中文名, 科拉丁名). Provides fuzzy matching against Chinese and Latin names (ranking: exact > prefix > contains). Auto-reloads when file mtime changes. Returns `SpeciesMatch` (chinese_name, latin_name, family_name, family_latin; `genus_name` derived from the first word of latin_name) and `FamilyMatch` (family_name, family_latin) frozen dataclasses. These feed into `classification_fields.py` converters for autofill.
- **`image_cache.py`** — `ThumbnailCache` stores JPEG thumbnails in `数据/缩略图缓存/`, keyed by source path + file size + mtime + target size. TIFF handling via tifffile/numpy (lazy-imported, uses stride downsampling for large files). In-memory LRU cache (default 64MB). `load_source_image` enforces a `_MAX_DECODE_PIXELS` (~24MP) decode cap — JPEGs use `Image.draft()` and any over-cap image is `reduce()`-downsampled *before* exif/convert/thumbnail allocate full-resolution buffers, so gigapixel specimen scans can't exhaust memory. TIFF pages get a forced stride cap even when no target size is given.
- **`startup_diag.py`** — `mark(stage)` logs per-stage elapsed time + peak process RSS to stderr (`[startup]` prefix) and appends to a size-capped `startup_diagnostics.log` in the app config dir (not the workspace). Hooked into `ExcelStore.__init__` substeps and `SpecimenWindow` startup so a freeze/hang can be pinpointed to a stage. Pure stdlib; logging failures never block startup.
- **`image_search.py`** — Scans workspace for images (excluding build/dist/releases/data dirs), builds disk-cached index. Scores images against core identifiers (extracted from tube numbers as `XXX-XXX-NNNNNN`). Supports query filtering, linked-photo detection, index appending without rescan.
- **`app_settings.py`** — Persists last workspace, recent workspaces, preview quality, search paths, grid filename toggle, window geometry, update-check preferences (`check_updates_on_startup`, `last_update_check`), inventory-summary visible columns (`summary_visible_columns`; empty = fall back to `SUMMARY_DEFAULT_VISIBLE_COLUMNS`) in `%APPDATA%/标本入库管理/settings.json` (Windows) or `~/.specimen_inventory/settings.json` (Linux).
- **`release_manager.py`** — `list_releases()` discovers versioned release directories under `releases/`, identifies executable files by platform-specific rules.
- **`updater.py`** — In-app GitHub update support (stdlib only, no new deps). `check_latest_release()` queries the repo's latest GitHub Release, picks the platform zip + `update_manifest_{plat}.json` assets; `is_newer()` / `_parse_version()` compare versions (prerelease suffixes like `-test.1` sort below the matching release). `download_update()` is the UI entry point: with a manifest it does **incremental update** — splits into a small `app_*.zip` and a content-hash-named `runtime_*.zip`, and if a local installed release has a matching `runtime_hash` (read from each bundle's `.update_meta.json`), it reuses that runtime locally and downloads only the app zip; otherwise downloads both; no manifest → falls back to `download_release()` (full zip). All downloads enforce sha256 verification and zip-slip-safe extraction into a temp dir before moving to `releases/v{version}/`. Only HTTPS + `github.com`/`githubusercontent.com` hosts allowed. Does not auto-launch — version switching stays manual via `VersionManagerDialog`.
- **`icon.py`** — Generates app icon programmatically (specimen bottle + label) as QImage, saves as PNG/ICO.
- **`batch_export.py`** — `BatchExportDialog` for exporting specimen data + photos by voucher number list (like NCBI Batch Entrez). Parses voucher numbers from pasted text, writes a multi-sheet Excel workbook, optionally copies photo files and packages as ZIP. Accessible from right-click menu ("批量导出选中") or toolbar button.
- **`__init__.py`** — Only exports `__version__` (currently `"0.3.0-test.1"`), used by `build_release.py` as the default build version.

### Data storage

All user data lives in the workspace's `数据/` directory as `.xlsx` and `.json` files:
- `标本信息.xlsx`, `照片信息.xlsx`, `分类信息.xlsx` — main data tables
- `编号索引.xlsx` — voucher index with UUIDs and record fingerprints
- `修改记录.xlsx` — two-sheet workbook (修改明细 + 修改汇总)
- `操作记录.xlsx` — undo/redo action log
- `数据版本记录.xlsx` — snapshot version history
- `工作区配置.json` — workspace config (prefix, next serial, undo depth)
- `数据版本/` — snapshot file copies
- `.workspace.lock` — process-level lock file

Templates live in `字段模版/`. External photos are archived (copied) into `照片/` with content-hash deduplication; same-name photos get numbered suffixes (`same.jpg` → `same_2.jpg`). Internal photos already in the workspace are left in place with relative paths recorded. The workspace is fully portable.

## Key conventions

- **Data compatibility is mandatory.** Every update must remain compatible with data that users have already entered. Do not break existing Excel/JSON files, field names, voucher numbers, UUIDs, relative photo paths, fingerprints, version snapshots, or undo/redo logs without a safe backward-compatible reader, migration path, or rollback plan. Before changing schemas, save/import/export behavior, or generated file formats, verify that previously recorded workspace data still opens and remains usable.
- **Preserve old logic with comments when updating.** For future code updates in this project, do not silently delete important original logic. Prefer making changes with `#` comments that record the old behavior, why it changed, and how compatibility is preserved. If code must be removed because keeping it would be unsafe or misleading, leave a concise `#` comment near the replacement explaining the original path and the compatibility decision.
- Excel files are fully rewritten on every save (`_write_plain_rows` creates a fresh `Workbook` each time), so openpyxl's read_only mode is used for all reads.
- Undo/redo is action-log based: every mutation appends to `操作记录.xlsx` with old/new JSON snapshots; undo applies the inverse operation.
- The `_loading` flag in UI prevents save handlers from firing during programmatic field updates.
- All user-facing text is Chinese (labels, field names, file names). Code identifiers are English.
- When `管内编号*` is updated, `采集日期`, `采集地点缩写*`, and `保存方式` are auto-derived from the tube number.
- Data-entry speedups for the fields in `CARRY_OVER_SPECIMEN_FIELDS` (`models.py`: 标本存放位置/信息录入人员/核对人员/保存方式): the toolbar "沿用上条信息" toggle (persisted as `carry_over_specimen_fields` in settings) makes `new_specimen()` carry these over from the current record; the voucher list's right-click "批量设置标本信息" opens `BatchSpecimenFieldsDialog` to write them to all selected vouchers via `store.set_fields` (one undoable action-log entry per voucher).
- Workspace lock detects stale locks (PID not running or lock older than 10 minutes).
- Image search defaults to TIFF files; users can switch to JPG-only or combined TIFF+JPG.
- The build system uses PyInstaller's `--onedir` mode (fast start, no temp extraction) — not `--onefile`.
- `CURRENT_DATA_SCHEMA_VERSION` in `models.py` controls automated upgrades on workspace open: missing optional classification columns (`属名`, `目`, `纲`, `门`, `备注`) are appended to existing workbooks; hash-prefixed photo archives (`abcdef__original.jpg`) are migrated to clean names.

## Release & in-app update

- `build_release.py` packages each build into: the full per-platform zip (`标本入库管理_v{version}_{plat}.zip`, kept for backward compat / fallback), a small `app_v{version}_{plat}.zip` (app code), a content-hash-named `runtime_{plat}_{hash}.zip` (the `_internal/` runtime), each with a `{zip}.sha256`, plus `update_manifest_{plat}.json`. `partition_bundle()` defines the app/runtime split (app = root exe ∪ `_internal/specimen_app/**` ∪ `.update_meta.json`). Each built bundle also gets a `.update_meta.json` recording its `runtime_hash` + `app_files`.
- `.github/workflows/release.yml` builds on `windows-latest` + `ubuntu-latest` when a `v*` tag is pushed, and uploads all zips + `.sha256` + `update_manifest_*.json` as GitHub Release assets.
- The app's **版本管理 → 软件版本 → 检查 GitHub 更新** button (and an opt-in startup check) uses `updater.py` to find a newer release and **incrementally** download it (reusing the runtime from a locally-installed version when `runtime_hash` matches — only the small app zip is downloaded), sha256-verifying everything and extracting into a complete, independent `releases/v{version}/`. New versions never overwrite old ones and are not auto-launched — the user switches manually, and `_launch_release()` offers a data snapshot first. See `docs/release-and-update.md`.

## Additional docs

- `docs/build-windows.md` — Windows build guide
- `docs/build-linux.md` — Linux build guide
- `docs/linux-user-guide.md` — Linux user guide
- `docs/code-review.md` — publication-grade architecture/naming review + the (deferred, incremental) `ui.py` split recommendation
