"""每日自动备份（v0.10.42）。

参考 3-2-1 备份原则与 Zotero 的自动数据库备份：数据要有不止一份、放在不止一个位置。
此前本软件只在导入 / 回退等危险操作前做快照，日常录入的数据没有定期备份。

做法：
  * 每个工作区每天第一次打开时，做一份「每日自动备份」快照（复用 create_data_snapshot：
    逐文件 SHA256 + manifest + 完成标记，可直接用「版本管理」还原）；
  * 再把这份快照复制到本机配置目录 ``backups/<工作区ID>/``——工作区常在 U 盘 / 网络盘 / 网盘，
    盘坏、误删、同步冲突时本机还有一份；
  * 工作区内与本机各保留最近 ``keep`` 份「每日自动备份」；**只清理自己做的每日备份**，
    手动快照、导入前快照、回退前快照一律不碰。

本模块不 import PyQt5；由 UI 交给后台写线程执行（与字段保存串行，关窗前会排空）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from .models import DATA_VERSION_DIR

DAILY_OPERATION_TYPE = "每日自动备份"
DEFAULT_KEEP = 14
_MANIFEST = "snapshot_manifest.json"
_COMPLETE = ".snapshot.complete"


def _read_manifest(snapshot_dir: Path) -> dict:
    try:
        return json.loads((snapshot_dir / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_daily(snapshot_dir: Path) -> bool:
    return (snapshot_dir / _COMPLETE).exists() and _read_manifest(snapshot_dir).get("operation_type") == DAILY_OPERATION_TYPE


def _daily_snapshots(directory: Path) -> list[Path]:
    """目录下完整的每日自动备份，按名字（= 时间）从旧到新。"""
    try:
        return sorted((p for p in directory.iterdir() if p.is_dir() and _is_daily(p)), key=lambda p: p.name)
    except OSError:
        return []


def workspace_backup_key(store) -> str:
    """本机备份子目录名：优先工作区 ID（换盘符 / 挪位置也不变），没有则用路径哈希。"""
    wid = str((getattr(store, "config", None) or {}).get("workspace_id", "")).strip()
    if wid:
        return wid
    return hashlib.sha1(str(Path(store.root).resolve()).casefold().encode("utf-8")).hexdigest()[:16]


def has_daily_backup_for(versions_dir: Path, today: date) -> bool:
    prefix = today.strftime("v%Y%m%d")
    return any(p.name.startswith(prefix) for p in _daily_snapshots(versions_dir))


def prune_daily_snapshots(directory: Path, keep: int = DEFAULT_KEEP) -> list[Path]:
    """删掉超出 keep 份的旧「每日自动备份」。其他类型快照不碰。返回删掉的目录。"""
    daily = _daily_snapshots(directory)
    removed: list[Path] = []
    for old in daily[: max(0, len(daily) - keep)]:
        try:
            shutil.rmtree(old)
            removed.append(old)
        except OSError:
            continue
    return removed


def run_daily_backup(store, local_root: Path | str, today: date | None = None,
                     keep: int = DEFAULT_KEEP) -> tuple[Path, Path | None] | None:
    """今天还没备份 → 做一份并复制到本机；返回 (工作区快照, 本机副本或 None)。今天已有 → None。"""
    today = today or date.today()
    versions_dir = Path(store.data_dir) / DATA_VERSION_DIR
    if has_daily_backup_for(versions_dir, today):
        return None
    snapshot = store.create_data_snapshot(DAILY_OPERATION_TYPE, f"{today.isoformat()} 打开工作区时自动备份")
    local_copy: Path | None = None
    try:
        local_dir = Path(local_root) / workspace_backup_key(store)
        local_dir.mkdir(parents=True, exist_ok=True)
        target = local_dir / snapshot.name
        tmp = local_dir / f".{snapshot.name}.copying"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(snapshot, tmp)
        (local_dir / "workspace.txt").write_text(str(store.root), encoding="utf-8")
        tmp.replace(target)  # 拷完再改名：本机副本要么完整、要么不存在
        local_copy = target
        prune_daily_snapshots(local_dir, keep)
    except OSError:
        local_copy = None  # 本机副本失败不影响工作区内那份
    prune_daily_snapshots(versions_dir, keep)
    return snapshot, local_copy


def list_local_backups(local_root: Path | str, store) -> list[Path]:
    """本机保存的该工作区备份（完整的），新的在前。"""
    local_dir = Path(local_root) / workspace_backup_key(store)
    try:
        items = [p for p in local_dir.iterdir() if p.is_dir() and (p / _COMPLETE).exists()]
    except OSError:
        return []
    return sorted(items, key=lambda p: p.name, reverse=True)


def describe_backup(snapshot_dir: Path) -> str:
    m = _read_manifest(snapshot_dir)
    return f"{m.get('created_at', snapshot_dir.name)}  ·  {m.get('operation_type', '')}  ·  v{m.get('software_version', '?')}"


def restore_from_local_backup(store, backup_dir: Path | str) -> Path:
    """把本机备份拷回工作区「数据版本」目录，再走标准还原（会先自动做「回退前快照」）。"""
    backup_dir = Path(backup_dir)
    versions_dir = Path(store.data_dir) / DATA_VERSION_DIR
    versions_dir.mkdir(parents=True, exist_ok=True)
    target = versions_dir / f"{backup_dir.name}_本机备份"
    n = 1
    while target.exists():
        target = versions_dir / f"{backup_dir.name}_本机备份_{n}"
        n += 1
    shutil.copytree(backup_dir, target)
    store.restore_data_snapshot(target)
    return target
