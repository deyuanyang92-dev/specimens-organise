"""CSV / Excel 风格表格预览 widget (规范化软件设计 2026-05 入库人员管理 Phase 2 抽出)。

公共复用:任何 dialog 想要 Excel 风格表格 (排序 / 筛选 / 复制 / 导出) 都嵌 SpreadsheetPreviewWidget。

功能:
- QTableWidget + 列头排序 (左键)
- 列头右键 → 隐藏列 / 显示所有列 / 自适应宽度 / 列筛选 (按列值快速过滤)
- Ctrl+C 复制选中行为 TSV (Excel 友好)
- Ctrl+A 全选
- 顶部:搜索框 (全列匹配)
- 底部按钮: [导出 Excel] [导出 CSV] [复制选中] [刷新]
- 数据 setter:set_data(columns, rows)
- 信号:row_double_clicked(int row_index)
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

from PyQt5.QtCore import Qt, QPoint, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QShortcut,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def sanitize_csv_value(val: Any) -> str:
    """规范化软件设计 2026-05 P2 审查修复:CSV 公式注入防御。
    Excel/LibreOffice 打开 CSV 时,首字 = / + / - / @ / Tab / CR 视为公式 → RCE 风险。
    在前缀加单引号 ' 让 Excel 当文本对待。
    """
    s = "" if val is None else str(val)
    if not s:
        return s
    if s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _fmt_duration(seconds: int) -> str:
    """秒 → 'XXh YYm' (workload report 复用)。"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "0m"
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class SpreadsheetPreviewWidget(QWidget):
    """csv / Excel 风格表格预览 widget。

    用法:
        w = SpreadsheetPreviewWidget()
        w.set_data(["列A", "列B"], [["a1", "b1"], ["a2", "b2"]])
        layout.addWidget(w)
    """

    row_double_clicked = pyqtSignal(int)  # 双击行

    def __init__(self, parent=None, *, show_export: bool = True, show_copy: bool = True,
                 show_search: bool = True) -> None:
        super().__init__(parent)
        self._columns: list[str] = []
        self._rows: list[list[Any]] = []
        self._show_export = show_export
        self._show_copy = show_copy
        self._show_search = show_search
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 顶部:搜索
        if self._show_search:
            search_row = QHBoxLayout()
            search_row.addWidget(QLabel("🔍 搜索:"))
            self._search = QLineEdit()
            self._search.setPlaceholderText("匹配任意列...")
            self._search.textChanged.connect(self._on_search_changed)
            search_row.addWidget(self._search, 1)
            layout.addLayout(search_row)
        else:
            self._search = None

        # 表格
        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.itemDoubleClicked.connect(self._on_double_clicked)
        # 列头右键
        header = self._table.horizontalHeader()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self._table, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        if self._show_copy:
            btn_copy = QPushButton("📋 复制选中")
            btn_copy.setToolTip("复制选中行为 TSV (Tab 分隔,Excel/记事本可粘贴)")
            btn_copy.clicked.connect(self.copy_selection)
            btn_row.addWidget(btn_copy)
        if self._show_export:
            btn_excel = QPushButton("📊 导出 Excel")
            btn_excel.clicked.connect(self._on_export_excel_clicked)
            btn_row.addWidget(btn_excel)
            btn_csv = QPushButton("📄 导出 CSV")
            btn_csv.clicked.connect(self._on_export_csv_clicked)
            btn_row.addWidget(btn_csv)
        btn_row.addStretch()
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet("color: #888;")
        btn_row.addWidget(self._info_lbl)
        layout.addLayout(btn_row)

        # Ctrl+C 快捷键
        QShortcut(QKeySequence.Copy, self._table, self.copy_selection)

    # ---- 数据 ----
    def set_data(self, columns: list[str], rows: list[list[Any]]) -> None:
        """填表。columns: 列名;rows: 每行数据 (list)。"""
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]
        self._table.setSortingEnabled(False)  # 填表时关排序避免错位
        self._table.clear()
        self._table.setColumnCount(len(self._columns))
        self._table.setHorizontalHeaderLabels(self._columns)
        self._table.setRowCount(len(self._rows))
        for r_idx, row in enumerate(self._rows):
            for c_idx, val in enumerate(row):
                text = "" if val is None else str(val)
                item = QTableWidgetItem(text)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    # 数字右对齐 + 排序数值
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    item.setData(Qt.UserRole, val)
                self._table.setItem(r_idx, c_idx, item)
        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()
        self._update_info()

    def _update_info(self) -> None:
        visible = sum(1 for i in range(self._table.rowCount())
                      if not self._table.isRowHidden(i))
        total = self._table.rowCount()
        if visible == total:
            self._info_lbl.setText(f"{total} 行")
        else:
            self._info_lbl.setText(f"{visible} / {total} 行")

    # ---- 复制 ----
    def copy_selection(self) -> None:
        """选中行复制为 TSV (Tab 分隔),Excel 友好。"""
        from PyQt5.QtWidgets import QApplication
        sel = self._table.selectedRanges()
        if not sel:
            return
        # 用 selectedRanges 拿所有选中区域,按行收集
        rows_set = set()
        for rng in sel:
            for r in range(rng.topRow(), rng.bottomRow() + 1):
                if not self._table.isRowHidden(r):
                    rows_set.add(r)
        if not rows_set:
            return
        lines = []
        # 列名头
        lines.append("\t".join(self._columns))
        for r in sorted(rows_set):
            cells = []
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))
        self._info_lbl.setText(f"已复制 {len(rows_set)} 行到剪贴板")

    # ---- 搜索 ----
    def _on_search_changed(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for r in range(self._table.rowCount()):
            if not needle:
                self._table.setRowHidden(r, False)
                continue
            hit = False
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                if item and needle in item.text().lower():
                    hit = True
                    break
            self._table.setRowHidden(r, not hit)
        self._update_info()

    # ---- 列头右键菜单 ----
    def _on_header_context_menu(self, pos: QPoint) -> None:
        header = self._table.horizontalHeader()
        col = header.logicalIndexAt(pos)
        menu = QMenu(self)
        if col >= 0:
            hide_act = menu.addAction(f"隐藏列「{self._columns[col]}」")
            hide_act.triggered.connect(lambda: self._table.setColumnHidden(col, True))
            menu.addSeparator()
            # 筛选 (列值快速过滤)
            filter_act = menu.addAction(f"按「{self._columns[col]}」列值筛选…")
            filter_act.triggered.connect(lambda: self._filter_by_column(col))
        show_all_act = menu.addAction("显示所有列")
        show_all_act.triggered.connect(self._show_all_columns)
        menu.addSeparator()
        fit_act = menu.addAction("列宽自适应")
        fit_act.triggered.connect(self._table.resizeColumnsToContents)
        menu.exec_(header.viewport().mapToGlobal(pos))

    def _show_all_columns(self) -> None:
        for c in range(self._table.columnCount()):
            self._table.setColumnHidden(c, False)

    def _filter_by_column(self, col: int) -> None:
        # 取该列的所有不同值,弹简单菜单让选
        values = sorted({
            (self._table.item(r, col).text() if self._table.item(r, col) else "")
            for r in range(self._table.rowCount())
        })
        if not values:
            return
        menu = QMenu(self)
        clear_act = menu.addAction("(清除筛选,显示全部)")
        clear_act.triggered.connect(lambda: self._apply_column_filter(col, None))
        menu.addSeparator()
        for v in values:
            label = v if v else "(空)"
            act = menu.addAction(label)
            act.triggered.connect(lambda checked=False, val=v: self._apply_column_filter(col, val))
        menu.exec_(self._table.mapToGlobal(QPoint(50, 50)))

    def _apply_column_filter(self, col: int, value: Optional[str]) -> None:
        for r in range(self._table.rowCount()):
            if value is None:
                self._table.setRowHidden(r, False)
                continue
            item = self._table.item(r, col)
            txt = item.text() if item else ""
            self._table.setRowHidden(r, txt != value)
        self._update_info()

    # ---- 导出 ----
    def _on_export_excel_clicked(self) -> None:
        path_s, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", "data.xlsx", "Excel 文件 (*.xlsx)"
        )
        if not path_s:
            return
        try:
            self.export_excel(Path(path_s))
            QMessageBox.information(self, "已导出", f"已导出 {len(self._visible_rows())} 行到\n{path_s}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def _on_export_csv_clicked(self) -> None:
        path_s, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV (UTF-8 BOM,Excel 可直接打开)", "data.csv", "CSV 文件 (*.csv)"
        )
        if not path_s:
            return
        try:
            self.export_csv(Path(path_s))
            QMessageBox.information(self, "已导出", f"已导出 {len(self._visible_rows())} 行到\n{path_s}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def _visible_rows(self) -> list[list[str]]:
        out = []
        for r in range(self._table.rowCount()):
            if self._table.isRowHidden(r):
                continue
            cells = []
            for c in range(self._table.columnCount()):
                if self._table.isColumnHidden(c):
                    continue
                item = self._table.item(r, c)
                cells.append(item.text() if item else "")
            out.append(cells)
        return out

    def _visible_columns(self) -> list[str]:
        return [self._columns[c] for c in range(self._table.columnCount())
                if not self._table.isColumnHidden(c)]

    def export_excel(self, path: Path) -> None:
        """导出当前可见(筛选+列可见)行到 Excel,UTF-8 中文 OK。
        P2 审查修复:每单元格走 sanitize_csv_value 防 Excel 公式注入。
        """
        from openpyxl import Workbook  # lazy
        wb = Workbook()
        try:
            ws = wb.active
            ws.title = "数据"
            ws.append([sanitize_csv_value(c) for c in self._visible_columns()])
            for row in self._visible_rows():
                ws.append([sanitize_csv_value(c) for c in row])
            wb.save(str(path))
        finally:
            try: wb.close()
            except Exception: pass

    def export_csv(self, path: Path) -> None:
        """导出当前可见行到 CSV (utf-8-sig BOM,Excel 中文 OK)。
        P2 审查修复:每单元格走 sanitize_csv_value 防 Excel 公式注入。
        """
        with open(str(path), "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([sanitize_csv_value(c) for c in self._visible_columns()])
            for row in self._visible_rows():
                writer.writerow([sanitize_csv_value(c) for c in row])

    # ---- 事件 ----
    def _on_double_clicked(self, item: QTableWidgetItem) -> None:
        self.row_double_clicked.emit(item.row())


class SpreadsheetPreviewDialog(QDialog):
    """SpreadsheetPreviewWidget 的独立弹窗包装。"""

    def __init__(self, columns: list[str], rows: list[list[Any]],
                 title: str = "表格预览", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        self.widget = SpreadsheetPreviewWidget(self)
        self.widget.set_data(columns, rows)
        layout.addWidget(self.widget, 1)
        # 底部:关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)
