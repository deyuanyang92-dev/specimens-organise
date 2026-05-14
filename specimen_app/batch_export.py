"""批量导出功能：类似 NCBI Batch Entrez，粘贴入库编号后一键导出标本数据与照片。

使用方式：
- 从凭证列表多选后右键 →「批量导出选中」
- 或点击工具栏「批量导出」按钮，在对话框中手动粘贴编号

修改记录（# 注释保留原有逻辑，注明变更原因与兼容性）。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from .excel_store import ExcelStore

# Excel 表头样式
_HEADER_FONT = Font(bold=True, size=11)
_HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
_HEADER_ALIGNMENT = Alignment(horizontal="center")

# 照片导出格式：「保持原格式」= 原样 shutil.copy2（旧行为，兼容）；其余用 Pillow 重新编码。
PHOTO_EXPORT_FORMATS = ("保持原格式", "JPG", "PNG", "TIFF")
_FORMAT_EXT = {"JPG": ".jpg", "PNG": ".png", "TIFF": ".tif"}


def _parse_voucher_numbers(text: str) -> list[str]:
    """从文本中解析入库编号列表。

    支持换行、逗号、空格、分号、中文逗号作为分隔符。
    返回去重并保持顺序的编号列表。
    """
    # 统一替换各种分隔符为换行
    cleaned = text.replace(",", "\n").replace("，", "\n")
    cleaned = cleaned.replace(";", "\n").replace("；", "\n")
    cleaned = cleaned.replace(" ", "\n")
    parts = [p.strip() for p in cleaned.split("\n")]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def _write_header_row(ws, headers: list[str]) -> None:
    """写入带样式的表头行。"""
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT


def _auto_width(ws, min_width: int = 8, max_width: int = 60) -> None:
    """自动调整列宽（根据内容）。"""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = None
        for cell in col_cells:
            if col_letter is None:
                col_letter = cell.column_letter
            if cell.value:
                # 中文字符按 2 个字符宽度计算
                text = str(cell.value)
                length = sum(2 if ord(c) > 127 else 1 for c in text)
                max_len = max(max_len, length)
        if col_letter:
            ws.column_dimensions[col_letter].width = max(min_width, min(max_len + 2, max_width))


class BatchExportDialog(QDialog):
    """批量导出对话框。

    支持从凭证列表多选带入编号，也可手动粘贴。
    可选择导出标本信息、分类信息、照片路径、照片文件，
    支持打包为 ZIP 方便分发。
    """

    def __init__(
        self,
        store: "ExcelStore",
        preselected: list[str] | None = None,
        parent=None,
        photo_focus: bool = False,
    ):
        # photo_focus=True：从「入库汇总 → 导出选中照片」入口进来，只关心照片文件，
        #   默认只勾「照片文件」、三个 Excel sheet 默认不勾，标题改「导出照片」。
        # photo_focus=False：工具栏/凭证列表右键的旧入口，保持原默认（旧行为不变）。
        super().__init__(parent)
        self.store = store
        self._photo_focus = photo_focus
        self.setWindowTitle("导出照片" if photo_focus else "批量导出")
        self.setMinimumSize(520, 480)

        layout = QVBoxLayout(self)

        # ---- 入库编号输入 ----
        layout.addWidget(QLabel("入库编号（支持换行 / 逗号 / 空格 / 分号分隔）："))
        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText("粘贴入库编号，每行一个...\n例如：\nYZZ000001\nYZZ000042")
        self._text_edit.setMaximumHeight(120)
        if preselected:
            self._text_edit.setPlainText("\n".join(preselected))
        self._text_edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._text_edit)

        self._count_label = QLabel("已识别 0 个有效编号")
        self._count_label.setStyleSheet("color: #59666b; font-size: 11px;")
        layout.addWidget(self._count_label)

        # ---- 导出内容选项 ----
        options_group = QGroupBox("导出内容")
        options_layout = QVBoxLayout(options_group)

        # 旧逻辑：标本/分类/照片路径默认勾选、照片文件默认不勾。
        # photo_focus 入口只导照片：三个非照片选项隐藏 + 取消勾选（控件仍创建，
        # _do_export 仍按 .isChecked() 引用、判断逻辑不动；隐藏行不占布局空间）。
        self._chk_specimen = QCheckBox("标本信息")
        self._chk_specimen.setChecked(not photo_focus)
        options_layout.addWidget(self._chk_specimen)

        self._chk_classification = QCheckBox("分类信息")
        self._chk_classification.setChecked(not photo_focus)
        options_layout.addWidget(self._chk_classification)

        self._chk_photo_paths = QCheckBox("照片路径清单")
        self._chk_photo_paths.setChecked(not photo_focus)
        options_layout.addWidget(self._chk_photo_paths)

        for _chk in (self._chk_specimen, self._chk_classification, self._chk_photo_paths):
            _chk.setVisible(not photo_focus)

        self._chk_photo_files = QCheckBox("照片文件（复制到导出目录）")
        self._chk_photo_files.setChecked(photo_focus)
        options_layout.addWidget(self._chk_photo_files)

        # ZIP 打包选项（仅当勾选照片文件时可用）
        zip_row = QHBoxLayout()
        zip_row.setContentsMargins(24, 0, 0, 0)
        self._chk_zip = QCheckBox("打包为 ZIP")
        self._chk_zip.setToolTip("将所有导出内容打包为一个 ZIP 文件，方便分享")
        self._chk_photo_files.toggled.connect(
            lambda checked: self._chk_zip.setEnabled(checked)
        )
        self._chk_zip.setEnabled(self._chk_photo_files.isChecked())
        zip_row.addWidget(self._chk_zip)
        zip_row.addStretch()
        options_layout.addLayout(zip_row)

        layout.addWidget(options_group)

        # ---- 照片导出格式 / 压缩 ----
        # 原代码照片只能原样复制；这里新增格式转换 + 压缩，整组仅在勾选「照片文件」时启用。
        fmt_group = QGroupBox("照片导出格式 / 压缩")
        fmt_layout = QVBoxLayout(fmt_group)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("导出格式："))
        self._photo_format = QComboBox()
        self._photo_format.addItems(PHOTO_EXPORT_FORMATS)
        self._photo_format.setToolTip("「保持原格式」= 原样复制；其余用 Pillow 重新编码导出")
        self._photo_format.currentIndexChanged.connect(self._sync_photo_format_enabled)
        fmt_row.addWidget(self._photo_format)
        fmt_row.addStretch()
        fmt_layout.addLayout(fmt_row)

        quality_row = QHBoxLayout()
        self._quality_label = QLabel("JPEG 质量：")
        quality_row.addWidget(self._quality_label)
        self._quality_slider = QSlider(Qt.Horizontal)
        self._quality_slider.setRange(1, 100)
        self._quality_slider.setValue(90)
        self._quality_value = QLabel("90")
        self._quality_value.setFixedWidth(28)
        self._quality_slider.valueChanged.connect(
            lambda v: self._quality_value.setText(str(v))
        )
        quality_row.addWidget(self._quality_slider, stretch=1)
        quality_row.addWidget(self._quality_value)
        fmt_layout.addLayout(quality_row)

        resize_row = QHBoxLayout()
        self._chk_resize = QCheckBox("限制最大边长")
        self._chk_resize.setToolTip("超过该像素的照片等比缩小（不放大）；TIFF 原图始终不变")
        self._chk_resize.toggled.connect(self._sync_photo_format_enabled)
        resize_row.addWidget(self._chk_resize)
        self._resize_spin = QSpinBox()
        self._resize_spin.setRange(100, 100000)
        self._resize_spin.setValue(4000)
        self._resize_spin.setSuffix(" px")
        resize_row.addWidget(self._resize_spin)
        resize_row.addStretch()
        fmt_layout.addLayout(resize_row)

        layout.addWidget(fmt_group)

        # 「照片文件」勾选状态联动整组启用（与 ZIP 选项同一个信号源）。
        self._chk_photo_files.toggled.connect(self._sync_photo_format_enabled)
        self._sync_photo_format_enabled()

        # ---- 输出目录 ----
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("输出目录："))
        self._output_path = QLabel("（请选择目录）")
        self._output_path.setStyleSheet("color: #59666b;")
        output_row.addWidget(self._output_path, stretch=1)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_output_dir)
        output_row.addWidget(browse_btn)
        layout.addLayout(output_row)

        # ---- 按钮 ----
        buttons = QDialogButtonBox()
        export_btn = buttons.addButton("导出", QDialogButtonBox.AcceptRole)
        export_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 4px 16px; }")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._do_export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_text_changed()

    # ---- 事件处理 ----

    def _on_text_changed(self) -> None:
        """更新已识别编号计数。"""
        vouchers = _parse_voucher_numbers(self._text_edit.toPlainText())
        valid = [v for v in vouchers if self.store.get_specimen(v)]
        self._count_label.setText(
            f"已识别 {len(vouchers)} 个编号（{len(valid)} 个有效，"
            f"{len(vouchers) - len(valid)} 个不存在）"
        )

    def _browse_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if directory:
            self._output_path.setText(directory)
            self._output_path.setStyleSheet("color: #000;")

    def _sync_photo_format_enabled(self) -> None:
        """照片格式/压缩组的联动启用：仅勾「照片文件」时可用；质量条仅 JPG 可用；
        最大边长仅在格式≠保持原格式时可用、数值框再随勾选启用。"""
        photo_on = self._chk_photo_files.isChecked()
        fmt = self._photo_format.currentText()
        self._photo_format.setEnabled(photo_on)
        is_jpg = photo_on and fmt == "JPG"
        self._quality_label.setEnabled(is_jpg)
        self._quality_slider.setEnabled(is_jpg)
        self._quality_value.setEnabled(is_jpg)
        can_resize = photo_on and fmt != "保持原格式"
        self._chk_resize.setEnabled(can_resize)
        self._resize_spin.setEnabled(can_resize and self._chk_resize.isChecked())

    # ---- 导出逻辑 ----

    def _do_export(self) -> None:
        """执行批量导出。

        流程：解析编号 → 验证勾选 → 创建输出目录 → 逐项导出 → 可选打包 ZIP。
        所有 `#` 注释说明每一步的设计意图与兼容性考虑。
        """
        # 1. 解析入库编号
        vouchers = _parse_voucher_numbers(self._text_edit.toPlainText())
        if not vouchers:
            QMessageBox.warning(self, "无编号", "请粘贴或输入至少一个入库编号。")
            return

        # 过滤不存在的编号
        valid_vouchers: list[str] = []
        missing: list[str] = []
        for v in vouchers:
            if self.store.get_specimen(v):
                valid_vouchers.append(v)
            else:
                missing.append(v)

        if not valid_vouchers:
            QMessageBox.warning(self, "无有效编号", "输入的所有编号均不存在于当前工作区。")
            return

        # 2. 验证至少勾选一个导出项
        if not any([
            self._chk_specimen.isChecked(),
            self._chk_classification.isChecked(),
            self._chk_photo_paths.isChecked(),
            self._chk_photo_files.isChecked(),
        ]):
            QMessageBox.warning(self, "未选择内容", "请至少勾选一项要导出的内容。")
            return

        # 3. 确定输出目录
        out_dir_text = self._output_path.text().strip()
        if out_dir_text == "（请选择目录）":
            QMessageBox.warning(self, "未选择目录", "请选择导出目录。")
            return

        out_dir = Path(out_dir_text)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_dir = out_dir / f"导出_{timestamp}"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "创建目录失败", f"无法创建导出目录：{e}")
            return

        # 4. 逐项导出
        errors: list[str] = []
        photo_count = 0

        try:
            # 写入合并 Excel 文件
            wb = Workbook()
            # 删除默认空白 sheet（openpyxl 新工作簿自带 "Sheet"）
            wb.remove(wb.active)

            if self._chk_specimen.isChecked():
                self._export_specimen_sheet(wb, valid_vouchers, errors)

            if self._chk_classification.isChecked():
                self._export_classification_sheet(wb, valid_vouchers, errors)

            if self._chk_photo_paths.isChecked():
                self._export_photo_paths_sheet(wb, valid_vouchers, errors)

            if wb.sheetnames:
                xlsx_path = export_dir / "导出汇总.xlsx"
                wb.save(xlsx_path)
            else:
                wb.close()

            # 复制照片文件
            if self._chk_photo_files.isChecked():
                photo_count = self._export_photo_files(valid_vouchers, export_dir, errors)

        except Exception as exc:
            QMessageBox.critical(self, "导出失败", f"导出过程中出现异常：{exc}")
            return

        # 5. 可选 ZIP 打包
        final_path = export_dir
        if self._chk_photo_files.isChecked() and self._chk_zip.isChecked():
            zip_base = str(export_dir)
            try:
                final_zip = shutil.make_archive(zip_base, "zip", export_dir.parent, export_dir.name)
                # 打包成功后清理原目录
                shutil.rmtree(export_dir)
                final_path = Path(final_zip)
            except Exception as exc:
                errors.append(f"ZIP 打包失败：{exc}")

        # 6. 结果反馈
        missing_warning = ""
        if missing:
            missing_warning = f"\n\n跳过的无效编号：{', '.join(missing[:10])}"
            if len(missing) > 10:
                missing_warning += f" ...等共 {len(missing)} 个"

        error_warning = ""
        if errors:
            error_warning = f"\n\n警告：\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                error_warning += f"\n...等共 {len(errors)} 个问题"

        QMessageBox.information(
            self,
            "导出完成",
            f"已成功导出 {len(valid_vouchers)} 个标本。\n"
            f"输出位置：{final_path}\n"
            f"导出照片：{photo_count} 张"
            + missing_warning
            + error_warning,
        )
        self.accept()

    def _export_specimen_sheet(self, wb: Workbook, vouchers: list[str], errors: list[str]) -> None:
        """导出标本信息 sheet。"""
        from .models import SPECIMEN_HEADERS

        ws = wb.create_sheet("标本信息")
        _write_header_row(ws, SPECIMEN_HEADERS)
        for row_idx, voucher in enumerate(vouchers, 2):
            specimen = self.store.get_specimen(voucher) or {}
            for col_idx, header in enumerate(SPECIMEN_HEADERS, 1):
                ws.cell(row=row_idx, column=col_idx, value=str(specimen.get(header, "")))
        _auto_width(ws)

    def _export_classification_sheet(self, wb: Workbook, vouchers: list[str], errors: list[str]) -> None:
        """导出分类信息 sheet。"""
        from .models import CLASSIFICATION_HEADERS

        ws = wb.create_sheet("分类信息")
        _write_header_row(ws, CLASSIFICATION_HEADERS)
        for row_idx, voucher in enumerate(vouchers, 2):
            classification = self.store.get_classification(voucher) or {}
            for col_idx, header in enumerate(CLASSIFICATION_HEADERS, 1):
                ws.cell(row=row_idx, column=col_idx, value=str(classification.get(header, "")))
        _auto_width(ws)

    def _export_photo_paths_sheet(self, wb: Workbook, vouchers: list[str], errors: list[str]) -> None:
        """导出照片路径清单 sheet。

        每行列出：入库编号、文件名、原始文件名、相对路径、绝对路径（解析后）。
        原代码无此功能；这里新增为独立 sheet，供用户核对照片位置。
        """
        headers = ["入库编号", "文件名", "原始文件名", "相对路径", "绝对路径", "文件存在"]
        ws = wb.create_sheet("照片路径")
        _write_header_row(ws, headers)
        row_idx = 2
        for voucher in vouchers:
            photos = self.store.get_photos(voucher)
            for photo in photos:
                resolved = self.store.resolve_photo_path(photo)
                ws.cell(row=row_idx, column=1, value=voucher)
                ws.cell(row=row_idx, column=2, value=str(photo.get("文件名", "")))
                ws.cell(row=row_idx, column=3, value=str(photo.get("原始文件名", "")))
                ws.cell(row=row_idx, column=4, value=str(photo.get("相对路径", "")))
                ws.cell(row=row_idx, column=5, value=str(resolved))
                ws.cell(row=row_idx, column=6, value="是" if resolved.exists() else "否")
                row_idx += 1
        _auto_width(ws)

    def _export_photo_files(self, vouchers: list[str], export_dir: Path, errors: list[str]) -> int:
        """导出照片文件到导出目录的 /照片 子文件夹。

        旧逻辑：一律 shutil.copy2 原样复制。保留为「保持原格式」分支（兼容）。
        新增：选 JPG/PNG/TIFF 时用 Pillow 重新编码（可调质量 + 可选等比缩放），
              dest 扩展名随目标格式变；原始照片 / 工作区归档副本只读取、不改动。
        处理同名冲突：使用 _2, _3 后缀。返回成功导出的照片数量。
        """
        photo_dir = export_dir / "照片"
        photo_dir.mkdir(exist_ok=True)
        count = 0
        seen_names: dict[str, int] = {}

        fmt = self._photo_format.currentText()
        quality = self._quality_slider.value()
        max_edge = self._resize_spin.value() if self._chk_resize.isChecked() else 0

        for voucher in vouchers:
            photos = self.store.get_photos(voucher)
            for photo in photos:
                resolved = self.store.resolve_photo_path(photo)
                if not resolved.exists():
                    continue
                filename = str(photo.get("文件名", "")) or resolved.name
                # 转格式时把扩展名换成目标格式（「保持原格式」不动，等同旧行为）。
                if fmt != "保持原格式":
                    stem, _ext = os.path.splitext(filename)
                    filename = f"{stem}{_FORMAT_EXT[fmt]}"
                # 处理同名冲突（基于最终文件名）
                if filename in seen_names:
                    seen_names[filename] += 1
                    stem, ext = os.path.splitext(filename)
                    dest_name = f"{stem}_{seen_names[filename]}{ext}"
                else:
                    seen_names[filename] = 1
                    dest_name = filename

                dest = photo_dir / dest_name
                try:
                    if fmt == "保持原格式":
                        shutil.copy2(resolved, dest)  # 原分支：原样复制
                    else:
                        self._reencode_photo(resolved, dest, fmt, quality, max_edge)
                    count += 1
                except Exception as exc:  # Pillow 解码/编码可能抛多种异常，逐张兜底不中断
                    errors.append(f"导出照片失败 [{filename}]：{exc}")

        return count

    @staticmethod
    def _reencode_photo(src: Path, dest: Path, fmt: str, quality: int, max_edge: int) -> None:
        """用 Pillow 把 src 重新编码为目标格式写到 dest。

        max_edge>0 时按最大边长等比缩小（不放大）。TIFF 等多页/特殊图先靠 Pillow
        原生解码；解不开会抛异常，由调用方逐张兜底记入 errors。
        """
        from PIL import Image, ImageOps

        with Image.open(src) as opened:
            img = ImageOps.exif_transpose(opened)  # 按 EXIF 摆正方向
            if max_edge > 0:
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            if fmt == "JPG":
                img.convert("RGB").save(dest, "JPEG", quality=quality, optimize=True)
            elif fmt == "PNG":
                img.save(dest, "PNG")
            else:  # TIFF
                img.save(dest, "TIFF")
