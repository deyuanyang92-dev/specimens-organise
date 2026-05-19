"""Persistent state for in-flight and just-applied upgrades.

Two file-backed records live under :func:`app_settings.app_config_dir`:

``pending_update.json``
    D3 state machine. Written when a new bundle has finished downloading
    in *download* / *install* mode and is waiting to be applied at the
    next launch. Cleared once the swap script has been launched.

``post_update_sentinel.json``
    D11 health check. Written by the *new* version as the first thing it
    does after launch; cleared 30 s into a successful startup. If a
    later launch sees the sentinel still present, the previous attempt
    crashed before reaching the 30 s mark — offer to roll back to the
    previous version.

All read paths are fault-tolerant: corrupted JSON or missing files just
return ``None`` (or an empty list) so a damaged state file never blocks
the app from launching.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .app_settings import app_config_dir

_PENDING_FILE = "pending_update.json"
_SENTINEL_FILE = "post_update_sentinel.json"


# --------------------------------------------------------------------------- #
# Pending update (D3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PendingUpdate:
    version: str
    bundle_dir: str
    exe_name: str
    from_version: str
    staged_at: str
    incremental: bool = False
    workspace: str = ""

    def is_stale(self) -> bool:
        """Bundle dir disappeared on disk → user manually deleted it /
        moved it elsewhere. Stale records must be cleared, not applied.
        """
        return not Path(self.bundle_dir).exists()


def _pending_path() -> Path:
    return app_config_dir() / _PENDING_FILE


def read_pending() -> PendingUpdate | None:
    path = _pending_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return PendingUpdate(
            version=str(raw.get("version", "")),
            bundle_dir=str(raw.get("bundle_dir", "")),
            exe_name=str(raw.get("exe_name", "")),
            from_version=str(raw.get("from_version", "")),
            staged_at=str(raw.get("staged_at", "")),
            incremental=bool(raw.get("incremental", False)),
            workspace=str(raw.get("workspace", "")),
        )
    except (OSError, ValueError, TypeError):
        return None


def write_pending(pending: PendingUpdate) -> None:
    path = _pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(pending), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_pending() -> None:
    path = _pending_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Post-update health sentinel (D11)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostUpdateSentinel:
    from_version: str
    current_version: str
    started_at: str
    from_bundle_dir: str = ""  # optional, for reverse-swap rollback target


def _sentinel_path() -> Path:
    return app_config_dir() / _SENTINEL_FILE


def write_post_update_sentinel(sentinel: PostUpdateSentinel) -> None:
    path = _sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(sentinel), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_post_update_sentinel() -> PostUpdateSentinel | None:
    path = _sentinel_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return PostUpdateSentinel(
            from_version=str(raw.get("from_version", "")),
            current_version=str(raw.get("current_version", "")),
            started_at=str(raw.get("started_at", "")),
            from_bundle_dir=str(raw.get("from_bundle_dir", "")),
        )
    except (OSError, ValueError, TypeError):
        return None


def clear_post_update_sentinel() -> None:
    path = _sentinel_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
