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

- **`models.py`** — All constants: Excel file names, headers (imported from `classification_fields.py`), required fields, save method options, category mappings, `CURRENT_DATA_SCHEMA_VERSION`. Dataclasses: `StatusFlags`, `ImportResult`, `ActionResult`, `Row` alias. Custom exceptions: `WorkspaceError`, `WorkspaceLockedError`, `WorkspaceNotInitializedError`, `DuplicateVoucherError`, `ImportConflictError`.
- **`classification_fields.py`** — Classification column definitions and autofill mappings. `CLASSIFICATION_COLUMNS` defines Excel write order; `SPECIES_LOOKUP_INPUT_COLUMNS` / `FAMILY_LOOKUP_INPUT_COLUMNS` dictate which fields trigger taxonomy preset searches. `classification_values_from_species_match()` and `classification_values_from_family_match()` convert `SpeciesMatch`/`FamilyMatch` results into classification form values (e.g., `SpeciesMatch` → `{种名*: "...", 种拉丁: "...", 属名: "...", 科*: "...", 科拉丁: "..."}`).
- **`excel_store.py`** — `ExcelStore` is the data layer backing the entire app. Reads/writes all Excel files via openpyxl. Handles CRUD, undo/redo (action-log based, default 200 steps), workspace import with fingerprint-based conflict detection, data snapshots (`create_data_snapshot` / `restore_data_snapshot`), and file locking (`数据/.workspace.lock`). Blocks numpy import during openpyxl usage to prevent library conflicts. Uses in-memory cache (`_row_cache`) to avoid re-parsing files on every read. Photos from outside the workspace are copied into `照片/` with content-hash deduplication; same-name photos get numbered archive names (`same.jpg` → `same_2.jpg`); hash-prefixed legacy archives are migrated on open.
- **`ui.py`** — `run_app()` creates the `QApplication` and `SpecimenApp(QMainWindow)`. Main window (~1500+ lines): voucher list with status flags (√/× for specimen/photo/classification), photo preview with zoom/pan/grid (2/4/6/8), species autocomplete, image search dialog, version manager, settings. Background photo loading via `QThread`.
- **`parsing.py`** — Voucher number formatting (`YZZ000001` pattern), tube number parsing (extracts location codes, dates, bottle labels from strings like `QD-LSD-SC001-1-R-250923`). Also exports `extract_tube_from_filename()` and `extract_photo_date()` for auto-filling specimen info from photo filenames via `_TUBE_PATTERN` and `_PHOTO_DATE_RE` regexes.
- **`workspace.py`** — Workspace discovery (`default_workspace()` checks last-used, CWD, executable parent), validation (rejects `build/`, `dist/`, `releases/` directories), initialization (copies `字段模版/` from template).
- **`species.py`** — `SpeciesMatcher` loads species presets from `字段模版/表格信息预设字段.xlsx` (columns: 物种中文名, 物种拉丁名, 科中文名, 科拉丁名). Provides fuzzy matching against Chinese and Latin names (ranking: exact > prefix > contains). Auto-reloads when file mtime changes. Returns `SpeciesMatch` (chinese_name, latin_name, family_name, family_latin; `genus_name` derived from the first word of latin_name) and `FamilyMatch` (family_name, family_latin) frozen dataclasses. These feed into `classification_fields.py` converters for autofill.
- **`image_cache.py`** — `ThumbnailCache` stores JPEG thumbnails in `数据/缩略图缓存/`, keyed by source path + file size + mtime + target size. TIFF handling via tifffile/numpy (lazy-imported, uses stride downsampling for large files). In-memory LRU cache (default 64MB).
- **`image_search.py`** — Scans workspace for images (excluding build/dist/releases/data dirs), builds disk-cached index. Scores images against core identifiers (extracted from tube numbers as `XXX-XXX-NNNNNN`). Supports query filtering, linked-photo detection, index appending without rescan.
- **`app_settings.py`** — Persists last workspace, recent workspaces, preview quality, search paths, grid filename toggle, window geometry in `%APPDATA%/标本入库管理/settings.json` (Windows) or `~/.specimen_inventory/settings.json` (Linux).
- **`release_manager.py`** — `list_releases()` discovers versioned release directories under `releases/`, identifies executable files by platform-specific rules.
- **`icon.py`** — Generates app icon programmatically (specimen bottle + label) as QImage, saves as PNG/ICO.
- **`batch_export.py`** — `BatchExportDialog` for exporting specimen data + photos by voucher number list (like NCBI Batch Entrez). Parses voucher numbers from pasted text, writes a multi-sheet Excel workbook, optionally copies photo files and packages as ZIP. Accessible from right-click menu ("批量导出选中") or toolbar button.
- **`__init__.py`** — Only exports `__version__` (currently `"0.2.5"`), used by `build_release.py` as the default build version.

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
- Workspace lock detects stale locks (PID not running or lock older than 10 minutes).
- Image search defaults to TIFF files; users can switch to JPG-only or combined TIFF+JPG.
- The build system uses PyInstaller's `--onedir` mode (fast start, no temp extraction) — not `--onefile`.
- `CURRENT_DATA_SCHEMA_VERSION` in `models.py` controls automated upgrades on workspace open: missing optional classification columns (`属名`, `目`, `纲`, `门`, `备注`) are appended to existing workbooks; hash-prefixed photo archives (`abcdef__original.jpg`) are migrated to clean names.

## Additional docs

- `docs/build-windows.md` — Windows build guide
- `docs/build-linux.md` — Linux build guide
- `docs/linux-user-guide.md` — Linux user guide
