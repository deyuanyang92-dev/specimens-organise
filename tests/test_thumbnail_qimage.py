"""v0.10.46 崩溃回归：缩略图 PIL → Qt 转换。

背景（boot_fault.log 实证）：v0.10.39–v0.10.41 共 9 次
``Windows fatal exception: access violation``，栈顶全部是
``specimen_app/ui.py pil_to_qpixmap`` ← ``ThumbnailWorker._process``（解码线程）。

两个独立缺陷：
1. ``QImage(data, w, h, fmt)`` 不传 bytesPerLine 时，Qt 按 32 位对齐算行宽
   （Qt 文档要求"每条扫描线 32 位对齐"），而 PIL 的 raw 行宽是 w*3 无填充。
   w 不是 4 的倍数 → Qt 越界读 → access violation。
2. QPixmap 只能在 GUI 线程创建，解码线程里造它是未定义行为。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class PilToQImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def test_scanline_not_32bit_aligned_does_not_read_past_buffer(self):
        """宽度不是 4 的倍数时，QImage 行宽必须等于 PIL 的真实行宽。"""
        from PIL import Image

        from specimen_app.ui import pil_to_qimage

        for width in (317, 318, 319, 321, 322, 323):
            with self.subTest(width=width):
                src = Image.new("RGB", (width, 23), (10, 200, 30))
                qimg = pil_to_qimage(src)
                self.assertEqual(qimg.width(), width)
                self.assertEqual(qimg.height(), 23)
                # 关键断言：每个像素都必须是原色。行宽算错时 Qt 会按 4 字节对齐
                # 的步长跨行读，像素发生通道偏移（实测 w=321 时行首读成 (30,10,200)），
                # 最后一行更是直接读到缓冲区之外的未定义内存。
                for x, y in ((0, 0), (width - 1, 0), (0, 11), (width - 1, 11),
                             (0, 22), (width - 1, 22)):
                    self.assertEqual(
                        qimg.pixelColor(x, y).getRgb()[:3], (10, 200, 30),
                        f"width={width} 像素({x},{y}) 错位——行宽算错",
                    )

    def test_rgba_and_palette_modes_round_trip(self):
        from PIL import Image

        from specimen_app.ui import pil_to_qimage

        rgba = Image.new("RGBA", (31, 7), (1, 2, 3, 255))
        qimg = pil_to_qimage(rgba)
        self.assertEqual(qimg.pixelColor(30, 6).getRgb(), (1, 2, 3, 255))
        self.assertEqual(qimg.pixelColor(0, 0).getRgb(), (1, 2, 3, 255))

        # 非 RGB/RGBA 模式先转 RGB，行宽同样要对
        gray = Image.new("L", (33, 5), 128)
        qimg = pil_to_qimage(gray)
        self.assertEqual(qimg.pixelColor(32, 4).getRgb()[:3], (128, 128, 128))

    def test_qimage_owns_its_memory_after_source_bytes_are_gone(self):
        """.copy() 必须生效：源 PIL 对象回收后像素仍可读。"""
        import gc

        from PIL import Image

        from specimen_app.ui import pil_to_qimage

        src = Image.new("RGB", (37, 11), (200, 100, 50))
        qimg = pil_to_qimage(src)
        del src
        gc.collect()
        self.assertEqual(qimg.pixelColor(36, 10).getRgb()[:3], (200, 100, 50))


class ThumbnailWorkerThreadSafetyTests(unittest.TestCase):
    """解码线程不得构造 QPixmap。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(["t"])

    def test_worker_emits_qimage_not_qpixmap(self):
        import queue
        import tempfile
        from pathlib import Path

        from PyQt5.QtGui import QImage, QPixmap
        from PIL import Image

        from specimen_app.image_cache import ThumbnailCache
        from specimen_app.ui import ThumbnailWorker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = root / "a.png"
            # 宽度 319：旧实现在这里就会越界读
            Image.new("RGB", (319, 211), (5, 6, 7)).save(photo)

            worker = ThumbnailWorker(ThumbnailCache(root), max_workers=1)
            got: queue.Queue = queue.Queue()
            worker.result_ready.connect(lambda tok, obj, exc: got.put((tok, obj, exc)))
            worker.start()
            try:
                worker.enqueue(photo, (320, 240), 7)
                deadline_app = self.app
                for _ in range(300):
                    deadline_app.processEvents()
                    if not got.empty():
                        break
                    QThreadSleep(10)
                token, obj, exc = got.get(timeout=5)
            finally:
                worker.stop()

        self.assertIsNone(exc)
        self.assertEqual(token, 7)
        self.assertIsInstance(obj, QImage)
        self.assertNotIsInstance(obj, QPixmap)
        self.assertFalse(obj.isNull())
        # 像素未错位（宽度 319 → 旧实现这里会读到越界内存）
        self.assertEqual(obj.pixelColor(0, 0).getRgb()[:3], (5, 6, 7))
        self.assertEqual(
            obj.pixelColor(obj.width() - 1, obj.height() - 1).getRgb()[:3], (5, 6, 7)
        )


def QThreadSleep(ms: int) -> None:
    from PyQt5.QtCore import QThread

    QThread.msleep(ms)


if __name__ == "__main__":
    unittest.main()
