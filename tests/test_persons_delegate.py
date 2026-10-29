"""v0.10.46 崩溃回归：人员下拉框的自绘代理。

crash_20261021_165731.log：
    File "specimen_app\\widgets_persons.py", line 99, in paint
    AttributeError: 'str' object has no attribute 'pinned'

"＋ 管理人员…" 这一项往 Qt.UserRole 里存的是字符串哨兵 ``SPECIAL_MANAGE``，
不是 None，旧代码的 ``if member is None`` 挡不住，紧接着 ``member.pinned`` 抛异常。
异常发生在 Qt 的 paint 回调里，没有 Python 调用栈能接住 —— 直接走崩溃钩子退出。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class PersonItemDelegatePaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def _paint_all_items(self, combo) -> None:
        """把下拉框每一项都画一遍，复现崩溃路径。"""
        from PyQt5.QtGui import QPainter, QPixmap
        from PyQt5.QtWidgets import QStyleOptionViewItem

        delegate = combo.itemDelegate()
        model = combo.model()
        pix = QPixmap(300, 40)
        painter = QPainter(pix)
        try:
            for row in range(combo.count()):
                option = QStyleOptionViewItem()
                option.rect = pix.rect()
                delegate.paint(painter, option, model.index(row, 0))
        finally:
            painter.end()

    def test_painting_the_manage_entry_does_not_raise(self):
        from specimen_app.widgets_persons import PersonComboBox

        combo = PersonComboBox(allow_manage=True)
        combo.refresh(members=[])  # 只有占位项 + "＋ 管理人员…"
        self.assertGreater(combo.count(), 1)
        self._paint_all_items(combo)  # 旧代码在这里 AttributeError

    def test_painting_real_members_still_uses_custom_drawing(self):
        from specimen_app.persons_store import TeamMember
        from specimen_app.widgets_persons import PersonComboBox

        members = [
            TeamMember(name="张三", role="recorder", pinned=True),
            TeamMember(name="李四", role="recorder", pinned=False),
        ]
        combo = PersonComboBox(allow_manage=True)
        combo.refresh(members=members)
        self._paint_all_items(combo)

    def test_manage_sentinel_is_not_mistaken_for_a_member(self):
        """哨兵必须是非 TeamMember，否则 paint 的类型判断就失去意义。"""
        from specimen_app.persons_store import TeamMember
        from specimen_app.widgets_persons import PersonComboBox

        self.assertNotIsInstance(PersonComboBox.SPECIAL_MANAGE, TeamMember)


if __name__ == "__main__":
    unittest.main()
