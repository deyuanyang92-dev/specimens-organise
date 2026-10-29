"""v0.10.46 宫格布局回归：空白 + 清晰度。

用户反馈："图片展示有很多空白，然后有些显示也不够清楚"。
两个原因：
1. 宫格形状写死（8 张永远 4 列 2 行），不看可用区域和照片长宽比 → 横构图照片
   塞进又窄又高的格子，上下大片空白。
2. 缩略图固定按 320×240 解码，而单元格普遍 400-600px 宽 → 放大显示 → 糊。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _shown_area(cols, rows, vw, vh, aspect, spacing=6, chrome=12):
    cell_w = (vw - spacing * (cols - 1)) / cols - chrome
    cell_h = (vh - spacing * (rows - 1)) / rows - chrome
    w = min(cell_w, cell_h * aspect)
    return w * (w / aspect)


class BestGridShapeTests(unittest.TestCase):
    def test_landscape_photos_in_wide_area_beat_the_old_fixed_shape(self):
        """用户截图的场景：8 张横构图、约 1290×1020 的区域。"""
        from specimen_app.ui import best_grid_shape, grid_shape

        vw, vh, aspect = 1290, 1020, 4 / 3
        new_cols, new_rows = best_grid_shape(8, vw, vh, aspect)
        old_cols, old_rows = grid_shape(8)

        new_area = _shown_area(new_cols, new_rows, vw, vh, aspect)
        old_area = _shown_area(old_cols, old_rows, vw, vh, aspect)
        self.assertGreater(
            new_area, old_area * 1.3,
            f"新排法 {new_cols}×{new_rows} 应明显比旧的 {old_cols}×{old_rows} 占满更多面积",
        )

    def test_portrait_photos_prefer_more_columns(self):
        """竖构图照片应该横向多排几列，而不是继续堆成高格子。"""
        from specimen_app.ui import best_grid_shape

        land_cols, _ = best_grid_shape(6, 1290, 1020, 4 / 3)
        port_cols, _ = best_grid_shape(6, 1290, 1020, 3 / 4)
        self.assertGreater(port_cols, land_cols)

    def test_narrow_window_stacks_vertically(self):
        from specimen_app.ui import best_grid_shape

        cols, rows = best_grid_shape(8, 600, 1200, 4 / 3)
        self.assertLessEqual(cols, 2)
        self.assertGreaterEqual(rows, 4)

    def test_every_photo_gets_a_slot(self):
        from specimen_app.ui import best_grid_shape

        for count in range(1, 13):
            for vw, vh in ((1290, 1020), (600, 1200), (1920, 480)):
                cols, rows = best_grid_shape(count, vw, vh)
                self.assertGreaterEqual(cols * rows, count, f"{count} 张放不下 {cols}×{rows}")
                self.assertLess((cols - 1) * rows, count, f"{cols}×{rows} 有整列是空的")

    def test_degenerate_sizes_fall_back_to_fixed_shape(self):
        """窗口尺寸还没算出来时不能崩，退回旧的固定形状。"""
        from specimen_app.ui import best_grid_shape, grid_shape

        self.assertEqual(best_grid_shape(6, 0, 0), grid_shape(6))
        self.assertEqual(best_grid_shape(6, -10, 500), grid_shape(6))
        self.assertEqual(best_grid_shape(4, 1290, 1020, 0), best_grid_shape(4, 1290, 1020, 4 / 3))


class GridThumbSizeTests(unittest.TestCase):
    """缩略图解码尺寸应跟随单元格大小，而不是写死 320×240。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def _sizer(self):
        from specimen_app.ui import SpecimenWindow

        # 只测纯计算方法，不建整个主窗口（它要工作区 / Excel）。
        return SpecimenWindow._grid_thumb_size

    def test_large_cells_request_larger_thumbnails_than_the_old_fixed_320(self):
        from PyQt5.QtWidgets import QWidget

        sizer = self._sizer()
        w, h = sizer(QWidget(), 1290, 1020, 3, 3)  # 单元格约 418×326
        self.assertGreater(w, 320, "单元格比 320px 宽得多时必须要更大的图，否则就是放大糊图")
        self.assertGreaterEqual(h, 320)

    def test_sizes_are_quantized_so_the_cache_still_hits(self):
        from PyQt5.QtWidgets import QWidget

        sizer = self._sizer()
        widget = QWidget()
        a = sizer(widget, 1290, 1020, 3, 3)
        b = sizer(widget, 1294, 1024, 3, 3)  # 窗口动了几像素
        self.assertEqual(a, b, "尺寸必须量化，否则每动一下窗口缓存就全失效")
        for v in a:
            self.assertEqual(v % 128, 0)

    def test_tiny_viewport_does_not_request_absurd_sizes(self):
        from PyQt5.QtWidgets import QWidget

        sizer = self._sizer()
        w, h = sizer(QWidget(), 200, 150, 4, 2)
        self.assertGreaterEqual(w, 320)
        self.assertLessEqual(w, 2048)
        self.assertLessEqual(h, 2048)

    def test_low_memory_profile_caps_the_request(self):
        from unittest import mock

        from PyQt5.QtWidgets import QWidget

        from specimen_app import ui

        settings = mock.Mock()
        settings.memory_profile = "extra_low"
        with mock.patch.object(ui, "load_settings", return_value=settings):
            w, h = ui.SpecimenWindow._grid_thumb_size(QWidget(), 2560, 1440, 2, 2)
        self.assertLessEqual(w, 800, "低内存档必须沿用 preview_max_size 上限")
        self.assertLessEqual(h, 600)


if __name__ == "__main__":
    unittest.main()
