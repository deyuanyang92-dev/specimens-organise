"""local_cache.py — 派生缓存（图片索引、缩略图）放本机，不放工作区。

2026-10-02 用户工作区在网络/外接盘（M:）："图片检索加载超级慢、像每次都重建索引、建索引时卡顿"。
根因之一：索引库和缩略图缓存都在 <工作区>/数据/ 下，每次读写（含主线程刷新时打开索引库）都走 SMB，
而且 SQLite WAL 在网络文件系统上官方不保证可靠。缓存是派生数据，每台机器各建各的即可——
放到本机 app 配置目录（Windows %APPDATA%/…；Linux ~/.specimen_inventory/）/cache/<kind>/<工作区哈希>/。

Everything 快的原因之一就是索引永远在本机。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import quote

ENV_OVERRIDE = "SPECIMEN_LOCAL_CACHE_DIR"


def _workspace_fingerprint(workspace_root: Path | str) -> str:
    text = os.path.normcase(os.path.abspath(str(workspace_root)))
    return hashlib.sha1(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]


def local_cache_root() -> Path:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return Path(override)
    from .app_settings import app_config_dir

    return Path(app_config_dir()) / "cache"


def local_cache_dir(workspace_root: Path | str, kind: str) -> Path:
    """<本机缓存根>/<kind>/<工作区指纹>/，不存在则创建。"""
    directory = local_cache_root() / kind / _workspace_fingerprint(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_only_sqlite_uri(path: Path | str) -> str:
    """sqlite3.connect(..., uri=True) 用的只读 URI：file:///C:/x/%E6%A0%87.sqlite?mode=ro。

    旧写法 f"file:{path.as_posix()}?mode=ro" 在 Windows 上会得到 file:M:/…，不是合法的 SQLite URI；
    并且中文/空格要 percent-encode。
    """
    absolute = Path(os.path.abspath(str(path)))
    posix = absolute.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix  # Windows: C:/x → /C:/x
    return "file://" + quote(posix, safe="/:") + "?mode=ro"
