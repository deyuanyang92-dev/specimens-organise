"""工具栏内容自定义对话框（规范化软件设计 2026-05 新增）。

三列布局：
- 左列「可用 action」：TOOLBAR_ACTIONS 全集减去已挂的；按 category 排序
- 中列「主工具栏」：settings.toolbar_layout["main"]，可上下排
- 右列「辅助工具栏」：settings.toolbar_layout["aux"]，可上下排

操作：
- 双击列表项 → 切换列（双击左 → 进主栏；双击中/右 → 回左）
- 按钮：→ 主栏 / → 辅栏 / ← 移除 / ↑ 上移 / ↓ 下移 / 重置默认 / 保存 / 取消

保存后写 settings.toolbar_layout，并调主窗口的 `_rebuild_toolbars()` 热更新。

依赖：`specimen_app.ui.TOOLBAR_ACTIONS` / `TOOLBAR_DEFAULT_LAYOUT` 注册表。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .app_settings import load_settings, save_settings


def _action_id_to_label(action_id: str) -> str:
    """通过 TOOLBAR_ACTIONS 把 id 翻成显示文字；未知 id 直接返回 id 本身。"""
    from .ui import TOOLBAR_ACTIONS  # 运行时 import 避免循环
    spec = TOOLBAR_ACTIONS.get(action_id)
    return spec["label"] if spec else action_id


def _make_item(action_id: str) -> QListWidgetItem:
    item = QListWidgetItem(_action_id_to_label(action_id))
    item.setData(Qt.UserRole, action_id)
    item.setToolTip(action_id)
    return item


class ToolbarCustomizeDialog(QDialog):
    """三列拖拽 + 按钮迁移的工具栏内容自定义对话框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自定义工具栏")
        self.resize(820, 500)
        self._parent_window = parent  # 保留引用以便保存后调 _rebuild_toolbars()
        self._build_ui()
        self._populate_from_settings()

    # ---- UI ----
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            "<b>自定义工具栏</b>　把高频按钮放主栏，低频按钮放辅栏（视图菜单可切换辅栏可见性）。"
            "双击或用中间按钮移动；上/下按钮调顺序。"
        ))

        body = QHBoxLayout()

        # 左列：可用
        left = QVBoxLayout()
        left.addWidget(QLabel("可用 action"))
        self._lst_avail = QListWidget()
        self._lst_avail.setSelectionMode(QAbstractItemView.SingleSelection)
        self._lst_avail.itemDoubleClicked.connect(lambda _: self._move(self._lst_avail, self._lst_main))
        left.addWidget(self._lst_avail, 1)
        body.addLayout(left, 1)

        # 中列：主栏
        mid = QVBoxLayout()
        mid.addWidget(QLabel("主工具栏（顺序）"))
        self._lst_main = QListWidget()
        self._lst_main.setSelectionMode(QAbstractItemView.SingleSelection)
        self._lst_main.itemDoubleClicked.connect(lambda _: self._move(self._lst_main, self._lst_avail))
        mid.addWidget(self._lst_main, 1)
        body.addLayout(mid, 1)

        # 右列：辅栏
        right = QVBoxLayout()
        right.addWidget(QLabel("辅助工具栏（顺序）"))
        self._lst_aux = QListWidget()
        self._lst_aux.setSelectionMode(QAbstractItemView.SingleSelection)
        self._lst_aux.itemDoubleClicked.connect(lambda _: self._move(self._lst_aux, self._lst_avail))
        right.addWidget(self._lst_aux, 1)
        body.addLayout(right, 1)

        outer.addLayout(body, 1)

        # 操作按钮排
        ops = QHBoxLayout()
        for label, fn in [
            ("→ 主栏", lambda: self._move(self._lst_avail, self._lst_main)),
            ("→ 辅栏", lambda: self._move(self._lst_avail, self._lst_aux)),
            ("← 移除（主→可用）", lambda: self._move(self._lst_main, self._lst_avail)),
            ("← 移除（辅→可用）", lambda: self._move(self._lst_aux, self._lst_avail)),
            ("↑ 上移", lambda: self._shift_selected(-1)),
            ("↓ 下移", lambda: self._shift_selected(+1)),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            ops.addWidget(btn)
        outer.addLayout(ops)

        # 底部：重置 / 保存 / 取消
        foot = QHBoxLayout()
        btn_reset = QPushButton("重置为默认")
        btn_reset.clicked.connect(self._reset_to_default)
        foot.addWidget(btn_reset)
        foot.addStretch()
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_save.setDefault(True)
        foot.addWidget(btn_save)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        foot.addWidget(btn_cancel)
        outer.addLayout(foot)

    # ---- 数据填充 / 收集 ----
    def _populate_from_settings(self) -> None:
        from .ui import TOOLBAR_ACTIONS, TOOLBAR_DEFAULT_LAYOUT
        layout = load_settings().toolbar_layout or {}
        main_ids = layout.get("main") or list(TOOLBAR_DEFAULT_LAYOUT["main"])
        aux_ids = layout.get("aux") or list(TOOLBAR_DEFAULT_LAYOUT["aux"])
        # 过滤未知 id（向后兼容）
        main_ids = [aid for aid in main_ids if aid in TOOLBAR_ACTIONS]
        aux_ids = [aid for aid in aux_ids if aid in TOOLBAR_ACTIONS]
        used = set(main_ids) | set(aux_ids)
        avail = [aid for aid in TOOLBAR_ACTIONS.keys() if aid not in used]

        for lst, ids in (
            (self._lst_avail, avail),
            (self._lst_main, main_ids),
            (self._lst_aux, aux_ids),
        ):
            lst.clear()
            for aid in ids:
                lst.addItem(_make_item(aid))

    def _collect_layout(self) -> dict:
        def _ids(lst: QListWidget) -> list[str]:
            return [lst.item(i).data(Qt.UserRole) for i in range(lst.count())]
        return {"main": _ids(self._lst_main), "aux": _ids(self._lst_aux)}

    # ---- 列表迁移 / 排序 ----
    def _move(self, src: QListWidget, dst: QListWidget) -> None:
        item = src.currentItem()
        if item is None:
            return
        aid = item.data(Qt.UserRole)
        src.takeItem(src.row(item))
        dst.addItem(_make_item(aid))

    def _shift_selected(self, delta: int) -> None:
        for lst in (self._lst_main, self._lst_aux, self._lst_avail):
            item = lst.currentItem()
            if item is None:
                continue
            row = lst.row(item)
            new_row = row + delta
            if not (0 <= new_row < lst.count()):
                return
            lst.takeItem(row)
            lst.insertItem(new_row, item)
            lst.setCurrentItem(item)
            return

    def _reset_to_default(self) -> None:
        from .ui import TOOLBAR_DEFAULT_LAYOUT
        ret = QMessageBox.question(
            self, "重置确认",
            "重置工具栏到默认布局？当前自定义会被丢弃。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        # 写空 dict 让 _rebuild_toolbars 回落到默认；UI 立即按默认填充
        settings = load_settings()
        settings.toolbar_layout = {}
        save_settings(settings)
        self._populate_from_settings()

    # ---- 保存 ----
    def _save(self) -> None:
        new_layout = self._collect_layout()
        settings = load_settings()
        settings.toolbar_layout = new_layout
        save_settings(settings)
        # 通知主窗口热更新
        parent = self._parent_window
        if parent is not None and hasattr(parent, "_rebuild_toolbars"):
            try:
                parent._rebuild_toolbars()
            except Exception as exc:
                QMessageBox.warning(
                    self, "重建失败",
                    f"工具栏重建失败（已保存设置，重启应用即可生效）：{exc}",
                )
        self.accept()
