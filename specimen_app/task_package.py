"""多人协作 — 任务包导出/导入（M3）。

任务包 = 中心机给录入员的一个 zip，主管在中心机预留好该录入员的独立前缀编号段，
打成 zip 后通过任意方式（U 盘 / 邮件 / 服务器 outbox/）发给录入员。录入员解压后
直接得到一个可打开的工作区，启动应用即可录入；新建标本编号自动落在预留段内。

工作流（结合 M1 收件箱聚合）：
1. 主管：`export_task_package(store, "张三", count=50, prefix="ZS-", dest_zip)`
   - 中心机 alloc_log 记录"任务开始"行（task_id + 编号段 + 系列）
   - 任务包 zip 内：空工作区骨架 + 字段模版/ + manifest.json
2. 录入员：双击 zip 或菜单「打开任务包…」→ `import_task_package(zip, target_dir)`
3. 录入员：用应用打开 target_dir，照常录入；编号自动从该系列推进
4. 录入员：菜单「打包工作区结果…」打出子目录放进 incoming/（M3.5 待做，或直接整目录 copy）
5. 主管：M1 「从收件箱聚合…」吃下

任务包结构（zip 内容）：
    数据/
        工作区配置.json        ← 已写好录入员专属系列定义 + active_series_name
        标本信息.xlsx 等       ← 空表头
        编号分发记录.xlsx
        ...
    字段模版/                 ← 从中心拷贝（如有）
    manifest.json             ← 任务元数据
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .excel_store import ExcelStore
from .models import CURRENT_DATA_SCHEMA_VERSION, WORKSPACE_CONFIG_FILE


MANIFEST_NAME = "manifest.json"


def _safe_extract(zip_file: zipfile.ZipFile, dest: Path) -> None:
    """zip-slip 防护：拒绝任何尝试逃逸 dest 目录的成员。

    规范化软件设计 2026-05 P1 审查修复:
    旧:用 `str(target).startswith(dest_resolved + os.sep)` 校验,Windows 路径含混合分隔符
    `..\\..` 可能绕过(target.resolve() 后路径已正规化,但与 dest_resolved 比较仍可能受
    平台分隔符影响)。
    现:用 Path.relative_to() 检测路径是否在 dest 内 —— pathlib 跨平台一致,
    relative_to 抛 ValueError 即逃逸。
    """
    dest_resolved = dest.resolve()
    for member in zip_file.namelist():
        # 拒绝绝对路径成员(zip 标准允许,但解压时易跑出 dest)
        if Path(member).is_absolute() or member.startswith("/") or member.startswith("\\"):
            raise ValueError(f"任务包含绝对路径,已中止解压:{member}")
        target = (dest / member).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise ValueError(f"任务包含非法路径,已中止解压:{member}")
    zip_file.extractall(dest)


def export_task_package(
    store: ExcelStore,
    assignee: str,
    count: int,
    prefix: str,
    dest_zip_path: Path | str,
    purpose: str = "录入任务包",
    note: str = "",
) -> Path:
    """主管侧：给指定录入员预留 count 个独立前缀编号 + 打成任务包 zip。

    返回最终 zip 路径。

    副作用（中心机 store）：
    - `accession_series` 多/复用录入员独立系列
    - `编号分发记录.xlsx` 多一行"任务开始"（含 task_id、编号段、系列）

    任务包内的空工作区骨架已配置好该系列为活动系列，录入员打开后 create_specimen()
    会直接生成预留段内的编号。
    """
    if count <= 0:
        raise ValueError("生成数量必须为正整数")
    dest_zip = Path(dest_zip_path).resolve()
    if dest_zip.exists():
        raise FileExistsError(f"目标文件已存在：{dest_zip}")

    # 1. 中心机预留段（前缀分人）
    series_name = store.ensure_assignee_series(assignee, prefix)
    series_cfg = store._get_series_config(series_name)
    if series_cfg is None:
        raise RuntimeError(f"系列 {series_name!r} 创建失败")
    numbers = store.batch_reserve_vouchers(count, series_name=series_name)

    # 2. 写中心机 alloc_log（"任务开始"行；task_id 复用 ALLOC_LOG_HEADERS 已有"记录ID"列）
    task_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")
    store.log_alloc_event(
        {
            "记录ID": task_id,
            "时间": now,
            "类型": "任务开始",
            "人员": assignee,
            "用途": purpose,
            "备注": note,
            "编号系列": series_name,
            "编号起始": numbers[0],
            "编号结束": numbers[-1],
            "数量": str(count),
        }
    )

    # 3. 构建任务包临时目录
    central_workspace_id = str(store.config.get("workspace_id", ""))
    central_schema_version = str(
        store.config.get("data_schema_version", CURRENT_DATA_SCHEMA_VERSION)
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix="task_pkg_"))
    try:
        skeleton = tmp_dir / "skeleton"
        skeleton.mkdir()
        # 用 ExcelStore 初始化空工作区骨架（自动建 数据/ + 模版 xlsx + 工作区配置.json）
        sk_store = ExcelStore(skeleton, lock=False, create_if_missing=True)
        # 写入录入员独立系列定义（next_counter=1，本地从该段第一号开始录）
        sk_store.config["accession_series"] = [
            {**series_cfg.to_dict(), "next_counter": 1}
        ]
        sk_store.config["active_series_name"] = series_name
        # 任务包工作区独立 workspace_id（与中心 ID 分开，便于 aggregate 时区分源）
        sk_store.config["workspace_id"] = str(uuid.uuid4())
        # 透传 schema 版本，回流聚合时可校验中心是否已升级到等同或更新的版本
        sk_store.config["data_schema_version"] = central_schema_version
        # 任务包元数据写进 config，方便录入员打包结果时回填 manifest
        sk_store.config["task_package_info"] = {
            "task_id": task_id,
            "assignee": assignee,
            "central_workspace_id": central_workspace_id,
            "series_name": series_name,
            "series_prefix": series_cfg.prefix,
            "voucher_range": [numbers[0], numbers[-1]],
            "issued_at": now,
        }
        sk_store._save_config()
        sk_store.close()

        # 字段模版/ 从中心拷过来（提升录入员体验：物种预设、字段说明）
        central_template = store.root / "字段模版"
        if central_template.exists():
            shutil.copytree(
                central_template,
                skeleton / "字段模版",
                dirs_exist_ok=True,
            )

        # 4. manifest.json — 即使骨架配置丢失，manifest 也能让聚合端追溯任务来源
        manifest: dict[str, Any] = {
            "task_id": task_id,
            "assignee": assignee,
            "central_workspace_id": central_workspace_id,
            "series_name": series_name,
            "series_prefix": series_cfg.prefix,
            "voucher_range": [numbers[0], numbers[-1]],
            "voucher_count": count,
            "issued_at": now,
            "software_version": __version__,
            "data_schema_version": central_schema_version,
        }
        (skeleton / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 5. 打 zip 到临时文件，校验通过后 move 到目标位置
        tmp_zip = tmp_dir / "task_package.zip"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in skeleton.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(skeleton).as_posix())

        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_zip), str(dest_zip))
        return dest_zip
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def import_task_package(
    zip_path: Path | str,
    target_root: Path | str,
) -> Path:
    """录入员侧：解压任务包到 target_root。

    target_root 不能含数据（避免污染既有工作区）。若不存在则自动创建。
    返回 target_root（已可作为工作区根直接被 ExcelStore 打开）。
    """
    zip_p = Path(zip_path).resolve()
    target = Path(target_root).resolve()
    if not zip_p.exists():
        raise FileNotFoundError(f"任务包不存在：{zip_p}")
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"目标目录非空，请选一个空目录：{target}")
    target.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="task_pkg_in_"))
    try:
        staging = tmp_dir / "staging"
        staging.mkdir()
        with zipfile.ZipFile(zip_p, "r") as zf:
            _safe_extract(zf, staging)

        # 校验：manifest + 数据/工作区配置.json 都必须存在
        manifest_path = staging / MANIFEST_NAME
        if not manifest_path.exists():
            raise ValueError(f"任务包缺少 {MANIFEST_NAME}（不是合法任务包）")
        config_path = staging / "数据" / WORKSPACE_CONFIG_FILE
        if not config_path.exists():
            raise ValueError(f"任务包缺少 数据/{WORKSPACE_CONFIG_FILE}")

        # 移到目标目录
        for child in staging.iterdir():
            shutil.move(str(child), str(target / child.name))
        return target
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def read_task_manifest(zip_path: Path | str) -> dict[str, Any]:
    """只读窥探：返回 zip 内 manifest.json 内容，不解压。失败抛 ValueError。"""
    zip_p = Path(zip_path).resolve()
    with zipfile.ZipFile(zip_p, "r") as zf:
        try:
            data = zf.read(MANIFEST_NAME)
        except KeyError as exc:
            raise ValueError(f"任务包缺少 {MANIFEST_NAME}") from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest.json 解析失败：{exc}") from exc
