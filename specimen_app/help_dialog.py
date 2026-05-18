"""帮助菜单的对话框集合（规范化软件设计 2026-05 新增）。

提供四个对话框：
- `UserManualDialog` —— 主使用说明（左侧 QTreeWidget 目录 + 右侧 QTextBrowser 渲染 markdown）
- `AboutDialog` —— 关于（版本/作者/构建信息/致谢/复制系统信息）
- `ShortcutsDialog` —— 快捷键速查（静态内容）
- `FieldHelpIndexDialog` —— 字段填写说明速查（读 field_help.load_field_help() 全表）

设计要点：
- Markdown 渲染走 `markdown` 库（纯 Python ~70KB）+ QTextBrowser。**不**引入 PyQtWebEngine，
  Qt 子集 HTML 已够用，体积涨幅 < 5MB。
- `manual_root()` 多根解析（仿 field_help.bundled_template_path）：源码 / PyInstaller `_MEIPASS` / `_internal/`。
- `UserManualDialog` 走单实例（详见 SpecimenWindow._manual_dialog），关闭即销毁。

依赖：`markdown>=3.4`（requirements.txt 已加）。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__


# ---------------------------------------------------------------
# 目录结构：(display_label, md_filename, anchor)
# anchor=None 表示打开文件顶部。markdown 库带 toc 扩展会自动生成中文标题锚点。
# ---------------------------------------------------------------
MANUAL_TOC: list[tuple[str, str, Optional[str]]] = [
    ("简介", "index.md", None),
    ("开始使用", "getting-started.md", None),
    ("工作区与数据", "workspace.md", None),
    ("入库编号", "voucher-numbers.md", None),
    ("编号系列管理", "accession-series.md", None),
    ("数据录入", "data-entry.md", None),
    ("照片管理", "photos.md", None),
    ("分类信息与 WoRMS", "classification.md", None),
    ("入库汇总", "ingest-summary.md", None),
    ("批量导出", "batch-export.md", None),
    ("Darwin Core 导出", "dwc-export.md", None),
    ("EXIF 回填", "exif-backfill.md", None),
    ("WoRMS 物种分类", "worms.md", None),
    ("数据版本快照", "version-snapshots.md", None),
    ("多人协作", "multi-user.md", None),
    ("合并 / 导入示例", "import-merge.md", None),
    ("软件更新", "update.md", None),
    ("故障排查", "troubleshooting.md", None),
    ("Linux 安装", "install-linux.md", None),
    ("快捷键速查", "shortcuts.md", None),
    ("关于", "about.md", None),
]


def manual_root() -> Optional[Path]:
    """定位 docs/manual/ 目录。源码 / PyInstaller frozen 都能找到，找不到返回 None。

    顺序：
    1. `sys._MEIPASS / "docs/manual"`（PyInstaller onefile）
    2. `Path(sys.executable).parent / "_internal/docs/manual"`（onedir frozen）
    3. `<repo>/docs/manual`（源码态）
    4. `Path.cwd() / "docs/manual"`（兜底）
    """
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "docs" / "manual")
        roots.append(Path(sys.executable).resolve().parent / "_internal" / "docs" / "manual")
    roots.append(Path(__file__).resolve().parent.parent / "docs" / "manual")
    roots.append(Path.cwd() / "docs" / "manual")
    for root in roots:
        if root.is_dir() and (root / "index.md").exists():
            return root
    # 兜底：只检查目录存在（即使 index.md 缺失，也允许打开其它章节）
    for root in roots:
        if root.is_dir():
            return root
    return None


def _render_markdown(md_text: str) -> str:
    """把 markdown 文本转 HTML（fenced_code/tables/toc/attr_list 扩展），失败回落为转义文本。"""
    try:
        import markdown  # 运行时依赖，requirements.txt 已加 markdown>=3.4
    except Exception:
        # markdown 未安装：把 md 当 plain text 显示，至少不崩。
        from html import escape
        return f"<pre>{escape(md_text)}</pre>"
    extensions = ["fenced_code", "tables", "toc", "attr_list", "sane_lists"]
    return markdown.markdown(md_text, extensions=extensions, output_format="html5")


# ---------------------------------------------------------------
# UserManualDialog
# ---------------------------------------------------------------
class UserManualDialog(QDialog):
    """主使用说明弹窗：左 QTreeWidget 目录 + 右 QTextBrowser 渲染 markdown。

    单实例：参考 SpecimenWindow._manual_dialog 引用，复用而非新建（见 ui._open_user_manual_dialog）。
    图片路径解析：通过 `QTextBrowser.setSearchPaths([md_path.parent])` 让相对路径图片可加载。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(1100, 720)
        self._root = manual_root()
        self._current_index = 0
        self._build_ui()
        self._populate_tree()
        # 默认打开 index.md
        if MANUAL_TOC:
            self._load_index(0)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # 左侧：搜索 + 目录树
        left = QVBoxLayout()
        left.setSpacing(4)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("过滤章节…")
        self._filter.textChanged.connect(self._on_filter_changed)
        left.addWidget(self._filter)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setFixedWidth(240)
        self._tree.currentItemChanged.connect(self._on_tree_item_changed)
        left.addWidget(self._tree, 1)
        layout.addLayout(left)

        # 右侧：渲染 + 底部按钮
        right = QVBoxLayout()
        right.setSpacing(4)
        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(True)
        # 允许 QTextBrowser 加载相对路径图片（每次 _load_index 时重置）
        right.addWidget(self._view, 1)

        btn_bar = QHBoxLayout()
        btn_prev = QPushButton("◀ 上一章")
        btn_prev.clicked.connect(self._go_prev)
        btn_next = QPushButton("下一章 ▶")
        btn_next.clicked.connect(self._go_next)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btn_bar.addWidget(btn_prev)
        btn_bar.addWidget(btn_next)
        btn_bar.addStretch()
        btn_bar.addWidget(btn_close)
        right.addLayout(btn_bar)
        layout.addLayout(right, 1)

    def _populate_tree(self) -> None:
        self._tree.clear()
        for idx, (label, _md, _anchor) in enumerate(MANUAL_TOC):
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, idx)
            self._tree.addTopLevelItem(item)

    def _on_filter_changed(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            visible = (not needle) or (needle in item.text(0).lower())
            item.setHidden(not visible)

    def _on_tree_item_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        idx = current.data(0, Qt.UserRole)
        if isinstance(idx, int):
            self._load_index(idx)

    def _load_index(self, idx: int) -> None:
        if not (0 <= idx < len(MANUAL_TOC)):
            return
        self._current_index = idx
        label, md_filename, anchor = MANUAL_TOC[idx]
        if self._root is None:
            self._view.setHtml(
                f"<h1>{label}</h1><p style='color:#888'>未找到 docs/manual/ 目录，"
                "请重装应用或拉取最新源码。</p>"
            )
            return
        md_path = self._root / md_filename
        if not md_path.exists():
            self._view.setHtml(
                f"<h1>{label}</h1><p style='color:#888'>此章节文件 <code>{md_filename}</code> "
                "尚未编写。</p>"
            )
            return
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            self._view.setHtml(f"<p>读取失败：{exc}</p>")
            return
        # 模板变量替换：{{version}} → __version__
        text = text.replace("{{version}}", __version__)
        html = _render_markdown(text)
        # 让 QTextBrowser 沿 md 所在目录解析相对路径图片（含 TODO_screenshots/）
        self._view.setSearchPaths([str(md_path.parent)])
        self._view.setHtml(html)
        if anchor:
            self._view.scrollToAnchor(anchor)
        # 同步左侧树选中（filter 触发时不更新避免循环）
        if self._tree.currentItem() is None or self._tree.currentItem().data(0, Qt.UserRole) != idx:
            top = self._tree.topLevelItem(idx)
            if top is not None:
                self._tree.setCurrentItem(top)

    def _go_prev(self) -> None:
        if self._current_index > 0:
            self._load_index(self._current_index - 1)

    def _go_next(self) -> None:
        if self._current_index < len(MANUAL_TOC) - 1:
            self._load_index(self._current_index + 1)

    def navigate_to(self, anchor: str) -> None:
        """供未来 deep-link 用：跳到指定锚点。本期不挂调用方，先留接口。"""
        self._view.scrollToAnchor(anchor)


# ---------------------------------------------------------------
# AboutDialog
# ---------------------------------------------------------------
class AboutDialog(QDialog):
    """关于对话框：版本号 / 作者 / 许可证 / 构建信息 / 致谢 / 复制系统信息。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("关于 标本入库管理")
        self.resize(560, 480)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        # 图标 + 标题
        head = QHBoxLayout()
        try:
            from .icon import get_app_icon
            from .app_settings import load_settings
            icon = get_app_icon(load_settings().app_icon_variant)
            pix: QPixmap = icon.pixmap(96, 96)
            icon_label = QLabel()
            icon_label.setPixmap(pix)
            head.addWidget(icon_label)
        except Exception:
            pass

        title_block = QVBoxLayout()
        title = QLabel(f"<h2 style='margin:0'>标本入库管理</h2>")
        version = QLabel(f"<p style='color:#666;margin:2px 0'>软件版本 v{__version__}</p>")
        tagline = QLabel(
            "<p style='color:#888;margin:2px 0'>PyQt5 桌面端 · 生物标本 Excel 数据管理</p>"
        )
        title_block.addWidget(title)
        title_block.addWidget(version)
        title_block.addWidget(tagline)
        title_block.addStretch()
        head.addLayout(title_block, 1)
        layout.addLayout(head)

        # 详细信息文本
        info = QTextBrowser()
        info.setOpenExternalLinks(True)
        info.setHtml(self._build_info_html())
        layout.addWidget(info, 1)

        # 按钮：复制系统信息 / 确定
        btn_bar = QHBoxLayout()
        btn_copy = QPushButton("复制系统信息")
        btn_copy.clicked.connect(self._copy_sys_info)
        btn_ok = QPushButton("确定")
        btn_ok.clicked.connect(self.accept)
        btn_ok.setDefault(True)
        btn_bar.addWidget(btn_copy)
        btn_bar.addStretch()
        btn_bar.addWidget(btn_ok)
        layout.addLayout(btn_bar)

    def _build_info_html(self) -> str:
        # 占位信息（首版可填空字符串，未来用户/开发者补）
        author = "标本入库管理 开发组"
        license_note = "保留所有权利 / 内部使用"
        homepage = ""  # TODO: 公开后填仓库 URL
        try:
            from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        except Exception:
            QT_VERSION_STR = PYQT_VERSION_STR = "?"
        py_ver = sys.version.split(" ", 1)[0]
        frozen = "PyInstaller 打包" if getattr(sys, "frozen", False) else "源码运行"
        platform_str = platform.platform()
        homepage_html = (
            f'<p><b>项目主页：</b><a href="{homepage}">{homepage}</a></p>'
            if homepage else "<p><b>项目主页：</b><i>（未公开）</i></p>"
        )
        return f"""
        <p><b>作者：</b>{author}</p>
        <p><b>许可证：</b>{license_note}</p>
        {homepage_html}
        <hr/>
        <p><b>构建信息：</b></p>
        <ul>
          <li>Python {py_ver}</li>
          <li>Qt {QT_VERSION_STR} / PyQt {PYQT_VERSION_STR}</li>
          <li>运行模式：{frozen}</li>
          <li>平台：{platform_str}</li>
        </ul>
        <hr/>
        <p><b>致谢：</b></p>
        <ul>
          <li><a href="https://www.qt.io/qt-for-python">PyQt5</a> — GUI 框架</li>
          <li><a href="https://openpyxl.readthedocs.io/">openpyxl</a> — Excel 读写</li>
          <li><a href="https://python-pillow.org/">Pillow</a> — 图像处理</li>
          <li><a href="https://github.com/cgohlke/tifffile">tifffile</a> — TIFF 解码</li>
          <li><a href="https://python-markdown.github.io/">markdown</a> — 文档渲染</li>
          <li><a href="https://www.marinespecies.org/aphia.php?p=webservice">WoRMS REST API</a> — 海洋物种分类</li>
          <li><a href="https://dwc.tdwg.org/">Darwin Core (TDWG)</a> — 生物多样性数据标准</li>
        </ul>
        """

    def _copy_sys_info(self) -> None:
        try:
            from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        except Exception:
            QT_VERSION_STR = PYQT_VERSION_STR = "?"
        lines = [
            f"标本入库管理 v{__version__}",
            f"Python {sys.version.split()[0]}",
            f"Qt {QT_VERSION_STR} / PyQt {PYQT_VERSION_STR}",
            f"Platform: {platform.platform()}",
            f"Frozen: {bool(getattr(sys, 'frozen', False))}",
        ]
        QApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(self, "已复制", "系统信息已复制到剪贴板，可贴到 Issue / 邮件中。")


# ---------------------------------------------------------------
# ShortcutsDialog
# ---------------------------------------------------------------
class ShortcutsDialog(QDialog):
    """快捷键速查（静态内容；与 ui.py 内 QShortcut/QAction 绑定保持同步）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("快捷键速查")
        self.resize(520, 480)
        layout = QVBoxLayout(self)
        view = QTextBrowser()
        view.setHtml(self._build_html())
        layout.addWidget(view, 1)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        btn_bar.addWidget(btn)
        layout.addLayout(btn_bar)

    @staticmethod
    def _build_html() -> str:
        rows = [
            ("Ctrl + A", "全选凭证（在入库编号列表内）"),
            ("Ctrl + Z", "撤回（操作记录回退）"),
            ("Ctrl + Y / Ctrl + Shift + Z", "重做"),
            ("Ctrl + =, Ctrl + +", "界面字体放大"),
            ("Ctrl + -", "界面字体缩小"),
            ("Ctrl + 0", "界面字体复位"),
            ("F", "照片适配窗口"),
            ("Esc", "返回照片网格"),
            ("F2", "编辑照片表当前单元格"),
            ("Ctrl + C", "复制照片表选中（在照片表内）"),
            ("（可自定义）", "从照片文件名填充标本信息（设置内修改快捷键）"),
        ]
        body = "".join(
            f"<tr><td style='padding:4px 12px;font-family:monospace'><b>{k}</b></td>"
            f"<td style='padding:4px 12px'>{v}</td></tr>"
            for k, v in rows
        )
        return f"""
        <h2>快捷键速查</h2>
        <p style='color:#888'>软件 v{__version__}。以下为当前版本可用快捷键。</p>
        <table border='0' cellspacing='0' style='border-collapse:collapse'>
          <thead><tr style='background:#f0f0f0'>
            <th style='padding:4px 12px;text-align:left'>快捷键</th>
            <th style='padding:4px 12px;text-align:left'>功能</th>
          </tr></thead>
          <tbody>{body}</tbody>
        </table>
        <hr/>
        <p style='color:#888;font-size:12px'>提示：右键菜单内的操作（如导出选中、批量设置标本信息）
        通常没有全局快捷键，请通过菜单或右键访问。</p>
        """


# ---------------------------------------------------------------
# FieldHelpIndexDialog
# ---------------------------------------------------------------
class FieldHelpIndexDialog(QDialog):
    """字段填写说明速查：表格展示 field_help.load_field_help() 全表。

    数据源：specimen_app/字段模版/数据录入字段及字段说明.xlsx Sheet2。
    列：字段名 / 示例 / 说明 / 其他要求。可搜索过滤。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("字段填写说明速查")
        self.resize(900, 600)
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header_html = (
            "<p>每个录入字段旁都有 <b>?</b> 按钮可单独查看说明；本列表汇总所有字段，便于扫读。<br/>"
            "<span style='color:#888'>数据源：</span><code>字段模版/数据录入字段及字段说明.xlsx</code> Sheet2</p>"
        )
        layout.addWidget(QLabel(header_html))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("按字段名 / 示例 / 说明过滤…")
        self._filter.textChanged.connect(self._on_filter_changed)
        layout.addWidget(self._filter)

        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["字段名", "示例", "说明", "其他要求"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        layout.addWidget(self._table, 1)

        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        btn_bar.addWidget(btn)
        layout.addLayout(btn_bar)

    def _populate(self) -> None:
        try:
            from .field_help import load_field_help
            data = load_field_help()
        except Exception as exc:
            data = {}
            QMessageBox.warning(self, "加载失败", f"字段说明加载失败：{exc}")
        rows = sorted(data.items(), key=lambda kv: kv[0])
        self._table.setRowCount(len(rows))
        for r, (name, info) in enumerate(rows):
            cells = [name, info.get("示例", ""), info.get("说明", ""), info.get("其他要求", "")]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setToolTip(val)
                self._table.setItem(r, c, item)
        if not rows:
            self._table.setRowCount(1)
            placeholder = QTableWidgetItem("（未加载到任何字段说明）")
            placeholder.setForeground(Qt.gray)
            self._table.setItem(0, 0, placeholder)
            self._table.setSpan(0, 0, 1, 4)

    def _on_filter_changed(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for r in range(self._table.rowCount()):
            visible = not needle
            if not visible:
                for c in range(self._table.columnCount()):
                    item = self._table.item(r, c)
                    if item and needle in item.text().lower():
                        visible = True
                        break
            self._table.setRowHidden(r, not visible)
