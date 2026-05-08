from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "标本入库管理"

ALLOWED_EXE_EXTENSIONS = {".exe"} if sys.platform == "win32" else {".AppImage", ""}


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    directory: Path
    exe_path: Path | None
    notes_path: Path | None


def release_roots(workspace_root: Path | str) -> list[Path]:
    root = Path(workspace_root).resolve()
    candidates = [root / "releases", root.parent / "releases"]
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            result.append(candidate)
    return result


def _is_allowed_executable(path: Path) -> bool:
    if sys.platform == "win32":
        return path.suffix.lower() == ".exe" and APP_NAME in path.stem
    if path.suffix == ".AppImage" and APP_NAME in path.stem:
        return True
    if APP_NAME in path.name and os.access(path, os.X_OK):
        return True
    return False


def _find_executable(directory: Path) -> Path | None:
    candidates = sorted(directory.rglob("*"), key=lambda item: (len(item.parts), str(item)))
    for candidate in candidates:
        if candidate.is_file() and _is_allowed_executable(candidate):
            return candidate
    return None


def list_releases(workspace_root: Path | str) -> list[ReleaseInfo]:
    releases: list[ReleaseInfo] = []
    for releases_root in release_roots(workspace_root):
        for directory in releases_root.iterdir():
            if not directory.is_dir() or not directory.name.startswith("v"):
                continue
            exe_path = _find_executable(directory)
            notes = directory / "release_notes.md"
            releases.append(
                ReleaseInfo(
                    version=directory.name,
                    directory=directory,
                    exe_path=exe_path,
                    notes_path=notes if notes.exists() else None,
                )
            )
    return sorted(releases, key=lambda item: item.version, reverse=True)
