"""v0.10.46 端到端：真实窗口 + 真实照片渲染宫格。

覆盖用户报的三个现象在同一条路径上的交汇点：
- 缩略图在后台线程解码（旧实现在这里 access violation 崩溃）；
- 宫格按可用区域和照片比例排布（旧实现大量空白）；
- 缩略图按单元格尺寸解码（旧实现固定 320×240 → 放大糊）。

照片宽度刻意取非 4 的倍数（旧 QImage 行宽算错就是从这种尺寸开始越界）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from specimen_app import app_settings
from specimen_app.excel_store import ExcelStore


class GridRenderEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._ps = [
            mock.patch.object(app_settings, "settings_path", return_value=self.tmp / "s.json"),
            mock.patch.object(app_settings, "app_config_dir", return_value=self.tmp / "cfg"),
        ]
        for p in self._ps:
            p.start()
        # 7 张照片：复现截图里的 "1-7 / 7 · 8宫格"
        self.photos = []
        src_dir = self.tmp / "原图"
        src_dir.mkdir()
        from PIL import Image

        for i in range(7):
            # 宽 1001 / 高 749：都不是 4 的倍数，横构图
            path = src_dir / f"A0{i:02d}_右侧面.png"
            Image.new("RGB", (1001, 749), (20 + i * 10, 150, 90)).save(path)
            self.photos.append(path)

    def tearDown(self):
        for p in self._ps:
            p.stop()
        from tests import close_all_open_stores

        close_all_open_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window_with_photos(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        st = ExcelStore(ws)
        voucher = st.create_specimen()
        st.release_lock()
        st.close()

        from specimen_app.ui import SpecimenWindow

        win = SpecimenWindow(ws)
        self._pump(1.0)  # 等 _finish_initial_load
        win.resize(1600, 1000)
        # 必须真的 show()：不显示的话 Qt 不会把尺寸传播到子控件，
        # grid_frame 会一直停在 640×480 的默认值，测不出真实布局。
        win.show()
        self._pump(0.5)
        win.select_voucher(voucher)
        with mock.patch("specimen_app.ui.QMessageBox"):
            added = win.add_photo_paths([str(p) for p in self.photos], ask_for_outside=False)
        self.assertEqual(added, 7)
        return win

    def test_grid_renders_all_photos_without_crashing(self):
        win = self._window_with_photos()
        try:
            win._set_view_mode(8)
            self._pump(3.0)  # 等后台解码 + 回主线程转 QPixmap

            self.assertEqual(len(win._grid_labels), 7, "7 张照片应各占一格")
            with_image = [c for c in win._grid_labels if c._view._pixmap_item is not None]
            self.assertEqual(len(with_image), 7, "每格都该拿到缩略图（旧实现在解码线程崩溃）")
        finally:
            win.close()
            self.app.processEvents()

    def test_thumbnails_are_decoded_bigger_than_the_old_fixed_320(self):
        win = self._window_with_photos()
        try:
            win._set_view_mode(8)
            self._pump(3.0)
            widths = [
                int(c._view._pixmap_item.pixmap().width())
                for c in win._grid_labels
                if c._view._pixmap_item is not None
            ]
            self.assertTrue(widths, "没有任何缩略图加载成功")
            self.assertGreater(
                max(widths), 320,
                "1600×1000 窗口下单元格远宽于 320px，必须解码更大的图，否则就是放大糊图",
            )
        finally:
            win.close()
            self.app.processEvents()

    def test_grid_shape_follows_the_viewport(self):
        """同样 7 张照片，宽窗口和窄窗口应排出不同形状。"""
        win = self._window_with_photos()
        try:
            win._set_view_mode(8)
            self._pump(2.0)
            wide_positions = {
                win.grid_layout.getItemPosition(i)[:2]
                for i in range(win.grid_layout.count())
            }
            wide_cols = len({c for _, c in wide_positions})

            win.resize(700, 1200)
            self._pump(0.3)
            win._render_grid()  # 跳过防抖直接重排
            self._pump(2.0)
            narrow_positions = {
                win.grid_layout.getItemPosition(i)[:2]
                for i in range(win.grid_layout.count())
            }
            narrow_cols = len({c for _, c in narrow_positions})

            self.assertLess(narrow_cols, wide_cols, "窄窗口应减少列数，而不是维持固定形状")
        finally:
            win.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
