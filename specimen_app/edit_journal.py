"""未保存修改的恢复日志（v0.10.40，"异常退出恢复机制"）。

问题：字段编辑先在内存里等 500 ms 防抖，再排进后台写线程落盘。这段窗口里程序崩溃 / 被杀 /
断电，用户刚敲的内容就没了；后台保存失败时控件还会被回滚成旧值。

做法：每次编辑（schedule_save）立刻把「编号 + 类别 + 字段 → 新值」写进本机配置目录的一个
小 JSON（``<app_config_dir>/recovery/<工作区>.json``，原子替换）；该字段真正落盘成功后再删掉。
所以日志里剩下的条目 == 还没确认写进工作区的修改。下次打开同一工作区时提示用户恢复。

本模块只用 stdlib，不 import PyQt5；所有 IO 失败静默（日志是兜底，不能反过来拖垮保存）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_KEY_SEP = "|"


def _workspace_key(workspace_root: Path | str) -> str:
    try:
        resolved = str(Path(workspace_root).resolve())
    except OSError:
        resolved = str(workspace_root)
    return hashlib.sha1(resolved.casefold().encode("utf-8")).hexdigest()[:16]


class EditJournal:
    def __init__(self, journal_dir: Path | str, workspace_root: Path | str) -> None:
        self.workspace_root = str(workspace_root)
        self.path = Path(journal_dir) / f"{_workspace_key(workspace_root)}.json"
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._load()

    # -- 读写 ---------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        clean: dict[str, dict] = {}
        for key, entry in entries.items():
            if isinstance(entry, dict) and isinstance(entry.get("fields"), dict) and entry["fields"]:
                clean[str(key)] = {"fields": {str(k): str(v) for k, v in entry["fields"].items()},
                                   "updated_at": str(entry.get("updated_at", ""))}
        return clean

    def _persist(self) -> None:
        try:
            if not self._entries:
                self.path.unlink(missing_ok=True)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"workspace_root": self.workspace_root, "entries": self._entries}
            tmp = self.path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # -- API ----------------------------------------------------------------
    def record(self, voucher: str, category: str, updates: dict[str, str]) -> None:
        """登记尚未落盘的字段新值（同字段覆盖为最新值）。"""
        if not voucher or not updates:
            return
        key = f"{voucher}{_KEY_SEP}{category}"
        with self._lock:
            entry = self._entries.setdefault(key, {"fields": {}, "updated_at": ""})
            entry["fields"].update({str(k): "" if v is None else str(v) for k, v in updates.items()})
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._persist()

    def confirm_saved(self, voucher: str, category: str, saved: dict[str, str]) -> None:
        """字段已写进工作区：删掉值与已写值一致的条目（写盘期间用户又改了的保留）。"""
        key = f"{voucher}{_KEY_SEP}{category}"
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            fields = entry["fields"]
            for name, value in saved.items():
                if name in fields and fields[name] == ("" if value is None else str(value)):
                    fields.pop(name)
            if not fields:
                self._entries.pop(key, None)
            self._persist()

    def pending(self) -> list[tuple[str, str, dict[str, str], str]]:
        """[(编号, 类别, {字段: 值}, 最后修改时间)]，按时间排序。"""
        with self._lock:
            items = []
            for key, entry in self._entries.items():
                voucher, _, category = key.rpartition(_KEY_SEP)
                items.append((voucher, category, dict(entry["fields"]), entry["updated_at"]))
        return sorted(items, key=lambda it: it[3])

    def discard(self, voucher: str, category: str) -> None:
        with self._lock:
            self._entries.pop(f"{voucher}{_KEY_SEP}{category}", None)
            self._persist()

    def discard_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._persist()
