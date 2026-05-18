"""应用内 GitHub 更新检查与下载。

设计要点（与 CLAUDE.md 的数据兼容性约束一致）：
- 纯标准库实现（urllib/ssl/json/zipfile/hashlib），不引入新的第三方依赖。
- 只检查 + 下载解压，不自动覆盖、不自动启动。新版本解压到 ``releases/v{version}/``，
  与旧版本并存，用户在"版本管理"里手动切换，坏版本可随时切回。
- 只允许 HTTPS、只允许官方 GitHub 仓库的 release 资产、强制 sha256 校验。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Callable

from . import __version__
from .release_manager import APP_NAME, release_roots


GITHUB_REPO = "deyuanyang92-dev/specimens-organise"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_USER_AGENT = f"specimen-inventory-updater/{__version__}"
_TIMEOUT = 10
_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
# 增量更新：bundle 目录内的元数据文件名，与 build_release.py 的 APP_META_FILE 保持一致。
APP_META_FILE = ".update_meta.json"


class UpdateError(Exception):
    """检查或下载更新失败时抛出。"""


@dataclass(frozen=True)
class LatestRelease:
    version: str          # 不含前导 v，例如 "0.3.1"
    tag: str              # 原始 tag，例如 "v0.3.1"
    zip_url: str          # 当前平台的完整下载包 URL
    zip_name: str         # 完整下载包文件名
    sha256_url: str | None  # 对应的 sha256 校验文件 URL（可能为 None）
    notes: str            # release 说明正文
    manifest_url: str | None = None  # 增量更新清单 update_manifest_{plat}.json 的 URL（老 release 为 None）


@dataclass(frozen=True)
class UpdatePlan:
    """从 update_manifest_{plat}.json 解析出的增量更新计划。"""
    version: str
    platform: str
    app_zip_url: str
    app_zip_name: str
    app_sha256: str
    runtime_zip_url: str
    runtime_zip_name: str
    runtime_sha256: str
    runtime_hash: str
    app_files: tuple[str, ...]


# ---------------------------------------------------------------------------
# 版本号解析与比较
# ---------------------------------------------------------------------------

def _parse_version(text: str) -> tuple:
    """把版本号解析成可比较的元组。

    支持 ``v0.3.0`` / ``0.3.0`` / ``0.3.0-test.1``。预发布后缀（-test/-rc/-beta…）
    排序低于同主版本的正式版：正式版用 ``(1, 0)`` 占位，预发布用 ``(0, 后缀数字)``。
    """
    text = str(text or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    core, _, pre = text.partition("-")
    nums: list[int] = []
    for part in core.split("."):
        match = re.match(r"\d+", part)
        nums.append(int(match.group()) if match else 0)
    while len(nums) < 3:
        nums.append(0)
    if pre:
        pre_match = re.search(r"\d+", pre)
        pre_key = (0, int(pre_match.group()) if pre_match else 0)
    else:
        pre_key = (1, 0)
    return (tuple(nums), pre_key)


def is_newer(candidate: str, current: str = __version__) -> bool:
    """candidate 版本是否比 current 新。"""
    return _parse_version(candidate) > _parse_version(current)


# ---------------------------------------------------------------------------
# 网络请求（标准库）
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> None:
    # 规范化软件设计 2026-05 P1 审查修复:用 parsed.hostname (剥端口) 而非 netloc,
    # 防 "github.com:80@evil.com" 类 URL 利用 netloc 含 userinfo/port 绕过校验。
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError("下载地址必须是 HTTPS。")
    if parsed.username or parsed.password:
        raise UpdateError("URL 不允许内嵌凭据。")
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
        raise UpdateError(f"拒绝从非 GitHub 域名下载:{host}")


def _http_get(url: str, *, timeout: int = _TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub 返回错误：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接 GitHub：{exc.reason}") from exc
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        raise UpdateError(f"网络请求失败：{exc}") from exc


def _download_to(url: str, dest: Path, progress_cb: Callable[[int], None] | None = None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT, context=ssl.create_default_context()) as response:
            total = int(response.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(100, int(downloaded * 100 / total)))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"下载失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"下载失败：{exc.reason}") from exc
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        raise UpdateError(f"下载失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 检查最新 release
# ---------------------------------------------------------------------------

def _platform_key() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def check_latest_release(timeout: int = _TIMEOUT) -> LatestRelease | None:
    """查询仓库最新 release。无可用 release 返回 None，网络/解析错误抛 UpdateError。"""
    try:
        payload = json.loads(_http_get(_API_LATEST, timeout=timeout).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"无法解析 GitHub 返回内容：{exc}") from exc

    tag = str(payload.get("tag_name", "") or "").strip()
    if not tag:
        return None
    version = tag[1:] if tag.lower().startswith("v") else tag

    assets = payload.get("assets") or []
    plat = _platform_key()
    zip_url = zip_name = ""
    for asset in assets:
        name = str(asset.get("name", "") or "")
        url = str(asset.get("browser_download_url", "") or "")
        # 旧：第一个含平台名的 .zip 就选中。新版 release 有 app_*、runtime_* 增量包，
        # 如果它们排在前面会被误选为全量 fallback 包。明确跳过增量包前缀。
        nl = name.lower()
        if (nl.endswith(".zip") and plat in nl
                and not nl.startswith("app_") and not nl.startswith("runtime_")):
            zip_url, zip_name = url, name
            break
    if not zip_url:
        raise UpdateError(f"该版本未提供适用于当前系统（{plat}）的下载包。")

    sha256_url: str | None = None
    manifest_url: str | None = None
    manifest_name = f"update_manifest_{plat}.json"
    for asset in assets:
        name = str(asset.get("name", "") or "")
        if name == f"{zip_name}.sha256":
            sha256_url = str(asset.get("browser_download_url", "") or "")
        elif name == manifest_name:
            manifest_url = str(asset.get("browser_download_url", "") or "")

    return LatestRelease(
        version=version,
        tag=tag,
        zip_url=zip_url,
        zip_name=zip_name,
        sha256_url=sha256_url,
        notes=str(payload.get("body", "") or ""),
        manifest_url=manifest_url,
    )


# ---------------------------------------------------------------------------
# 下载 + 校验 + 解压
# ---------------------------------------------------------------------------

def default_download_root(workspace_root: Path | str) -> Path:
    """新版本应下载到的 releases 根目录（复用 release_manager 的扫描规则）。"""
    roots = release_roots(workspace_root)
    if roots:
        return roots[0]
    return Path(workspace_root).resolve() / "releases"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_expected_hash(text: str, zip_name: str) -> str | None:
    """从 sha256 文件文本里取出 zip_name 对应的摘要。

    文件格式为 ``{digest}  {filename}``，可能多行。找不到匹配行且只有一行时，
    回退取该行的第一个字段。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == zip_name:
            return parts[0]
    if len(lines) == 1:
        parts = lines[0].split()
        if parts:
            return parts[0]
    return None


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压 zip,并防止 zip-slip(成员路径逃逸出 dest)。

    规范化软件设计 2026-05 P1 审查修复:用 Path.relative_to() 跨平台一致,
    且拒绝绝对路径成员。
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            if Path(member).is_absolute() or member.startswith("/") or member.startswith("\\"):
                raise UpdateError(f"压缩包包含绝对路径,已中止:{member}")
            target = (dest / member).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                raise UpdateError(f"压缩包包含非法路径,已中止:{member}")
        archive.extractall(dest)


def download_release(
    release: LatestRelease,
    dest_root: Path | str,
    progress_cb: Callable[[int], None] | None = None,
) -> Path:
    """下载并解压指定 release 到 ``dest_root/v{version}/``，返回解压目录。

    全程在临时目录操作，sha256 校验通过后才移入正式位置；失败则清理临时文件。
    """
    _validate_url(release.zip_url)
    dest_root = Path(dest_root)
    target_dir = dest_root / f"v{release.version}"
    if target_dir.exists():
        raise UpdateError(f"版本目录已存在，无需重复下载：\n{target_dir}")

    expected_hash: str | None = None
    if release.sha256_url:
        _validate_url(release.sha256_url)
        sha_text = _http_get(release.sha256_url).decode("utf-8", errors="replace")
        expected_hash = _extract_expected_hash(sha_text, release.zip_name)

    tmp_dir = Path(tempfile.mkdtemp(prefix="specimen-update-"))
    try:
        tmp_zip = tmp_dir / release.zip_name
        _download_to(release.zip_url, tmp_zip, progress_cb)

        if expected_hash:
            actual_hash = _file_sha256(tmp_zip)
            if actual_hash.lower() != expected_hash.lower():
                raise UpdateError(
                    "下载文件校验失败（sha256 不匹配），已中止安装。\n"
                    f"期望：{expected_hash}\n实际：{actual_hash}"
                )

        staging = tmp_dir / "extracted"
        _safe_extract(tmp_zip, staging)

        dest_root.mkdir(parents=True, exist_ok=True)
        # staging 移出 tmp_dir 后即与临时目录无关，finally 的清理不会影响它。
        shutil.move(str(staging), str(target_dir))
        return target_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 增量更新：拆分包（应用包 + 运行时包）+ 复用本地运行时
# ---------------------------------------------------------------------------

def _asset_url(tag: str, name: str) -> str:
    """构造 GitHub release 资产的下载 URL（browser_download_url 的稳定格式）。"""
    return (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{urllib.parse.quote(tag)}/{urllib.parse.quote(name)}"
    )


def _validate_rel_path(rel: str) -> None:
    """校验 bundle 内相对路径不逃逸（防止 .. / 绝对路径）。"""
    p = PurePosixPath(rel)
    if not rel.strip() or p.is_absolute() or ".." in p.parts:
        raise UpdateError(f"非法的文件路径，已中止：{rel}")


def _verify_sha256(path: Path, expected: str) -> None:
    if not expected:
        return  # manifest 正常都会带摘要；缺失时不阻断（与 download_release 容错一致）
    actual = _file_sha256(path)
    if actual.lower() != expected.lower():
        raise UpdateError(
            "下载文件校验失败（sha256 不匹配），已中止安装。\n"
            f"文件：{path.name}\n期望：{expected}\n实际：{actual}"
        )


def _scaled_cb(progress_cb: Callable[[int], None] | None, lo: int, hi: int):
    """把单文件 0-100 的进度映射到整体进度区间 [lo, hi]。"""
    if progress_cb is None:
        return None
    return lambda pct: progress_cb(lo + (hi - lo) * pct // 100)


def _fetch_update_plan(release: LatestRelease) -> UpdatePlan:
    """下载并解析 update_manifest_{plat}.json。"""
    assert release.manifest_url is not None
    _validate_url(release.manifest_url)
    try:
        data = json.loads(_http_get(release.manifest_url).decode("utf-8"))
        app_zip = str(data["app_zip"])
        runtime_zip = str(data["runtime_zip"])
        return UpdatePlan(
            version=str(data.get("version", release.version)),
            platform=str(data.get("platform", "")),
            app_zip_url=_asset_url(release.tag, app_zip),
            app_zip_name=app_zip,
            app_sha256=str(data.get("app_sha256", "")),
            runtime_zip_url=_asset_url(release.tag, runtime_zip),
            runtime_zip_name=runtime_zip,
            runtime_sha256=str(data.get("runtime_sha256", "")),
            runtime_hash=str(data.get("runtime_hash", "")),
            app_files=tuple(str(p) for p in data.get("app_files", [])),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise UpdateError(f"无法解析更新清单：{exc}") from exc


def _find_reusable_runtime(
    local_roots: list[Path] | None, runtime_hash: str
) -> tuple[Path, dict] | None:
    """在本地已装版本里找运行时 hash 匹配的 bundle 目录，作为运行时复用源。

    返回 (bundle_dir, meta)；找不到返回 None。
    """
    if not runtime_hash or not local_roots:
        return None
    for root in local_roots:
        root = Path(root)
        if not root.exists():
            continue
        for vdir in sorted(root.iterdir(), reverse=True):
            if not vdir.is_dir() or not vdir.name.startswith("v"):
                continue
            for meta_path in vdir.rglob(APP_META_FILE):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if str(meta.get("runtime_hash", "")) == runtime_hash:
                    return meta_path.parent, meta
    return None


def _copy_runtime_files(src_bundle: Path, src_meta: dict, dst_bundle: Path) -> None:
    """把复用源 bundle 里的运行时分区文件（= 非应用分区、非元数据）拷到目标 bundle。"""
    app_set = {str(p) for p in src_meta.get("app_files", [])}
    for path in src_bundle.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_bundle).as_posix()
        if rel in app_set or rel == APP_META_FILE:
            continue  # 应用分区文件不复用，由下载的应用包提供
        _validate_rel_path(rel)
        target = dst_bundle / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def download_update(
    release: LatestRelease,
    dest_root: Path | str,
    local_roots: list[Path] | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> tuple[Path, bool]:
    """增量更新入口：尽量只下载应用包，运行时从本地复用。

    返回 ``(target_dir, incremental)``：``incremental`` 为 True 表示走了增量路径（只下应用包）。
    老 release（无 update_manifest）自动回退到 :func:`download_release` 的完整 zip 路径。
    """
    # 1. 老 release / 服务端没拆包 → 回退完整 zip
    if not release.manifest_url:
        return download_release(release, dest_root, progress_cb), False

    plan = _fetch_update_plan(release)
    for url in (plan.app_zip_url, plan.runtime_zip_url):
        _validate_url(url)
    for rel in plan.app_files:
        _validate_rel_path(rel)

    dest_root = Path(dest_root)
    target_dir = dest_root / f"v{plan.version}"
    if target_dir.exists():
        raise UpdateError(f"版本目录已存在，无需重复下载：\n{target_dir}")

    bundle_name = f"{APP_NAME}_v{plan.version}"
    reusable = _find_reusable_runtime(local_roots, plan.runtime_hash)

    tmp_dir = Path(tempfile.mkdtemp(prefix="specimen-update-"))
    try:
        staging = tmp_dir / "staging"
        staging_bundle = staging / bundle_name
        staging_bundle.mkdir(parents=True)

        if reusable is not None:
            # 增量路径：复用本地运行时，只下载应用包
            src_bundle, src_meta = reusable
            _copy_runtime_files(src_bundle, src_meta, staging_bundle)
            app_zip = tmp_dir / plan.app_zip_name
            _download_to(plan.app_zip_url, app_zip, progress_cb)
            _verify_sha256(app_zip, plan.app_sha256)
            _safe_extract(app_zip, staging)  # arcname 带 bundle 前缀，合并进 staging_bundle
            incremental = True
        else:
            # 完整路径：下载应用包 + 运行时包（运行时是大头，占 0-85% 进度）
            runtime_zip = tmp_dir / plan.runtime_zip_name
            app_zip = tmp_dir / plan.app_zip_name
            _download_to(plan.runtime_zip_url, runtime_zip, _scaled_cb(progress_cb, 0, 85))
            _verify_sha256(runtime_zip, plan.runtime_sha256)
            _download_to(plan.app_zip_url, app_zip, _scaled_cb(progress_cb, 85, 100))
            _verify_sha256(app_zip, plan.app_sha256)
            _safe_extract(runtime_zip, staging)
            _safe_extract(app_zip, staging)
            incremental = False

        # 写入 .update_meta.json，让该目录日后也能作为运行时复用源
        (staging_bundle / APP_META_FILE).write_text(
            json.dumps(
                {
                    "version": plan.version,
                    "runtime_hash": plan.runtime_hash,
                    "app_files": list(plan.app_files),
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target_dir))
        return target_dir, incremental
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
