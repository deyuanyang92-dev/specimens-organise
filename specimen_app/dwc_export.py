"""Darwin Core Archive (DwC-A) 导出（A1 改进）。

DwC = TDWG 标准生物多样性数据交换格式；DwC-A = ZIP 含 meta.xml + occurrence.txt
（+ 可选 multimedia.txt、measurementorfact.txt、eml.xml）。GBIF / iDigBio / VertNet
等聚合器都用此格式 harvest。

参考：
- https://www.gbif.org/darwin-core
- https://ipt.gbif.org/manual/en/ipt/latest/dwca-guide
- https://dwc.tdwg.org/terms/
- https://eml.ecoinformatics.org/

本模块把中心机工作区（标本信息 + 分类信息 + 照片信息）一键转成 DwC-A zip。
**转换是只读的，不动任何工作区文件**。

字段映射策略（中文列 → DwC term）：
- 入库编号*  → occurrenceID（主键，也复制到 catalogNumber 便于人类阅读）
- 管内编号*  → fieldNumber
- 保存方式   → preparations
- 采集日期   → eventDate
- 采集地点缩写* → verbatimLocality（短码，原样保留）
- 信息录入人员 → recordedBy（注：与 DwC 严格语义略有差异，DwC.recordedBy 是采集人；
                              本工作区缺独立"采集人"字段，先用录入员代替，A3 字段补全后再分开）
- 核对人员   → identifiedBy
- 备注       → occurrenceRemarks
- 种拉丁     → scientificName
- 种名*      → vernacularName
- 属名       → genus
- 科拉丁     → family
- 目         → order
- 纲         → class
- 门         → phylum
- 分类备注   → identificationRemarks
- 照片 → Audubon Core Multimedia extension（identifier / accessURI / description /
        fileFormat / fileHash via 文件SHA256）
"""

from __future__ import annotations

import csv
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__
from .excel_store import ExcelStore


# 命名空间 URI（meta.xml term 必须用完整 IRI；GBIF 校验严格要求）
DWC_TERM_BASE = "http://rs.tdwg.org/dwc/terms/"
AC_TERM_BASE = "http://rs.tdwg.org/ac/terms/"  # Audubon Core (多媒体扩展)


# 占位 source field "__voucher__" 表示从 voucher 直接生成（而非读 specimen / classification 表）
# 映射表元素：(dwc_term, source_category, source_field)
# source_category ∈ {"voucher", "specimen", "classification"}
_OCCURRENCE_FIELDS: list[tuple[str, str, str]] = [
    # 第 0 列必须是主键
    ("occurrenceID", "voucher", "__voucher__"),
    ("catalogNumber", "voucher", "__voucher__"),
    ("fieldNumber", "specimen", "管内编号*"),
    ("preparations", "specimen", "保存方式"),
    ("eventDate", "specimen", "采集日期"),
    ("verbatimLocality", "specimen", "采集地点缩写*"),
    ("recordedBy", "specimen", "信息录入人员"),
    ("identifiedBy", "specimen", "核对人员"),
    ("occurrenceRemarks", "specimen", "备注"),
    ("scientificName", "classification", "种拉丁"),
    ("vernacularName", "classification", "种名*"),
    ("genus", "classification", "属名"),
    ("family", "classification", "科拉丁"),
    ("order", "classification", "目"),
    ("class", "classification", "纲"),
    ("phylum", "classification", "门"),
    ("identificationRemarks", "classification", "备注"),
]

# 照片 → Audubon Core Multimedia 扩展。coreid 是与 core 文件 occurrenceID 关联的外键。
_MULTIMEDIA_FIELDS: list[tuple[str, str]] = [
    # (ac_term, source_field) — coreid 单独从 voucher 派生
    ("identifier", "文件名"),
    ("accessURI", "绝对路径"),
    ("description", "描述"),
    ("fileFormat", "__file_format__"),   # 从文件名扩展名推断
    ("hashFunction", "__hash_function__"),  # 常量 "MD5/SHA-256"
    ("hashValue", "文件SHA256"),
]


# 文件扩展名 → MIME 类型（DwC/AC 推荐用 IANA media type）
_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def _mime_from_filename(filename: str) -> str:
    return _EXT_TO_MIME.get(Path(filename).suffix.lower(), "")


def _abs_path_to_uri(abs_path: str) -> str:
    """把绝对路径包成 file:// URI（DwC.accessURI 要求 URI 形式）。"""
    if not abs_path:
        return ""
    p = abs_path.replace("\\", "/")
    # Windows 路径 C:/Users/... 也用 file:/// 前缀
    if p.startswith("/"):
        return "file://" + p
    return "file:///" + p


def export_dwc_archive(
    store: ExcelStore,
    dest_zip_path: Path | str,
    dataset_title: str = "Specimen Inventory Workspace Export",
    dataset_creator: str = "",
) -> Path:
    """工作区 → DwC-A zip。返回 zip 路径。

    dest 已存在 → FileExistsError（避免误覆盖）。
    工作区无 voucher → 仍生成合法空 archive（occurrence.txt 只有表头）。
    """
    dest = Path(dest_zip_path).resolve()
    if dest.exists():
        raise FileExistsError(f"目标文件已存在：{dest}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="dwc_export_"))
    try:
        # ── occurrence.txt ───────────────────────────────────────────────
        occ_terms = [t[0] for t in _OCCURRENCE_FIELDS]
        occ_path = tmp_dir / "occurrence.txt"
        with occ_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(
                f, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n"
            )
            writer.writerow(occ_terms)
            for voucher in store.list_vouchers():
                spec = store.get_specimen(voucher) or {}
                cls = store.get_classification(voucher) or {}
                row: list[str] = []
                for _, cat, field in _OCCURRENCE_FIELDS:
                    if cat == "voucher":
                        row.append(str(voucher))
                    elif cat == "specimen":
                        row.append(str(spec.get(field, "") or ""))
                    elif cat == "classification":
                        row.append(str(cls.get(field, "") or ""))
                    else:
                        row.append("")
                writer.writerow(row)

        # ── multimedia.txt ───────────────────────────────────────────────
        mm_terms = ["coreid"] + [t[0] for t in _MULTIMEDIA_FIELDS]
        mm_path = tmp_dir / "multimedia.txt"
        photo_count = 0
        with mm_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(
                f, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n"
            )
            writer.writerow(mm_terms)
            for photo in store.read_rows("photo"):
                voucher = str(photo.get("入库编号*", "") or "")
                if not voucher:
                    continue
                filename = str(photo.get("文件名", "") or "")
                abs_path = str(photo.get("绝对路径", "") or "")
                desc = str(photo.get("描述", "") or "")
                sha = str(photo.get("文件SHA256", "") or "")
                row = [voucher]
                for ac_term, src_field in _MULTIMEDIA_FIELDS:
                    if src_field == "__file_format__":
                        row.append(_mime_from_filename(filename))
                    elif src_field == "__hash_function__":
                        row.append("SHA-256" if sha else "")
                    elif src_field == "文件名":
                        row.append(filename)
                    elif src_field == "绝对路径":
                        row.append(_abs_path_to_uri(abs_path))
                    else:
                        row.append(str(photo.get(src_field, "") or ""))
                writer.writerow(row)
                photo_count += 1

        # ── meta.xml ─────────────────────────────────────────────────────
        (tmp_dir / "meta.xml").write_text(_build_meta_xml(), encoding="utf-8")

        # ── eml.xml ──────────────────────────────────────────────────────
        voucher_count = len(store.list_vouchers())
        (tmp_dir / "eml.xml").write_text(
            _build_eml_xml(store, dataset_title, dataset_creator, voucher_count, photo_count),
            encoding="utf-8",
        )

        # ── 打 zip ───────────────────────────────────────────────────────
        tmp_zip = tmp_dir / "dwca.zip"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in ("occurrence.txt", "multimedia.txt", "meta.xml", "eml.xml"):
                zf.write(tmp_dir / name, name)

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_zip), str(dest))
        return dest
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _build_meta_xml() -> str:
    """DwC-A meta.xml — 描述 archive 内文件 + 字段映射 + 行格式。

    fieldsTerminatedBy 用字面量 `\\t`、linesTerminatedBy 用 `\\n`，与导出时一致。
    fieldsEnclosedBy 留空（csv.QUOTE_MINIMAL，只在值含分隔符或换行时引号包裹）。
    """
    # core fields — 从第 1 列开始（第 0 列是 id）
    core_field_xml: list[str] = []
    for i, (term, _, _) in enumerate(_OCCURRENCE_FIELDS):
        if i == 0:
            continue  # occurrenceID 由 <id index="0"/> 声明，不重复
        core_field_xml.append(f'      <field index="{i}" term="{DWC_TERM_BASE}{term}"/>')

    # multimedia fields — index 0 是 coreid，term 从 index 1 开始
    mm_field_xml: list[str] = []
    for i, (term, _) in enumerate(_MULTIMEDIA_FIELDS, start=1):
        mm_field_xml.append(f'      <field index="{i}" term="{AC_TERM_BASE}{term}"/>')

    core_fields_str = "\n".join(core_field_xml)
    mm_fields_str = "\n".join(mm_field_xml)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">
  <core encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="{DWC_TERM_BASE}Occurrence">
    <files>
      <location>occurrence.txt</location>
    </files>
    <id index="0"/>
{core_fields_str}
  </core>
  <extension encoding="UTF-8" fieldsTerminatedBy="\\t" linesTerminatedBy="\\n" fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="{AC_TERM_BASE}Multimedia">
    <files>
      <location>multimedia.txt</location>
    </files>
    <coreid index="0"/>
{mm_fields_str}
  </extension>
</archive>
"""


def _build_eml_xml(
    store: ExcelStore,
    title: str,
    creator: str,
    voucher_count: int,
    photo_count: int,
) -> str:
    """精简版 EML 2.2.0 — GBIF 要求的 dataset 元数据。"""
    now = datetime.now().isoformat(timespec="seconds")
    workspace_id = str(store.config.get("workspace_id", "") or uuid.uuid4())
    abstract = (
        f"标本入库管理工作区导出。包含 {voucher_count} 条 occurrence 记录、{photo_count} 张照片。"
        f"由 specimen-organise v{__version__} 于 {now} 生成。字段映射详见随附 meta.xml。"
    )
    creator_block = ""
    if creator:
        creator_block = f"""
    <creator>
      <individualName>
        <surName>{escape(creator)}</surName>
      </individualName>
    </creator>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         packageId="{escape(workspace_id)}" system="specimen-organise" xml:lang="zh">
  <dataset>
    <title xml:lang="zh">{escape(title)}</title>{creator_block}
    <pubDate>{now[:10]}</pubDate>
    <language>zh</language>
    <abstract>
      <para>{escape(abstract)}</para>
    </abstract>
    <intellectualRights>
      <para>本数据由 specimen-organise 工作区导出。版权归原标本馆藏所有者，使用前请联系。</para>
    </intellectualRights>
  </dataset>
</eml:eml>
"""
