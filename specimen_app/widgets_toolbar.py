"""规范化软件设计 2026-05 Phase 5 — 菜单右键加快捷 + 工具栏 inline 拖换位。

公共 widget:
- ToolbarAwareMenu: QMenu 子类,菜单项右键弹"加主栏/辅栏/移除"小菜单 → 改
  settings.toolbar_layout + 调主窗口 _rebuild_toolbars 热更新。
- DraggableToolBar: QToolBar 子类,支持 action inline 拖换位 + 跨主/辅栏拖。

依赖:
- specimen_app.app_settings.load_settings / save_settings
- specimen_app.ui.TOOLBAR_ACTIONS (action_id 注册表)
- 主窗口需有 _rebuild_toolbars() 方法 (上轮 Phase 4 已加)
"""

from __future__ import annotations

from typing import Optional, Callable

from PyQt5.QtCore import Qt, QMimeData, QPoint, QByteArray
from PyQt5.QtGui import QDrag, QCursor
from PyQt5.QtWidgets import QAction, QMenu, QToolBar, QApplication


_MIME_TYPE = "application/x-toolbar-action-id"


def _action_id(action) -> Optional[str]:
    """从 QAction 取关联的 action_id (用 setData 或 setProperty 'action_id' 关联)。"""
    if action is None:
        return None
    data = action.data()
    if isinstance(data, str) and data:
        return data
    prop = action.property("action_id")
    if isinstance(prop, str) and prop:
        return prop
    return None


def _toolbar_layout() -> dict:
    """读 settings.toolbar_layout, 缺省回落 TOOLBAR_DEFAULT_LAYOUT。"""
    try:
        from .app_settings import load_settings
        layout = load_settings().toolbar_layout or {}
    except Exception:
        layout = {}
    from .ui import TOOLBAR_DEFAULT_LAYOUT
    main = list(layout.get("main") or TOOLBAR_DEFAULT_LAYOUT["main"])
    aux = list(layout.get("aux") or TOOLBAR_DEFAULT_LAYOUT["aux"])
    return {"main": main, "aux": aux}


def _save_toolbar_layout(layout: dict) -> None:
    try:
        from .app_settings import load_settings, save_settings
        s = load_settings()
        s.toolbar_layout = layout
        save_settings(s)
    except Exception:
        pass


def _find_main_window(widget):
    """向上找到 SpecimenWindow (主窗口) 以调 _rebuild_toolbars。"""
    w = widget
    seen = set()
    while w is not None and id(w) not in seen:
        seen.add(id(w))
        if hasattr(w, "_rebuild_toolbars") and callable(w._rebuild_toolbars):
            return w
        w = w.parent() if hasattr(w, "parent") else None
    return None


# ---- ToolbarAwareMenu: 菜单项右键加快捷 ----

class ToolbarAwareMenu(QMenu):
    """QMenu 子类:菜单项右键弹"加到主栏 / 辅栏 / 移除"。

    要求: 菜单项 QAction.setData(action_id) 关联 TOOLBAR_ACTIONS 注册表里的 key。
    无 action_id 的菜单项 (元操作如"自定义工具栏...") 不显右键菜单。
    """

    def contextMenuEvent(self, event):
        # 找鼠标位置对应的 action
        action = self.actionAt(event.pos())
        if action is None or action.isSeparator():
            super().contextMenuEvent(event)
            return
        action_id = _action_id(action)
        if not action_id:
            # 无 action_id 关联 — 当作普通菜单项,不弹右键
            super().contextMenuEvent(event)
            return
        # 检查 TOOLBAR_ACTIONS 是否含此 id (有则可加,没则忽略)
        try:
            from .ui import TOOLBAR_ACTIONS
            if action_id not in TOOLBAR_ACTIONS:
                super().contextMenuEvent(event)
                return
        except Exception:
            super().contextMenuEvent(event)
            return
        # 弹小菜单
        layout = _toolbar_layout()
        in_main = action_id in layout["main"]
        in_aux = action_id in layout["aux"]
        ctx = QMenu(self)
        if in_main:
            act_main = ctx.addAction("✓ 已在主工具栏 (点击移除)")
            act_main.triggered.connect(lambda: self._remove_from(action_id))
        else:
            act_main = ctx.addAction("📍 加到主工具栏")
            act_main.triggered.connect(lambda: self._add_to(action_id, "main"))
        if in_aux:
            act_aux = ctx.addAction("✓ 已在辅工具栏 (点击移除)")
            act_aux.triggered.connect(lambda: self._remove_from(action_id))
        else:
            act_aux = ctx.addAction("📌 加到辅工具栏")
            act_aux.triggered.connect(lambda: self._add_to(action_id, "aux"))
        if in_main or in_aux:
            ctx.addSeparator()
            act_rm = ctx.addAction("✗ 从工具栏移除")
            act_rm.triggered.connect(lambda: self._remove_from(action_id))
        ctx.exec_(event.globalPos())

    def _add_to(self, action_id: str, where: str) -> None:
        layout = _toolbar_layout()
        # 先从两栏移除 (防重复)
        layout["main"] = [a for a in layout["main"] if a != action_id]
        layout["aux"] = [a for a in layout["aux"] if a != action_id]
        # 加到目标栏末尾
        layout[where].append(action_id)
        _save_toolbar_layout(layout)
        self._rebuild()

    def _remove_from(self, action_id: str) -> None:
        layout = _toolbar_layout()
        layout["main"] = [a for a in layout["main"] if a != action_id]
        layout["aux"] = [a for a in layout["aux"] if a != action_id]
        _save_toolbar_layout(layout)
        self._rebuild()

    def _rebuild(self) -> None:
        win = _find_main_window(self)
        if win is not None:
            try:
                win._rebuild_toolbars()
            except Exception:
                pass


# ---- DraggableToolBar: inline 拖换位 ----

class DraggableToolBar(QToolBar):
    """QToolBar 子类:action 内部拖换位 + 跨栏拖。

    实现:
    - mousePressEvent 记起点
    - mouseMoveEvent 距离 > QApplication.startDragDistance() 时启 QDrag
    - dragEnterEvent 接 mime _MIME_TYPE
    - dropEvent 算插入位置 (按 actionAt pos 算 index) + 改 settings.toolbar_layout + rebuild
    """

    def __init__(self, title: str = "", *, slot: str = "main", parent=None) -> None:
        super().__init__(title, parent)
        self._drag_slot = slot  # "main" / "aux" — 标识此 toolbar 属哪 layout 列
        self._drag_start_pos: Optional[QPoint] = None
        self._dragging_action_id: Optional[str] = None
        self.setAcceptDrops(True)
        # Phase 5 修复:启自定义右键菜单 → 工具栏按钮上右键弹"移除/移到辅栏/移到主栏"
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _on_context_menu(self, pos: QPoint) -> None:
        action = self.actionAt(pos)
        aid = _action_id(action)
        if not aid:
            return
        layout = _toolbar_layout()
        ctx = QMenu(self)
        if self._drag_slot == "main":
            act_to_aux = ctx.addAction("→ 移到辅工具栏")
            act_to_aux.triggered.connect(lambda: self._move_to(aid, "aux"))
        else:
            act_to_main = ctx.addAction("→ 移到主工具栏")
            act_to_main.triggered.connect(lambda: self._move_to(aid, "main"))
        ctx.addSeparator()
        act_remove = ctx.addAction("✗ 从工具栏移除")
        act_remove.triggered.connect(lambda: self._remove(aid))
        ctx.exec_(self.mapToGlobal(pos))

    def _move_to(self, action_id: str, target_slot: str) -> None:
        layout = _toolbar_layout()
        layout["main"] = [a for a in layout["main"] if a != action_id]
        layout["aux"] = [a for a in layout["aux"] if a != action_id]
        layout[target_slot].append(action_id)
        _save_toolbar_layout(layout)
        win = _find_main_window(self)
        if win is not None:
            try:
                win._rebuild_toolbars()
            except Exception:
                pass

    def _remove(self, action_id: str) -> None:
        layout = _toolbar_layout()
        layout["main"] = [a for a in layout["main"] if a != action_id]
        layout["aux"] = [a for a in layout["aux"] if a != action_id]
        _save_toolbar_layout(layout)
        win = _find_main_window(self)
        if win is not None:
            try:
                win._rebuild_toolbars()
            except Exception:
                pass

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            action = self.actionAt(event.pos())
            aid = _action_id(action)
            if aid:
                self._drag_start_pos = event.pos()
                self._dragging_action_id = aid
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (event.buttons() & Qt.LeftButton) and self._drag_start_pos is not None \
                and self._dragging_action_id is not None:
            dist = (event.pos() - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._start_drag(self._dragging_action_id)
                self._drag_start_pos = None
                self._dragging_action_id = None
                return
        super().mouseMoveEvent(event)

    def _start_drag(self, action_id: str) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_MIME_TYPE, QByteArray(action_id.encode("utf-8")))
        drag.setMimeData(mime)
        drag.exec_(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(_MIME_TYPE):
            super().dropEvent(event)
            return
        action_id = bytes(event.mimeData().data(_MIME_TYPE)).decode("utf-8", errors="ignore")
        if not action_id:
            return
        # 算 drop 位置的目标 index (按 actionAt + 比 mid x 决定 before/after)
        target_action = self.actionAt(event.pos())
        target_index = -1  # -1 = 末尾
        if target_action is not None:
            actions = list(self.actions())
            for i, a in enumerate(actions):
                if a is target_action:
                    target_index = i
                    break
        # 更新 layout
        layout = _toolbar_layout()
        # 先从两栏移除该 action_id
        layout["main"] = [a for a in layout["main"] if a != action_id]
        layout["aux"] = [a for a in layout["aux"] if a != action_id]
        # 插入本栏 target_index 位置
        slot = self._drag_slot
        if target_index < 0 or target_index >= len(layout[slot]):
            layout[slot].append(action_id)
        else:
            layout[slot].insert(target_index, action_id)
        _save_toolbar_layout(layout)
        # rebuild
        win = _find_main_window(self)
        if win is not None:
            try:
                win._rebuild_toolbars()
            except Exception:
                pass
        event.acceptProposedAction()
