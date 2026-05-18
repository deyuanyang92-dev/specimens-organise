#!/usr/bin/env python3
"""Build a compressed WoRMS cache SQLite for distribution as a GitHub Release asset.

Usage
-----
# Option A: REST 全量抓取（推荐，整库 ~50 万条，约 30-60 分钟）
python tools/build_worms_cache.py --rest

# Option B: 仅导出当前本地缓存为 .sqlite.gz（不抓取，不联网）
python tools/build_worms_cache.py --export

# Option C: 从已下载好的 WoRMS DwC-A zip 导入（zip 需用户另行获取）
python tools/build_worms_cache.py path/to/WoRMS_DwCA.zip

Output
------
worms_cache_YYYY-QN.sqlite.gz  (current directory by default; --output 可覆盖)

The file is safe to upload as a GitHub Release asset.  The in-app
"从 GitHub Release 下载" button looks for assets whose name matches
``worms_cache*.sqlite.gz``.

Notes on the deprecated --download flag
---------------------------------------
旧版本支持 `--download` 自动从 marinespecies.org 拉取 DwC-A zip 全库。该端点
2026-05 起被 WoRMS 限制为 GBIF IP 白名单，客户端固定 HTTP 403 Forbidden。
`--download` 现作为 `--rest` 的弃用别名保留，并打印警告。

License note
------------
WoRMS data is released under CC BY 4.0.  If you redistribute this file,
include attribution: WoRMS Editorial Board (www.marinespecies.org).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import date

# Ensure the repo root is on sys.path so specimen_app is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from specimen_app.worms_client import (  # noqa: E402
    crawl_full_rest,
    import_dwca,
    export_cache_gz,
    cache_stats,
)


def _quarter(d: date) -> str:
    return f"Q{(d.month - 1) // 3 + 1}"


def _output_name() -> str:
    today = date.today()
    return f"worms_cache_{today.year}-{_quarter(today)}.sqlite.gz"


def _do_rest_crawl() -> int:
    """REST 递归全量抓取。返回写入条数。"""
    print("Crawling WoRMS via REST API (AphiaChildrenByAphiaID, recursive)…")
    print("  Rate limit: ~3 req/s, expect 30-60 minutes for the full tree.")
    print("  Resume state: ~/.specimen_inventory/worms_crawl_state.json")
    last_taxon = [""]
    last_print_at = [0]

    def _progress(n: int, current: str) -> None:
        last_taxon[0] = current or last_taxon[0]
        # 节流 stdout：每 500 条打印一次
        if n - last_print_at[0] >= 500 or n < 100:
            last_print_at[0] = n
            print(f"\r  Imported {n:,} records (current: {last_taxon[0][:40]})", end="", flush=True)

    from specimen_app.app_settings import app_config_dir
    state_path = app_config_dir() / "worms_crawl_state.json"
    count = crawl_full_rest(
        progress_cb=_progress,
        resume_state_path=state_path,
    )
    print(f"\n  Wrote {count:,} new records this session.")
    return count


def _do_import(zip_path: Path) -> int:
    print(f"Importing {zip_path} …")
    last_pct = [-1]

    def _progress(n: int, t: int) -> None:
        if t > 0:
            pct = n * 100 // t
            if pct != last_pct[0]:
                last_pct[0] = pct
                print(f"\r  {n:,} / {t:,} rows  ({pct}%)", end="", flush=True)

    count = import_dwca(zip_path, progress_cb=_progress)
    print(f"\n  Imported {count:,} accepted records.")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "zip_path",
        nargs="?",
        help="Path to an already-downloaded WoRMS DwC-A zip (Option C)",
    )
    parser.add_argument(
        "--rest",
        action="store_true",
        help="Crawl full WoRMS via public REST API (Option A, replaces --download)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Only export the current local cache to .sqlite.gz (no crawl, Option B)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="DEPRECATED — alias for --rest (DwC-A zip is now IP-restricted to GBIF)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output filename (default: worms_cache_YYYY-QN.sqlite.gz)",
    )
    args = parser.parse_args()

    # --download 兼容旧脚本调用
    if args.download and not args.rest:
        print(
            "WARNING: --download is deprecated. "
            "WoRMS DwC-A zip endpoint is now IP-restricted to GBIF (HTTP 403). "
            "Falling back to --rest (REST recursive crawl).",
            file=sys.stderr,
        )
        args.rest = True

    # 必须选一个数据来源
    if not args.zip_path and not args.rest and not args.export:
        parser.print_help()
        sys.exit(1)

    output = Path(args.output or _output_name())

    # --- acquire data ---
    if args.rest:
        try:
            _do_rest_crawl()
        except KeyboardInterrupt:
            print("\nInterrupted. Resume state saved; rerun with --rest to continue.", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"\nREST crawl failed: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.zip_path:
        zip_path = Path(args.zip_path)
        if not zip_path.is_file():
            print(f"File not found: {zip_path}", file=sys.stderr)
            sys.exit(1)
        try:
            count = _do_import(zip_path)
            if count == 0:
                print(
                    "WARNING: no accepted records imported — check DwC-A content.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            sys.exit(1)
    # --export mode: skip data acquisition, go straight to export

    # --- export cache to gz ---
    stats = cache_stats()
    if stats.get("count", 0) == 0:
        print(
            "ERROR: local cache is empty — nothing to export. "
            "Run with --rest first to populate the cache.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Compressing cache to {output} …")
    exported = export_cache_gz(output)
    size_mb = output.stat().st_size / 1_048_576
    print(f"Done: {output}  ({size_mb:.1f} MB, {exported:,} records)")
    print()
    print("Next step: upload this file as a GitHub Release asset.")
    print("  The in-app downloader looks for assets named  worms_cache*.sqlite.gz")
    print()
    print("Attribution (CC BY 4.0):")
    print("  WoRMS Editorial Board (eds.) (year). World Register of Marine Species.")
    print("  Available at https://www.marinespecies.org")


if __name__ == "__main__":
    main()
