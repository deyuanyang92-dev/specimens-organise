"""共享 widgets — 入库人员相关 (规范化软件设计 2026-05 新增)。

- PersonAvatar: 圆形色块 + 姓名首字
- PersonComboBox: 智能选人下拉(头像 + 角色 + 星标 + 钉位标识)
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QSize, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap, QBrush, QPen
from PyQt5.QtWidgets import (
    QComboBox,
    QLabel,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from .persons_store import TeamMember, avatar_text, color_for, load_members, sort_key, ROLE_OPTIONS


class PersonAvatar(QLabel):
    """圆形色块 + 字首。可在任意 layout 用。"""

    def __init__(self, name: str = "", size: int = 24, parent=None) -> None:
        super().__init__(parent)
        self._name = ""
        self._size = size
        self.setFixedSize(size, size)
        self.set_person(name)

    def set_person(self, name: str, color: str = "") -> None:
        self._name = name or ""
        bg = color or color_for(name) if name else "#cccccc"
        text = avatar_text(name) if name else ""
        pix = QPixmap(self._size, self._size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QBrush(QColor(bg)))
            p.setPen(Qt.NoPen)
            p.drawEllipse(0, 0, self._size, self._size)
            if text:
                p.setPen(QPen(QColor("#ffffff")))
                font = QFont()
                font.setBold(True)
                # 字号 = 60% size,中文/字母都可读
                font.setPointSizeF(max(7.0, self._size * 0.45))
                p.setFont(font)
                p.drawText(QRect(0, 0, self._size, self._size), Qt.AlignCenter, text)
        finally:
            p.end()
        self.setPixmap(pix)


class _PersonItemDelegate(QStyledItemDelegate):
    """自定义 QComboBox 项渲染:头像 + 姓名 + 角色 + 星标 + 钉位。"""

    AVATAR_SIZE = 20
    ROW_HEIGHT = 28

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        member: Optional[TeamMember] = index.data(Qt.UserRole)
        if member is None:
            # 占位项 (如 "+ 新增人员…" / "未设置")
            super().paint(painter, option, index)
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            rect = option.rect
            # 选中高亮
            if option.state & 0x00000001:  # QStyle.State_Selected
                painter.fillRect(rect, QColor("#e8f0fe"))
            # 钉位标识 (左 4px 蓝竖条)
            if member.pinned:
                painter.fillRect(rect.left(), rect.top(), 3, rect.height(), QColor("#1a5faa"))
            # 头像圆
            x = rect.left() + 8
            cy = rect.top() + rect.height() // 2
            avatar_y = cy - self.AVATAR_SIZE // 2
            painter.setBrush(QBrush(QColor(member.color_hint or color_for(member.name))))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(x, avatar_y, self.AVATAR_SIZE, self.AVATAR_SIZE)
            # 字首
            painter.setPen(QPen(QColor("#ffffff")))
            f = QFont()
            f.setBold(True)
            f.setPointSizeF(9.0)
            painter.setFont(f)
            painter.drawText(QRect(x, avatar_y, self.AVATAR_SIZE, self.AVATAR_SIZE),
                             Qt.AlignCenter, avatar_text(member.name))
            # 姓名
            name_x = x + self.AVATAR_SIZE + 8
            painter.setPen(QPen(QColor("#222")))
            f2 = QFont()
            f2.setBold(member.pinned)
            painter.setFont(f2)
            painter.drawText(QRect(name_x, rect.top(), 100, rect.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, member.name)
            # 角色
            role_text = ROLE_OPTIONS.get(member.role, member.role)
            painter.setPen(QPen(QColor("#888")))
            painter.setFont(QFont())
            painter.drawText(QRect(name_x + 80, rect.top(), 60, rect.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, role_text)
            # 星标 (右侧)
            if member.starred > 0:
                painter.setPen(QPen(QColor("#f5a623")))
                painter.drawText(QRect(rect.right() - 90, rect.top(), 80, rect.height()),
                                 Qt.AlignLeft | Qt.AlignVCenter, "★" * member.starred)
        finally:
            painter.restore()


class PersonComboBox(QComboBox):
    """智能选人下拉。

    signal `member_changed(str name)` 选中变化时发出 (含手输不在库的)。
    显示当前选中的 PersonAvatar + 姓名(经 setEditable=False 时显完整姓名)。
    """

    member_changed = pyqtSignal(str)

    SPECIAL_ADD_NEW = "__add_new__"
    SPECIAL_MANAGE = "__manage__"

    def __init__(self, parent=None, *, allow_manage: bool = True) -> None:
        super().__init__(parent)
        self._members: list[TeamMember] = []
        self._allow_manage = allow_manage
        self._delegate = _PersonItemDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setMinimumHeight(28)
        self.setEditable(False)
        self.currentIndexChanged.connect(self._on_index_changed)
        self.refresh()

    def refresh(self, members: Optional[list[TeamMember]] = None,
                preselect: str = "") -> None:
        """重新加载 members 列表。preselect=姓名 指定选中谁。"""
        if members is None:
            members = load_members()
        # 排序保险 (load_members 已排,但 caller 直传时仍需保证)
        members = sorted(members, key=sort_key)
        self._members = members

        self.blockSignals(True)
        try:
            self.clear()
            if not members:
                self.addItem("未设置 — 点击 [管理人员…] 添加")
                self.setItemData(0, None, Qt.UserRole)
            else:
                for m in members:
                    self.addItem(m.name)
                    self.setItemData(self.count() - 1, m, Qt.UserRole)
            # 特殊项
            if self._allow_manage:
                self.insertSeparator(self.count())
                self.addItem("＋ 管理人员…")
                self.setItemData(self.count() - 1, self.SPECIAL_MANAGE, Qt.UserRole)
            # preselect
            if preselect:
                for i in range(self.count()):
                    d = self.itemData(i, Qt.UserRole)
                    if isinstance(d, TeamMember) and d.name == preselect:
                        self.setCurrentIndex(i)
                        break
        finally:
            self.blockSignals(False)

    def current_member(self) -> Optional[TeamMember]:
        d = self.itemData(self.currentIndex(), Qt.UserRole)
        return d if isinstance(d, TeamMember) else None

    def current_name(self) -> str:
        m = self.current_member()
        return m.name if m else ""

    def _on_index_changed(self, idx: int) -> None:
        d = self.itemData(idx, Qt.UserRole)
        if d == self.SPECIAL_MANAGE:
            # 通知父对话框 "用户想开管理面板"
            self.member_changed.emit(self.SPECIAL_MANAGE)
            return
        if isinstance(d, TeamMember):
            self.member_changed.emit(d.name)
        else:
            self.member_changed.emit("")
