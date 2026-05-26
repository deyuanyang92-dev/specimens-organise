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
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

from . import __version__
from .release_manager import APP_NAME, release_roots


GITHUB_REPO = "deyuanyang92-dev/specimens-organise"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_USER_AGENT = f"specimen-inventory-updater/{__version__}"
_TIMEOUT = 10
_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")


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
    # v0.10.8 增量更新基础设施
    manifest_url: str = ""       # update_manifest_{plat}.json URL（缺时表示 release 无增量包）
    app_zip_url: str = ""        # app_v*_{plat}.zip URL（仅 app 代码部分）
    app_zip_name: str = ""       # app zip 文件名
    app_zip_sha256_url: str = ""  # app zip 对应 .sha256 URL


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


def check_latest_release(
    timeout: int = _TIMEOUT,
    *,
    platform_override: str | None = None,
    channel: str = "stable",
) -> LatestRelease | None:
    """查询仓库最新 release。无可用 release 返回 None，网络/解析错误抛 UpdateError。

    platform_override: D5 跨平台分发 — 强制选 ``"windows"`` / ``"linux"`` /
        ``"macos"`` 的包，不按当前 ``sys.platform`` 自动选。
    channel: D18 Claude Code 风 channel 切换 — ``"stable"`` 取
        ``/releases/latest`` (默认), ``"prerelease"`` 改成 ``/releases`` 列表
        过滤 ``prerelease: true`` 最新一项。
    """
    if channel == "prerelease":
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=20"
    else:
        api_url = _API_LATEST
    try:
        raw = _http_get(api_url, timeout=timeout).decode("utf-8")
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"无法解析 GitHub 返回内容：{exc}") from exc

    if channel == "prerelease":
        if not isinstance(payload, list):
            return None
        candidates = [
            entry for entry in payload
            if isinstance(entry, dict) and entry.get("prerelease") is True
        ]
        if not candidates:
            return None
        payload = candidates[0]

    tag = str(payload.get("tag_name", "") or "").strip()
    if not tag:
        return None
    version = tag[1:] if tag.lower().startswith("v") else tag

    assets = payload.get("assets") or []
    plat = (platform_override or _platform_key()).lower()
    if plat not in ("windows", "linux", "macos"):
        raise UpdateError(f"不支持的平台：{plat}")
    zip_url = zip_name = ""
    for asset in assets:
        name = str(asset.get("name", "") or "")
        url = str(asset.get("browser_download_url", "") or "")
        # 完整安装包永远是 setup_v{ver}_{plat}.zip。用白名单(startswith setup_)精确匹配,
        # 不再用黑名单跳过 app_/runtime_ —— 增量包已改 update-only_ 前缀,黑名单会失效。
        nl = name.lower()
        if nl.startswith("setup_") and nl.endswith(".zip") and plat in nl:
            zip_url, zip_name = url, name
            break
    # v0.8.0 修:不再在缺包时直接 raise。返回 LatestRelease 让调用方先比版本号 ——
    # 若当前 == 最新即"已是最新"(常见正常路径),不应误报"下载错误"。
    # 仅当调用方真要下载且 zip_url 为空时,download_release 抛 UpdateError。

    sha256_url: str | None = None
    for asset in assets:
        name = str(asset.get("name", "") or "")
        if name == f"{zip_name}.sha256":
            sha256_url = str(asset.get("browser_download_url", "") or "")
            break

    # v0.10.8 增量更新资产：app_v*_{plat}.zip + update_manifest_{plat}.json
    app_zip_url = app_zip_name = app_zip_sha256_url = manifest_url = ""
    for asset in assets:
        name = str(asset.get("name", "") or "")
        url = str(asset.get("browser_download_url", "") or "")
        nl = name.lower()
        if nl.startswith("app_") and nl.endswith(".zip") and plat in nl:
            app_zip_url, app_zip_name = url, name
        elif nl == f"update_manifest_{plat}.json":
            manifest_url = url
    if app_zip_name:
        for asset in assets:
            name = str(asset.get("name", "") or "")
            if name == f"{app_zip_name}.sha256":
                app_zip_sha256_url = str(asset.get("browser_download_url", "") or "")
                break

    return LatestRelease(
        version=version,
        tag=tag,
        zip_url=zip_url,
        zip_name=zip_name,
        sha256_url=sha256_url,
        notes=str(payload.get("body", "") or ""),
        manifest_url=manifest_url,
        app_zip_url=app_zip_url,
        app_zip_name=app_zip_name,
        app_zip_sha256_url=app_zip_sha256_url,
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


def _find_local_release_with_matching_runtime_hash(
    local_roots: list[Path | str], required_runtime_hash: str,
) -> Path | None:
    """plan v0.10.8：扫 local_roots/v*/ 找含相同 runtime_hash 的旧版本目录。

    每个 release 目录里 PyInstaller 产物的根目录（``标本入库管理_v{version}/``）含
    ``.update_meta.json``，里面记 runtime_hash。若任一旧版本 runtime_hash 与
    required_runtime_hash 相同 → 表示其 _internal/（除 specimen_app）可被复用，
    增量更新只需下 app_v*.zip 覆盖代码即可。
    """
    if not required_runtime_hash:
        return None
    for root in local_roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for version_dir in sorted(root_path.iterdir()):
            if not version_dir.is_dir():
                continue
            # PyInstaller bundle 直接在 version_dir 下，名字是 标本入库管理_v{version}/
            for bundle_dir in version_dir.iterdir():
                if not bundle_dir.is_dir():
                    continue
                meta_path = bundle_dir / ".update_meta.json"
                if not meta_path.is_file():
                    continue
                try:
                    with meta_path.open("r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                    if str(meta.get("runtime_hash", "")) == required_runtime_hash:
                        return bundle_dir
                except (OSError, json.JSONDecodeError):
                    continue
    return None


def download_release_with_optional_incremental(
    release: LatestRelease,
    dest_root: Path | str,
    local_roots: list[Path | str] | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> tuple[Path, bool]:
    """plan v0.10.8：优先走 app-only 增量；不可走则 fallback 全量 setup zip。

    返回 (target_dir, was_incremental)。was_incremental=True 表示真走了增量路径
    （只下了 ~5MB 的 app zip，复用了本地旧版 runtime ~60MB），False 表示走全量
    setup_v*.zip（~66-135MB）。

    增量路径条件：
      - release 提供了 manifest_url + app_zip_url
      - 本地 local_roots 中存在 ``.update_meta.json`` runtime_hash 与 manifest
        中 runtime_hash 相同的旧版本目录
    任一条件不满足 → fallback download_release 全量。
    """
    local_roots = list(local_roots or [])
    dest_root = Path(dest_root)
    target_dir = dest_root / f"v{release.version}"

    # 条件 1：必须有 manifest + app_zip
    if not release.manifest_url or not release.app_zip_url:
        return download_release(release, dest_root, progress_cb), False

    if target_dir.exists():
        raise UpdateError(f"版本目录已存在，无需重复下载：\n{target_dir}")

    # 拉 manifest 取 runtime_hash
    try:
        _validate_url(release.manifest_url)
        manifest_bytes = _http_get(release.manifest_url)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        required_runtime_hash = str(manifest.get("runtime_hash", "") or "")
        expected_app_sha256 = str(manifest.get("app_zip", {}).get("sha256", "") or "")
    except (UpdateError, json.JSONDecodeError, UnicodeDecodeError):
        return download_release(release, dest_root, progress_cb), False

    if not required_runtime_hash:
        return download_release(release, dest_root, progress_cb), False

    # 条件 2：本地找匹配 runtime 的旧版
    source_bundle_dir = _find_local_release_with_matching_runtime_hash(local_roots, required_runtime_hash)
    if source_bundle_dir is None:
        return download_release(release, dest_root, progress_cb), False

    # 走增量：先 copy 旧 release 整体 → 解压 app zip 覆盖 specimen_app + root exe
    _validate_url(release.app_zip_url)
    tmp_dir = Path(tempfile.mkdtemp(prefix="specimen-update-app-"))
    try:
        tmp_zip = tmp_dir / release.app_zip_name
        _download_to(release.app_zip_url, tmp_zip, progress_cb)
        if expected_app_sha256:
            _verify_sha256(tmp_zip, expected_app_sha256)
        # source_bundle_dir 是 …/v{old}/标本入库管理_v{old}/；其 parent 是 v{old}/
        source_version_dir = source_bundle_dir.parent
        dest_root.mkdir(parents=True, exist_ok=True)
        # 整体复制到目标 v{new}/，然后改 bundle 目录名 + 覆盖 app 部分
        shutil.copytree(source_version_dir, target_dir)
        # 旧 bundle dir 名（如 标本入库管理_v0.10.7）→ 重命名为新版 (v0.10.8)
        old_bundle_in_target = target_dir / source_bundle_dir.name
        new_bundle_in_target = target_dir / f"{APP_NAME}_v{release.version}"
        if old_bundle_in_target != new_bundle_in_target:
            old_bundle_in_target.rename(new_bundle_in_target)
        # 解压 app zip 覆盖到 target_dir（zip 内根目录与新 bundle 同名）
        _safe_extract(tmp_zip, target_dir)
        return target_dir, True
    except Exception:
        # 增量失败 → 清理已 copy 的 target_dir，fallback 全量
        shutil.rmtree(target_dir, ignore_errors=True)
        return download_release(release, dest_root, progress_cb), False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download_release(
    release: LatestRelease,
    dest_root: Path | str,
    progress_cb: Callable[[int], None] | None = None,
) -> Path:
    """下载并解压指定 release 到 ``dest_root/v{version}/``，返回解压目录。

    全程在临时目录操作，sha256 校验通过后才移入正式位置；失败则清理临时文件。
    """
    if not release.zip_url:
        # v0.8.0 修:check_latest_release 不再在缺包时 raise(以便 is_newer 比较先走)。
        # 真要下载时若 zip_url 仍为空 → 多半 CI 构建未完成,给可执行错误。
        raise UpdateError(
            f"v{release.version} 的安装包尚未就绪（GitHub Release 中未找到对应平台的 zip 资产）。\n"
            "可能是 GitHub Actions 构建未完成；请稍后重试。"
        )
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


def _verify_sha256(path: Path, expected: str) -> None:
    if not expected:
        return  # 缺摘要时不阻断（与 download_release 容错一致）
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


# --------------------------------------------------------------------------- #
# D1 stable-entry helpers: current/ junction (Windows) / symlink (Linux)
# --------------------------------------------------------------------------- #


def make_current_symlink(install_root: Path, bundle_dir: Path) -> bool:
    """Atomically (re)point ``install_root/current`` at ``bundle_dir`` via
    a POSIX symlink.

    Returns True on success. Returns False (and leaves the existing link
    untouched) when the underlying filesystem doesn't support symlinks
    (e.g. FAT/exFAT) — the caller should fall back to a rename swap.
    """
    install_root = Path(install_root)
    bundle_dir = Path(bundle_dir).resolve()
    if not bundle_dir.is_dir():
        raise UpdateError(f"bundle directory not found: {bundle_dir}")
    install_root.mkdir(parents=True, exist_ok=True)
    current_link = install_root / "current"
    tmp_link = install_root / f".current.{os.getpid()}.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    try:
        os.symlink(bundle_dir, tmp_link)
    except (OSError, NotImplementedError):
        return False
    try:
        os.replace(tmp_link, current_link)
    except OSError:
        # Some filesystems refuse to replace a non-symlink directory with a
        # symlink via os.replace; remove and retry.
        if current_link.exists() or current_link.is_symlink():
            try:
                if current_link.is_symlink() or current_link.is_file():
                    current_link.unlink()
                else:
                    shutil.rmtree(current_link)
            except OSError:
                tmp_link.unlink(missing_ok=True)
                return False
        try:
            os.replace(tmp_link, current_link)
        except OSError:
            tmp_link.unlink(missing_ok=True)
            return False
    return True


def make_current_junction(install_root: Path, bundle_dir: Path) -> bool:
    """(Re)point ``install_root\\current`` at ``bundle_dir`` via a Windows
    NTFS directory junction.

    Junctions are local-only and require no admin rights; they work for any
    NTFS volume. Returns True on success, False when the FS doesn't support
    junctions (FAT/exFAT) — caller should fall back to rename swap.
    """
    install_root = Path(install_root)
    bundle_dir = Path(bundle_dir).resolve()
    if not bundle_dir.is_dir():
        raise UpdateError(f"bundle directory not found: {bundle_dir}")
    install_root.mkdir(parents=True, exist_ok=True)
    current_link = install_root / "current"

    # mklink /J refuses to overwrite — remove first. Use rmdir (safe for
    # junctions: removes the link itself, not the target).
    if current_link.exists() or current_link.is_symlink():
        try:
            if current_link.is_dir() and not current_link.is_symlink():
                # Plain directory (no prior junction) — remove via rmtree.
                # Caller should have already moved any content of value out.
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/S", "/Q", str(current_link)],
                    check=True, capture_output=True,
                )
            else:
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(current_link)],
                    check=True, capture_output=True,
                )
        except subprocess.CalledProcessError:
            return False

    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(current_link), str(bundle_dir)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def repoint_current(install_root: Path, bundle_dir: Path) -> bool:
    """Cross-platform atomic ``current/`` repoint dispatcher.

    Returns True on success. False return signals the caller to fall back
    to the rename-swap strategy (rename old bundle → .bak, move new bundle
    to a stable name).
    """
    if sys.platform == "win32":
        return make_current_junction(install_root, bundle_dir)
    return make_current_symlink(install_root, bundle_dir)


# --------------------------------------------------------------------------- #
# D4 local-zip probe + import
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZipProbe:
    """Result of inspecting a downloaded / sneaker-netted update zip."""
    kind: str            # "full" | "app-only" | "unknown"
    version: str         # parsed from the bundle dir name, "" if unknown
    platform: str        # "windows" | "linux" | "macos" | "" if unknown
    runtime_hash: str    # 12-char hash for app-only zips, "" otherwise
    bundle_dir_name: str  # top-level dir name inside the zip, "" if flat


def _probe_zip_filename_platform(name: str) -> str:
    """Best-effort platform guess from a zip filename like
    ``setup_v0.7.0_windows.zip`` or ``app_v0.7.0_linux.zip``.
    """
    lower = name.lower()
    if "windows" in lower:
        return "windows"
    if "linux" in lower:
        return "linux"
    if "macos" in lower or "darwin" in lower:
        return "macos"
    return ""


def _probe_zip_filename_version(name: str) -> str:
    """Extract X.Y.Z from a zip filename like ``setup_v0.7.0_windows.zip``."""
    m = re.search(r"v?(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)", name)
    return m.group(1) if m else ""


def probe_zip(zip_path: Path | str) -> ZipProbe:
    """Inspect ``zip_path`` and classify it as a full setup bundle, an
    app-only delta, or unknown. Does not extract.

    Detection rules:
    - Full bundle: contains a top-level dir ``<APP_NAME>_v*`` with an exe
      directly inside.
    - App-only: contains a top-level dir ``<APP_NAME>_v*`` with
      ``.update_meta.json`` listing ``app_files`` but missing any
      ``_internal/PyQt5`` / ``_internal/python`` runtime markers.
    - Anything else: ``"unknown"``.

    Filename hints (``setup_*_<platform>.zip`` / ``app_*_<platform>.zip``)
    populate the platform / version best-effort even when the zip is
    malformed enough that members can't be read.
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise UpdateError(f"未找到 zip 文件：{zip_path}")
    plat = _probe_zip_filename_platform(zip_path.name)
    version = _probe_zip_filename_version(zip_path.name)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if not names:
                return ZipProbe("unknown", version, plat, "", "")
            top = names[0].split("/", 1)[0]
            bundle_prefix = f"{APP_NAME}_v"
            looks_bundled = top.startswith(bundle_prefix)
            has_meta = any(n.endswith(f"{top}/.update_meta.json") for n in names)
            has_exe_at_top = any(
                n == f"{top}/{top}.exe" or n == f"{top}/{top}"
                or (n.startswith(f"{top}/{APP_NAME}") and "/" not in n[len(top) + 1:])
                for n in names
            )
            has_runtime = any(
                f"{top}/_internal/PyQt5" in n or f"{top}/_internal/python" in n
                for n in names
            )
            runtime_hash = ""
            if has_meta:
                try:
                    with zf.open(f"{top}/.update_meta.json") as fh:
                        meta = json.loads(fh.read().decode("utf-8"))
                    runtime_hash = str(meta.get("runtime_hash", "") or "")
                    if not version:
                        version = str(meta.get("version", "") or "")
                except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
                    pass

            if looks_bundled and has_exe_at_top and has_runtime:
                return ZipProbe("full", version, plat, runtime_hash, top)
            if looks_bundled and has_meta and not has_runtime:
                return ZipProbe("app-only", version, plat, runtime_hash, top)
            return ZipProbe("unknown", version, plat, runtime_hash, top)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"zip 文件已损坏：{exc}") from exc


def import_local_zip(
    zip_path: Path | str,
    dest_root: Path | str,
    *,
    expected_platform: str | None = None,
    sha256_path: Path | str | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> tuple[Path, ZipProbe]:
    """Install an update from a local zip without going through GitHub.

    Used by D4 "从本地文件安装更新" — same security model as
    :func:`download_release`: sha256 (when supplied) is verified, zip-slip
    is blocked, extraction goes to a temp dir first then atomic-moves
    into ``dest_root / v{version}/``.

    ``expected_platform`` (``"windows"`` / ``"linux"`` / ``"macos"``) lets
    the caller reject a wrong-OS zip up front.

    Returns ``(target_dir, probe)``. Raises :class:`UpdateError` for any
    mismatch or extraction failure.
    """
    zip_path = Path(zip_path)
    probe = probe_zip(zip_path)
    if probe.kind == "unknown":
        raise UpdateError(
            "无法识别该 zip 类型。请确认是 标本入库管理 的完整安装包（setup_v*.zip）"
            "或应用增量包（app_v*.zip）。"
        )
    if probe.kind == "app-only":
        raise UpdateError(
            "导入应用增量包 (app_v*.zip) 暂不支持。v0.8.0 仅接受完整安装包"
            " (setup_v*.zip)。"
        )
    if expected_platform and probe.platform and probe.platform != expected_platform:
        raise UpdateError(
            f"平台不匹配：zip 是 {probe.platform},当前系统是 {expected_platform}。"
        )
    if sha256_path:
        sha256_path = Path(sha256_path)
        if not sha256_path.is_file():
            raise UpdateError(f"未找到 sha256 校验文件：{sha256_path}")
        expected_hash = _extract_expected_hash(
            sha256_path.read_text(encoding="utf-8"), zip_path.name
        )
        if not expected_hash:
            raise UpdateError(f"sha256 文件 {sha256_path.name} 内未找到对应条目。")
        _verify_sha256(zip_path, expected_hash)

    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    version = probe.version or "unknown"
    target_dir = dest_root / f"v{version}"
    if target_dir.exists():
        raise UpdateError(f"目标目录已存在：{target_dir}。请先删除或选其他位置。")

    tmp_dir = Path(tempfile.mkdtemp(prefix="specimen_import_"))
    try:
        if progress_cb:
            progress_cb(20)
        _safe_extract(zip_path, tmp_dir)
        if progress_cb:
            progress_cb(85)
        staging = tmp_dir / probe.bundle_dir_name
        if not staging.exists():
            raise UpdateError("zip 解压结果与探测不符,可能已损坏。")
        # mirror download_release: move the bundle dir under target_dir.
        target_dir.mkdir(parents=True, exist_ok=False)
        for item in staging.iterdir():
            shutil.move(str(item), str(target_dir / item.name))
        if progress_cb:
            progress_cb(100)
        return target_dir, probe
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# D5 cross-platform installer download (for sneaker-net distribution)
# --------------------------------------------------------------------------- #


def download_assets_for_distribution(
    release: LatestRelease,
    dest_dir: Path | str,
    *,
    include_sha256: bool = True,
    progress_cb: Callable[[int], None] | None = None,
) -> list[Path]:
    """Download the GitHub release's full setup zip (and optionally its
    sha256) into ``dest_dir`` without extracting.

    Used by D5 "下载安装包供分发": admin on a Windows box wants the Linux
    setup zip to USB-stick over to an offline Linux machine.
    """
    if not release.zip_url:
        raise UpdateError(
            f"v{release.version} 的安装包尚未就绪（GitHub Release 中未找到对应平台的 zip）。\n"
            "可能是 GitHub Actions 构建未完成；请稍后重试。"
        )
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    zip_dest = dest_dir / release.zip_name
    if progress_cb:
        progress_cb(0)
    _download_to(release.zip_url, zip_dest, _scaled_cb(progress_cb, 0, 90))
    written.append(zip_dest)

    if include_sha256 and release.sha256_url:
        sha_dest = dest_dir / f"{release.zip_name}.sha256"
        _download_to(release.sha256_url, sha_dest, _scaled_cb(progress_cb, 90, 100))
        written.append(sha_dest)
        # belt-and-braces: verify what we just wrote.
        expected = _extract_expected_hash(
            sha_dest.read_text(encoding="utf-8"), release.zip_name
        )
        if expected:
            _verify_sha256(zip_dest, expected)

    if progress_cb:
        progress_cb(100)
    return written
