"""快捷键自定义对话框（规范化软件设计 2026-05 新增）。

读 `specimen_app.ui.SHORTCUTABLE_ACTIONS` 注册表，列表展示每个 action 的：
- 显示名 / 默认快捷键 / 当前快捷键（从 settings.custom_shortcuts 或 default 派生）
- QKeySequenceEdit 录入新 keyseq；空 = 用默认；冲突时阻止保存
- 「恢复默认」按钮按单行清自定义

保存写 settings.custom_shortcuts，并调主窗口的 `_apply_custom_shortcuts()` 热更新。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .app_settings import load_settings, save_settings


class ShortcutsCustomizeDialog(QDialog):
    """快捷键自定义对话框。"""

    COL_FEATURE = 0
    COL_DEFAULT = 1
    COL_CURRENT = 2
    COL_EDIT = 3
    COL_RESET = 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自定义快捷键")
        self.resize(720, 540)
        self._parent_window = parent
        self._key_edits: dict[str, QKeySequenceEdit] = {}
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>自定义快捷键</b>　点击「当前快捷键」单元格内的录入框，按下你想绑的组合键。"
            "留空 = 用默认值；按「单行恢复默认」清掉自定义；保存前会检测冲突。"
        ))

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["功能", "默认", "当前", "录入", "单行重置"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, 1)

        # 底部按钮
        foot = QHBoxLayout()
        btn_reset_all = QPushButton("全部重置为默认")
        btn_reset_all.clicked.connect(self._reset_all)
        foot.addWidget(btn_reset_all)
        foot.addStretch()
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_save.setDefault(True)
        foot.addWidget(btn_save)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        foot.addWidget(btn_cancel)
        layout.addLayout(foot)

    def _populate(self) -> None:
        from .ui import SHORTCUTABLE_ACTIONS
        custom = load_settings().custom_shortcuts or {}
        self._table.setRowCount(len(SHORTCUTABLE_ACTIONS))
        self._key_edits.clear()
        for row, (action_id, spec) in enumerate(SHORTCUTABLE_ACTIONS.items()):
            label = spec["label"]
            default = spec.get("default") or ""
            current = custom.get(action_id, default)

            it_feature = QTableWidgetItem(label)
            it_feature.setData(Qt.UserRole, action_id)
            it_feature.setToolTip(f"action_id = {action_id}")
            self._table.setItem(row, self.COL_FEATURE, it_feature)

            self._table.setItem(row, self.COL_DEFAULT, QTableWidgetItem(default or "（无）"))
            self._table.setItem(row, self.COL_CURRENT, QTableWidgetItem(current or "（无）"))

            edit = QKeySequenceEdit()
            if current:
                edit.setKeySequence(QKeySequence(current))
            edit.keySequenceChanged.connect(lambda seq, r=row: self._on_edit_changed(r, seq))
            self._table.setCellWidget(row, self.COL_EDIT, edit)
            self._key_edits[action_id] = edit

            btn_reset = QPushButton("默认")
            btn_reset.setToolTip("把此行恢复为默认快捷键")
            btn_reset.clicked.connect(lambda _=None, aid=action_id: self._reset_row(aid))
            self._table.setCellWidget(row, self.COL_RESET, btn_reset)

    # ---- 列表事件 ----
    def _on_edit_changed(self, row: int, seq: QKeySequence) -> None:
        text = seq.toString()
        self._table.item(row, self.COL_CURRENT).setText(text or "（无）")

    def _reset_row(self, action_id: str) -> None:
        from .ui import SHORTCUTABLE_ACTIONS
        spec = SHORTCUTABLE_ACTIONS.get(action_id)
        default = (spec.get("default") if spec else "") or ""
        edit = self._key_edits.get(action_id)
        if edit is not None:
            edit.setKeySequence(QKeySequence(default))

    def _reset_all(self) -> None:
        from .ui import SHORTCUTABLE_ACTIONS
        ret = QMessageBox.question(
            self, "重置确认",
            "把所有快捷键重置为默认？当前自定义会被丢弃。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        for aid, spec in SHORTCUTABLE_ACTIONS.items():
            default = spec.get("default") or ""
            edit = self._key_edits.get(aid)
            if edit is not None:
                edit.setKeySequence(QKeySequence(default))

    # ---- 收集与保存 ----
    def _collect(self) -> dict[str, str]:
        """返回 action_id -> keyseq；空 keyseq 也保留 ""，让保存阶段决定是否落 settings。"""
        result: dict[str, str] = {}
        for aid, edit in self._key_edits.items():
            result[aid] = edit.keySequence().toString()
        return result

    def _detect_conflicts(self, mapping: dict[str, str]) -> Optional[str]:
        """检测同一 keyseq 绑两个不同 action；返回首个冲突字符串描述，None=无冲突。"""
        seen: dict[str, str] = {}
        for aid, seq in mapping.items():
            if not seq:
                continue
            if seq in seen:
                from .ui import SHORTCUTABLE_ACTIONS
                a = SHORTCUTABLE_ACTIONS.get(aid, {}).get("label", aid)
                b = SHORTCUTABLE_ACTIONS.get(seen[seq], {}).get("label", seen[seq])
                return f"快捷键 [{seq}] 同时绑给了「{a}」和「{b}」。请改其中之一。"
            seen[seq] = aid
        return None

    def _save(self) -> None:
        from .ui import SHORTCUTABLE_ACTIONS
        collected = self._collect()
        # 冲突检测
        conflict = self._detect_conflicts(collected)
        if conflict:
            QMessageBox.warning(self, "快捷键冲突", conflict)
            return
        # 把和 default 一致的项移除（不必持久化），其余写入 settings
        custom: dict[str, str] = {}
        for aid, seq in collected.items():
            default = (SHORTCUTABLE_ACTIONS.get(aid) or {}).get("default") or ""
            if seq != default:
                custom[aid] = seq  # 包括 "" 表示主动清空（与 default != "" 时区分）
        settings = load_settings()
        settings.custom_shortcuts = custom
        save_settings(settings)
        # 热更新到主窗口
        parent = self._parent_window
        if parent is not None and hasattr(parent, "_apply_custom_shortcuts"):
            try:
                parent._apply_custom_shortcuts()
            except Exception as exc:
                QMessageBox.warning(
                    self, "应用失败",
                    f"已保存设置，但运行时应用失败（重启应用即可生效）：{exc}",
                )
        self.accept()
