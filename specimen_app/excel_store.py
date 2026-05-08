from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

# openpyxl 会自动 import numpy 用于某些内部优化路径，
# 但 openpyxl 的 numpy 交互与 tifffile 等科学计算库存在冲突。
# 此处临时阻断 numpy import，确保 openpyxl 使用纯 Python 路径。
_numpy_module = sys.modules.get("numpy")
_blocked_numpy_for_openpyxl = "numpy" not in sys.modules
if _blocked_numpy_for_openpyxl:
    sys.modules["numpy"] = None
try:
    from openpyxl import Workbook, load_workbook
finally:
    if _blocked_numpy_for_openpyxl:
        sys.modules.pop("numpy", None)
    elif _numpy_module is not None:
        sys.modules["numpy"] = _numpy_module

from . import __version__
from .models import (
    ACTION_LOG_FILE,
    ACTION_LOG_HEADERS,
    CATEGORY_FILES,
    CATEGORY_HEADERS,
    CHANGE_LOG_FILE,
    CHANGE_LOG_HEADERS,
    CHANGE_SUMMARY_HEADERS,
    CLASSIFICATION_FILE,
    CLASSIFICATION_HEADERS,
    CLASSIFICATION_REQUIRED,
    CURRENT_DATA_SCHEMA_VERSION,
    DATA_VERSION_DIR,
    DATA_VERSION_LOG_FILE,
    DATA_VERSION_LOG_HEADERS,
    DISPLAY_CATEGORY_NAMES,
    INDEX_FILE,
    INDEX_HEADERS,
    ImportConflictError,
    ImportResult,
    PHOTO_FILE,
    PHOTO_HEADERS,
    SPECIMEN_FILE,
    SPECIMEN_HEADERS,
    SPECIMEN_REQUIRED,
    WORKSPACE_CONFIG_FILE,
    WorkspaceLockedError,
    WorkspaceNotInitializedError,
    Row,
    StatusFlags,
)
from .parsing import extract_collection_date, extract_location_code, format_voucher, parse_voucher_serial


DEFAULT_CONFIG = {
    "workspace_id": "",
    "prefix": "YZZ",
    "next_serial": 1,
    "undo_depth": 200,
    "data_schema_version": CURRENT_DATA_SCHEMA_VERSION,
}


class ExcelStore:
    def __init__(self, workspace_root: Path | str, lock: bool = False, create_if_missing: bool = True):
        self.root = Path(workspace_root).resolve()
        self.data_dir = self.root / "数据"
        self.lock_file = self.data_dir / ".workspace.lock"
        self._locked = False
        self._create_if_missing = create_if_missing
        self._row_cache: dict[str, list[Row]] = {}
        self._file_mtimes: dict[str, float] = {}
        if not self.data_dir.exists():
            if not create_if_missing:
                raise WorkspaceNotInitializedError(f"该工作目录尚未初始化，缺少数据目录：{self.data_dir}")
            self.data_dir.mkdir(exist_ok=True)
        if not self.data_dir.is_dir():
            raise WorkspaceNotInitializedError(f"数据路径不是目录：{self.data_dir}")
        if not create_if_missing and not self._has_workspace_seed_files():
            raise WorkspaceNotInitializedError(f"该工作目录尚未初始化，缺少数据文件：{self.data_dir}")
        self.config = self._load_or_create_config()
        if lock:
            self.acquire_lock()
            import atexit
            atexit.register(self.release_lock)
        self.ensure_files()
        self._assert_supported_data_schema()
        self.ensure_index()
        self._sync_next_serial()

    def close(self) -> None:
        self.release_lock()

    def acquire_lock(self) -> None:
        if self._locked:
            return
        payload = {
            "pid": os.getpid(),
            "time": datetime.now().isoformat(timespec="seconds"),
            "workspace": str(self.root),
        }
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._lock_is_stale():
                content = ""
                try:
                    content = self.lock_file.read_text(encoding="utf-8")
                except OSError:
                    pass
                raise WorkspaceLockedError(f"工作区已被占用：{content}")
            try:
                self.lock_file.unlink()
            except OSError:
                pass
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                raise WorkspaceLockedError("工作区已被占用") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self._locked = True

    def _lock_is_stale(self) -> bool:
        try:
            content = self.lock_file.read_text(encoding="utf-8")
            info = json.loads(content)
            pid = int(info.get("pid", 0))
            lock_time = info.get("time", "")
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return True
        if pid <= 0 or pid == os.getpid():
            return True
        if lock_time:
            try:
                locked_at = datetime.fromisoformat(lock_time)
                if (datetime.now() - locked_at).total_seconds() > 600:
                    return True
            except (ValueError, TypeError):
                pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def release_lock(self) -> None:
        if not self._locked:
            return
        try:
            self.lock_file.unlink(missing_ok=True)
        finally:
            self._locked = False

    def ensure_files(self) -> None:
        self._ensure_workbook(self.data_dir / SPECIMEN_FILE, SPECIMEN_HEADERS)
        self._ensure_workbook(self.data_dir / PHOTO_FILE, PHOTO_HEADERS)
        self._ensure_workbook(self.data_dir / CLASSIFICATION_FILE, CLASSIFICATION_HEADERS)
        self._ensure_workbook(self.data_dir / INDEX_FILE, INDEX_HEADERS)
        self._ensure_change_log()
        self._ensure_workbook(self.data_dir / ACTION_LOG_FILE, ACTION_LOG_HEADERS)
        self._ensure_workbook(self.data_dir / DATA_VERSION_LOG_FILE, DATA_VERSION_LOG_HEADERS)

    def _has_workspace_seed_files(self) -> bool:
        return any(
            (self.data_dir / file_name).exists()
            for file_name in [WORKSPACE_CONFIG_FILE, SPECIMEN_FILE, PHOTO_FILE, CLASSIFICATION_FILE, INDEX_FILE]
        )

    def list_vouchers(self) -> list[str]:
        rows = self.read_rows("specimen")
        vouchers = [self._value(row, "入库编号*") for row in rows if self._value(row, "入库编号*")]
        return sorted(vouchers, key=lambda value: parse_voucher_serial(value) or 0)

    def voucher_photo_counts(self) -> dict[str, int]:
        """Return {voucher: photo_count} for all vouchers that have photos."""
        counts: dict[str, int] = {}
        for row in self.read_rows("photo"):
            v = self._value(row, "入库编号*")
            if v:
                counts[v] = counts.get(v, 0) + 1
        return counts

    def status_for(self, voucher: str) -> StatusFlags:
        specimen = self.get_specimen(voucher) or {}
        classification = self.get_classification(voucher) or {}
        photos = self.get_photos(voucher)
        return StatusFlags(
            specimen_complete=all(self._value(specimen, field) for field in SPECIMEN_REQUIRED),
            has_photo=bool(photos),
            classification_complete=all(self._value(classification, field) for field in CLASSIFICATION_REQUIRED),
        )

    def all_status_flags(self) -> dict[str, StatusFlags]:
        specimens = self.read_rows("specimen")
        classifications = self.read_rows("classification")
        photos = self.read_rows("photo")
        class_by_voucher: dict[str, Row] = {}
        for row in classifications:
            v = self._value(row, "入库编号*")
            if v:
                class_by_voucher[v] = row
        photo_vouchers = {self._value(row, "入库编号*") for row in photos if self._value(row, "入库编号*")}
        result: dict[str, StatusFlags] = {}
        for row in specimens:
            v = self._value(row, "入库编号*")
            if not v:
                continue
            class_row = class_by_voucher.get(v, {})
            result[v] = StatusFlags(
                specimen_complete=all(self._value(row, f) for f in SPECIMEN_REQUIRED),
                has_photo=v in photo_vouchers,
                classification_complete=bool(class_row) and all(self._value(class_row, f) for f in CLASSIFICATION_REQUIRED),
            )
        return result

    def get_specimen(self, voucher: str) -> Row | None:
        return self._find_one("specimen", voucher)

    def get_classification(self, voucher: str) -> Row | None:
        return self._find_one("classification", voucher)

    def get_photos(self, voucher: str) -> list[Row]:
        return [row for row in self.read_rows("photo") if self._value(row, "入库编号*") == voucher]

    def create_specimen(self) -> str:
        voucher = self.next_voucher()
        now = self._now()
        row = {header: "" for header in SPECIMEN_HEADERS}
        row["入库编号*"] = voucher
        row["入库日期"] = datetime.now().date().isoformat()
        self._append_row("specimen", row)
        self._append_index(voucher, now, "", "", self.record_fingerprint(voucher, specimen_override=row))
        self._ensure_summary_row(voucher, created_at=now)
        self._record_action("create_specimen", voucher, "specimen", "", {}, row)
        self.config["next_serial"] = max(int(self.config.get("next_serial", 1)), (parse_voucher_serial(voucher) or 0) + 1)
        self._save_config()
        return voucher

    def delete_specimen(self, voucher: str) -> None:
        specimen = self.get_specimen(voucher)
        if not specimen:
            return
        old = {
            "specimen": specimen,
            "classification": self.get_classification(voucher),
            "photos": self.get_photos(voucher),
            "index": self._find_index(voucher),
        }
        self._delete_rows("specimen", voucher)
        self._delete_rows("classification", voucher)
        self._delete_rows("photo", voucher)
        self._delete_index(voucher)
        self._record_action("delete_specimen", voucher, "specimen", "", old, {})

    def clear_photos(self, voucher: str) -> int:
        photos = self.get_photos(voucher)
        if not photos:
            return 0
        self._record_action("clear_photos", voucher, "photo", "", {"photos": photos}, {})
        self._delete_rows("photo", voucher)
        self._invalidate_cache(PHOTO_FILE)
        return len(photos)

    def set_fields(self, category: str, voucher: str, updates: dict[str, Any], action_type: str = "update_fields") -> bool:
        if category not in ("specimen", "classification"):
            raise ValueError(f"Unsupported category: {category}")
        headers = CATEGORY_HEADERS[category]
        updates = {field: self._string(value) for field, value in updates.items() if field in headers}
        if not updates:
            return False

        rows = self.read_rows(category)
        index = self._row_index(rows, voucher)
        if index is None:
            new_row = {header: "" for header in headers}
            new_row["入库编号*"] = voucher
            rows.append(new_row)
            index = len(rows) - 1
        old_row = rows[index].copy()
        changed = {field: value for field, value in updates.items() if self._value(old_row, field) != value}
        if not changed:
            return False

        rows[index].update(changed)
        if category == "specimen" and "管内编号*" in changed:
            tube = rows[index].get("管内编号*", "")
            auto_updates = {
                "采集日期": extract_collection_date(tube),
                "采集地点缩写*": extract_location_code(tube),
            }
            for field, value in auto_updates.items():
                if value and rows[index].get(field) != value:
                    rows[index][field] = value
                    changed[field] = value

        self._write_rows(category, rows)
        new_row = rows[index].copy()
        self._write_changes_and_summary(voucher, category, old_row, new_row, action_type)
        self._update_index_fingerprint(voucher)
        self._record_action(action_type, voucher, category, "", old_row, new_row)
        return True

    def add_photo(self, voucher: str, photo_path: Path | str, allow_outside: bool = False) -> Row:
        row = self._photo_row(voucher, photo_path, allow_outside=allow_outside)
        self._append_row("photo", row)
        self._update_summary_modified(voucher)
        self._record_action("add_photo", voucher, "photo", "", {}, row)
        return row

    def add_photos(self, voucher: str, photo_paths: list[Path | str], allow_outside: bool = False) -> list[Row]:
        rows_to_add = [self._photo_row(voucher, path, allow_outside=allow_outside) for path in photo_paths]
        if not rows_to_add:
            return []
        rows = self.read_rows("photo")
        rows.extend(rows_to_add)
        self._write_rows("photo", rows)
        self._update_summary_modified(voucher)
        self._record_action("add_photos", voucher, "photo", "", {}, rows_to_add)
        return rows_to_add

    def find_photo_conflicts(self, photo_paths: list[Path | str], target_voucher: str) -> dict[str, str]:
        resolved_inputs = {Path(p).resolve() for p in photo_paths}
        if not resolved_inputs:
            return {}
        conflicts: dict[str, str] = {}
        for row in self.read_rows("photo"):
            voucher = self._value(row, "入库编号*")
            if voucher == target_voucher:
                continue
            row_path = self.resolve_photo_path(row)
            if row_path:
                resolved = Path(row_path).resolve()
                if resolved in resolved_inputs:
                    conflicts[str(resolved)] = voucher
        return conflicts

    def export_all_data(self, target: Path) -> int:
        wb = Workbook()
        wb.remove(wb.active)
        count = 0
        for category in CATEGORY_FILES:
            rows = self.read_rows(category)
            if not rows:
                continue
            ws = wb.create_sheet(title=category)
            headers = CATEGORY_HEADERS.get(category, [])
            ws.append(headers)
            for row in rows:
                ws.append([row.get(h, "") for h in headers])
            count += len(rows)
        wb.save(str(target))
        return count

    def import_from_file(self, source: Path) -> ImportResult:
        source_rows = self._read_external_rows(source, SPECIMEN_HEADERS)
        if not source_rows:
            return ImportResult(imported=0, skipped=0, photos_imported=0)
        existing = set(self.list_vouchers())
        imported_ids: list[str] = []
        skipped = 0
        for row in source_rows:
            voucher = self._value(row, "入库编号*")
            if not voucher:
                continue
            if voucher in existing:
                skipped += 1
                continue
            imported_ids.append(voucher)
        if not imported_ids:
            return ImportResult(imported=0, skipped=skipped, photos_imported=0)
        self.create_data_snapshot("导入前快照", f"导入数据文件前自动快照：{source}")
        target_specimens = self.read_rows("specimen")
        target_classes = self.read_rows("classification")
        target_photos = self.read_rows("photo")
        target_index = self._read_plain_rows(self.data_dir / INDEX_FILE)
        now = self._now()
        import_set = set(imported_ids)
        for row in source_rows:
            voucher = self._value(row, "入库编号*")
            if voucher in import_set:
                target_specimens.append(self._fit_headers(row, SPECIMEN_HEADERS))
                target_index.append(
                    {
                        "入库编号": voucher,
                        "record_id": str(uuid.uuid4()),
                        "创建时间": now,
                        "来源工作区": str(source),
                        "来源记录ID": "",
                        "记录指纹": self._fingerprint_from_rows(row),
                    }
                )
                self._ensure_summary_row(voucher, created_at=now)
        self._write_rows("specimen", target_specimens)
        self._write_rows("classification", target_classes)
        self._write_rows("photo", target_photos)
        self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, target_index)
        self._sync_next_serial()
        self._record_action("import_file", "", "workspace", "", {}, {"source": str(source), "imported": imported_ids})
        self._record_data_version("导入数据文件", f"来源：{source}；导入 {len(imported_ids)} 个标本")
        return ImportResult(imported=len(imported_ids), skipped=skipped, photos_imported=0)

    def _photo_row(self, voucher: str, photo_path: Path | str, allow_outside: bool = False) -> Row:
        path = Path(photo_path).resolve()
        relative = self.relative_photo_path(path, allow_outside=allow_outside)
        return {
            "入库编号*": voucher,
            "文件名": path.name,
            "相对路径": relative,
            "描述": "",
            "来源工作区根路径": "",
        }

    def delete_photo(self, voucher: str, photo_index: int) -> bool:
        rows = self.read_rows("photo")
        matching_positions = [i for i, row in enumerate(rows) if self._value(row, "入库编号*") == voucher]
        if photo_index < 0 or photo_index >= len(matching_positions):
            return False
        position = matching_positions[photo_index]
        old_row = rows.pop(position)
        self._write_rows("photo", rows)
        self._update_summary_modified(voucher)
        self._record_action("delete_photo", voucher, "photo", "", old_row, {})
        return True

    def set_photo_description(self, voucher: str, photo_index: int, description: str) -> bool:
        rows = self.read_rows("photo")
        matching_positions = [i for i, row in enumerate(rows) if self._value(row, "入库编号*") == voucher]
        if photo_index < 0 or photo_index >= len(matching_positions):
            return False
        position = matching_positions[photo_index]
        old_row = rows[position].copy()
        rows[position]["描述"] = self._string(description)
        if old_row == rows[position]:
            return False
        self._write_rows("photo", rows)
        self._append_field_changes(voucher, "photo", old_row, rows[position], "update_photo")
        self._update_summary_modified(voucher)
        self._record_action("update_photo", voucher, "photo", "描述", old_row, rows[position].copy())
        return True

    def next_voucher(self) -> str:
        self.assert_unique_vouchers()
        max_serial = self._max_existing_serial()
        return format_voucher(max(max_serial + 1, 1))

    def assert_unique_vouchers(self) -> None:
        duplicate_messages: list[str] = []
        for category in ("specimen", "classification"):
            ids = [self._value(row, "入库编号*") for row in self.read_rows(category) if self._value(row, "入库编号*")]
            duplicates = [voucher for voucher, count in Counter(ids).items() if count > 1]
            if duplicates:
                duplicate_messages.append(f"{DISPLAY_CATEGORY_NAMES[category]} 重复: {', '.join(duplicates)}")

        index_ids = [self._value(row, "入库编号") for row in self._read_plain_rows(self.data_dir / INDEX_FILE) if self._value(row, "入库编号")]
        duplicates = [voucher for voucher, count in Counter(index_ids).items() if count > 1]
        if duplicates:
            duplicate_messages.append(f"编号索引重复: {', '.join(duplicates)}")

        if duplicate_messages:
            raise ImportConflictError("发现重复入库编号，已阻止继续写入。\n" + "\n".join(duplicate_messages))

    def import_workspace(self, source_root: Path | str) -> ImportResult:
        source = Path(source_root).resolve()
        source_data = source / "数据"
        if not source_data.exists():
            source_data = source
        source_specimens = self._read_external_rows(source_data / SPECIMEN_FILE, SPECIMEN_HEADERS)
        source_classes = self._read_external_rows(source_data / CLASSIFICATION_FILE, CLASSIFICATION_HEADERS)
        source_photos = self._read_external_rows(source_data / PHOTO_FILE, PHOTO_HEADERS)

        source_ids = [self._value(row, "入库编号*") for row in source_specimens if self._value(row, "入库编号*")]
        duplicate_source = [voucher for voucher, count in Counter(source_ids).items() if count > 1]
        if duplicate_source:
            report = self._write_conflict_report(
                [{"入库编号": voucher, "冲突类型": "源工作区内部重复", "源记录摘要": "", "目标记录摘要": ""} for voucher in duplicate_source]
            )
            raise ImportConflictError("源工作区存在重复入库编号，导入已阻止。", report)

        target_fingerprints = {voucher: self.record_fingerprint(voucher) for voucher in self.list_vouchers()}
        source_classes_by_id = {self._value(row, "入库编号*"): row for row in source_classes if self._value(row, "入库编号*")}
        conflicts: list[dict[str, str]] = []
        skipped = 0
        import_ids: list[str] = []

        for row in source_specimens:
            voucher = self._value(row, "入库编号*")
            if not voucher:
                continue
            source_fp = self._fingerprint_from_rows(row, source_classes_by_id.get(voucher))
            if voucher in target_fingerprints:
                if source_fp == target_fingerprints[voucher]:
                    skipped += 1
                    continue
                conflicts.append(
                    {
                        "入库编号": voucher,
                        "冲突类型": "目标工作区已有不同标本",
                        "源记录摘要": self._record_summary(row, source_classes_by_id.get(voucher)),
                        "目标记录摘要": self._record_summary(self.get_specimen(voucher), self.get_classification(voucher)),
                    }
                )
            else:
                import_ids.append(voucher)

        if conflicts:
            report = self._write_conflict_report(conflicts)
            raise ImportConflictError("发现入库编号冲突，导入已阻止。", report)

        if not import_ids:
            return ImportResult(imported=0, skipped=skipped, photos_imported=0)

        self.create_data_snapshot("导入前快照", f"导入工作区前自动快照：{source}")
        target_specimens = self.read_rows("specimen")
        target_classes = self.read_rows("classification")
        target_photos = self.read_rows("photo")
        target_index = self._read_plain_rows(self.data_dir / INDEX_FILE)
        source_classes_by_id = {self._value(row, "入库编号*"): row for row in source_classes if self._value(row, "入库编号*")}
        import_id_set = set(import_ids)
        now = self._now()

        for row in source_specimens:
            voucher = self._value(row, "入库编号*")
            if voucher in import_id_set:
                target_specimens.append(self._fit_headers(row, SPECIMEN_HEADERS))
                class_row = source_classes_by_id.get(voucher)
                if class_row:
                    target_classes.append(self._fit_headers(class_row, CLASSIFICATION_HEADERS))
                target_index.append(
                    {
                        "入库编号": voucher,
                        "record_id": str(uuid.uuid4()),
                        "创建时间": now,
                        "来源工作区": str(source),
                        "来源记录ID": "",
                        "记录指纹": self._fingerprint_from_rows(row, class_row),
                    }
                )
                self._ensure_summary_row(voucher, created_at=now)

        photos_imported = 0
        for photo in source_photos:
            voucher = self._value(photo, "入库编号*")
            if voucher in import_id_set:
                fitted = self._fit_headers(photo, PHOTO_HEADERS)
                if not fitted.get("来源工作区根路径"):
                    fitted["来源工作区根路径"] = str(source)
                target_photos.append(fitted)
                photos_imported += 1

        self._write_rows("specimen", target_specimens)
        self._write_rows("classification", target_classes)
        self._write_rows("photo", target_photos)
        self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, target_index)
        self._sync_next_serial()
        self._record_action("import_workspace", "", "workspace", "", {}, {"source": str(source), "imported": import_ids})
        self._record_data_version("导入工作区", f"来源：{source}；导入 {len(import_ids)} 个标本，照片 {photos_imported} 张")
        return ImportResult(imported=len(import_ids), skipped=skipped, photos_imported=photos_imported)

    def create_data_snapshot(self, operation_type: str = "手动快照", summary: str = "") -> Path:
        version_id = datetime.now().strftime("v%Y%m%d_%H%M%S")
        snapshot_dir = self.data_dir / DATA_VERSION_DIR / version_id
        suffix = 1
        while snapshot_dir.exists():
            snapshot_dir = self.data_dir / DATA_VERSION_DIR / f"{version_id}_{suffix}"
            suffix += 1
        snapshot_dir.mkdir(parents=True)
        for path in self.data_dir.iterdir():
            if path.name == DATA_VERSION_DIR or path.name == ".workspace.lock":
                continue
            if path.is_file() and path.suffix.lower() in {".xlsx", ".json"}:
                shutil.copy2(path, snapshot_dir / path.name)
        manifest = {
            "version_id": snapshot_dir.name,
            "created_at": self._now(),
            "operation_type": operation_type,
            "software_version": __version__,
            "data_schema_version": self.config.get("data_schema_version", CURRENT_DATA_SCHEMA_VERSION),
            "summary": summary,
            "workspace": str(self.root),
        }
        with (snapshot_dir / "snapshot_manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        self._record_data_version(operation_type, summary or operation_type, snapshot_dir)
        return snapshot_dir

    def list_data_versions(self) -> list[Row]:
        rows = self._read_plain_rows(self.data_dir / DATA_VERSION_LOG_FILE, DATA_VERSION_LOG_HEADERS)
        return [row for row in rows if self._value(row, "快照路径")]

    def restore_data_snapshot(self, snapshot_path: Path | str) -> None:
        snapshot = Path(snapshot_path).resolve()
        expected_parent = (self.data_dir / DATA_VERSION_DIR).resolve()
        if not str(snapshot).startswith(str(expected_parent)):
            raise ValueError(f"快照路径不在数据版本目录内：{snapshot}")
        if not snapshot.exists() or not snapshot.is_dir():
            raise FileNotFoundError(f"数据版本不存在：{snapshot}")
        self.create_data_snapshot("回退前快照", f"回退到 {snapshot.name} 前自动保存当前状态")
        for path in snapshot.iterdir():
            if not path.is_file():
                continue
            if path.name in {DATA_VERSION_LOG_FILE, "snapshot_manifest.json", ".workspace.lock"}:
                continue
            if path.suffix.lower() in {".xlsx", ".json"}:
                shutil.copy2(path, self.data_dir / path.name)
        self.config = self._load_or_create_config()
        self._record_data_version("回退数据版本", f"已恢复：{snapshot.name}", snapshot)
        self.ensure_files()
        self.ensure_index()
        self._sync_next_serial()

    def undo_last(self) -> str | None:
        rows = self._read_plain_rows(self.data_dir / ACTION_LOG_FILE)
        depth = int(self.config.get("undo_depth", 200))
        candidates = [row for row in rows[-depth:] if self._value(row, "是否撤销") != "是"]
        if not candidates:
            return None
        action = candidates[-1]
        self._apply_action(action, undo=True)
        action["是否撤销"] = "是"
        self._write_plain_rows(self.data_dir / ACTION_LOG_FILE, ACTION_LOG_HEADERS, rows)
        return self._value(action, "操作类型")

    def redo_last(self) -> str | None:
        rows = self._read_plain_rows(self.data_dir / ACTION_LOG_FILE)
        if not any(self._value(row, "是否撤销") == "是" for row in rows):
            return None
        start = len(rows) - 1
        while start >= 0 and self._value(rows[start], "是否撤销") == "是":
            start -= 1
        action_index = start + 1
        if action_index >= len(rows):
            return None
        action = rows[action_index]
        self._apply_action(action, undo=False)
        action["是否撤销"] = ""
        self._write_plain_rows(self.data_dir / ACTION_LOG_FILE, ACTION_LOG_HEADERS, rows)
        return self._value(action, "操作类型")

    def set_undo_depth(self, depth: int) -> None:
        self.config["undo_depth"] = max(1, min(int(depth), 1000))
        self._save_config()

    def resolve_photo_path(self, photo_row: Row) -> Path:
        relative = self._value(photo_row, "相对路径")
        candidates = [self._resolve_relative(self.root, relative)]
        source_root = self._value(photo_row, "来源工作区根路径")
        if source_root:
            src = Path(source_root)
            if src.is_dir():
                candidates.append(self._resolve_relative(src, relative))
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def relative_photo_path(self, path: Path, allow_outside: bool = False) -> str:
        path = path.resolve()
        try:
            relative = path.relative_to(self.root)
            return "./" + relative.as_posix()
        except ValueError:
            if not allow_outside:
                raise ValueError("照片不在当前工作区内，无法生成稳定的工作区相对路径")
            return Path(os.path.relpath(path, self.root)).as_posix()

    def _cached_rows(self, file_key: str, loader: Callable[[], list[Row]]) -> list[Row]:
        file_path = self.data_dir / file_key
        try:
            current_mtime = file_path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        cached_mtime = self._file_mtimes.get(file_key, -1.0)
        if file_key in self._row_cache and cached_mtime == current_mtime:
            return [row.copy() for row in self._row_cache[file_key]]
        rows = loader()
        self._row_cache[file_key] = rows
        self._file_mtimes[file_key] = current_mtime
        return [row.copy() for row in rows]

    def _invalidate_cache(self, *file_keys: str) -> None:
        for key in file_keys:
            self._row_cache.pop(key, None)
            self._file_mtimes.pop(key, None)

    def read_rows(self, category: str) -> list[Row]:
        file_key = CATEGORY_FILES[category]
        return self._cached_rows(
            file_key,
            lambda: self._read_plain_rows(self.data_dir / file_key, CATEGORY_HEADERS[category]),
        )

    def record_fingerprint(
        self,
        voucher: str,
        specimen_override: Row | None = None,
        classification_override: Row | None = None,
    ) -> str:
        specimen = specimen_override if specimen_override is not None else self.get_specimen(voucher)
        classification = classification_override if classification_override is not None else self.get_classification(voucher)
        return self._fingerprint_from_rows(specimen, classification)

    def ensure_index(self) -> None:
        index_rows = self._read_plain_rows(self.data_dir / INDEX_FILE)
        indexed = {self._value(row, "入库编号") for row in index_rows if self._value(row, "入库编号")}
        now = self._now()
        changed = False
        for voucher in self.list_vouchers():
            if voucher not in indexed:
                index_rows.append(
                    {
                        "入库编号": voucher,
                        "record_id": str(uuid.uuid4()),
                        "创建时间": now,
                        "来源工作区": "",
                        "来源记录ID": "",
                        "记录指纹": self.record_fingerprint(voucher),
                    }
                )
                changed = True
        if changed:
            self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, index_rows)

    def _load_or_create_config(self) -> dict[str, Any]:
        path = self.data_dir / WORKSPACE_CONFIG_FILE
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            merged = {**DEFAULT_CONFIG, **data}
        else:
            if not self._create_if_missing:
                raise WorkspaceNotInitializedError(f"该工作目录尚未初始化，缺少配置文件：{path}")
            merged = DEFAULT_CONFIG.copy()
        if not merged.get("workspace_id"):
            merged["workspace_id"] = str(uuid.uuid4())
        if not merged.get("data_schema_version"):
            merged["data_schema_version"] = CURRENT_DATA_SCHEMA_VERSION
        self.config = merged
        self._save_config()
        return merged

    def _assert_supported_data_schema(self) -> None:
        current = str(self.config.get("data_schema_version", CURRENT_DATA_SCHEMA_VERSION))
        if _version_tuple(current) > _version_tuple(CURRENT_DATA_SCHEMA_VERSION):
            raise ImportConflictError(
                f"该工作区数据版本为 {current}，高于当前软件支持的 {CURRENT_DATA_SCHEMA_VERSION}，已禁止写入。"
            )

    def _save_config(self) -> None:
        path = self.data_dir / WORKSPACE_CONFIG_FILE
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.config, handle, ensure_ascii=False, indent=2)

    def _ensure_workbook(self, path: Path, headers: list[str]) -> None:
        if not path.exists():
            wb = Workbook()
            ws = wb.active
            ws.title = "Sheet1"
            ws.append(headers)
            wb.save(path)
            return
        rows = self._read_plain_rows(path)
        existing_headers = self._headers(path)
        missing = [header for header in headers if header not in existing_headers]
        if missing:
            self._write_plain_rows(path, existing_headers + missing, rows)

    def _ensure_change_log(self) -> None:
        path = self.data_dir / CHANGE_LOG_FILE
        if path.exists():
            return
        wb = Workbook()
        ws = wb.active
        ws.title = "修改明细"
        ws.append(CHANGE_LOG_HEADERS)
        summary = wb.create_sheet("修改汇总")
        summary.append(CHANGE_SUMMARY_HEADERS)
        wb.save(path)

    def _record_data_version(self, operation_type: str, summary: str, snapshot_path: Path | None = None) -> None:
        rows = self._read_plain_rows(self.data_dir / DATA_VERSION_LOG_FILE, DATA_VERSION_LOG_HEADERS)
        rows.append(
            {
                "版本ID": snapshot_path.name if snapshot_path else datetime.now().strftime("v%Y%m%d_%H%M%S"),
                "时间": self._now(),
                "操作类型": operation_type,
                "软件版本": __version__,
                "数据结构版本": self.config.get("data_schema_version", CURRENT_DATA_SCHEMA_VERSION),
                "操作者": os.environ.get("USERNAME") or os.environ.get("USER") or "",
                "摘要": summary,
                "快照路径": str(snapshot_path.resolve()) if snapshot_path else "",
            }
        )
        self._write_plain_rows(self.data_dir / DATA_VERSION_LOG_FILE, DATA_VERSION_LOG_HEADERS, rows)

    def _write_changes_and_summary(self, voucher: str, category: str, old_row: Row, new_row: Row, action_type: str) -> None:
        """Append field changes and update summary in a single file write."""
        now = self._now()
        detail_rows = self._read_change_detail_rows()
        summary_rows = self._read_summary_rows()
        for field in CATEGORY_HEADERS[category]:
            old = self._value(old_row, field)
            new = self._value(new_row, field)
            if old != new:
                detail_rows.append(
                    {
                        "入库编号": voucher,
                        "信息类别": DISPLAY_CATEGORY_NAMES[category],
                        "字段名": field,
                        "旧值": old,
                        "新值": new,
                        "修改时间": now,
                        "操作类型": action_type,
                    }
                )
        if not any(self._value(row, "入库编号") == voucher for row in summary_rows):
            summary_rows.append(
                {
                    "入库编号": voucher,
                    "创建时间": now,
                    "第一次修改时间": "",
                    "第二次修改时间": "",
                    "最近修改时间": "",
                    "修改次数": 0,
                }
            )
        for row in summary_rows:
            if self._value(row, "入库编号") == voucher:
                count = int(row.get("修改次数") or 0) + 1
                row["修改次数"] = count
                if count == 1:
                    row["第一次修改时间"] = now
                elif count == 2:
                    row["第二次修改时间"] = now
                row["最近修改时间"] = now
                break
        path = self.data_dir / CHANGE_LOG_FILE
        self._ensure_change_log()
        with self._open_workbook(path) as wb:
            if "修改明细" not in wb.sheetnames:
                wb.create_sheet("修改明细")
            self._replace_sheet(wb["修改明细"], CHANGE_LOG_HEADERS, detail_rows)
            if "修改汇总" not in wb.sheetnames:
                wb.create_sheet("修改汇总")
            self._replace_sheet(wb["修改汇总"], CHANGE_SUMMARY_HEADERS, summary_rows)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            wb.save(tmp)
            tmp.replace(path)

    def _read_change_detail_rows(self) -> list[Row]:
        path = self.data_dir / CHANGE_LOG_FILE
        return self._read_sheet_rows(path, "修改明细", CHANGE_LOG_HEADERS)

    def _write_change_detail_rows(self, rows: list[Row]) -> None:
        path = self.data_dir / CHANGE_LOG_FILE
        with self._open_workbook(path) as wb:
            if "修改明细" not in wb.sheetnames:
                wb.create_sheet("修改明细")
            ws = wb["修改明细"]
            self._replace_sheet(ws, CHANGE_LOG_HEADERS, rows)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            wb.save(tmp)
            tmp.replace(path)

    def _read_summary_rows(self) -> list[Row]:
        return self._read_sheet_rows(self.data_dir / CHANGE_LOG_FILE, "修改汇总", CHANGE_SUMMARY_HEADERS)

    def _write_summary_rows(self, rows: list[Row]) -> None:
        path = self.data_dir / CHANGE_LOG_FILE
        with self._open_workbook(path) as wb:
            if "修改汇总" not in wb.sheetnames:
                wb.create_sheet("修改汇总")
            ws = wb["修改汇总"]
            self._replace_sheet(ws, CHANGE_SUMMARY_HEADERS, rows)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            wb.save(tmp)
            tmp.replace(path)

    def _ensure_summary_row(self, voucher: str, created_at: str | None = None) -> None:
        rows = self._read_summary_rows()
        if any(self._value(row, "入库编号") == voucher for row in rows):
            return
        rows.append(
            {
                "入库编号": voucher,
                "创建时间": created_at or self._now(),
                "第一次修改时间": "",
                "第二次修改时间": "",
                "最近修改时间": "",
                "修改次数": 0,
            }
        )
        self._write_summary_rows(rows)

    def _update_summary_modified(self, voucher: str) -> None:
        rows = self._read_summary_rows()
        if not any(self._value(row, "入库编号") == voucher for row in rows):
            self._ensure_summary_row(voucher)
            rows = self._read_summary_rows()
        now = self._now()
        for row in rows:
            if self._value(row, "入库编号") == voucher:
                count = int(row.get("修改次数") or 0) + 1
                row["修改次数"] = count
                if count == 1:
                    row["第一次修改时间"] = now
                elif count == 2:
                    row["第二次修改时间"] = now
                row["最近修改时间"] = now
                break
        self._write_summary_rows(rows)

    def _record_action(
        self,
        action_type: str,
        voucher: str,
        category: str,
        field: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        rows = self._read_plain_rows(self.data_dir / ACTION_LOG_FILE, ACTION_LOG_HEADERS)
        rows.append(
            {
                "操作ID": str(uuid.uuid4()),
                "时间": self._now(),
                "操作类型": action_type,
                "入库编号": voucher,
                "信息类别": category,
                "字段名": field,
                "旧值JSON": json.dumps(old_value, ensure_ascii=False, default=str),
                "新值JSON": json.dumps(new_value, ensure_ascii=False, default=str),
                "是否撤销": "",
            }
        )
        self._write_plain_rows(self.data_dir / ACTION_LOG_FILE, ACTION_LOG_HEADERS, rows)

    def _apply_action(self, action: Row, undo: bool) -> None:
        action_type = self._value(action, "操作类型")
        voucher = self._value(action, "入库编号")
        category = self._value(action, "信息类别")
        old_value = self._json(action.get("旧值JSON"))
        new_value = self._json(action.get("新值JSON"))
        value = old_value if undo else new_value

        if action_type in ("update_fields", "classification_autofill"):
            rows = self.read_rows(category)
            index = self._row_index(rows, voucher)
            if index is None and value:
                rows.append(value)
            elif index is not None:
                rows[index] = self._fit_headers(value, CATEGORY_HEADERS[category])
            self._write_rows(category, rows)
            self._update_index_fingerprint(voucher)
        elif action_type == "update_photo":
            rows = self.read_rows("photo")
            target = old_value if undo else new_value
            opposite = new_value if undo else old_value
            idx = self._find_photo_row_index(rows, opposite)
            if idx is not None:
                rows[idx] = self._fit_headers(target, PHOTO_HEADERS)
                self._write_rows("photo", rows)
        elif action_type == "add_photo":
            if undo:
                self._remove_photo_row(old_value=new_value)
            else:
                self._append_row("photo", new_value)
        elif action_type == "add_photos":
            photos = new_value if isinstance(new_value, list) else []
            if undo:
                for photo in photos:
                    self._remove_photo_row(old_value=photo)
            else:
                rows = self.read_rows("photo")
                rows.extend(self._fit_headers(photo, PHOTO_HEADERS) for photo in photos)
                self._write_rows("photo", rows)
        elif action_type == "delete_photo":
            if undo:
                self._append_row("photo", old_value)
            else:
                self._remove_photo_row(old_value=old_value)
        elif action_type == "create_specimen":
            if undo:
                self._delete_rows("specimen", voucher)
                self._delete_index(voucher)
            else:
                self._append_row("specimen", new_value)
                self._append_index(voucher, self._now(), "", "", self.record_fingerprint(voucher, specimen_override=new_value))
        elif action_type == "delete_specimen":
            if undo:
                specimen = old_value.get("specimen")
                classification = old_value.get("classification")
                photos = old_value.get("photos") or []
                index = old_value.get("index")
                if specimen:
                    self._append_row("specimen", specimen)
                if classification:
                    self._append_row("classification", classification)
                for photo in photos:
                    self._append_row("photo", photo)
                if index:
                    self._append_index_row(index)
            else:
                self._delete_rows("specimen", voucher)
                self._delete_rows("classification", voucher)
                self._delete_rows("photo", voucher)
                self._delete_index(voucher)
        elif action_type == "clear_photos":
            if undo:
                for photo in old_value.get("photos") or []:
                    self._append_row("photo", photo)
                self._invalidate_cache(PHOTO_FILE)
            else:
                self._delete_rows("photo", voucher)
                self._invalidate_cache(PHOTO_FILE)

    def _remove_photo_row(self, old_value: Row) -> None:
        rows = self.read_rows("photo")
        idx = self._find_photo_row_index(rows, old_value)
        if idx is not None:
            rows.pop(idx)
            self._write_rows("photo", rows)

    def _find_photo_row_index(self, rows: list[Row], target: Row) -> int | None:
        fitted = self._fit_headers(target, PHOTO_HEADERS)
        for idx, row in enumerate(rows):
            if self._fit_headers(row, PHOTO_HEADERS) == fitted:
                return idx
        return None

    def _append_row(self, category: str, row: Row) -> None:
        rows = self.read_rows(category)
        rows.append(self._fit_headers(row, CATEGORY_HEADERS[category]))
        self._write_rows(category, rows)

    def _write_rows(self, category: str, rows: list[Row]) -> None:
        self._write_plain_rows(self.data_dir / CATEGORY_FILES[category], CATEGORY_HEADERS[category], rows)
        self._invalidate_cache(CATEGORY_FILES[category])

    def _delete_rows(self, category: str, voucher: str) -> None:
        rows = [row for row in self.read_rows(category) if self._value(row, "入库编号*") != voucher]
        self._write_rows(category, rows)

    def _find_one(self, category: str, voucher: str) -> Row | None:
        for row in self.read_rows(category):
            if self._value(row, "入库编号*") == voucher:
                return row
        return None

    def _row_index(self, rows: list[Row], voucher: str) -> int | None:
        for idx, row in enumerate(rows):
            if self._value(row, "入库编号*") == voucher:
                return idx
        return None

    def _append_index(self, voucher: str, created_at: str, source_workspace: str, source_record_id: str, fingerprint: str) -> None:
        self._append_index_row(
            {
                "入库编号": voucher,
                "record_id": str(uuid.uuid4()),
                "创建时间": created_at,
                "来源工作区": source_workspace,
                "来源记录ID": source_record_id,
                "记录指纹": fingerprint,
            }
        )

    def _append_index_row(self, row: Row) -> None:
        rows = self._read_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS)
        voucher = self._value(row, "入库编号")
        if any(self._value(existing, "入库编号") == voucher for existing in rows):
            return
        rows.append(self._fit_headers(row, INDEX_HEADERS))
        self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, rows)

    def _find_index(self, voucher: str) -> Row | None:
        for row in self._read_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS):
            if self._value(row, "入库编号") == voucher:
                return row
        return None

    def _delete_index(self, voucher: str) -> None:
        rows = [row for row in self._read_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS) if self._value(row, "入库编号") != voucher]
        self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, rows)

    def _update_index_fingerprint(self, voucher: str) -> None:
        rows = self._read_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS)
        changed = False
        for row in rows:
            if self._value(row, "入库编号") == voucher:
                row["记录指纹"] = self.record_fingerprint(voucher)
                changed = True
        if changed:
            self._write_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS, rows)

    def _max_existing_serial(self) -> int:
        serials: list[int] = []
        for category in ("specimen", "classification", "photo"):
            header = "入库编号*"
            serials.extend(
                serial
                for row in self.read_rows(category)
                for serial in [parse_voucher_serial(self._value(row, header))]
                if serial is not None
            )
        serials.extend(
            serial
            for row in self._read_plain_rows(self.data_dir / INDEX_FILE, INDEX_HEADERS)
            for serial in [parse_voucher_serial(self._value(row, "入库编号"))]
            if serial is not None
        )
        return max(serials, default=0)

    def _sync_next_serial(self) -> None:
        self.config["next_serial"] = self._max_existing_serial() + 1
        self._save_config()

    def _write_conflict_report(self, conflicts: list[dict[str, str]]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.data_dir / f"导入冲突报告_{timestamp}.xlsx"
        headers = ["入库编号", "冲突类型", "源记录摘要", "目标记录摘要"]
        self._write_plain_rows(path, headers, conflicts)
        return path

    def _record_summary(self, specimen: Row | None, classification: Row | None) -> str:
        if not specimen and not classification:
            return ""
        specimen = specimen or {}
        classification = classification or {}
        parts = [
            f"管内编号={self._value(specimen, '管内编号*')}",
            f"地点={self._value(specimen, '采集地点缩写*')}",
            f"日期={self._value(specimen, '采集日期')}",
            f"种名={self._value(classification, '种名*')}",
            f"科={self._value(classification, '科*')}",
        ]
        return "; ".join(parts)

    def _fingerprint_from_rows(self, specimen: Row | None, classification: Row | None) -> str:
        payload = {
            "specimen": self._fit_headers(specimen or {}, SPECIMEN_HEADERS),
            "classification": self._fit_headers(classification or {}, CLASSIFICATION_HEADERS),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _read_external_rows(self, path: Path, required_headers: list[str]) -> list[Row]:
        if not path.exists():
            return []
        return [self._fit_headers(row, required_headers) for row in self._read_plain_rows(path, required_headers)]

    def _headers(self, path: Path) -> list[str]:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            return [self._string(cell.value) for cell in next(ws.iter_rows(max_row=1))]
        finally:
            wb.close()

    def _read_plain_rows(self, path: Path, fallback_headers: list[str] | None = None) -> list[Row]:
        if not path.exists():
            return []
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return []
            headers = [self._string(value) for value in rows[0]]
            if fallback_headers:
                headers = headers or fallback_headers
            data: list[Row] = []
            for raw in rows[1:]:
                row = {header: self._string(raw[idx]) if idx < len(raw) else "" for idx, header in enumerate(headers) if header}
                if any(value != "" for value in row.values()):
                    data.append(row)
            return data
        finally:
            wb.close()

    def _read_sheet_rows(self, path: Path, sheet_name: str, fallback_headers: list[str]) -> list[Row]:
        if not path.exists():
            return []
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet_name not in wb.sheetnames:
                return []
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return []
            headers = [self._string(value) for value in rows[0]] or fallback_headers
            data: list[Row] = []
            for raw in rows[1:]:
                row = {header: self._string(raw[idx]) if idx < len(raw) else "" for idx, header in enumerate(headers) if header}
                if any(value != "" for value in row.values()):
                    data.append(row)
            return data
        finally:
            wb.close()

    def _write_plain_rows(self, path: Path, headers: list[str], rows: list[Row]) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(headers)
        for row in rows:
            fitted = self._fit_headers(row, headers)
            ws.append([fitted.get(header, "") for header in headers])
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        wb.save(tmp)
        tmp.replace(path)
        self._invalidate_cache(path.name)

    def _replace_sheet(self, ws: Any, headers: list[str], rows: list[Row]) -> None:
        ws.delete_rows(1, ws.max_row)
        ws.append(headers)
        for row in rows:
            fitted = self._fit_headers(row, headers)
            ws.append([fitted.get(header, "") for header in headers])

    @contextmanager
    def _open_workbook(self, path: Path) -> Iterator[Any]:
        wb = load_workbook(path)
        try:
            yield wb
        finally:
            wb.close()

    def _fit_headers(self, row: Row, headers: list[str]) -> Row:
        return {header: self._string((row or {}).get(header, "")) for header in headers}

    def _value(self, row: Row | None, field: str) -> str:
        if not row:
            return ""
        return self._string(row.get(field, ""))

    def _string(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat(sep=" ", timespec="seconds")
        return str(value).strip()

    def _json(self, value: object) -> Any:
        if value in (None, ""):
            return {}
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            return {}

    def _resolve_relative(self, root: Path, relative: str) -> Path:
        text = str(relative or "").strip()
        if text.startswith("./"):
            text = text[2:]
        resolved = (root / text).resolve()
        if not str(resolved).startswith(str(root.resolve())):
            return root / text
        return resolved

    def _now(self) -> str:
        return datetime.now().isoformat(sep=" ", timespec="seconds")


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = []
    for raw in str(value).split(".")[:3]:
        try:
            parts.append(int(raw))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
