"""v0.10.46：启动 splash 不得盖住模态对话框，也不得无限期停留。

用户现象：桌面上一个 "标本入库管理 / 打开工作区… 25%" 的窗口挂了 15 小时关不掉。

证据链（C:\\Users\\11791\\AppData\\Roaming\\标本入库管理\\）：
- boot_fault.log：22:09:31 pid=10732 与 22:09:32 pid=6844 —— 一秒内两个进程抢同一把
  工作区锁，必有一个撞上 ui.py 的「工作区被占用」QMessageBox。
- 那之后没有任何 gui_stall_*.log。探针在 open_workspace 之前就起了，主线程停摆 >2s
  必记一条。没有记录 ⇒ 主线程没卡死，事件循环一直在跑 ⇒ 它坐在 mb.exec_() 的
  嵌套事件循环里等用户点按钮。
- splash 带 Qt.WindowStaysOnTopHint 且 setEnabled(False)，在 Windows 上盖住了那个
  对话框 —— 用户看不见、点不到，于是进程永远停在 25%。

修复：splash 看到任何模态窗口出现就自己让开；再加一道超时兜底。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class SplashAutoHideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def setUp(self):
        from specimen_app.splash import SplashScreen

        self.splash = SplashScreen()
        self.splash.show()
        self.app.processEvents()
        self.assertTrue(self.splash.isVisible())

    def tearDown(self):
        self.splash.close()
        self.app.processEvents()

    def _pump(self, ms: int) -> None:
        import time

        from PyQt5.QtCore import QThread

        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            self.app.processEvents()
            QThread.msleep(5)

    def test_modal_dialog_makes_the_splash_step_aside(self):
        """这就是桌面上那个关不掉的窗口的成因。

        这里用 QDialog 而不是产品代码里真正用的 QMessageBox：QMessageBox 在
        offscreen 平台插件 + Windows 下光是 show() 就会 access violation（本机实测，
        与本项目代码无关），而真实 windows 插件下一切正常。守卫判定的是
        ``obj.isModal()``，不看具体类，所以用 QDialog 覆盖的是同一条路径。
        """
        from PyQt5.QtWidgets import QDialog

        self.splash.enable_auto_hide(self.app, timeout_ms=60_000)
        box = QDialog()
        box.setWindowTitle("工作区被占用")
        box.setModal(True)
        box.show()  # 不用 exec_()，否则测试自己也会卡在嵌套循环里
        self._pump(100)
        try:
            self.assertFalse(
                self.splash.isVisible(),
                "模态对话框弹出时 splash 必须让开，否则它会盖住对话框让用户无法操作",
            )
        finally:
            box.close()

    def test_timeout_releases_the_splash_even_without_a_dialog(self):
        """兜底：启动迟迟不结束也不能一直糊在屏幕上。"""
        self.splash.enable_auto_hide(self.app, timeout_ms=60)
        self._pump(300)
        self.assertFalse(self.splash.isVisible(), "超过超时时间后 splash 必须自行退场")

    def test_normal_startup_keeps_the_splash_visible(self):
        """没有对话框、也没超时时，splash 正常显示进度（不能误伤正常启动）。"""
        self.splash.enable_auto_hide(self.app, timeout_ms=60_000)
        self.splash.show_stage("打开工作区…", 25)
        self._pump(100)
        self.assertTrue(self.splash.isVisible())

    def test_non_modal_window_does_not_hide_the_splash(self):
        """普通非模态窗口不该把 splash 顶掉。"""
        from PyQt5.QtWidgets import QWidget

        self.splash.enable_auto_hide(self.app, timeout_ms=60_000)
        w = QWidget()
        w.show()
        self._pump(100)
        try:
            self.assertTrue(self.splash.isVisible())
        finally:
            w.close()

    def test_auto_hide_is_removed_when_the_splash_finishes(self):
        """splash 结束后不该再留着事件过滤器拖慢整个应用。"""
        from PyQt5.QtWidgets import QMainWindow

        self.splash.enable_auto_hide(self.app, timeout_ms=60_000)
        win = QMainWindow()
        win.show()
        self.splash.finish(win)
        self._pump(50)
        try:
            self.assertIsNone(getattr(self.splash, "_auto_hide", None))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
