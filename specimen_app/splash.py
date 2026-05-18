"""启动 Splash Screen(规范化软件设计 2026-05 启动卡死优化)。

启动时立刻显示 splash,给用户视觉反馈 — 避免黑屏 1-3s 让用户以为"卡死"。
显示内容:应用图标 + 标题 + 版本号 + 进度条 + 当前阶段文字。

用法:
```
app = QApplication(sys.argv)
splash = SplashScreen()
splash.show()
app.processEvents()  # 让 splash 立刻可见

splash.show_stage("加载配置…", 10)
# ... 启动各阶段
splash.show_stage("加载界面…", 80)
app.processEvents()

# 主窗口构建完毕
splash.finish(main_window)  # 自动关闭 splash 并把焦点转给 main_window
```
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt5.QtWidgets import QSplashScreen, QApplication

from . import __version__


_SPLASH_WIDTH = 480
_SPLASH_HEIGHT = 220
_BG_COLOR = QColor("#fbfbfd")
_BORDER_COLOR = QColor("#1a5faa")
_TEXT_COLOR = QColor("#222")
_MUTED_COLOR = QColor("#888")
_PROGRESS_BG = QColor("#e8eef7")
_PROGRESS_FG = QColor("#1a5faa")


class SplashScreen(QSplashScreen):
    """启动 splash:图标 + 标题 + 版本 + 进度条 + 阶段文字。

    自绘 pixmap,不依赖外部图片资源(避免 PyInstaller 资源路径问题)。
    若 app 图标可解析则贴左侧;否则跳过图标只显文字。
    """

    def __init__(self, parent=None) -> None:
        pix = self._build_pixmap()
        super().__init__(parent, pix, Qt.WindowStaysOnTopHint)
        self._stage_text = "正在启动…"
        self._percent = 0
        # QSplashScreen 自带 showMessage,但我们要画自定义进度条 -> 用 drawContents 重绘
        self.setEnabled(False)  # splash 期间禁交互避免误点

    # ---- API ----
    def show_stage(self, text: str, percent: int) -> None:
        """更新当前阶段文字 + 进度百分比;调用后立刻 repaint + processEvents 让其可见。"""
        self._stage_text = str(text or "")
        self._percent = max(0, min(100, int(percent)))
        self.repaint()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    # ---- Qt 自绘 ----
    def drawContents(self, painter: QPainter) -> None:
        """在 splash pixmap 上叠加阶段文字 + 进度条(QSplashScreen 调用)。"""
        if painter is None:
            return
        rect = self.rect()
        # 阶段文字
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(_MUTED_COLOR)
        text_rect = QRect(20, _SPLASH_HEIGHT - 60, _SPLASH_WIDTH - 40, 20)
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self._stage_text)
        # 进度条背景
        bar_rect = QRect(20, _SPLASH_HEIGHT - 36, _SPLASH_WIDTH - 40, 10)
        painter.fillRect(bar_rect, _PROGRESS_BG)
        # 进度条填充
        if self._percent > 0:
            fill_width = int((bar_rect.width() * self._percent) / 100)
            fill_rect = QRect(bar_rect.x(), bar_rect.y(), fill_width, bar_rect.height())
            painter.fillRect(fill_rect, _PROGRESS_FG)
        # 进度百分比文字(进度条右端)
        painter.setPen(_MUTED_COLOR)
        font.setPointSize(8)
        painter.setFont(font)
        pct_rect = QRect(20, _SPLASH_HEIGHT - 22, _SPLASH_WIDTH - 40, 14)
        painter.drawText(pct_rect, Qt.AlignRight | Qt.AlignVCenter, f"{self._percent}%")

    # ---- pixmap 构建(静态部分:背景 / 边框 / 图标 / 标题 / 版本) ----
    @staticmethod
    def _build_pixmap() -> QPixmap:
        pix = QPixmap(_SPLASH_WIDTH, _SPLASH_HEIGHT)
        pix.fill(_BG_COLOR)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            # 边框
            painter.setPen(_BORDER_COLOR)
            painter.drawRect(0, 0, _SPLASH_WIDTH - 1, _SPLASH_HEIGHT - 1)
            # 应用图标(取当前 variant;失败则跳过)
            try:
                from .icon import get_app_icon
                from .app_settings import load_settings
                icon = get_app_icon(load_settings().app_icon_variant)
                icon_pix = icon.pixmap(72, 72)
                painter.drawPixmap(28, 32, icon_pix)
            except Exception:
                pass
            # 标题
            painter.setPen(_TEXT_COLOR)
            font = QFont()
            font.setPointSize(16)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRect(120, 32, _SPLASH_WIDTH - 140, 30),
                             Qt.AlignLeft | Qt.AlignVCenter, "标本入库管理")
            # 副标题
            font = QFont()
            font.setPointSize(9)
            painter.setFont(font)
            painter.setPen(_MUTED_COLOR)
            painter.drawText(QRect(120, 64, _SPLASH_WIDTH - 140, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, "PyQt5 桌面端 · 生物标本 Excel 数据管理")
            # 版本号
            painter.drawText(QRect(120, 84, _SPLASH_WIDTH - 140, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, f"v{__version__}")
        finally:
            painter.end()
        return pix
