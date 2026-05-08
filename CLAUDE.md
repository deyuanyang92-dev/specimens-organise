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
```

## Architecture

Entry point: `run_app.py` → `specimen_app/main.py:main()` → `specimen_app/ui.py:run_app()`.

### Core modules

- **`models.py`** — All constants: Excel file names, headers, required fields, save method options, category mappings. Dataclasses: `StatusFlags`, `ImportResult`, `ActionResult`, `Row` alias. Custom exceptions: `WorkspaceError`, `WorkspaceLockedError`, `WorkspaceNotInitializedError`, `DuplicateVoucherError`, `ImportConflictError`.
- **`excel_store.py`** — `ExcelStore` is the data layer backing the entire app. Reads/writes all Excel files via openpyxl. Handles CRUD, undo/redo (action-log based, default 200 steps), workspace import with fingerprint-based conflict detection, data snapshots (`create_data_snapshot` / `restore_data_snapshot`), and file locking (`数据/.workspace.lock`). Blocks numpy import during openpyxl usage to prevent library conflicts. Uses in-memory cache (`_row_cache`) to avoid re-parsing files on every read.
- **`ui.py`** — `run_app()` creates the `QApplication` and `SpecimenApp(QMainWindow)`. Main window (~1500+ lines): voucher list with status flags (√/× for specimen/photo/classification), photo preview with zoom/pan/grid (2/4/6/8), species autocomplete, image search dialog, version manager, settings. Background photo loading via `QThread`.
- **`parsing.py`** — Voucher number formatting (`YZZ000001` pattern), tube number parsing (extracts location codes, dates, bottle labels from strings like `QD-LSD-SC001-1-R-250923`). Also exports `extract_tube_from_filename()` and `extract_photo_date()` for auto-filling specimen info from photo filenames via `_TUBE_PATTERN` and `_PHOTO_DATE_RE` regexes.
- **`workspace.py`** — Workspace discovery (`default_workspace()` checks last-used, CWD, executable parent), validation (rejects `build/`, `dist/`, `releases/` directories), initialization (copies `字段模版/` from template).
- **`species.py`** — `SpeciesMatcher` loads species presets from `字段模版/表格信息预设字段.xlsx` (columns: 物种中文名, 物种拉丁名, 科中文名, 科拉丁名). Provides fuzzy matching against Chinese and Latin names. Auto-reloads when file mtime changes.
- **`image_cache.py`** — `ThumbnailCache` stores JPEG thumbnails in `数据/缩略图缓存/`, keyed by source path + file size + mtime + target size. TIFF handling via tifffile/numpy (lazy-imported, uses stride downsampling for large files). In-memory LRU cache (default 64MB).
- **`image_search.py`** — Scans workspace for images (excluding build/dist/releases/data dirs), builds disk-cached index. Scores images against core identifiers (extracted from tube numbers as `XXX-XXX-NNNNNN`). Supports query filtering, linked-photo detection, index appending without rescan.
- **`app_settings.py`** — Persists last workspace, recent workspaces, preview quality, search paths, grid filename toggle, window geometry in `%APPDATA%/标本入库管理/settings.json` (Windows) or `~/.specimen_inventory/settings.json` (Linux).
- **`release_manager.py`** — `list_releases()` discovers versioned release directories under `releases/`, identifies executable files by platform-specific rules.
- **`icon.py`** — Generates app icon programmatically (specimen bottle + label) as QImage, saves as PNG/ICO.

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

Templates live in `字段模版/`. Photos are never copied — only relative paths are recorded. The workspace is fully portable.

## Key conventions

- Excel files are fully rewritten on every save (`_write_plain_rows` creates a fresh `Workbook` each time), so openpyxl's read_only mode is used for all reads.
- Undo/redo is action-log based: every mutation appends to `操作记录.xlsx` with old/new JSON snapshots; undo applies the inverse operation.
- The `_loading` flag in UI prevents save handlers from firing during programmatic field updates.
- All user-facing text is Chinese (labels, field names, file names). Code identifiers are English.
- When `管内编号*` is updated, `采集日期` and `采集地点缩写*` are auto-derived from the tube number.
- Workspace lock detects stale locks (PID not running or lock older than 10 minutes).
- Image search defaults to TIFF files; users can switch to JPG-only or combined TIFF+JPG.
- The build system uses PyInstaller's `--onedir` mode (fast start, no temp extraction) — not `--onefile`.

## Additional docs

- `docs/build-windows.md` — Windows build guide
- `docs/build-linux.md` — Linux build guide
- `docs/linux-user-guide.md` — Linux user guide
