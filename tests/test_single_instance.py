"""v0.10.46 单实例守卫回归。

boot_fault.log 实证：``22:09:31 pid=10732`` 和 ``22:09:32 pid=6844`` —— 一秒内
两个进程打开同一个工作区。第二个进程必须自己退出，并把已有窗口叫到前台。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

APP_ID = "specimen-organise-test"


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def setUp(self):
        self._guards = []

    def tearDown(self):
        for g in self._guards:
            g.release()

    def _acquire(self):
        from specimen_app.single_instance import acquire

        g = acquire(APP_ID)
        if g is not None:
            self._guards.append(g)
        return g

    def test_first_acquires_second_is_refused(self):
        first = self._acquire()
        self.assertIsNotNone(first, "第一个进程必须拿到锁")
        second = self._acquire()
        self.assertIsNone(second, "第二个进程必须被拒绝，否则就会开出第二个窗口")

    def test_lock_released_allows_next_instance(self):
        first = self._acquire()
        self.assertIsNotNone(first)
        first.release()
        self._guards.remove(first)
        second = self._acquire()
        self.assertIsNotNone(second, "上一个实例退出后，新实例必须能启动")

    def test_second_launch_asks_first_to_show_its_window(self):
        from specimen_app.single_instance import notify_existing_instance

        first = self._acquire()
        self.assertIsNotNone(first)
        raised = []
        first.activate_requested.connect(lambda: raised.append(True))

        self.assertTrue(notify_existing_instance(APP_ID), "通知必须送达已有实例")
        for _ in range(200):
            self.app.processEvents()
            if raised:
                break
        self.assertTrue(raised, "已有实例必须收到'把窗口拉到前台'的请求")

    def test_acquire_never_raises_when_lock_dir_is_unwritable(self):
        """单实例只是体验优化：它自己出问题不能挡住软件启动。"""
        from unittest import mock

        from specimen_app import single_instance

        with mock.patch.object(single_instance, "QLockFile", side_effect=OSError("boom")):
            g = single_instance.acquire(APP_ID + "-broken")
        self.assertIsNotNone(g, "守卫内部异常时必须放行启动")
        g.release()


if __name__ == "__main__":
    unittest.main()
