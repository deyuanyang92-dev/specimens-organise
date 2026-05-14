"""共享界面主题：调色板常量 + 全局 QSS + 应用入口。

背景：本 app 原本没有任何全局 QSS / palette / setStyle，外观全靠各处零散的
`setStyleSheet` 调用，右键菜单等沿用 Qt 系统默认样式，显得粗糙。本模块抽出一套
统一的浅色清爽主题（参考 Geneious / PhyloSuite 观感），由 `run_app()` 在
`QApplication` 级别统一应用。

重要约束：
- **QSS 绝不写 font / font-size 规则** —— 字体由 `ui.apply_app_font_size()`
  (`QApplication.setFont`) 与 `voucher_table` 的 `_refresh_scaled_fonts()` 负责，
  主题只管颜色 / 边框 / 圆角 / 内边距 / 交互态，避免和字体缩放系统打架。
- 各处已有的 widget 级 `setStyleSheet` 保留 —— widget 级样式优先级高于全局
  QSS，仍照旧生效，不冲突。
"""

from __future__ import annotations

# ---- 调色板（合并原先散落在 ui.py / batch_export.py 的 hex，命名语义化）----
ACCENT = "#2a6fbd"          # 主强调色：选中、focus、主按钮
ACCENT_DARK = "#1a5faa"     # 深强调色：pressed、图标渐变
ACCENT_LIGHT = "#e8f0fe"    # 浅强调色：菜单 hover、轻高亮底
TEXT = "#263238"            # 正文文字
TEXT_DIM = "#59666b"        # 次要 / 提示文字
BORDER = "#c2cad3"          # 常规边框
BORDER_STRONG = "#a9b3bd"   # 强调边框 / 分隔
BG_WINDOW = "#f4f6f8"       # 窗口背景
BG_PANEL = "#eef2f3"        # 面板 / 表头背景
BG_BASE = "#ffffff"         # 输入框 / 菜单 / 表格基底
SEL_BG = "#d8ebff"          # 表格行选中底色

# ---- 全局样式表 ----
# 只设颜色 / 边框 / 圆角 / 内边距 / hover-pressed-selected 态，不碰字体。
APP_QSS = f"""
QMainWindow, QDialog {{
    background: {BG_WINDOW};
}}

/* 右键菜单：圆角、item 内边距、hover 高亮、分隔线 —— 美化重点 */
QMenu {{
    background: {BG_BASE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 22px 5px 14px;
    border-radius: 4px;
    color: {TEXT};
}}
QMenu::item:selected {{
    background: {ACCENT_LIGHT};
    color: {ACCENT_DARK};
}}
QMenu::item:disabled {{
    color: {BORDER_STRONG};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

/* 按钮：浅灰底、1px 边框、圆角、舒适 padding */
QPushButton {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 12px;
    color: {TEXT};
}}
QPushButton:hover {{
    background: {ACCENT_LIGHT};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background: {BORDER};
}}
QPushButton:disabled {{
    color: {BORDER_STRONG};
    background: {BG_WINDOW};
    border-color: {BORDER};
}}

/* 输入类控件：边框 + 圆角，focus 时边框变强调色 */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {BG_BASE};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px 4px;
    selection-background-color: {ACCENT};
    selection-color: {BG_BASE};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}

/* 表格与表头 */
QHeaderView::section {{
    background: {BG_PANEL};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    color: {TEXT};
}}
QTableView, QTableWidget {{
    background: {BG_BASE};
    gridline-color: {BORDER};
    selection-background-color: {SEL_BG};
    selection-color: {TEXT};
    border: 1px solid {BORDER};
}}
QTableView::item:selected, QTableWidget::item:selected {{
    background: {SEL_BG};
    color: {TEXT};
}}

/* 工具栏 */
QToolBar {{
    background: {BG_PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    spacing: 4px;
    padding: 2px;
}}

/* 分组框 */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 4px;
    color: {TEXT_DIM};
}}

/* 标签页 */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QTabBar::tab {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 4px 12px;
}}
QTabBar::tab:selected {{
    background: {BG_BASE};
    color: {ACCENT_DARK};
}}

/* 滚动条：细化、圆角 handle */
QScrollBar:vertical {{
    background: {BG_WINDOW};
    width: 12px;
    margin: 0;
}}
QScrollBar:horizontal {{
    background: {BG_WINDOW};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-width: 24px;
    min-height: 24px;
}}
QScrollBar::handle:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
"""


def apply_app_theme(app) -> None:
    """把全局主题 QSS 应用到 QApplication（在 run_app 建好 app 后调用一次）。"""
    if app is None:
        return
    app.setStyleSheet(APP_QSS)
