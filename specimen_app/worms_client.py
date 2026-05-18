"""WoRMS REST API client — stdlib only, no new dependencies.

Queries https://www.marinespecies.org/rest/AphiaRecordsByName/{name}
and returns the first accepted AphiaRecord, or None when no accepted
match exists.  Raises WormsError on network or parse failure.

Local SQLite cache (global, app-wide):
  - lookup_cache(name)            — read from cache
  - write_to_cache(rec)           — write to cache
  - query_worms_with_cache(name)  — cache-first, then network
  - import_dwca(zip_path, cb)     — bulk import from WoRMS DwC-A zip (legacy)
  - crawl_full_rest(...)          — bulk import full taxonomy via REST recursive crawl
  - cache_stats()                 — {count, last_import}
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import __version__


_WORMS_REST = "https://www.marinespecies.org/rest"
_ALLOWED_HOST = "www.marinespecies.org"
_USER_AGENT = f"specimen-inventory/{__version__}"
_TIMEOUT = 10

# Public URL for full DwC-A download.
#
# 历史用途：`tools/build_worms_cache.py --download` 及 UI「从 WoRMS 官网下载全量
# DwC-A」按钮曾用此 URL 一次性拉取全库 zip。2026 年 5 月起 WoRMS 将该路径限制为
# GBIF IP 白名单（一般客户端返回 HTTP 403）。真实 URL 为
# `http://www.marinespecies.org/export/gbif/WoRMS_DwC-A.zip`（GBIF 元数据
# doi:10.14284/170 确认），仍 403。
#
# 保留常量供 `导入本地 DwC-A zip` 路径使用：用户在其他渠道（usersrequest.php
# 人工申请 / GBIF 客户端 / 历史归档）拿到合法 zip 后，仍走 `import_dwca()` 离线
# 导入。新增 `crawl_full_rest()` 作为公开网络全量抓取的替代方案。
WORMS_DWCA_URL = "https://www.marinespecies.org/export/exports/WoRMS_DwCA.zip"

# Kingdom 根 AphiaID（WoRMS 顶层分类）—— REST 递归全树抓取的起点。
# 实测 `AphiaChildrenByAphiaID/<id>` 即可向下遍历。Animalia=2 已实测返回 48 phyla。
# 数值来源：WoRMS taxon tree 浏览器手工核对（2026-05）。如未来 WoRMS 调整根 ID，
# `crawl_full_rest()` 会在启动前用 `AphiaRecordByName/{name}` 动态校验并替换。
_DEFAULT_KINGDOM_NAMES: tuple[str, ...] = (
    "Animalia", "Plantae", "Fungi", "Protozoa",
    "Chromista", "Bacteria", "Archaea", "Viruses",
)
_DEFAULT_KINGDOM_APHIA_IDS: tuple[int, ...] = (
    2, 4, 3, 383, 146, 147, 148, 149,
)
# WoRMS AphiaChildrenByAphiaID 分页大小（API 固定 50）。
_CHILDREN_PAGE_SIZE = 50


class WormsError(Exception):
    """Network, host-safety, or parse failure from WoRMS."""


@dataclass(frozen=True)
class WoRMSRecord:
    aphia_id: int
    status: str          # "accepted" / "unaccepted" / …
    valid_name: str      # accepted scientific name (Latin)
    valid_aphia_id: int
    phylum: str
    class_: str
    order: str
    family: str
    genus: str
    authority: str       # e.g. "Linnaeus, 1758"
    rank: str            # e.g. "Species"


def query_worms(name: str) -> WoRMSRecord | None:
    """Query WoRMS for *name*; return first accepted record or None.

    Parameters
    ----------
    name:
        Scientific name (Latin).  Empty/whitespace-only returns None
        immediately without making a network request.

    Returns
    -------
    WoRMSRecord if an accepted match was found, None otherwise.

    Raises
    ------
    WormsError
        On network failure, unexpected host, or JSON parse error.
    """
    name = name.strip()
    if not name:
        return None

    encoded = urllib.parse.quote(name, safe="")
    url = (
        f"{_WORMS_REST}/AphiaRecordsByName/{encoded}"
        "?like=false&fuzzy=false&marine_only=false"
    )

    # Safety: only allow our known host — same pattern as updater._ALLOWED_HOSTS.
    # 规范化软件设计 2026-05 P1 审查修复:加 scheme = https 强制校验,防 http/file/data scheme 绕过。
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise WormsError(f"Only https allowed, got: {parsed.scheme!r}")
    if parsed.hostname != _ALLOWED_HOST:
        raise WormsError(f"Unexpected host: {parsed.hostname!r}")

    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
        # 旧：把空 body 当 JSON 错误抛。新：WoRMS 用 HTTP 204 / 空 body 表示
        # 「无匹配」，等同于返回 None；只在真有内容时再走 json.loads。
        if status == 204 or not raw or not raw.strip():
            return None
    except urllib.error.URLError as exc:
        # 旧：raise WormsError(str(exc))，错误信息笼统，离线用户无法判断下一步。
        # 新：按异常 reason 分类，给出可操作的中文提示，引导用户走离线路径。
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.gaierror):
            raise WormsError(
                "DNS 解析失败：无法连接 marinespecies.org。\n"
                "建议：检查网络连接，或从其他联网电脑下载 DwC-A 后拷贝过来导入。"
            ) from exc
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise WormsError("请求超时：网络可能不稳定，可稍后重试。") from exc
        if isinstance(reason, ssl.SSLError):
            raise WormsError(
                f"SSL 错误：{reason}\n建议：检查系统时间是否正确，或代理配置。"
            ) from exc
        raise WormsError(f"网络不可达：{reason}") from exc

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WormsError(f"JSON parse error: {exc}") from exc

    if not isinstance(data, list) or not data:
        return None

    # Pick first accepted record; ignore if none are accepted.
    accepted = next((r for r in data if r.get("status") == "accepted"), None)
    if accepted is None:
        return None

    try:
        return WoRMSRecord(
            aphia_id=int(accepted.get("AphiaID") or 0),
            status=str(accepted.get("status", "")),
            valid_name=str(accepted.get("valid_name") or accepted.get("scientificname", "")),
            valid_aphia_id=int(accepted.get("valid_AphiaID") or 0),
            phylum=str(accepted.get("phylum") or ""),
            class_=str(accepted.get("class") or ""),
            order=str(accepted.get("order") or ""),
            family=str(accepted.get("family") or ""),
            genus=str(accepted.get("genus") or ""),
            authority=str(accepted.get("authority") or ""),
            rank=str(accepted.get("rank") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise WormsError(f"Record parse error: {exc}") from exc


# ---------------------------------------------------------------------------
# SQLite cache — global (shared across all workspaces)
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS worms_taxa (
    aphia_id       INTEGER PRIMARY KEY,
    status         TEXT NOT NULL,
    valid_name     TEXT NOT NULL,
    valid_aphia_id INTEGER,
    phylum         TEXT,
    class_name     TEXT,
    ord            TEXT,
    family         TEXT,
    genus          TEXT,
    authority      TEXT,
    rank           TEXT,
    cached_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_valid_name
    ON worms_taxa(valid_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_genus
    ON worms_taxa(genus COLLATE NOCASE);
"""


def _cache_db_path() -> Path:
    from .app_settings import app_config_dir
    return app_config_dir() / "worms_cache.sqlite"


def _open_cache() -> sqlite3.Connection:
    """Open (or create) the global WoRMS SQLite cache."""
    db_path = _cache_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(_CREATE_TABLE)
    conn.commit()
    return conn


def _row_to_record(row: tuple) -> WoRMSRecord:
    """Convert a worms_taxa DB row (ordered by SELECT *) to WoRMSRecord."""
    # columns: aphia_id, status, valid_name, valid_aphia_id, phylum, class_name,
    #          ord, family, genus, authority, rank, cached_at
    return WoRMSRecord(
        aphia_id=int(row[0] or 0),
        status=str(row[1] or ""),
        valid_name=str(row[2] or ""),
        valid_aphia_id=int(row[3] or 0),
        phylum=str(row[4] or ""),
        class_=str(row[5] or ""),
        order=str(row[6] or ""),
        family=str(row[7] or ""),
        genus=str(row[8] or ""),
        authority=str(row[9] or ""),
        rank=str(row[10] or ""),
    )


def lookup_cache(name: str) -> WoRMSRecord | None:
    """Look up *name* (case-insensitive) in the local SQLite cache.

    Matches against valid_name.  Returns WoRMSRecord or None.
    """
    name = name.strip()
    if not name:
        return None
    # 规范化软件设计 2026-05 P1 审查修复:SQLite conn 在 try/finally 内关闭,
    # 防止 execute/fetchone 抛异常时 conn 泄漏。
    conn = None
    try:
        conn = _open_cache()
        cur = conn.execute(
            "SELECT * FROM worms_taxa WHERE valid_name = ? COLLATE NOCASE LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return _row_to_record(row) if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def write_to_cache(rec: WoRMSRecord) -> None:
    """Insert or replace *rec* in the local SQLite cache."""
    # 规范化软件设计 2026-05 P1 审查修复:SQLite conn try/finally,防执行/提交异常泄漏。
    conn = None
    try:
        conn = _open_cache()
        conn.execute(
            """INSERT OR REPLACE INTO worms_taxa
               (aphia_id, status, valid_name, valid_aphia_id,
                phylum, class_name, ord, family, genus, authority, rank)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.aphia_id, rec.status, rec.valid_name, rec.valid_aphia_id,
                rec.phylum, rec.class_, rec.order, rec.family, rec.genus,
                rec.authority, rec.rank,
            ),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def query_worms_with_cache(name: str) -> WoRMSRecord | None:
    """Cache-first WoRMS lookup.

    1. Check local SQLite; return if found.
    2. Otherwise query network via query_worms().
    3. On network success (even None), cache the result when a record is returned.

    Raises WormsError on network/parse failure (same as query_worms).
    """
    name = name.strip()
    if not name:
        return None

    cached = lookup_cache(name)
    if cached is not None:
        return cached

    rec = query_worms(name)
    if rec is not None:
        write_to_cache(rec)
    return rec


def import_dwca(
    zip_path: str | Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> int:
    """Bulk-import accepted taxa from a WoRMS DwC-A zip into the local cache.

    DwC-A zip must contain a ``Taxon.tsv`` (tab-separated, UTF-8).
    Only rows with ``taxonomicStatus == "accepted"`` are imported.

    Parameters
    ----------
    zip_path:
        Path to the downloaded WoRMS DwC-A zip file.
    progress_cb:
        Optional callback called as ``(rows_processed, total_rows)`` during
        import.  Called from the same thread — put heavy-lifting in a QThread.

    Returns
    -------
    int
        Number of accepted records imported.
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise WormsError(f"DwC-A zip not found: {zip_path}")

    # Find Taxon.tsv inside the zip (case-insensitive match).
    with zipfile.ZipFile(zip_path, "r") as zf:
        names_in_zip = zf.namelist()
        taxon_file = next(
            (n for n in names_in_zip if n.lower().endswith("taxon.tsv")),
            None,
        )
        if taxon_file is None:
            raise WormsError(
                "DwC-A zip does not contain Taxon.tsv. "
                f"Found: {names_in_zip[:10]}"
            )
        raw_bytes = zf.read(taxon_file)

    lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return 0

    header = [c.strip() for c in lines[0].split("\t")]

    def _col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    col_taxon_id        = _col("taxonID")
    col_status          = _col("taxonomicStatus")
    col_valid_name      = _col("scientificName")
    col_accepted_id     = _col("acceptedNameUsageID")
    col_rank            = _col("taxonRank")
    col_authority       = _col("scientificNameAuthorship")
    col_phylum          = _col("phylum")
    col_class           = _col("class")
    col_order           = _col("order")
    col_family          = _col("family")
    col_genus           = _col("genus")

    def _get(fields: list[str], idx: int) -> str:
        if idx < 0 or idx >= len(fields):
            return ""
        return fields[idx].strip()

    data_lines = lines[1:]
    total = len(data_lines)
    conn = _open_cache()
    imported = 0
    batch: list[tuple] = []

    for i, line in enumerate(data_lines):
        if progress_cb and i % 1000 == 0:
            progress_cb(i, total)
        if not line.strip():
            continue
        fields = line.split("\t")
        status = _get(fields, col_status)
        if status.lower() != "accepted":
            continue

        try:
            aphia_id = int(_get(fields, col_taxon_id) or 0)
        except ValueError:
            aphia_id = 0
        try:
            valid_aphia_id = int(_get(fields, col_accepted_id) or aphia_id)
        except ValueError:
            valid_aphia_id = aphia_id

        batch.append((
            aphia_id,
            status,
            _get(fields, col_valid_name),
            valid_aphia_id,
            _get(fields, col_phylum),
            _get(fields, col_class),
            _get(fields, col_order),
            _get(fields, col_family),
            _get(fields, col_genus),
            _get(fields, col_authority),
            _get(fields, col_rank),
        ))
        imported += 1

        if len(batch) >= 2000:
            conn.executemany(
                """INSERT OR REPLACE INTO worms_taxa
                   (aphia_id, status, valid_name, valid_aphia_id,
                    phylum, class_name, ord, family, genus, authority, rank)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany(
            """INSERT OR REPLACE INTO worms_taxa
               (aphia_id, status, valid_name, valid_aphia_id,
                phylum, class_name, ord, family, genus, authority, rank)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            batch,
        )
        conn.commit()

    if progress_cb:
        progress_cb(total, total)
    conn.close()
    return imported


def cache_stats() -> dict:
    """Return ``{"count": int, "last_import": str | None}`` from the local cache."""
    # P1 审查修复:try/finally conn close。
    conn = None
    try:
        conn = _open_cache()
        row = conn.execute("SELECT COUNT(*) FROM worms_taxa").fetchone()
        count = row[0] if row else 0
        row2 = conn.execute(
            "SELECT MAX(cached_at) FROM worms_taxa"
        ).fetchone()
        last_import = row2[0] if row2 else None
        return {"count": count, "last_import": last_import}
    except Exception:
        return {"count": 0, "last_import": None}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def clear_cache() -> None:
    """Delete all rows from the local WoRMS cache table."""
    # P1 审查修复:try/finally conn close。
    conn = None
    try:
        conn = _open_cache()
        conn.execute("DELETE FROM worms_taxa")
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def export_cache_gz(output_path: str | Path) -> int:
    """Compress the local SQLite cache to *output_path* (gzip, level 9). Returns row count.

    Raises WormsError if the local cache does not exist.
    Used by ``tools/build_worms_cache.py`` to produce the GitHub Release asset.
    """
    import gzip
    import shutil
    db_path = _cache_db_path()
    if not db_path.is_file():
        raise WormsError(f"本地缓存不存在：{db_path}")
    output_path = Path(output_path)
    with open(db_path, "rb") as f_in, gzip.open(output_path, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return cache_stats().get("count", 0)


_BOOTSTRAP_ASSET_NAME = "worms_cache_bootstrap.sqlite.gz"


def _resolve_bootstrap_path() -> Path | None:
    """跨 source / PyInstaller _MEIPASS / cwd 查内置 bootstrap 缓存。

    与 field_help.bundled_template_path() 同模式：支持开发态运行和打包态运行。
    返回首个存在的候选路径，或 None。
    """
    candidates: list[Path] = []
    # 1) PyInstaller 打包后的 _MEIPASS 路径（--add-data 落点）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "specimen_app" / "assets" / _BOOTSTRAP_ASSET_NAME)
    # 2) 源代码态：与 worms_client.py 同目录 assets/
    here = Path(__file__).resolve().parent
    candidates.append(here / "assets" / _BOOTSTRAP_ASSET_NAME)
    for p in candidates:
        if p.is_file():
            return p
    return None


def ensure_bootstrap_cache() -> str | None:
    """首启动注入：用户缓存为空 + 仓库内置 bootstrap 存在 → 自动解压安装。

    用于离线环境开箱即用 WoRMS 分类查询。复用 install_cache_gz() 的
    原子写入 + SQLite 校验。任何失败均静默返回 None，不阻塞 app 启动。

    返回值：成功时返回 bootstrap 来源路径（用于 UI 显示）；跳过或失败返回 None。
    """
    # 旧：用 user_cache.stat().st_size > 0 判断「有数据」——错误，因为空 SQLite
    # （只有 schema 没有数据行）也有 ~16KB；首次 lookup_cache 调用 _open_cache 就会
    # 留下一个空 SQLite 文件，导致 bootstrap 被永久跳过。
    # 新：以「记录条数 > 0」为标准。空文件 / 无表 / 0 行都视为「无数据」可注入。
    try:
        count = cache_stats().get("count", 0)
    except Exception:
        count = 0
    if count > 0:
        # 用户已有真实数据（手动导入或上次 bootstrap），避免覆盖
        return None
    boot = _resolve_bootstrap_path()
    if boot is None:
        # 未打包 bootstrap（如开发态或精简发布版），无操作
        return None
    try:
        install_cache_gz(boot)
        return str(boot)
    except Exception:
        # 静默失败：bootstrap 损坏不能阻止 app 启动
        return None


# ---------------------------------------------------------------------------
# REST recursive full-tree crawler — replaces deprecated DwC-A direct download
# ---------------------------------------------------------------------------
#
# 设计动机：WoRMS DwC-A zip（`/export/gbif/WoRMS_DwC-A.zip`）2026-05 起改为 GBIF IP
# 白名单，客户端 403。公开网络可用且能拿到全量分类（accepted + 同义名）的唯一
# 路径 = REST API。本节实现：
#   - 从 8 个 kingdom 根 AphiaID 出发 BFS 递归
#   - 每节点调 AphiaChildrenByAphiaID/{id}?offset=N，分页 50/call
#   - 子节点 JSON 已含完整 WoRMSRecord 字段 → 直接 batch INSERT，无需二次查询
#   - 速率限制 token-bucket（默认 3 req/s），429/503 指数退避重试
#   - 断点续传：visited set + queue + imported 计数 → JSON 持久化
#
# 估算：50 万节点 / 50 per page ≈ 10k 请求 / 3 qps ≈ 55 min（单线程，无并发）。
# 用户已确认 30-60 min 可接受。


# 长会话瞬时错误白名单 —— 命中即退避重试。
# 旧版本仅含 gaierror/timeout/ConnectionError，用户实测漏掉 ssl.SSLError（包括
# `SSL_ERROR_EOF` / `UNEXPECTED_EOF_WHILE_READING`：远端 TLS 提前关闭，长会话
# 10k+ 请求必然偶发）。补全 SSL、IncompleteRead、RemoteDisconnected、OSError
# 兜底（多数 IO 错误在长会话中本质是瞬时）。
_TRANSIENT_NET_ERRORS: tuple[type, ...] = (
    socket.gaierror,
    socket.timeout,
    TimeoutError,
    ConnectionError,          # 含 ConnectionResetError / ConnectionAbortedError 等
    ssl.SSLError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    OSError,                  # 最后兜底；urllib.error.URLError 也是 OSError 子类
)


def _http_get_json(url: str, timeout: int = 30, max_retries: int = 7) -> list | dict | None:
    """GET *url*，返回 JSON。专给 crawl 用：含 429/503 + 瞬时 SSL/Reset 指数退避 + 详细错误。

    返回 None 表示 HTTP 204（无内容）—— 调用方按"该节点无子节点"处理。
    所有其他失败都 raise WormsError，错误信息含完整诊断（URL + 状态码 + 头 + 体首段），
    UI 端经 `_CopyableErrorDialog` 一键复制反馈。

    重试策略：
      - 命中 `_TRANSIENT_NET_ERRORS` / HTTP 429 / 503 → 指数退避 + 10-30% jitter，
        上限 60s，最多 *max_retries* 次（默认 7）。
      - 4xx (除 429) / 不在白名单内的异常 → 立即抛 WormsError，不重试。
    """
    parsed = urllib.parse.urlparse(url)
    # P1 审查修复:同 query_worms,scheme + hostname 双校验
    if parsed.scheme != "https":
        raise WormsError(f"Only https allowed, got: {parsed.scheme!r}")
    if parsed.hostname != _ALLOWED_HOST:
        raise WormsError(f"Unexpected host: {parsed.hostname!r}")
    backoff = 1.0
    last_exc: Exception | None = None

    def _sleep_with_jitter(base: float) -> None:
        """退避基值 + 10-30% jitter，避免与服务器侧节流形成共振。"""
        time.sleep(base * (1.0 + random.uniform(0.0, 0.3)))

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                # 规范化软件设计 2026-05 P1 审查修复:resp.read() 无上限可被恶意/异常 Content-Length 撑爆内存。
                # WoRMS REST 单页 JSON 通常 <500KB,设 50MB 硬上限作为兜底(远超合理值,但防 OOM)。
                _MAX_RESP_BYTES = 50 * 1024 * 1024
                raw = resp.read(_MAX_RESP_BYTES + 1)
                if len(raw) > _MAX_RESP_BYTES:
                    raise WormsError(
                        f"WoRMS REST 响应过大(>{_MAX_RESP_BYTES // 1024 // 1024}MB),已截断防 OOM\nURL: {url}"
                    )
                if status == 204 or not raw or not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise WormsError(
                        f"WoRMS REST 返回非 JSON\n"
                        f"URL: {url}\nHTTP {status}\n"
                        f"响应体首 500 字符: {raw[:500]!r}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            # 429 (Too Many Requests) / 503 (Service Unavailable) → 退避重试
            if exc.code in (429, 503) and attempt < max_retries - 1:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                _sleep_with_jitter(wait)
                backoff = min(backoff * 2, 60.0)
                last_exc = exc
                continue
            try:
                body = exc.read()[:500]
            except Exception:
                body = b""
            raise WormsError(
                f"WoRMS REST 请求失败\n"
                f"URL: {url}\nHTTP {exc.code} {exc.reason}\n"
                f"响应头: {dict(exc.headers) if exc.headers else {}}\n"
                f"响应体首 500 字符: {body!r}\n"
                f"建议: 若是 403/404 → 端点可能已变更；若是 429 → 降低 rate_limit_qps；"
                f"其他 5xx → 稍后重试。"
            ) from exc
        except urllib.error.URLError as exc:
            # urlopen 早期错误（连接建立前）被包成 URLError，reason 是底层异常。
            reason = getattr(exc, "reason", exc)
            if attempt < max_retries - 1 and isinstance(reason, _TRANSIENT_NET_ERRORS):
                _sleep_with_jitter(backoff)
                backoff = min(backoff * 2, 60.0)
                last_exc = exc
                continue
            # 重试已用尽 → 按 reason 子类型分流给出可操作中文提示
            if isinstance(reason, socket.gaierror):
                raise WormsError(
                    f"DNS 解析失败：无法连接 marinespecies.org\nURL: {url}\n建议：检查网络。"
                ) from exc
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise WormsError(
                    f"请求超时（{timeout}s）\nURL: {url}\n建议：网络可能不稳定，可重试或降速。"
                ) from exc
            if isinstance(reason, ssl.SSLError):
                raise WormsError(
                    f"SSL 错误：{reason}\nURL: {url}\n"
                    f"已自动重试 {max_retries} 次仍失败。\n"
                    f"建议：稍后再点「全量抓取」自动续传；如长期失败检查代理 / 系统时间。"
                ) from exc
            raise WormsError(f"网络不可达：{reason}\nURL: {url}") from exc
        except _TRANSIENT_NET_ERRORS as exc:
            # `resp.read()` 阶段抛出的瞬时错误（SSL EOF / IncompleteRead /
            # 连接重置）不会被 URLError 包装，单独 catch 后退避重试。
            if attempt < max_retries - 1:
                _sleep_with_jitter(backoff)
                backoff = min(backoff * 2, 60.0)
                last_exc = exc
                continue
            # 重试用尽，分流报错
            if isinstance(exc, ssl.SSLError):
                raise WormsError(
                    f"SSL 错误：{exc}\nURL: {url}\n"
                    f"已自动重试 {max_retries} 次仍失败。\n"
                    f"建议：稍后再点「全量抓取」自动续传；如长期失败检查代理 / 系统时间。"
                ) from exc
            if isinstance(exc, http.client.IncompleteRead):
                raise WormsError(
                    f"响应不完整：{exc}\nURL: {url}\n"
                    f"已自动重试 {max_retries} 次仍失败。建议稍后续传。"
                ) from exc
            raise WormsError(
                f"瞬时网络错误（{type(exc).__name__}）：{exc}\nURL: {url}\n"
                f"已自动重试 {max_retries} 次仍失败。建议稍后续传。"
            ) from exc
    # 重试用尽（理论上不应到此 —— 上面所有分支已 raise / continue）
    raise WormsError(f"WoRMS REST 重试 {max_retries} 次后仍失败\nURL: {url}\n末次错误: {last_exc!r}")


def _json_to_record_tuple(rec: dict) -> tuple | None:
    """Convert one AphiaChildrenByAphiaID JSON record to a worms_taxa row tuple.

    Returns None if record is malformed (missing AphiaID).
    """
    try:
        aphia_id = int(rec.get("AphiaID") or 0)
    except (TypeError, ValueError):
        return None
    if aphia_id <= 0:
        return None
    try:
        valid_aphia_id = int(rec.get("valid_AphiaID") or aphia_id)
    except (TypeError, ValueError):
        valid_aphia_id = aphia_id
    return (
        aphia_id,
        str(rec.get("status") or ""),
        str(rec.get("valid_name") or rec.get("scientificname") or ""),
        valid_aphia_id,
        str(rec.get("phylum") or ""),
        str(rec.get("class") or ""),
        str(rec.get("order") or ""),
        str(rec.get("family") or ""),
        str(rec.get("genus") or ""),
        str(rec.get("authority") or ""),
        str(rec.get("rank") or ""),
    )


def _save_resume_state(path: Path, state: dict) -> None:
    """Atomically persist resume state to *path*。失败静默 —— 抓取继续，仅丢续传能力。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        tmp.replace(path)
    except Exception:
        pass


def _load_resume_state(path: Path) -> dict | None:
    """Read resume state from *path*；不存在或损坏返回 None（重头开始）。"""
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        # 兼容性：必备键齐全才视为有效
        if "visited" not in data or "queue" not in data:
            return None
        return data
    except Exception:
        return None


def crawl_full_rest(
    progress_cb: Callable[[int, str], None] | None = None,
    rate_limit_qps: float = 2.0,
    resume_state_path: Path | None = None,
    should_stop: Callable[[], bool] | None = None,
    kingdoms: Sequence[int] | None = None,
    timeout: int = 30,
) -> int:
    """递归遍历 WoRMS 全分类树并写入本地 SQLite 缓存。

    BFS 从 *kingdoms*（默认 8 个 WoRMS kingdom）出发，逐节点调
    ``AphiaChildrenByAphiaID/{id}?marine_only=false&offset=N`` 取子节点（分页 50/call）。
    子 JSON 已含完整分类字段，直接 INSERT OR REPLACE。

    韧性策略（2026-05-16-b 修复）：
      - 单节点拉取重试 7 次仍失败 → 加入 ``state["failed"]`` 跳过，**继续抓取下一节点**，
        不再 abort 整个会话。失败列表持久化到 state；完成时进度回调附带跳过数。
      - ``visited.add(current_id)`` 推迟到该节点分页全部成功后才执行；中途异常 →
        节点保留在 queue 头，resume 时重抓（避免上版本 visited 提前标记导致丢子节点）。
      - 默认 ``rate_limit_qps=2.0``（旧 3.0），长会话更稳；用户可调高。

    Parameters
    ----------
    progress_cb:
        ``(imported_count, current_taxon_name_or_status)`` — 每写 500 条 / 跳过节点 /
        完成时调一次，**禁止在内部阻塞**。UI 端连 PyQt signal 即可。
    rate_limit_qps:
        请求节流（默认 2 req/s，长会话保守值，可调）。
    resume_state_path:
        断点续传 JSON 文件路径。存在则恢复 visited+queue+failed+imported。
    should_stop:
        UI 取消按钮回调。每次 HTTP 调用前检查；返回 True 抛 ``InterruptedError``。
    kingdoms:
        起始 AphiaID 列表。默认 8 个 kingdom；测试可传子集。
    timeout:
        单次 HTTP 超时（秒）。

    Returns
    -------
    int
        本次会话写入的记录数（断点续传时不含上次已写）。

    Raises
    ------
    WormsError
        全局阻塞性错误（非单节点失败，如 host 不通过白名单 / 缓存写不进）。
    InterruptedError
        用户取消。已写入数据保留，state 已保存可续传。
    """
    if kingdoms is None:
        kingdoms = _DEFAULT_KINGDOM_APHIA_IDS
    interval = 1.0 / max(rate_limit_qps, 0.1)
    state = _load_resume_state(resume_state_path) if resume_state_path else None
    if state is None:
        visited: set[int] = set()
        failed: set[int] = set()
        queue: list[int] = list(kingdoms)
        imported_total = 0
    else:
        visited = set(state.get("visited", []))
        failed = set(state.get("failed", []))
        queue = list(state.get("queue", []))
        imported_total = int(state.get("imported", 0))
        # 兼容：若 state 是旧版本 / kingdoms 调整过，补充未访问且未失败的 kingdom 根
        for k in kingdoms:
            if k not in visited and k not in failed and k not in queue:
                queue.append(k)

    conn = _open_cache()
    batch: list[tuple] = []
    written_this_session = 0
    last_save_at = 0  # imported count when state last persisted
    last_request_at = 0.0

    def _flush() -> None:
        nonlocal batch
        if batch:
            conn.executemany(
                """INSERT OR REPLACE INTO worms_taxa
                   (aphia_id, status, valid_name, valid_aphia_id,
                    phylum, class_name, ord, family, genus, authority, rank)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            conn.commit()
            batch = []

    def _persist_state() -> None:
        if resume_state_path is not None:
            _save_resume_state(resume_state_path, {
                "visited": sorted(visited),
                "failed": sorted(failed),
                "queue": queue,
                "imported": imported_total,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })

    def _check_stop() -> None:
        """抛 InterruptedError 前确保已 flush + persist。"""
        if should_stop and should_stop():
            _flush()
            _persist_state()
            raise InterruptedError("用户取消")

    try:
        while queue:
            _check_stop()

            current_id = queue.pop(0)
            if current_id in visited or current_id in failed:
                continue
            # 旧 bug：此处提前 `visited.add(current_id)` → 节点中途抛错时 resume 跳过
            # 该节点，丢失剩余子节点。改为仅在分页 loop 完整成功后才加 visited。

            # 分页拉取 current_id 的全部子节点。整个节点的 fetch 用 try 包裹，
            # 任何 WormsError 都计入 failed 跳过，不抛出整个 crawl。
            node_succeeded = False
            try:
                offset = 1
                while True:
                    _check_stop()

                    # 节流：保证两次请求间隔 >= interval
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < interval:
                        time.sleep(interval - elapsed)

                    url = (
                        f"{_WORMS_REST}/AphiaChildrenByAphiaID/{current_id}"
                        f"?marine_only=false&offset={offset}"
                    )
                    last_request_at = time.monotonic()
                    children = _http_get_json(url, timeout=timeout)

                    if not children:
                        # 204 / 空数组 = 当前节点无更多子节点
                        break
                    if not isinstance(children, list):
                        break

                    for rec in children:
                        if not isinstance(rec, dict):
                            continue
                        row = _json_to_record_tuple(rec)
                        if row is None:
                            continue
                        aphia_id = row[0]
                        rank = row[10]
                        # 写入所有节点（accepted + unaccepted + synonym 等都保留）
                        batch.append(row)
                        imported_total += 1
                        written_this_session += 1
                        # 非叶子节点继续入队（叶子 = Species / Subspecies / Variety / Form）
                        # 例：Genus 仍要递归找其下的 Species
                        if (aphia_id not in visited and aphia_id not in failed
                                and rank.lower() not in {
                                    "species", "subspecies", "variety",
                                    "form", "forma",
                                }):
                            queue.append(aphia_id)

                        if len(batch) >= 500:
                            _flush()
                            if progress_cb:
                                progress_cb(imported_total, row[2])  # row[2] = valid_name

                        # 每 5000 条持久化一次断点
                        if imported_total - last_save_at >= 5000:
                            _persist_state()
                            last_save_at = imported_total

                    # 不足整页 = 此父节点已抽完
                    if len(children) < _CHILDREN_PAGE_SIZE:
                        break
                    offset += _CHILDREN_PAGE_SIZE
                node_succeeded = True
            except InterruptedError:
                # 取消向上传播：state 已含 current_id in queue（之前 pop 出，需放回）
                # 但 _check_stop 已 persist 了 state（不含 current_id 在 queue）。
                # 这里在 raise 前把 current_id 放回 queue 头并重新持久化，确保
                # 下次续传时重抓该节点。
                queue.insert(0, current_id)
                _persist_state()
                raise
            except (WormsError, sqlite3.Error) as exc:
                # 单节点重试用尽 / SQLite 写入异常 → 记入 failed,写入已积攒的,进度回调通知跳过,继续。
                # 规范化软件设计 2026-05 P1 审查修复:旧只 catch WormsError,sqlite3.Error 未捕导致
                # 上层 crash;现一并捕获,保证 daemon 不因写入瞬时锁/磁盘满崩溃。
                try:
                    _flush()
                except sqlite3.Error:
                    pass  # 二次 flush 仍失败也不阻断
                failed.add(current_id)
                try:
                    _persist_state()
                except sqlite3.Error:
                    pass
                if progress_cb:
                    short_err = str(exc).splitlines()[0][:80]
                    progress_cb(
                        imported_total,
                        f"⚠ AphiaID {current_id} 跳过:{short_err}",
                    )
                # 继续下一节点
                continue

            if node_succeeded:
                visited.add(current_id)

        _flush()
        if progress_cb:
            if failed:
                progress_cb(
                    imported_total,
                    f"完成。{len(failed)} 个节点重试用尽被跳过，"
                    f"首批: {sorted(failed)[:5]}",
                )
            elif written_this_session > 0:
                progress_cb(imported_total, "")
        # 完成 → 清除断点 state；保留 failed 列表为独立日志便于诊断
        if resume_state_path is not None and resume_state_path.is_file():
            try:
                if failed:
                    failed_log = resume_state_path.with_name(
                        resume_state_path.stem + "_failed.json"
                    )
                    try:
                        with open(failed_log, "w", encoding="utf-8") as f:
                            json.dump(
                                {"failed_aphia_ids": sorted(failed),
                                 "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                f, ensure_ascii=False, indent=2,
                            )
                    except Exception:
                        pass
                resume_state_path.unlink()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return written_this_session


def install_cache_gz(gz_path: str | Path) -> int:
    """Decompress a gzip-compressed SQLite file and install as the local WoRMS cache.

    Writes to a temp file first, verifies it opens as a valid SQLite, then
    atomically replaces the live cache.  Raises WormsError on any failure.
    Returns the number of records installed.
    """
    import gzip
    import shutil
    gz_path = Path(gz_path)
    if not gz_path.is_file():
        raise WormsError(f"文件不存在：{gz_path}")
    db_path = _cache_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_suffix(".sqlite.tmp")
    try:
        with gzip.open(gz_path, "rb") as f_in, open(tmp_db, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        # Quick sanity check — will raise if file is not a valid SQLite.
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("SELECT COUNT(*) FROM worms_taxa")
        conn.close()
        if db_path.exists():
            db_path.unlink()
        tmp_db.rename(db_path)
    except WormsError:
        raise
    except Exception as exc:
        if tmp_db.exists():
            tmp_db.unlink(missing_ok=True)
        raise WormsError(f"安装缓存失败：{exc}") from exc
    return cache_stats().get("count", 0)
