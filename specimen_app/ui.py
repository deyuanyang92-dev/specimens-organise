from __future__ import annotations

import os
import subprocess
import sys
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QPoint, QSize, QStringListModel, QByteArray
from PyQt5.QtGui import QImage, QPixmap, QKeySequence, QFont, QPainter, QCursor, QFontMetrics
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QCompleter,
)
from PyQt5.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

from . import __version__
from .app_settings import PREVIEW_QUALITY_OPTIONS, PREVIEW_QUALITY_SIZES, load_settings, remember_workspace, save_settings
from .icon import get_app_icon
from .excel_store import ExcelStore
from .image_cache import ThumbnailCache
from .image_search import (
    ImageSearchIndex,
    ImageSearchResult,
    _get_or_build_search_index,
    append_images_to_index,
    clear_image_index,
    default_image_query,
    image_index_exists,
    image_search_results,
    suffixes_for_image_type,
)
from .models import (
    CLASSIFICATION_HEADERS,
    ImportConflictError,
    SAVE_METHOD_OPTIONS,
    SPECIMEN_HEADERS,
    WorkspaceLockedError,
    WorkspaceNotInitializedError,
)
from .parsing import extract_photo_date, extract_tube_from_filename, extract_location_code, parse_voucher_serial
from .release_manager import list_releases
from .species import SpeciesMatch, SpeciesMatcher
from .workspace import default_workspace, has_workspace_data, initialize_workspace, is_generated_workspace_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def grid_shape(count: int) -> tuple[int, int]:
    if count <= 2:
        return max(1, count), 1
    if count <= 4:
        return 2, 2
    if count <= 6:
        return 3, 2
    return 4, 2


VIEW_MODES = [
    ("单张", 1, "1"),
    ("2宫格", 2, "2"),
    ("4宫格", 4, "4"),
    ("6宫格", 6, "6"),
    ("8宫格", 8, "8"),
]

_VIEW_BTN_STYLE = """
    QPushButton { border: 1px solid #aab; border-radius: 4px; padding: 4px 8px; font-weight: bold; }
    QPushButton:checked { background-color: #2a6fbd; color: white; }
"""


def _shorten(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max(1, max_length - 1)] + "…"


def pil_to_qpixmap(pil_image: Image.Image) -> QPixmap:
    if pil_image.mode == "RGBA":
        data = pil_image.tobytes("raw", "RGBA")
        fmt = QImage.Format_RGBA8888
    else:
        if pil_image.mode != "RGB":
            pil_image = pil_image.convert("RGB")
        data = pil_image.tobytes("raw", "RGB")
        fmt = QImage.Format_RGB888
    qimg = QImage(data, pil_image.width, pil_image.height, fmt).copy()
    return QPixmap.fromImage(qimg)


def _open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", str(path)], shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        QMessageBox.critical(None, "打开失败", str(exc))


# ---------------------------------------------------------------------------
# Background thumbnail loader (QThread + signal — no polling)
# ---------------------------------------------------------------------------

class _ThumbnailRequest:
    __slots__ = ("path", "size", "token")

    def __init__(self, path: Path, size: tuple[int, int], token: int):
        self.path = path
        self.size = size
        self.token = token


class ThumbnailWorker(QThread):
    result_ready = pyqtSignal(int, object, object)  # token, QPixmap|None, Exception|None

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self._queue: queue.Queue[_ThumbnailRequest | None] = queue.Queue()
        self._running = True
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="thumb")

    def enqueue(self, path: Path, size: tuple[int, int], token: int) -> None:
        self._queue.put(_ThumbnailRequest(path, size, token))
        if not self.isRunning():
            self.start()

    def clear_pending(self) -> None:
        while True:
            try:
                req = self._queue.get_nowait()
            except queue.Empty:
                break
            if req is None:
                self._queue.put(None)
                break

    def stop(self) -> None:
        self._running = False
        self.clear_pending()
        self._queue.put(None)  # sentinel to unblock get()
        self._pool.shutdown(wait=True)
        self.wait(3000)

    def run(self) -> None:
        while self._running:
            try:
                req = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if req is None:
                break
            self._pool.submit(self._process, req)

    def _process(self, req: _ThumbnailRequest) -> None:
        try:
            pil_img = self.cache.thumbnail(req.path, req.size)
            qpixmap = pil_to_qpixmap(pil_img)
            self.result_ready.emit(req.token, qpixmap, None)
        except Exception as exc:
            self.result_ready.emit(req.token, None, exc)


# ---------------------------------------------------------------------------
# Photo preview — single mode (QGraphicsView with zoom/pan/drag-drop)
# ---------------------------------------------------------------------------

class PhotoGraphicsView(QGraphicsView):
    photo_dropped = pyqtSignal(list)  # list of str paths
    zoom_changed = pyqtSignal(float)  # zoom level (1.0 = 100%)

    _ZOOM_MIN = 0.05
    _ZOOM_MAX = 20.0
    _ZOOM_STEP = 1.15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._zoom_level = 1.0
        self._is_fit_mode = True
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._normal_style = "background-color: #d8d8d8; border: 1px solid #667;"
        self._drag_style = "background-color: #d8d8d8; border: 2px dashed #2a6fbd;"
        self.setStyleSheet(self._normal_style)
        self.setAcceptDrops(True)

    def set_image(self, pixmap: QPixmap) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
        self._is_fit_mode = True
        self._zoom_level = self._current_zoom()

    def clear_image(self) -> None:
        self._scene.clear()
        self._pixmap_item = None
        self.resetTransform()
        self._zoom_level = 1.0
        self._is_fit_mode = True

    def fit_to_window(self) -> None:
        if self._pixmap_item:
            self.resetTransform()
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._is_fit_mode = True
            self._zoom_level = self._current_zoom()
            self.zoom_changed.emit(self._zoom_level)

    def zoom(self, factor: float) -> None:
        new_level = self._zoom_level * factor
        if self._ZOOM_MIN <= new_level <= self._ZOOM_MAX:
            self._zoom_level = new_level
            self._is_fit_mode = False
            self.scale(factor, factor)
            self.zoom_changed.emit(self._zoom_level)

    def zoom_percent(self) -> int:
        return int(self._zoom_level * 100)

    def _current_zoom(self) -> float:
        if not self._pixmap_item:
            return 1.0
        return self.transform().m11()

    def wheelEvent(self, event) -> None:
        factor = self._ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self._ZOOM_STEP
        new_level = self._zoom_level * factor
        if self._ZOOM_MIN <= new_level <= self._ZOOM_MAX:
            self._zoom_level = new_level
            self._is_fit_mode = False
            self.scale(factor, factor)
            self.zoom_changed.emit(self._zoom_level)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._pixmap_item:
            return super().mouseDoubleClickEvent(event)
        if self._is_fit_mode:
            self.resetTransform()
            self._zoom_level = 1.0
            self._is_fit_mode = False
            self.centerOn(self._pixmap_item)
        else:
            self.fit_to_window()
        self.zoom_changed.emit(self._zoom_level)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_fit_mode and self._pixmap_item:
            self.resetTransform()
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._zoom_level = self._current_zoom()
            self.zoom_changed.emit(self._zoom_level)

    # -- Drag-drop for photos --
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(self._drag_style)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet(self._normal_style)

    def dropEvent(self, event) -> None:
        self.setStyleSheet(self._normal_style)
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.photo_dropped.emit(paths)
        event.acceptProposedAction()


# ---------------------------------------------------------------------------
# Interactive image cells for grid mode
# ---------------------------------------------------------------------------

class GridPhotoView(QGraphicsView):
    clicked = pyqtSignal()
    double_clicked = pyqtSignal()
    right_clicked = pyqtSignal(object)
    zoom_changed = pyqtSignal(float)

    _ZOOM_MIN = 0.2
    _ZOOM_MAX = 8.0
    _ZOOM_STEP = 1.15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._zoom_level = 1.0
        self._is_fit_mode = True
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("background-color: #e8edf0; border: none;")

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self.fit_to_window()

    def show_message(self, text: str, error: bool = False) -> None:
        self._scene.clear()
        self._pixmap_item = None
        self.resetTransform()
        self._zoom_level = 1.0
        self._is_fit_mode = True
        item = self._scene.addText(text)
        item.setDefaultTextColor(Qt.darkRed if error else Qt.darkGray)
        rect = item.boundingRect()
        item.setPos(-rect.width() / 2, -rect.height() / 2)
        self._scene.setSceneRect(-120, -60, 240, 120)

    def fit_to_window(self) -> None:
        if not self._pixmap_item:
            return
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
        self._zoom_level = 1.0
        self._is_fit_mode = True
        self.zoom_changed.emit(self._zoom_level)

    def zoom(self, factor: float) -> None:
        if not self._pixmap_item:
            return
        new_level = self._zoom_level * factor
        if self._ZOOM_MIN <= new_level <= self._ZOOM_MAX:
            self._zoom_level = new_level
            self._is_fit_mode = False
            self.scale(factor, factor)
            self.zoom_changed.emit(self._zoom_level)

    def zoom_percent(self) -> int:
        return int(self._zoom_level * 100)

    def wheelEvent(self, event) -> None:
        factor = self._ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self._ZOOM_STEP
        self.zoom(factor)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.right_clicked.emit(event)
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_fit_mode and self._pixmap_item:
            self.fit_to_window()


_GRID_CELL_NORMAL_STYLE = "QFrame#GridPhotoCell { background-color: #eef2f3; border: 2px solid #7a858a; }"
_GRID_CELL_SELECTED_STYLE = "QFrame#GridPhotoCell { background-color: #eef2f3; border: 2px solid #2a6fbd; }"
_GRID_FILENAME_STYLE = "color: #263238; background-color: #dfe7eb; padding: 1px 4px; font-size: 11px;"


class GridPhotoCell(QFrame):
    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)
    right_clicked = pyqtSignal(int, object)
    zoom_changed = pyqtSignal(int, float)

    def __init__(self, index: int, show_filename: bool, parent=None):
        super().__init__(parent)
        self._index = index
        self._filename = ""
        self.setObjectName("GridPhotoCell")
        self.setStyleSheet(_GRID_CELL_NORMAL_STYLE)
        self.setMinimumSize(80, 90)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        self._name_label = QLabel()
        self._name_label.setAlignment(Qt.AlignCenter)
        self._name_label.setFixedHeight(20)
        self._name_label.setStyleSheet(_GRID_FILENAME_STYLE)
        self._name_label.setVisible(show_filename)
        layout.addWidget(self._name_label)

        self._view = GridPhotoView()
        self._view.clicked.connect(lambda: self.clicked.emit(self._index))
        self._view.double_clicked.connect(lambda: self.double_clicked.emit(self._index))
        self._view.right_clicked.connect(lambda event: self.right_clicked.emit(self._index, event))
        self._view.zoom_changed.connect(lambda zoom: self.zoom_changed.emit(self._index, zoom))
        layout.addWidget(self._view, stretch=1)

        self.set_loading()

    @property
    def photo_index(self) -> int:
        return self._index

    def set_filename(self, filename: str) -> None:
        self._filename = filename
        self._name_label.setToolTip(filename)
        self._update_filename_label()

    def set_filename_visible(self, visible: bool) -> None:
        self._name_label.setVisible(visible)
        self._update_filename_label()

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(_GRID_CELL_SELECTED_STYLE if selected else _GRID_CELL_NORMAL_STYLE)

    def set_loading(self) -> None:
        self._view.show_message("加载中")

    def set_error(self, text: str) -> None:
        self._view.show_message(text, error=True)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._view.set_pixmap(pixmap)

    def zoom(self, factor: float) -> None:
        self._view.zoom(factor)

    def fit_to_window(self) -> None:
        self._view.fit_to_window()

    def zoom_percent(self) -> int:
        return self._view.zoom_percent()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_filename_label()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.right_clicked.emit(self._index, event)
        event.accept()

    def _update_filename_label(self) -> None:
        if not self._filename:
            self._name_label.setText("")
            return
        width = max(50, self._name_label.width() - 8)
        text = QFontMetrics(self._name_label.font()).elidedText(self._filename, Qt.ElideMiddle, width)
        self._name_label.setText(text)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SpecimenWindow(QMainWindow):
    def __init__(self, workspace_root: Path | str | None):
        super().__init__()
        self.setWindowTitle("标本入库管理")
        self.setWindowIcon(get_app_icon())
        self.resize(1320, 820)
        self.setMinimumSize(QSize(1100, 680))

        prepared = self._prepare_start_workspace(workspace_root)
        if prepared is None:
            self.close()
            raise SystemExit
        self.workspace_root, create_workspace_files = prepared

        try:
            self.store = ExcelStore(self.workspace_root, lock=True, create_if_missing=create_workspace_files)
        except WorkspaceLockedError as exc:
            lock_path = self.workspace_root / "数据" / ".workspace.lock"
            msg = f'{exc}\n\n如果软件已退出但仍然被占用，可以点击"强制解锁"。'
            btn = QMessageBox.critical(self, "工作区被占用", msg, QMessageBox.Abort | QMessageBox.Retry)
            if btn == QMessageBox.Retry and lock_path.exists():
                try:
                    lock_path.unlink()
                    self.store = ExcelStore(self.workspace_root, lock=True, create_if_missing=create_workspace_files)
                except Exception:
                    self.close()
                    raise SystemExit
            else:
                self.close()
                raise SystemExit from exc
        except WorkspaceNotInitializedError as exc:
            QMessageBox.critical(self, "工作区未初始化", str(exc))
            self.close()
            raise SystemExit from exc

        self.matcher = SpeciesMatcher(self.workspace_root / "字段模版" / "表格信息预设字段.xlsx")
        self.current_voucher: str | None = None
        self.current_photos: list[dict[str, str]] = []
        self.current_photo_index = 0
        self._loading = False
        self._is_closing = False
        self._photo_load_token = 0
        self._grid_load_token = 0
        self._import_job_active = False
        self._grid_mode_before_expand = ""
        self._photo_page_size = 200
        self._photo_page = 0
        self._photo_view_states: dict[str, tuple[float, int, int]] = {}
        self._show_grid_filenames = load_settings().show_grid_filenames

        self.thumbnail_cache = ThumbnailCache(self.workspace_root)
        self.search_index: ImageSearchIndex | None = None
        self._index_build_worker: IndexBuildWorker | None = None
        self._thumb_worker = ThumbnailWorker(self.thumbnail_cache, self)
        self._thumb_worker.result_ready.connect(self._on_thumbnail_ready)
        self._thumb_worker.start()

        self.specimen_widgets: dict[str, QLineEdit | QComboBox] = {}
        self.class_widgets: dict[str, QLineEdit] = {}
        self.photo_widgets: dict[str, QLineEdit] = {}

        self._save_timers: dict[str, QTimer] = {}
        self._list_refresh_timer = QTimer(self)
        self._list_refresh_timer.setSingleShot(True)
        self._list_refresh_timer.timeout.connect(self.refresh_list)
        self._view_mode: str = "单张"
        self._current_qpixmap: QPixmap | None = None
        self._grid_labels: list[GridPhotoCell] = []
        self._grid_requests: dict[int, int] = {}  # token -> slot index

        self._build_ui()

        remember_workspace(self.workspace_root)
        self.refresh_list()
        vouchers = self.store.list_vouchers()
        if vouchers:
            self.select_voucher(vouchers[0])
        QTimer.singleShot(500, self._build_search_index_background)

    # ---- workspace preparation ----

    def _prepare_start_workspace(self, workspace_root: Path | str | None) -> tuple[Path, bool] | None:
        candidate = Path(workspace_root).resolve() if workspace_root else None
        while True:
            if candidate is None:
                selected = QFileDialog.getExistingDirectory(self, "选择工作区目录")
                if not selected:
                    return None
                candidate = Path(selected).resolve()
            prepared = self._prepare_workspace_candidate(candidate)
            if prepared is not None:
                return prepared
            candidate = None

    def _prepare_workspace_candidate(self, path: Path) -> tuple[Path, bool] | None:
        if is_generated_workspace_path(path):
            QMessageBox.critical(
                self, "不能使用软件目录",
                f"不能把 build/dist/releases 等软件构建或版本目录作为工作区：\n{path}\n\n请选择实际保存数据和照片的工作目录。",
            )
            return None
        if has_workspace_data(path):
            return path, False
        if QMessageBox.question(
            self, "初始化工作区",
            f"该目录还没有数据文件：\n{path}\n\n是否在此目录创建新的数据文件夹和 Excel 数据文件？",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return None
        initialize_workspace(path, self._template_source_for_initialization(path))
        return path, True

    def _template_source_for_initialization(self, target: Path) -> Path | None:
        candidates = [
            self.workspace_root if hasattr(self, "workspace_root") else None,
            target,
            Path.cwd(),
            Path(sys.executable).resolve().parent,
            Path(sys.executable).resolve().parent.parent,
            Path(sys.executable).resolve().parent.parent.parent,
            Path(__file__).resolve().parents[1],
        ]
        for candidate in candidates:
            if not candidate:
                continue
            source = Path(candidate).resolve()
            if (source / "字段模版").exists():
                return source
        return None

    # ---- close ----

    def closeEvent(self, event) -> None:
        self._is_closing = True
        # Stop background index builder if running
        idx_worker = getattr(self, "_index_build_worker", None)
        if idx_worker is not None and idx_worker.isRunning():
            idx_worker.requestInterruption()
            idx_worker.wait(5000)
        # Stop thumbnail worker
        thumb = getattr(self, "_thumb_worker", None)
        if thumb is not None:
            thumb.stop()
        try:
            settings = load_settings()
            settings.window_geometry = self.saveGeometry().toBase64().data().decode()
            settings.splitter_sizes = [
                [int(x) for x in self.main_splitter.sizes()],
                [int(x) for x in self.right_splitter.sizes()],
            ]
            settings.show_grid_filenames = getattr(self, "_show_grid_filenames", settings.show_grid_filenames)
            save_settings(settings)
        except Exception:
            pass
        store = getattr(self, "store", None)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        event.accept()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.photo_table and event.type() == event.KeyPress:
            if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_A:
                self.photo_table.selectAll()
                return True
        return super().eventFilter(obj, event)

    # ---- UI building ----

    def _build_ui(self) -> None:
        # Toolbar
        toolbar = QToolBar("主工具栏")
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for label, slot in [
            ("＋新增标本", self.new_specimen),
            ("导入工作区", self.import_workspace),
            ("导入数据", self.import_data_file),
            ("导出数据", self.export_data),
            ("切换工作区", self.switch_workspace),
            ("撤回", self.undo),
            ("返回", self.redo),
            ("清除照片关联", self.clear_photos),
            ("入库汇总", self.open_ingest_summary),
            ("版本管理", self.open_version_manager),
            ("设置", self.open_settings),
        ]:
            action = QAction(label, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)

        # Workspace bar
        ws_bar = QHBoxLayout()
        ws_label = QLabel(f"当前工作目录：{self.workspace_root}")
        ws_label.setContentsMargins(8, 4, 8, 4)
        self.workspace_label = ws_label
        ws_bar.addWidget(ws_label)
        ws_bar.addStretch()
        self.dashboard_label = QLabel()
        self.dashboard_label.setStyleSheet(
            "color: #1a5faa; font-weight: bold; font-size: 13px; padding: 4px 12px;"
            "background-color: #e8f0fe; border-radius: 4px;"
        )
        ws_bar.addWidget(self.dashboard_label)
        self._update_dashboard()

        # Central widget — photo preview area
        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(8, 8, 8, 4)
        central_layout.addLayout(ws_bar)

        # Stacked: graphics view (single) + grid frame (grid)
        self._photo_stack_container = QWidget()
        photo_stack = QVBoxLayout(self._photo_stack_container)
        photo_stack.setContentsMargins(0, 0, 0, 0)

        self.photo_view = PhotoGraphicsView()
        self.photo_view.photo_dropped.connect(self.add_photo_paths_async)
        self.photo_view.zoom_changed.connect(self._on_zoom_changed)
        photo_stack.addWidget(self.photo_view)

        self.grid_frame = QFrame()
        self.grid_layout = QGridLayout(self.grid_frame)
        self.grid_layout.setSpacing(10)
        self.grid_frame.setAcceptDrops(True)
        self.grid_frame.dragEnterEvent = lambda e: (e.acceptProposedAction(), self.grid_frame.setStyleSheet("border: 2px dashed #2a6fbd;")) if e.mimeData().hasUrls() else None
        self.grid_frame.dragMoveEvent = lambda e: e.acceptProposedAction() if e.mimeData().hasUrls() else None
        self.grid_frame.dragLeaveEvent = lambda e: self.grid_frame.setStyleSheet("")
        self.grid_frame.dropEvent = self._on_grid_drop
        self.grid_frame.hide()
        photo_stack.addWidget(self.grid_frame)

        self._placeholder_label = QLabel('点击"添加照片"关联图片')
        self._placeholder_label.setAlignment(Qt.AlignCenter)
        self._placeholder_label.setStyleSheet("color: #59666b; font-size: 14px;")
        photo_stack.addWidget(self._placeholder_label)
        self._placeholder_label.raise_()

        central_layout.addWidget(self._photo_stack_container, stretch=1)

        # Photo controls
        controls = QHBoxLayout()
        self._return_grid_btn = QPushButton("返回网格")
        self._return_grid_btn.clicked.connect(self.return_to_grid)
        self._return_grid_btn.hide()
        controls.addWidget(self._return_grid_btn)
        for label, slot in [
            ("添加照片", self.add_photo),
            ("检索图片", self.open_image_search),
            ("删除照片", self.delete_photo),
            ("分配入库编号", self._assign_voucher_to_selected),
            ("上一张", lambda: self.shift_photo(-1)),
            ("下一张", lambda: self.shift_photo(1)),
            ("-", lambda: self.adjust_zoom(0.8)),
            ("适配", self.fit_image),
            ("+", lambda: self.adjust_zoom(1.25)),
            ("打开原图", self.open_current_photo_external),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            if label in ("-", "+"):
                btn.setFixedWidth(30)
            controls.addWidget(btn)
        self.photo_counter = QLabel("0 / 0")
        controls.addWidget(self.photo_counter, alignment=Qt.AlignRight)
        self._confirm_btn = QPushButton("确定保存")
        self._confirm_btn.setStyleSheet("QPushButton { font-weight: bold; background-color: #2a6fbd; color: white; padding: 4px 12px; border-radius: 3px; }")
        self._confirm_btn.clicked.connect(self._confirm_save)
        controls.addWidget(self._confirm_btn)
        self._zoom_label = QLabel("")
        self._zoom_label.setStyleSheet("color: #59666b;")
        controls.addWidget(self._zoom_label)
        central_layout.addLayout(controls)

        # View mode
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("显示"))
        self._view_buttons = []
        for label, count, short in VIEW_MODES:
            btn = QPushButton(short)
            btn.setCheckable(True)
            btn.setFixedWidth(36)
            btn.setToolTip(label)
            btn.setProperty("grid_count", count)
            btn.setStyleSheet(_VIEW_BTN_STYLE)
            btn.clicked.connect(lambda checked, c=count: self._set_view_mode(c))
            view_row.addWidget(btn)
            self._view_buttons.append(btn)
        self._view_buttons[0].setChecked(True)
        self._show_filename_check = QCheckBox("显示文件名")
        self._show_filename_check.setChecked(self._show_grid_filenames)
        self._show_filename_check.stateChanged.connect(self._on_show_grid_filenames_changed)
        view_row.addWidget(self._show_filename_check)
        view_row.addStretch()
        central_layout.addLayout(view_row)

        # ---- Panels (QSplitter layout — no QDockWidgets) ----
        # Left: voucher panel with table + filter + pagination
        voucher_content = QWidget()
        voucher_layout = QVBoxLayout(voucher_content)
        voucher_layout.setContentsMargins(0, 0, 0, 0)
        voucher_layout.setSpacing(2)
        # Search + quick filter
        filter_row = QHBoxLayout()
        self._voucher_search = QLineEdit()
        self._voucher_search.setPlaceholderText("搜索编号...")
        self._voucher_search.setClearButtonEnabled(True)
        self._voucher_search.textChanged.connect(self._apply_voucher_filter)
        filter_row.addWidget(self._voucher_search)
        voucher_layout.addLayout(filter_row)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(2)
        self._filter_buttons: dict[str, QPushButton] = {}
        for key, label in [("all","全部"),("claimed","已认领"),("complete","完整"),("incomplete","待补全")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet("QPushButton { font-size: 10px; padding: 1px 6px; } QPushButton:checked { background-color: #2a6fbd; color: white; }")
            btn.clicked.connect(lambda checked, k=key: self._set_voucher_filter(k))
            quick_row.addWidget(btn)
            self._filter_buttons[key] = btn
        self._filter_buttons["all"].setChecked(True)
        self._active_filter = "all"
        voucher_layout.addLayout(quick_row)
        # Table: 入库编号 | 标本 | 照片 | 分类 | 认领 | 照片数
        self.voucher_table = QTableWidget(0, 6)
        self.voucher_table.setHorizontalHeaderLabels(["入库编号","标本","照片","分类","认领","照片数"])
        self.voucher_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.voucher_table.setSelectionMode(QTableWidget.SingleSelection)
        self.voucher_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.voucher_table.verticalHeader().setVisible(False)
        self.voucher_table.setColumnWidth(0, 85)
        self.voucher_table.setColumnWidth(1, 36)
        self.voucher_table.setColumnWidth(2, 36)
        self.voucher_table.setColumnWidth(3, 36)
        self.voucher_table.setColumnWidth(4, 52)
        self.voucher_table.setColumnWidth(5, 42)
        self.voucher_table.horizontalHeader().setStretchLastSection(True)
        self.voucher_table.setFont(QFont("Consolas", 10))
        self.voucher_table.itemSelectionChanged.connect(self._on_voucher_table_selected)
        self.voucher_table.horizontalHeader().sectionClicked.connect(self._on_voucher_header_clicked)
        self.voucher_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.voucher_table.customContextMenuRequested.connect(self._voucher_context_menu)
        self._col_filters: dict[int, str] = {}  # col_index -> filter value
        self._col_header_labels = ["入库编号","标本","照片","分类","认领","照片数"]
        voucher_layout.addWidget(self.voucher_table, stretch=1)
        # Pagination
        page_row = QHBoxLayout()
        self._voucher_page_label = QLabel("")
        page_row.addWidget(self._voucher_page_label)
        page_row.addStretch()
        self._voucher_prev_btn = QPushButton("◀")
        self._voucher_prev_btn.setFixedWidth(28)
        self._voucher_prev_btn.clicked.connect(self._voucher_prev_page)
        page_row.addWidget(self._voucher_prev_btn)
        self._voucher_next_btn = QPushButton("▶")
        self._voucher_next_btn.setFixedWidth(28)
        self._voucher_next_btn.clicked.connect(self._voucher_next_page)
        page_row.addWidget(self._voucher_next_btn)
        voucher_layout.addLayout(page_row)
        self._voucher_page = 0
        self._voucher_page_size = 200
        self.voucher_panel = self._create_panel("入库编号", voucher_content, collapsible=False)
        self.voucher_panel.setMinimumWidth(290)

        # Right: specimen info panel
        specimen_content = QWidget()
        sf_layout = QFormLayout(specimen_content)
        sf_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        sf_layout.setContentsMargins(0, 0, 0, 0)
        for field in SPECIMEN_HEADERS:
            if field == "保存方式":
                widget = QComboBox()
                widget.addItems(SAVE_METHOD_OPTIONS)
                widget.setEditable(True)
                widget.currentTextChanged.connect(lambda text, f=field: self.schedule_save("specimen", f))
            else:
                widget = QLineEdit()
                widget.textChanged.connect(lambda text, f=field: self.schedule_save("specimen", f))
            sf_layout.addRow(field, widget)
            self.specimen_widgets[field] = widget
        spec_save_btn = QPushButton("保存标本信息")
        spec_save_btn.clicked.connect(lambda: self._save_panel("specimen"))
        sf_layout.addRow(spec_save_btn)
        self.specimen_panel = self._create_panel("标本信息", specimen_content)

        # Right: photo info panel
        photo_content = QWidget()
        pf_layout = QVBoxLayout(photo_content)
        pf_layout.setContentsMargins(0, 0, 0, 0)
        self.photo_table = QTableWidget(0, 4)
        self.photo_table.setHorizontalHeaderLabels(["序号", "文件名", "相对路径", "描述"])
        self.photo_table.horizontalHeader().setStretchLastSection(True)
        self.photo_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.photo_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.photo_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.photo_table.setColumnWidth(0, 44)
        self.photo_table.setColumnWidth(1, 150)
        self.photo_table.setColumnWidth(2, 210)
        self.photo_table.currentCellChanged.connect(self._on_photo_table_row_changed)
        self.photo_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.photo_table.customContextMenuRequested.connect(self._photo_table_context_menu)
        self.photo_table.installEventFilter(self)
        pf_layout.addWidget(self.photo_table, stretch=1)
        page_row = QHBoxLayout()
        self._page_label = QLabel("")
        page_row.addWidget(self._page_label, stretch=1)
        self._prev_page_btn = QPushButton("上一页")
        self._prev_page_btn.clicked.connect(self._photo_prev_page)
        page_row.addWidget(self._prev_page_btn)
        self._next_page_btn = QPushButton("下一页")
        self._next_page_btn.clicked.connect(self._photo_next_page)
        page_row.addWidget(self._next_page_btn)
        pf_layout.addLayout(page_row)
        for field in ("文件名", "相对路径", "描述"):
            row = QHBoxLayout()
            row.addWidget(QLabel(field))
            widget = QLineEdit()
            if field == "相对路径":
                widget.setReadOnly(True)
            elif field == "文件名":
                widget.textChanged.connect(lambda text, f=field: self.schedule_save("photo", f))
                widget.setContextMenuPolicy(Qt.CustomContextMenu)
                widget.customContextMenuRequested.connect(lambda pos, w=widget: self._filename_context_menu(w, pos))
            else:
                widget.textChanged.connect(lambda text: self.schedule_save("photo", "描述"))
            self.photo_widgets[field] = widget
            row.addWidget(widget)
            pf_layout.addLayout(row)
        photo_save_btn = QPushButton("保存照片信息")
        photo_save_btn.clicked.connect(lambda: self._save_panel("photo"))
        pf_layout.addWidget(photo_save_btn)
        self.photo_panel = self._create_panel("照片信息", photo_content)

        # Right: classification panel
        class_content = QWidget()
        cf_layout = QFormLayout(class_content)
        cf_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        cf_layout.setContentsMargins(0, 0, 0, 0)
        for field in CLASSIFICATION_HEADERS[1:]:
            widget = QLineEdit()
            widget.textChanged.connect(lambda text, f=field: self.schedule_save("classification", f))
            cf_layout.addRow(field, widget)
            self.class_widgets[field] = widget
            if field == "种名*":
                self._setup_species_completer(widget)
        class_save_btn = QPushButton("保存分类信息")
        class_save_btn.clicked.connect(lambda: self._save_panel("classification"))
        cf_layout.addRow(class_save_btn)
        self.class_panel = self._create_panel("分类信息", class_content)

        # Right vertical splitter
        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.addWidget(self.specimen_panel)
        self.right_splitter.addWidget(self.photo_panel)
        self.right_splitter.addWidget(self.class_panel)
        self.right_splitter.setSizes([260, 280, 200])

        # Right side container with save-all button at top
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 4)
        right_layout.setSpacing(4)
        save_all_btn = QPushButton("一键保存所有")
        save_all_btn.setStyleSheet("QPushButton { font-weight: bold; background-color: #2a6fbd; color: white; padding: 6px; border-radius: 3px; }")
        save_all_btn.clicked.connect(self._save_all_panels)
        right_layout.addWidget(save_all_btn)
        right_layout.addWidget(self.right_splitter, stretch=1)

        # Main horizontal splitter — reuse existing 'central' widget as center
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.voucher_panel)
        self.main_splitter.addWidget(central)
        self.main_splitter.addWidget(right_container)
        self.main_splitter.setSizes([220, 580, 480])
        self.main_splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.main_splitter)

        # Restore saved splitter sizes
        saved = load_settings()
        if saved.splitter_sizes and len(saved.splitter_sizes) >= 2:
            try:
                self.main_splitter.setSizes([int(x) for x in saved.splitter_sizes[0]])
                self.right_splitter.setSizes([int(x) for x in saved.splitter_sizes[1]])
            except Exception:
                pass
        if saved.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(saved.window_geometry.encode()))

        # ---- Menu bar ----
        tools_menu = self.menuBar().addMenu("工具")
        tools_menu.addAction("入库汇总", self.open_ingest_summary)
        tools_menu.addAction("操作记录", self._open_action_log)

        view_menu = self.menuBar().addMenu("视图")
        for panel_name, panel_ref in [
            ("入库编号", self.voucher_panel),
            ("标本信息", self.specimen_panel),
            ("照片信息", self.photo_panel),
            ("分类信息", self.class_panel),
        ]:
            action = QAction(panel_name, self, checkable=True, checked=True)
            action.toggled.connect(lambda checked, p=panel_ref: p.setVisible(checked))
            view_menu.addAction(action)
        view_menu.addSeparator()
        reset_layout_action = QAction("重置窗口布局", self)
        reset_layout_action.triggered.connect(self._reset_window_layout)
        view_menu.addAction(reset_layout_action)

        # ---- Status bar ----
        self.statusBar().showMessage("就绪")
        self._status_dashboard = QLabel()
        self._status_dashboard.setStyleSheet("color: #1a5faa; font-weight: bold; padding: 0 8px;")
        self.statusBar().addPermanentWidget(self._status_dashboard)
        self.statusBar().addPermanentWidget(QLabel(f"软件版本：v{__version__}"))

        # ---- Keyboard shortcuts ----
        fit_shortcut = QAction(self)
        fit_shortcut.setShortcut(QKeySequence("F"))
        fit_shortcut.triggered.connect(self.fit_image)
        self.addAction(fit_shortcut)

        esc_shortcut = QAction(self)
        esc_shortcut.setShortcut(QKeySequence("Esc"))
        esc_shortcut.triggered.connect(self.return_to_grid)
        self.addAction(esc_shortcut)

    # ---- Species autocomplete ----

    def _setup_species_completer(self, line_edit: QLineEdit) -> None:
        self._species_completer = QCompleter(self)
        self._species_model = QStringListModel(self)
        self._species_completer.setModel(self._species_model)
        self._species_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._species_completer.activated.connect(self._on_species_activated)
        line_edit.setCompleter(self._species_completer)
        self._species_match_map: dict[str, SpeciesMatch] = {}
        line_edit.textChanged.connect(self._update_species_suggestions)

    def _update_species_suggestions(self, text: str) -> None:
        if not text or len(text) < 1:
            return
        matches = self.matcher.matches(text)
        display_list = []
        self._species_match_map.clear()
        for m in matches:
            display = f"{m.chinese_name}  {m.latin_name}  {m.family_name}"
            display_list.append(display)
            self._species_match_map[display] = m
        self._species_model.setStringList(display_list)

    def _on_species_activated(self, text: str) -> None:
        match = self._species_match_map.get(text)
        if not match or not self.current_voucher:
            return
        self._loading = True
        self.class_widgets["种名*"].setText(match.chinese_name)
        self.class_widgets["种拉丁"].setText(match.latin_name)
        self.class_widgets["科*"].setText(match.family_name)
        self.class_widgets["科拉丁"].setText(match.family_latin)
        self.store.set_fields(
            "classification", self.current_voucher,
            {"种名*": match.chinese_name, "种拉丁": match.latin_name,
             "科*": match.family_name, "科拉丁": match.family_latin},
            action_type="classification_autofill",
        )
        self.refresh_list()
        self._loading = False

    # ---- Voucher list ----

    def _schedule_list_refresh(self) -> None:
        self._list_refresh_timer.start(300)

    def refresh_list(self) -> None:
        current = self.current_voucher
        self._all_vouchers = self.store.list_vouchers()
        self._all_flags = self.store.all_status_flags()
        self._all_photo_counts = self.store.voucher_photo_counts()
        self._apply_voucher_filter()
        if current and current in self._all_flags:
            self._select_voucher_in_table(current)
        self._update_dashboard()

    def _on_voucher_header_clicked(self, col: int) -> None:
        """Cycle column filter: all -> √ -> × -> all (or all -> 已认领 -> 未认领 -> all for col 4)."""
        if col == 0 or col == 5:  # 入库编号 or 照片数 — no per-column filter
            return
        # Clear quick filter when using column filters
        self._active_filter = "all"
        for btn in self._filter_buttons.values():
            btn.setChecked(False)
        self._filter_buttons["all"].setChecked(True)
        current = self._col_filters.get(col, "")
        if col == 4:  # 认领 column cycles: all -> 已认领 -> 未认领
            cycle = {"": "已认领", "已认领": "未认领", "未认领": ""}
        else:  # specimen/photo/class columns cycle: all -> √ -> ×
            cycle = {"": "√", "√": "×", "×": ""}
        self._col_filters[col] = cycle.get(current, "")
        self._update_header_labels()
        self._voucher_page = 0
        self._apply_voucher_filter()

    def _update_header_labels(self) -> None:
        labels = list(self._col_header_labels)
        for col, val in self._col_filters.items():
            if val:
                labels[col] = f"{self._col_header_labels[col]} {val}"
        self.voucher_table.setHorizontalHeaderLabels(labels)

    def _apply_voucher_filter(self) -> None:
        search = self._voucher_search.text().strip().lower()
        vouchers = self._all_vouchers
        # Apply search
        if search:
            vouchers = [v for v in vouchers if search in v.lower()]
        # Apply quick filter
        af = self._active_filter
        if af == "claimed":
            vouchers = [v for v in vouchers if self._all_photo_counts.get(v, 0) > 0]
        elif af == "complete":
            vouchers = [v for v in vouchers if self._all_flags.get(v) and self._all_flags[v].label() == "√√√"]
        elif af == "incomplete":
            vouchers = [v for v in vouchers if self._all_flags.get(v) and self._all_flags[v].label() != "√√√"]
        # Apply per-column filters
        for col, val in self._col_filters.items():
            if not val:
                continue
            if col == 4:  # 认领
                vouchers = [v for v in vouchers if (self._all_photo_counts.get(v, 0) > 0) == (val == "已认领")]
            elif col in (1, 2, 3):  # 标本/照片/分类: filter by √ or ×
                idx = col - 1  # 0=specimen, 1=photo, 2=class
                vouchers = [v for v in vouchers if self._all_flags.get(v) and self._all_flags[v].label()[idx] == val]
        # Paginate
        self._filtered_vouchers = vouchers
        total_pages = max(1, (len(vouchers) + self._voucher_page_size - 1) // self._voucher_page_size)
        self._voucher_page = min(self._voucher_page, total_pages - 1)
        start = self._voucher_page * self._voucher_page_size
        page = vouchers[start:start + self._voucher_page_size]
        # Populate table
        self.voucher_table.blockSignals(True)
        self.voucher_table.setRowCount(len(page))
        for i, v in enumerate(page):
            f = self._all_flags.get(v)
            label = f.label() if f else "×××"
            pc = self._all_photo_counts.get(v, 0)
            claimed = "已认领" if pc > 0 else "未认领"
            self.voucher_table.setItem(i, 0, QTableWidgetItem(v))
            self.voucher_table.setItem(i, 1, QTableWidgetItem(label[0]))
            self.voucher_table.setItem(i, 2, QTableWidgetItem(label[1]))
            self.voucher_table.setItem(i, 3, QTableWidgetItem(label[2]))
            self.voucher_table.setItem(i, 4, QTableWidgetItem(claimed))
            self.voucher_table.setItem(i, 5, QTableWidgetItem(str(pc)))
        self.voucher_table.blockSignals(False)
        self._voucher_page_label.setText(f"第 {self._voucher_page + 1}/{total_pages} 页")
        self._voucher_prev_btn.setEnabled(self._voucher_page > 0)
        self._voucher_next_btn.setEnabled(self._voucher_page < total_pages - 1)

    def _set_voucher_filter(self, key: str) -> None:
        for k, btn in self._filter_buttons.items():
            btn.setChecked(k == key)
        self._active_filter = key
        self._col_filters.clear()
        self._update_header_labels()
        self._voucher_page = 0
        self._apply_voucher_filter()

    def _voucher_prev_page(self) -> None:
        if self._voucher_page > 0:
            self._voucher_page -= 1
            self._apply_voucher_filter()

    def _voucher_next_page(self) -> None:
        total_pages = max(1, (len(self._filtered_vouchers) + self._voucher_page_size - 1) // self._voucher_page_size)
        if self._voucher_page < total_pages - 1:
            self._voucher_page += 1
            self._apply_voucher_filter()

    def _on_voucher_table_selected(self) -> None:
        rows = self.voucher_table.selectionModel().selectedRows()
        if not rows:
            return
        voucher = self.voucher_table.item(rows[0].row(), 0).text()
        self.select_voucher(voucher)

    def _select_voucher_in_table(self, voucher: str) -> None:
        for row in range(self.voucher_table.rowCount()):
            item = self.voucher_table.item(row, 0)
            if item and item.text() == voucher:
                self.voucher_table.selectRow(row)
                return

    def _update_dashboard(self) -> None:
        if not hasattr(self, "_dashboard_timer"):
            self._dashboard_timer = QTimer(self)
            self._dashboard_timer.setSingleShot(True)
            self._dashboard_timer.timeout.connect(self._compute_dashboard)
        self._dashboard_timer.start(1000)  # debounce 1s

    def _compute_dashboard(self) -> None:
        all_v = self.store.list_vouchers()
        photo_counts = self.store.voucher_photo_counts()
        total_photos = sum(photo_counts.values())
        claimed = sum(1 for v in all_v if photo_counts.get(v, 0) > 0)
        flags = self.store.all_status_flags()
        complete = sum(1 for v in all_v if flags.get(v) and flags[v].label() == "√√√")
        text = f"照片：{total_photos} 张 | 已认领：{claimed} 个 | 完整：{complete} 个"
        self.dashboard_label.setText(text)
        if hasattr(self, "_status_dashboard"):
            self._status_dashboard.setText(text)

    def select_voucher(self, voucher: str) -> None:
        self._save_current_photo_view_state()
        self._loading = True
        self.current_voucher = voucher
        specimen = self.store.get_specimen(voucher) or {}
        classification = self.store.get_classification(voucher) or {}
        for field, widget in self.specimen_widgets.items():
            widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                widget.setCurrentText(str(specimen.get(field, "")))
            else:
                widget.setText(str(specimen.get(field, "")))
            widget.blockSignals(False)
        for field, widget in self.class_widgets.items():
            widget.blockSignals(True)
            widget.setText(str(classification.get(field, "")))
            widget.blockSignals(False)
        self.current_photos = self.store.get_photos(voucher)
        self.current_photo_index = min(self.current_photo_index, max(0, len(self.current_photos) - 1))
        self._photo_page = 0
        self._loading = False
        self.refresh_photo_table()
        self.load_current_photo()

    # ---- Field save (debounced) ----

    def schedule_save(self, category: str, field: str) -> None:
        if self._loading or not self.current_voucher:
            return
        voucher = self.current_voucher
        key = f"{category}:{field}"
        if key not in self._save_timers:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda v=voucher, c=category, f=field: self.save_field(c, f, v))
            self._save_timers[key] = timer
        self._save_timers[key].start(500)

    def _cancel_pending_saves(self) -> None:
        for timer in self._save_timers.values():
            timer.stop()
        self._save_timers.clear()

    def save_field(self, category: str, field: str, voucher: str | None = None) -> None:
        if voucher is None:
            voucher = self.current_voucher
        if not voucher:
            return
        # If the user switched vouchers since the timer was scheduled, discard
        if voucher != self.current_voucher:
            return
        try:
            if category == "specimen":
                widget = self.specimen_widgets[field]
                value = widget.currentText() if isinstance(widget, QComboBox) else widget.text()
                changed = self.store.set_fields("specimen", voucher, {field: value})
                if changed:
                    specimen = self.store.get_specimen(voucher) or {}
                    self._loading = True
                    for auto_field in ("采集日期", "采集地点缩写*"):
                        w = self.specimen_widgets[auto_field]
                        w.blockSignals(True)
                        w.setText(str(specimen.get(auto_field, "")))
                        w.blockSignals(False)
                    self._loading = False
            elif category == "classification":
                self.store.set_fields("classification", voucher, {field: self.class_widgets[field].text()})
            elif category == "photo":
                if field == "描述":
                    self.store.set_photo_description(voucher, self.current_photo_index, self.photo_widgets["描述"].text())
                elif field == "文件名":
                    rows = self.store.read_rows("photo")
                    matches = [i for i, r in enumerate(rows) if self.store._value(r, "入库编号*") == voucher]
                    if self.current_photo_index < len(matches):
                        idx = matches[self.current_photo_index]
                        rows[idx]["文件名"] = self.photo_widgets["文件名"].text()
                        self.store._write_rows("photo", rows)
                        self.store._update_summary_modified(voucher)
                        self.store._record_action("update_photo", voucher, "photo", "文件名", {}, rows[idx].copy())
                self.current_photos = self.store.get_photos(voucher)
                self.refresh_photo_table()
            self._schedule_list_refresh()
        except Exception as exc:
            # Roll back widget to last-known-good value from the store
            try:
                if category == "specimen":
                    row = self.store.get_specimen(voucher) or {}
                    widget = self.specimen_widgets[field]
                    widget.blockSignals(True)
                    stored = str(row.get(field, ""))
                    if isinstance(widget, QComboBox):
                        widget.setCurrentText(stored)
                    else:
                        widget.setText(stored)
                    widget.blockSignals(False)
                elif category == "classification":
                    row = self.store.get_classification(voucher) or {}
                    widget = self.class_widgets[field]
                    widget.blockSignals(True)
                    widget.setText(str(row.get(field, "")))
                    widget.blockSignals(False)
            except Exception:
                pass
            QMessageBox.critical(self, "保存失败", str(exc))

    # ---- Specimen CRUD ----

    def new_specimen(self) -> None:
        try:
            voucher = self.store.create_specimen()
            self.refresh_list()
            self.select_voucher(voucher)
        except Exception as exc:
            QMessageBox.critical(self, "新增失败", str(exc))

    def clear_photos(self) -> None:
        if not self.current_voucher:
            return
        photos = self.store.get_photos(self.current_voucher)
        if not photos:
            QMessageBox.information(self, "清除照片关联", f"{self.current_voucher} 没有关联的照片。")
            return
        if QMessageBox.question(
            self, "清除照片关联", f"确定清除 {self.current_voucher} 的全部 {len(photos)} 张照片关联吗？\n\n（入库编号、标本信息和分类信息将保留不变）",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        count = self.store.clear_photos(self.current_voucher)
        self.statusBar().showMessage(f"已清除 {self.current_voucher} 的 {count} 张照片关联", 3000)
        self.reload_current()

    def _voucher_context_menu(self, pos) -> None:
        index = self.voucher_table.indexAt(pos)
        if not index.isValid():
            return
        voucher = self.voucher_table.item(index.row(), 0)
        if not voucher:
            return
        voucher_text = voucher.text().strip()
        if not voucher_text:
            return

        menu = QMenu(self)
        menu.addAction("清除照片关联", lambda: self._context_clear_photos(voucher_text))
        menu.addSeparator()
        has_photos = bool(self.store.get_photos(voucher_text))
        if not has_photos:
            menu.addAction("删除入库编号", lambda: self._context_delete_voucher(voucher_text))
        else:
            menu.addAction("删除入库编号（需先清除照片关联）").setEnabled(False)
        menu.exec_(self.voucher_table.viewport().mapToGlobal(pos))

    def _context_clear_photos(self, voucher: str) -> None:
        photos = self.store.get_photos(voucher)
        if not photos:
            QMessageBox.information(self, "清除照片关联", f"{voucher} 没有关联的照片。")
            return
        if QMessageBox.question(
            self, "清除照片关联", f"确定清除 {voucher} 的全部 {len(photos)} 张照片关联吗？\n\n（入库编号、标本信息和分类信息将保留不变）",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        count = self.store.clear_photos(voucher)
        self.statusBar().showMessage(f"已清除 {voucher} 的 {count} 张照片关联", 3000)
        self.reload_current()

    def _context_delete_voucher(self, voucher: str) -> None:
        password, ok = QInputDialog.getText(
            self, "删除入库编号",
            f"删除 {voucher} 将永久移除该编号及其标本信息、分类信息。\n\n请输入管理密码确认删除：",
            QLineEdit.Password,
        )
        if not ok or not password:
            return
        if password != "123":
            QMessageBox.warning(self, "密码错误", "密码不正确，操作已取消。")
            return
        answer = QMessageBox.warning(
            self, "确认删除",
            f"密码验证通过。\n\n确定要永久删除 {voucher} 吗？此操作不可恢复（可撤回）。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.store.delete_specimen(voucher)
        self.current_voucher = None
        self.refresh_list()
        vouchers = self.store.list_vouchers()
        if vouchers:
            self.select_voucher(vouchers[0])
        self.statusBar().showMessage(f"已删除 {voucher}", 3000)

    def undo(self) -> None:
        action = self.store.undo_last()
        if not action:
            QMessageBox.information(self, "撤回", "没有可撤回的操作。")
            return
        self.statusBar().showMessage(f"已撤回：{action}", 3000)
        self.reload_current()

    def redo(self) -> None:
        action = self.store.redo_last()
        if not action:
            QMessageBox.information(self, "返回", "没有可返回的操作。")
            return
        self.statusBar().showMessage(f"已返回：{action}", 3000)
        self.reload_current()

    def _undo_redo_counts(self) -> tuple[int, int]:
        """Return (undo_count, redo_count) for display."""
        rows = self.store._read_plain_rows(self.store.data_dir / "操作记录.xlsx")
        if not rows:
            return 0, 0
        undone = sum(1 for r in rows if self.store._value(r, "是否撤销") == "是")
        # Find last non-undone index
        depth = int(self.store.config.get("undo_depth", 200))
        candidates = [r for r in rows[-depth:] if self.store._value(r, "是否撤销") != "是"]
        undo_count = len(candidates)
        redo_count = undone
        return undo_count, redo_count

    def reload_current(self) -> None:
        vouchers = self.store.list_vouchers()
        if self.current_voucher not in vouchers:
            self.current_voucher = vouchers[0] if vouchers else None
        self.refresh_list()
        if self.current_voucher:
            self.select_voucher(self.current_voucher)

    # ---- Photo management ----

    def add_photo(self) -> None:
        if not self.current_voucher:
            QMessageBox.information(self, "请选择标本", "请先选择或新增一个标本。")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择照片", "",
            "图片文件 (*.tif *.tiff *.jpg *.jpeg *.png *.bmp);;所有文件 (*.*)",
        )
        if paths:
            self.add_photo_paths_async(paths)

    def add_photo_paths(self, paths: list[str], ask_for_outside: bool = True) -> int:
        if not self.current_voucher:
            QMessageBox.information(self, "请选择标本", "请先选择或新增一个标本。")
            return 0
        photo_paths, allow_outside, skipped = self._prepare_photo_paths(paths, ask_for_outside)
        if not photo_paths:
            self._show_skipped_photos(skipped)
            return 0
        photo_paths = self._check_photo_conflicts(photo_paths, self.current_voucher)
        if not photo_paths:
            return 0
        try:
            added_rows = self.store.add_photos(self.current_voucher, photo_paths, allow_outside=allow_outside)
            added = len(added_rows)
        except Exception as exc:
            QMessageBox.critical(self, "照片关联失败", str(exc))
            return 0
        self._append_added_photos_to_image_index(added_rows)
        self.current_photos = self.store.get_photos(self.current_voucher)
        self.current_photo_index = max(0, len(self.current_photos) - 1)
        self.refresh_photo_table()
        self.load_current_photo()
        self.refresh_list()
        self._show_skipped_photos(skipped)
        return added

    def add_photo_paths_async(self, paths: list[str], ask_for_outside: bool = True) -> None:
        if self._import_job_active:
            QMessageBox.information(self, "照片导入中", "当前已有照片导入任务正在执行。")
            return
        if not self.current_voucher:
            QMessageBox.information(self, "请选择标本", "请先选择或新增一个标本。")
            return
        voucher = self.current_voucher
        photo_paths, allow_outside, skipped = self._prepare_photo_paths(paths, ask_for_outside)
        if not photo_paths:
            self._show_skipped_photos(skipped)
            return
        photo_paths = self._check_photo_conflicts(photo_paths, voucher)
        if not photo_paths:
            return
        # Show batch confirmation for 3+ photos
        if len(photo_paths) >= 3:
            dlg = PhotoBatchDialog(voucher, photo_paths, self)
            if dlg.exec_() != QDialog.Accepted or not dlg.confirmed:
                return
        self._import_job_active = True
        self.statusBar().showMessage(f"正在关联 {len(photo_paths)} 张照片...")

        class ImportThread(QThread):
            finished = pyqtSignal(list, str)
            def __init__(self, store, voucher, paths, allow_outside):
                super().__init__()
                self.store = store
                self.voucher = voucher
                self.paths = paths
                self.allow_outside = allow_outside
            def run(self):
                try:
                    rows = self.store.add_photos(self.voucher, self.paths, allow_outside=self.allow_outside)
                    self.finished.emit(rows, "")
                except Exception as e:
                    self.finished.emit([], str(e))

        self._import_thread = ImportThread(self.store, voucher, photo_paths, allow_outside)
        self._import_thread.finished.connect(
            lambda rows, err: self._on_import_done(voucher, rows, err, skipped)
        )
        self._import_thread.start()

    def _on_import_done(self, voucher: str, rows: list, err: str, skipped: list[str]) -> None:
        self._import_job_active = False
        if err:
            self.statusBar().showMessage("照片关联失败")
            QMessageBox.critical(self, "照片关联失败", err)
            return
        if self.current_voucher == voucher:
            self.current_photos = self.store.get_photos(voucher)
            self.current_photo_index = max(0, len(self.current_photos) - 1)
            self.refresh_photo_table()
            self.load_current_photo()
        self._append_added_photos_to_image_index(rows)
        self.refresh_list()
        self._show_skipped_photos(skipped)
        self.statusBar().showMessage(f"已关联 {len(rows)} 张照片")

    def _append_added_photos_to_image_index(self, rows: list[dict[str, Any]]) -> None:
        paths: list[Path] = []
        for row in rows:
            try:
                path = self.store.resolve_photo_path(row)
            except Exception:
                continue
            if path.exists():
                paths.append(path)
        if paths:
            append_images_to_index(self.workspace_root, paths)
            self.search_index = None
            QTimer.singleShot(200, self._build_search_index_background)

    def _prepare_photo_paths(self, paths: list[str], ask_for_outside: bool = True) -> tuple[list[Path], bool, list[str]]:
        skipped: list[str] = []
        photo_paths: list[Path] = []
        allowed_suffixes = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
        for raw in paths:
            path = Path(raw).resolve()
            if path.suffix.lower() not in allowed_suffixes:
                skipped.append(path.name)
                continue
            photo_paths.append(path)
        return photo_paths, True, skipped

    def _show_skipped_photos(self, skipped: list[str]) -> None:
        if skipped:
            QMessageBox.information(self, "部分文件已跳过", "以下文件不是支持的图片格式：\n" + "\n".join(skipped[:20]))

    def _check_photo_conflicts(self, photo_paths: list[Path], voucher: str) -> list[Path]:
        conflicts = self.store.find_photo_conflicts(photo_paths, voucher)
        if not conflicts:
            return photo_paths
        conflict_resolved = {Path(p).resolve() for p in conflicts}
        names = ", ".join(Path(p).name for p in list(conflicts)[:5])
        extra = f" 等{len(conflicts)}张" if len(conflicts) > 5 else ""
        self.statusBar().showMessage(f"已跳过已分配照片：{names}{extra}", 5000)
        return [p for p in photo_paths if p.resolve() not in conflict_resolved]

    def delete_photo(self) -> None:
        if not self.current_voucher or not self.current_photos:
            return
        self.delete_photo_at(self.current_photo_index)

    def delete_photo_at(self, photo_index: int) -> None:
        if not self.current_voucher or not self.current_photos:
            return
        self.store.delete_photo(self.current_voucher, photo_index)
        self.current_photos = self.store.get_photos(self.current_voucher)
        self.current_photo_index = min(photo_index, max(0, len(self.current_photos) - 1))
        if not self.current_photos:
            self._grid_mode_before_expand = ""
        self.refresh_photo_table()
        self.load_current_photo()
        self.refresh_list()

    def shift_photo(self, delta: int) -> None:
        if not self.current_photos:
            return
        self._save_current_photo_view_state()
        step = delta * self._grid_count() if self._is_grid_mode() else delta
        self.current_photo_index = (self.current_photo_index + step) % len(self.current_photos)
        self.load_current_photo()

    # ---- Photo table ----

    def refresh_photo_table(self) -> None:
        total = len(self.current_photos)
        max_page = max(0, (total - 1) // self._photo_page_size) if total else 0
        self._photo_page = min(self._photo_page, max_page)
        start = self._photo_page * self._photo_page_size
        end = min(start + self._photo_page_size, total)
        page_photos = self.current_photos[start:end]

        self.photo_table.blockSignals(True)
        self.photo_table.setRowCount(len(page_photos))
        for idx, row in enumerate(page_photos):
            real_idx = start + idx
            self.photo_table.setItem(idx, 0, QTableWidgetItem(str(real_idx + 1)))
            self.photo_table.setItem(idx, 1, QTableWidgetItem(str(row.get("文件名", ""))))
            self.photo_table.setItem(idx, 2, QTableWidgetItem(str(row.get("相对路径", ""))))
            self.photo_table.setItem(idx, 3, QTableWidgetItem(str(row.get("描述", ""))))
        self.photo_table.blockSignals(False)
        self._select_photo_table_row()

        if total > self._photo_page_size:
            self._page_label.setText(f"显示 {start + 1}-{end} / 共 {total} 张")
            self._prev_page_btn.setEnabled(self._photo_page > 0)
            self._next_page_btn.setEnabled(self._photo_page < max_page)
            self._page_label.show()
            self._prev_page_btn.show()
            self._next_page_btn.show()
        else:
            self._page_label.setText(f"共 {total} 张")
            self._prev_page_btn.hide()
            self._next_page_btn.hide()

    def _photo_prev_page(self) -> None:
        if self._photo_page > 0:
            self._photo_page -= 1
            self.refresh_photo_table()

    def _photo_next_page(self) -> None:
        total = len(self.current_photos)
        max_page = max(0, (total - 1) // self._photo_page_size) if total else 0
        if self._photo_page < max_page:
            self._photo_page += 1
            self.refresh_photo_table()

    def _select_photo_table_row(self) -> None:
        if not self.current_photos:
            return
        real_idx = min(self.current_photo_index, len(self.current_photos) - 1)
        page_row = real_idx - self._photo_page * self._photo_page_size
        if page_row < 0 or page_row >= self.photo_table.rowCount():
            return
        self.photo_table.blockSignals(True)
        self.photo_table.selectRow(page_row)
        self.photo_table.scrollToItem(self.photo_table.item(page_row, 0))
        self.photo_table.blockSignals(False)

    def _on_photo_table_row_changed(self, row: int, _col: int, _prev_row: int, _prev_col: int) -> None:
        if self._loading or row < 0:
            return
        real_idx = self._photo_page * self._photo_page_size + row
        if real_idx >= len(self.current_photos):
            return
        self._save_current_photo_view_state()
        self.current_photo_index = real_idx
        self.load_current_photo()

    def _photo_table_context_menu(self, pos) -> None:
        menu = QMenu(self)
        rows = sorted(set(idx.row() for idx in self.photo_table.selectedItems()))
        if not rows:
            return
        # Copy filename
        real_rows = [self._photo_page * self._photo_page_size + r for r in rows if r < self.photo_table.rowCount()]
        if real_rows:
            menu.addAction("复制文件名", lambda: self._copy_photo_filename(real_rows[0]))
        menu.addSeparator()
        if len(rows) == 1:
            menu.addAction("替换此照片", self._replace_current_photo)
            menu.addAction("删除此照片", self.delete_photo)
        else:
            menu.addAction(f"删除选中的 {len(rows)} 张照片", self._delete_selected_photos)
        menu.exec_(self.photo_table.viewport().mapToGlobal(pos))

    def _copy_photo_filename(self, real_idx: int) -> None:
        if 0 <= real_idx < len(self.current_photos):
            name = str(self.current_photos[real_idx].get("文件名", ""))
            QApplication.clipboard().setText(name)

    def _filename_context_menu(self, widget: QLineEdit, pos) -> None:
        menu = widget.createStandardContextMenu()
        menu.exec_(widget.mapToGlobal(pos))

    def _replace_current_photo(self) -> None:
        if not self.current_voucher or self.current_photo_index >= len(self.current_photos):
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择替换照片", "", "图片 (*.tif *.tiff *.jpg *.jpeg *.png *.bmp)")
        if not paths:
            return
        self.delete_photo_at(self.current_photo_index)
        self.add_photo_paths(paths)

    def _delete_selected_photos(self) -> None:
        if not self.current_voucher or not self.current_photos:
            return
        rows = sorted(set(idx.row() for idx in self.photo_table.selectedItems()), reverse=True)
        if not rows:
            return
        real_indices = [self._photo_page * self._photo_page_size + r for r in rows]
        real_indices = [i for i in real_indices if 0 <= i < len(self.current_photos)]
        if not real_indices:
            return
        answer = QMessageBox.question(
            self, "批量删除", f"确定删除选中的 {len(real_indices)} 张照片？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        all_photos = self.store.get_photos(self.current_voucher)
        for idx in sorted(real_indices, reverse=True):
            if idx < len(all_photos):
                self.store.delete_photo(self.current_voucher, idx)
        self.current_photos = self.store.get_photos(self.current_voucher)
        self.current_photo_index = min(self.current_photo_index, max(0, len(self.current_photos) - 1))
        self.refresh_photo_table()
        self.load_current_photo()
        self.refresh_list()

    # ---- Photo rendering ----

    def _preview_size(self) -> tuple[int, int] | None:
        if not hasattr(self, '_cached_preview_quality'):
            self._cached_preview_quality = load_settings().preview_quality
        return PREVIEW_QUALITY_SIZES.get(self._cached_preview_quality, (800, 600))

    def _is_grid_mode(self) -> bool:
        return self._view_mode != "单张"

    def _grid_count(self) -> int:
        for count in (2, 4, 6, 8):
            if self._view_mode.startswith(str(count)):
                return count
        return 1

    def _on_view_mode_changed(self, text: str) -> None:
        self._grid_mode_before_expand = ""
        self._return_grid_btn.hide()
        self._view_mode = text
        self._save_current_photo_view_state()
        self.load_current_photo()

    def _set_view_mode(self, count: int) -> None:
        for btn in self._view_buttons:
            btn.setChecked(btn.property("grid_count") == count)
        mode = "单张" if count == 1 else f"{count}宫格"
        self._on_view_mode_changed(mode)

    def _sync_view_buttons(self, count: int) -> None:
        for btn in self._view_buttons:
            btn.blockSignals(True)
            btn.setChecked(btn.property("grid_count") == count)
            btn.blockSignals(False)

    def _on_show_grid_filenames_changed(self, state: int) -> None:
        self._show_grid_filenames = state == Qt.Checked
        for cell in self._grid_labels:
            cell.set_filename_visible(self._show_grid_filenames)
        try:
            settings = load_settings()
            settings.show_grid_filenames = self._show_grid_filenames
            save_settings(settings)
        except Exception:
            pass

    def load_current_photo(self) -> None:
        self._loading = True
        for widget in self.photo_widgets.values():
            widget.blockSignals(True)
            widget.setText("")
            widget.blockSignals(False)

        self._placeholder_label.hide()
        self.photo_view.clear_image()
        self._clear_grid()
        self._current_qpixmap = None
        self._zoom_label.setText("")

        if not self.current_photos:
            self.photo_counter.setText("0 / 0")
            self._placeholder_label.setText("暂无照片")
            self._placeholder_label.show()
            self._loading = False
            return

        row = self.current_photos[self.current_photo_index]
        self._select_photo_table_row()
        mode_suffix = f" · {self._view_mode}" if self._is_grid_mode() else ""
        self.photo_counter.setText(f"{self.current_photo_index + 1} / {len(self.current_photos)}{mode_suffix}")
        self.photo_widgets["文件名"].blockSignals(True)
        self.photo_widgets["文件名"].setText(str(row.get("文件名", "")))
        self.photo_widgets["文件名"].blockSignals(False)
        self.photo_widgets["相对路径"].blockSignals(True)
        self.photo_widgets["相对路径"].setText(str(row.get("相对路径", "")))
        self.photo_widgets["相对路径"].blockSignals(False)
        self.photo_widgets["描述"].blockSignals(True)
        self.photo_widgets["描述"].setText(str(row.get("描述", "")))
        self.photo_widgets["描述"].blockSignals(False)

        if self._is_grid_mode():
            self._render_grid()
            self._loading = False
            return

        path = self.store.resolve_photo_path(row)
        self._photo_load_token += 1
        token = self._photo_load_token
        self._placeholder_label.setText("正在加载预览...")
        self._placeholder_label.show()
        self.statusBar().showMessage(f"正在加载照片：{path.name}")
        size = self._preview_size() or (2800, 2200)
        self._thumb_worker.clear_pending()
        self._thumb_worker.enqueue(path, size, token)
        self._loading = False

    def _on_thumbnail_ready(self, token: int, qpixmap: QPixmap | None, exc: Exception | None) -> None:
        # Check if this is a single-photo load
        if token == self._photo_load_token and not self._is_grid_mode():
            self._finish_single_photo_load(token, qpixmap, exc)
            return
        # Check grid loads
        if token in self._grid_requests:
            slot = self._grid_requests.pop(token)
            if token >= self._grid_load_token * 100:
                self._finish_grid_thumbnail(slot, qpixmap, exc)

    def _finish_single_photo_load(self, token: int, qpixmap: QPixmap | None, exc: Exception | None) -> None:
        self._placeholder_label.hide()
        if exc:
            self._current_qpixmap = None
            self._placeholder_label.setText(f"无法预览照片\n{exc}")
            self._placeholder_label.setStyleSheet("color: #8b2f2f; font-size: 12px;")
            self._placeholder_label.show()
            self.statusBar().showMessage("照片预览加载失败")
            return
        if qpixmap is None:
            return
        self._current_qpixmap = qpixmap
        self.photo_view.set_image(qpixmap)
        self.photo_view.show()
        self.grid_frame.hide()
        self.statusBar().showMessage("就绪")

    def _render_grid(self) -> None:
        self._clear_grid()
        self._grid_load_token += 1
        token = self._grid_load_token
        if not self.current_photos:
            return
        self._thumb_worker.clear_pending()
        count = self._grid_count()
        cols, rows = grid_shape(count)
        page_start = (self.current_photo_index // count) * count
        page_indices = list(range(page_start, min(page_start + count, len(self.current_photos))))

        self.photo_view.hide()
        self._placeholder_label.hide()
        self.grid_frame.show()

        for slot, photo_index in enumerate(page_indices):
            r = slot // cols
            c = slot % cols
            photo = self.current_photos[photo_index]
            label = GridPhotoCell(photo_index, self._show_grid_filenames)
            label.clicked.connect(self._on_grid_cell_clicked)
            label.double_clicked.connect(self._on_grid_cell_double_clicked)
            label.right_clicked.connect(self._show_grid_context_menu)
            label.zoom_changed.connect(self._on_grid_cell_zoom_changed)
            label.set_selected(photo_index == self.current_photo_index)
            path = self.store.resolve_photo_path(photo)
            filename = str(photo.get("文件名", ""))
            label.set_filename(filename)
            if path.exists():
                self._grid_requests[token * 100 + slot] = slot
                self._thumb_worker.enqueue(path, (320, 240), token * 100 + slot)
            else:
                label.set_error("照片不存在")
            self.grid_layout.addWidget(label, r, c)
            self._grid_labels.append(label)

        page_text = f"{page_start + 1}-{page_indices[-1] + 1} / {len(self.current_photos)}" if page_indices else f"0 / {len(self.current_photos)}"
        self.photo_counter.setText(f"{page_text} · {self._view_mode}")
        self._zoom_label.setText("")

    def _clear_grid(self) -> None:
        self._grid_requests.clear()
        for label in self._grid_labels:
            self.grid_layout.removeWidget(label)
            label.deleteLater()
        self._grid_labels.clear()

    def _finish_grid_thumbnail(self, slot: int, qpixmap: QPixmap | None, exc: Exception | None) -> None:
        if slot >= len(self._grid_labels):
            return
        label = self._grid_labels[slot]
        if not label:
            return
        if exc or qpixmap is None:
            label.set_error("无法预览")
            return
        label.set_pixmap(qpixmap)

    def _on_grid_cell_clicked(self, photo_index: int) -> None:
        if photo_index < 0 or photo_index >= len(self.current_photos):
            return
        self.current_photo_index = photo_index
        self._select_photo_table_row()
        self._populate_photo_fields(self.current_photos[photo_index])
        self._refresh_grid_selection()

    def _on_grid_cell_double_clicked(self, photo_index: int) -> None:
        self.enlarge_photo_from_grid(photo_index)

    def _on_grid_cell_zoom_changed(self, photo_index: int, zoom: float) -> None:
        if photo_index == self.current_photo_index:
            self._zoom_label.setText(f"宫格 {int(zoom * 100)}%")

    def _populate_photo_fields(self, row: dict[str, Any]) -> None:
        for field in ("文件名", "相对路径", "描述"):
            widget = self.photo_widgets[field]
            widget.blockSignals(True)
            widget.setText(str(row.get(field, "")))
            widget.blockSignals(False)

    def _refresh_grid_selection(self) -> None:
        for cell in self._grid_labels:
            cell.set_selected(cell.photo_index == self.current_photo_index)

    def _current_grid_cell(self) -> GridPhotoCell | None:
        for cell in self._grid_labels:
            if cell.photo_index == self.current_photo_index:
                return cell
        return None

    def _show_grid_context_menu(self, photo_index: int, event) -> None:
        menu = QMenu(self)
        menu.addAction("放大显示", lambda: self.enlarge_photo_from_grid(photo_index))
        menu.addAction("打开原图", lambda: self.open_current_photo_external(photo_index))
        menu.addAction("分配入库编号", self._assign_voucher_to_selected)
        menu.addAction("删除照片", lambda: self.delete_photo_at(photo_index))
        menu.addSeparator()
        menu.addAction("从照片名提取信息", lambda: self._extract_info_from_photo(photo_index))
        menu.addAction("复制相对路径", lambda: self.copy_photo_relative_path(photo_index))
        menu.exec_(event.globalPos())

    def _get_selected_photo_indices(self) -> list[int]:
        """Return list of currently selected photo indices across all views."""
        if not self.current_photos:
            return []
        if self._is_grid_mode():
            # In grid mode, use the current_photo_index (grid shows one page)
            count = self._grid_count()
            start = (self.current_photo_index // count) * count
            page_indices = list(range(start, min(start + count, len(self.current_photos))))
            return [page_indices[self.current_photo_index % count]] if page_indices else []
        else:
            return [self.current_photo_index]

    def _assign_voucher_to_selected(self) -> None:
        """Assign selected photos to a (new or existing) voucher number."""
        if not self.current_photos:
            QMessageBox.information(self, "提示", "当前没有可分配的照片。")
            return
        indices = self._get_selected_photo_indices()
        if not indices:
            QMessageBox.information(self, "提示", "请先在预览区选择要分配的照片。")
            return
        # Show dialog
        voucher, ok = QInputDialog.getText(
            self, "分配入库编号", "输入目标入库编号（留空自动新建）：", QLineEdit.Normal, ""
        )
        if not ok:
            return
        voucher = voucher.strip()
        if not voucher:
            # Create new voucher
            try:
                voucher = self.store.create_specimen()
            except Exception as exc:
                QMessageBox.critical(self, "创建失败", str(exc))
                return
        elif voucher not in self.store.list_vouchers():
            QMessageBox.warning(self, "编号不存在", f"入库编号 {voucher} 不存在，请先创建或使用已有编号。")
            return
        # Move the selected photos to the target voucher
        moved = 0
        photo_rows = self.current_photos  # snapshot before deletion
        for photo_index in sorted(indices, reverse=True):
            if photo_index < len(photo_rows):
                photo = photo_rows[photo_index]
                path = self.store.resolve_photo_path(photo)
                if path.exists():
                    try:
                        self.store.add_photo(voucher, path)
                        moved += 1
                    except Exception:
                        pass
                try:
                    self.store.delete_photo(self.current_voucher, photo_index)
                except Exception:
                    pass
        self.current_photos = self.store.get_photos(self.current_voucher or "")
        self.current_photo_index = min(self.current_photo_index, max(0, len(self.current_photos) - 1))
        self.refresh_photo_table()
        self.load_current_photo()
        self.refresh_list()
        self.statusBar().showMessage(f"已将 {moved} 张照片分配给 {voucher}", 5000)

    def _save_panel(self, category: str) -> None:
        """Save all pending changes for a specific panel."""
        if not self.current_voucher:
            return
        saved = 0
        for key, timer in list(self._save_timers.items()):
            if key.startswith(category + ":"):
                timer.stop()
                parts = key.split(":", 1)
                if len(parts) == 2:
                    self.save_field(parts[0], parts[1], self.current_voucher)
                self._save_timers.pop(key, None)
                saved += 1
        names = {"specimen": "标本信息", "photo": "照片信息", "classification": "分类信息"}
        self.statusBar().showMessage(f"{names.get(category, category)}已保存 ({saved} 项)", 2000)

    def _save_all_panels(self) -> None:
        """Save all pending changes across all panels."""
        for category in ("specimen", "photo", "classification"):
            self._save_panel(category)
        self.statusBar().showMessage("所有信息已保存", 3000)

    def _confirm_save(self) -> None:
        """Force-save any pending field changes and show confirmation."""
        self._save_all_panels()

    def _extract_info_from_photo(self, photo_index: int) -> None:
        if not self.current_voucher or photo_index >= len(self.current_photos):
            return
        filename = str(self.current_photos[photo_index].get("文件名", ""))
        tube = extract_tube_from_filename(filename)
        date_str = extract_photo_date(filename)
        location = extract_location_code(tube) if tube else ""
        if not tube and not date_str:
            QMessageBox.information(self, "无法提取", "该照片文件名中未找到规范的编号或日期信息。")
            return
        # Update specimen fields
        if tube:
            self._loading = True
            self.specimen_widgets["管内编号*"].setText(tube)
            if location:
                self.specimen_widgets["采集地点缩写*"].setText(location)
            self._loading = False
            self.store.set_fields("specimen", self.current_voucher, {
                "管内编号*": tube,
                **({"采集地点缩写*": location} if location else {}),
            })
        if date_str:
            self._loading = True
            self.specimen_widgets["采集日期"].setText(date_str)
            self._loading = False
            self.store.set_fields("specimen", self.current_voucher, {"采集日期": date_str})
        details = []
        if tube: details.append(f"管内编号：{tube}")
        if location: details.append(f"地点：{location}")
        if date_str: details.append(f"日期：{date_str}")
        self.refresh_list()
        QMessageBox.information(self, "已提取", "\n".join(details) or "已提取信息。")

    def adjust_zoom(self, factor: float) -> None:
        if self._is_grid_mode():
            cell = self._current_grid_cell()
            if cell:
                cell.zoom(factor)
                self._zoom_label.setText(f"宫格 {cell.zoom_percent()}%")
            return
        self.photo_view.zoom(factor)

    def fit_image(self) -> None:
        if self._is_grid_mode():
            cell = self._current_grid_cell()
            if cell:
                cell.fit_to_window()
                self._zoom_label.setText("宫格 100%")
            return
        self.photo_view.fit_to_window()

    def open_current_photo_external(self, photo_index: int | None = None) -> None:
        if not self.current_photos:
            return
        index = self.current_photo_index if photo_index is None else photo_index
        if index < 0 or index >= len(self.current_photos):
            return
        path = self.store.resolve_photo_path(self.current_photos[index])
        if not path.exists():
            QMessageBox.critical(self, "照片不存在", str(path))
            return
        if path.suffix.lower() not in {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}:
            QMessageBox.critical(self, "安全限制", "仅支持打开图片文件。")
            return
        _open_path(path)

    def enlarge_photo_from_grid(self, photo_index: int) -> None:
        if photo_index < 0 or photo_index >= len(self.current_photos):
            return
        if self._is_grid_mode():
            self._grid_mode_before_expand = self._view_mode
        self.current_photo_index = photo_index
        self._view_mode = "单张"
        self._sync_view_buttons(1)
        self._return_grid_btn.show()
        self.refresh_photo_table()
        self.load_current_photo()

    def return_to_grid(self) -> None:
        if not self._grid_mode_before_expand:
            return
        mode = self._grid_mode_before_expand
        self._grid_mode_before_expand = ""
        self._view_mode = mode
        count = 1
        for _, c, _ in VIEW_MODES:
            if (f"{c}宫格" if c > 1 else "单张") == mode:
                count = c
                break
        self._sync_view_buttons(count)
        self._return_grid_btn.hide()
        self._save_current_photo_view_state()
        self.load_current_photo()

    def copy_photo_relative_path(self, photo_index: int) -> None:
        if photo_index < 0 or photo_index >= len(self.current_photos):
            return
        QApplication.clipboard().setText(str(self.current_photos[photo_index].get("相对路径", "")))

    def _on_grid_drop(self, event) -> None:
        self.grid_frame.setStyleSheet("")
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.add_photo_paths_async(paths)
        event.acceptProposedAction()

    def _on_zoom_changed(self, level: float) -> None:
        if not self._is_grid_mode():
            self._zoom_label.setText(f"| {int(level * 100)}%")

    # ---- Photo view state persistence ----

    def _photo_state_key(self) -> str:
        if not self.current_photos:
            return ""
        row = self.current_photos[self.current_photo_index]
        return f"{row.get('入库编号*','')}|{row.get('相对路径','')}|{self.current_photo_index}"

    def _save_current_photo_view_state(self) -> None:
        key = self._photo_state_key()
        if key:
            self._photo_view_states[key] = (1.0, 0, 0)

    def _view_state_for_current_photo(self) -> tuple[float, int, int]:
        return self._photo_view_states.get(self._photo_state_key(), (1.0, 0, 0))

    # ---- Workspace operations ----

    def import_workspace(self) -> None:
        source = QFileDialog.getExistingDirectory(self, "选择要导入的旧工作区")
        if not source:
            return
        try:
            result = self.store.import_workspace(source)
            QMessageBox.information(self, "导入完成", f"导入 {result.imported} 个标本，跳过 {result.skipped} 个重复记录，关联照片 {result.photos_imported} 张。")
            self.refresh_list()
        except ImportConflictError as exc:
            detail = str(exc)
            if exc.report_path:
                detail += f"\n冲突报告：{exc.report_path}"
            QMessageBox.critical(self, "导入已阻止", detail)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def export_data(self) -> None:
        default_path = str(self.workspace_root / "标本数据导出.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "导出数据", default_path, "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            count = self.store.export_all_data(Path(path))
            QMessageBox.information(self, "导出完成", f"已导出 {count} 条记录到\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def import_data_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入数据", "", "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            result = self.store.import_from_file(Path(path))
            QMessageBox.information(self, "导入完成", f"导入 {result.imported} 个标本，跳过 {result.skipped} 个重复记录。")
            self.refresh_list()
        except ImportConflictError as exc:
            detail = str(exc)
            if exc.report_path:
                detail += f"\n冲突报告：{exc.report_path}"
            QMessageBox.critical(self, "导入已阻止", detail)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def switch_workspace(self) -> None:
        target = QFileDialog.getExistingDirectory(self, "选择工作区目录")
        if not target:
            return
        target_path = Path(target).resolve()
        if target_path == self.workspace_root:
            return
        if is_generated_workspace_path(target_path):
            QMessageBox.critical(
                self, "不能使用软件目录",
                f"不能把 build/dist/releases 等软件构建或版本目录作为工作区：\n{target_path}\n\n请选择实际保存数据和照片的工作目录。",
            )
            return
        create_workspace_files = False
        if not has_workspace_data(target_path):
            if QMessageBox.question(
                self, "初始化工作区",
                f"该目录还没有数据文件：\n{target_path}\n\n是否在此目录创建新的数据文件夹和 Excel 数据文件？",
                QMessageBox.Yes | QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            initialize_workspace(target_path, self._template_source_for_initialization(target_path))
            create_workspace_files = True
        try:
            new_store = ExcelStore(target_path, lock=True, create_if_missing=create_workspace_files)
        except Exception as exc:
            QMessageBox.critical(self, "切换失败", str(exc))
            return
        self.store.close()
        self.store = new_store
        self.workspace_root = target_path
        self.matcher = SpeciesMatcher(self.workspace_root / "字段模版" / "表格信息预设字段.xlsx")
        self.thumbnail_cache.set_workspace(self.workspace_root)
        self.search_index = None
        clear_image_index()
        QTimer.singleShot(300, self._build_search_index_background)
        self._thumb_worker.stop()
        self._thumb_worker = ThumbnailWorker(self.thumbnail_cache, self)
        self._thumb_worker.result_ready.connect(self._on_thumbnail_ready)
        self._thumb_worker.start()
        self.workspace_label.setText(f"当前工作目录：{self.workspace_root}")
        remember_workspace(self.workspace_root)
        self.current_voucher = None
        self.current_photos = []
        self.current_photo_index = 0
        self._photo_view_states.clear()
        self.refresh_list()
        vouchers = self.store.list_vouchers()
        if vouchers:
            self.select_voucher(vouchers[0])
        else:
            self._loading = True
            for w in list(self.specimen_widgets.values()) + list(self.class_widgets.values()) + list(self.photo_widgets.values()):
                w.blockSignals(True)
                if isinstance(w, QComboBox):
                    w.setCurrentIndex(0)
                else:
                    w.setText("")
                w.blockSignals(False)
            self._loading = False
            self.load_current_photo()

    # ---- Image search ----

    def open_image_search(self) -> None:
        if not self.current_voucher:
            QMessageBox.information(self, "请选择标本", "请先选择或新增一个标本。")
            return
        dlg = ImageSearchDialog(self)
        dlg.exec_()

    def open_ingest_summary(self) -> None:
        dlg = IngestSummaryDialog(self)
        dlg.exec_()

    def _open_action_log(self) -> None:
        dlg = ActionLogDialog(self)
        dlg.exec_()

    # ---- Search index ----

    def _build_search_index_background(self) -> None:
        """Build the image search index in the background after startup."""
        if self._is_closing or self._index_build_worker is not None:
            return
        self._index_build_worker = IndexBuildWorker(self.workspace_root, self)
        self._index_build_worker.index_ready.connect(self._on_index_ready)
        self._index_build_worker.finished.connect(
            lambda: setattr(self, "_index_build_worker", None)
        )
        self._index_build_worker.start()

    def _on_index_ready(self, index: ImageSearchIndex | None) -> None:
        self.search_index = index

    # ---- Version manager ----

    def open_version_manager(self) -> None:
        dlg = VersionManagerDialog(self)
        dlg.exec_()

    # ---- Settings ----

    def open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.store.set_undo_depth(dlg.undo_depth)
            current_settings = load_settings()
            quality_keys = list(PREVIEW_QUALITY_OPTIONS.keys())
            idx = dlg.quality_combo.currentIndex()
            current_settings.preview_quality = quality_keys[idx] if 0 <= idx < len(quality_keys) else "compressed"
            save_settings(current_settings)
            self._cached_preview_quality = current_settings.preview_quality

    # ---- Panel helpers ----

    @staticmethod
    def _create_panel(title: str, content: QWidget, collapsible: bool = True) -> QFrame:
        frame = QFrame()
        frame.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        title_bar = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px 0;")
        title_bar.addWidget(title_label)
        title_bar.addStretch()
        if collapsible:
            collapse_btn = QPushButton("−")
            collapse_btn.setFixedSize(22, 22)
            collapse_btn.setToolTip("折叠/展开面板")
            collapse_btn.clicked.connect(lambda: _toggle_content(collapse_btn, content))
            title_bar.addWidget(collapse_btn)
        layout.addLayout(title_bar)
        layout.addWidget(content, stretch=1)
        # Store toggle function in closure
        def _toggle_content(btn, w):
            visible = w.isVisible()
            w.setVisible(not visible)
            btn.setText("−" if not visible else "+")
        return frame

    def _reset_window_layout(self) -> None:
        if QMessageBox.question(
            self, "重置窗口布局",
            "确定要重置窗口布局为默认状态吗？",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            settings = load_settings()
            settings.window_geometry = ""
            settings.splitter_sizes = []
            save_settings(settings)
        except Exception:
            pass
        self.main_splitter.setSizes([220, 580, 480])
        self.right_splitter.setSizes([260, 280, 200])
        self.voucher_panel.show()
        self.specimen_panel.show()
        self.photo_panel.show()
        self.class_panel.show()
        self.resize(1320, 820)
        QMessageBox.information(self, "布局已重置", "窗口布局已恢复为默认状态。")


# ---------------------------------------------------------------------------
# Image search dialog
# ---------------------------------------------------------------------------

DEFAULT_PHOTO_SCOPE = "工作区/照片"
WORKSPACE_SCOPE = "整个工作区"
IMAGE_TYPE_CHOICES = [
    ("TIF", "tif"),
    ("JPG", "jpg"),
    ("TIF+JPG", "tif_jpg"),
]


class IndexBuildWorker(QThread):
    """Background worker that builds the image search index at startup."""

    index_ready = pyqtSignal(object)  # ImageSearchIndex | None

    def __init__(self, workspace_root: Path, parent=None):
        super().__init__(parent)
        self.workspace_root = workspace_root

    def run(self) -> None:
        try:
            roots = [Path(self.workspace_root).resolve() / "照片"]
            if not roots[0].is_dir():
                roots = [Path(self.workspace_root).resolve()]
            if self.isInterruptionRequested():
                self.index_ready.emit(None)
                return
            index = _get_or_build_search_index(
                roots,
                max_depth=0,
                should_stop=self.isInterruptionRequested,
                force_rebuild=False,
                cache_root=self.workspace_root,
            )
            self.index_ready.emit(index)
        except Exception:
            self.index_ready.emit(None)


class ImageSearchWorker(QThread):
    result_ready = pyqtSignal(int, object, object)  # token, list[ImageSearchResult], Exception|None

    def __init__(
        self,
        token: int,
        workspace_root: Path,
        voucher: str,
        specimen: dict[str, Any],
        classification: dict[str, Any],
        linked_paths: list[Path],
        query: str,
        search_roots: list[str] | None,
        image_type: str,
        search_index: ImageSearchIndex | None = None,
        force_rebuild: bool = False,
        limit: int = 50,
        parent=None,
    ):
        super().__init__(parent)
        self.token = token
        self.workspace_root = workspace_root
        self.voucher = voucher
        self.specimen = specimen
        self.classification = classification
        self.linked_paths = linked_paths
        self.query = query
        self.search_roots = search_roots
        self.image_type = image_type
        self.search_index = search_index
        self.force_rebuild = force_rebuild
        self.limit = limit

    def run(self) -> None:
        try:
            results = image_search_results(
                self.workspace_root,
                self.voucher,
                self.specimen,
                self.classification,
                self.linked_paths,
                query=self.query,
                extra_roots=self.search_roots,
                suffixes=suffixes_for_image_type(self.image_type),
                limit=self.limit,
                should_stop=self.isInterruptionRequested,
                search_index=self.search_index,
                force_rebuild=self.force_rebuild,
            )
        except Exception as exc:
            self.result_ready.emit(self.token, [], exc)
        else:
            self.result_ready.emit(self.token, results, None)


class ImageSearchDialog(QDialog):
    def __init__(self, app: SpecimenWindow):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("图片检索")
        self.resize(980, 680)
        self.setMinimumSize(QSize(760, 520))

        self.results: list[ImageSearchResult] = []
        self.selected_indices: set[int] = set()
        self.last_selected_index: int | None = None
        self.result_limit: int = 50
        self._render_token = 0
        self._search_token = 0
        self._search_workers: list[ImageSearchWorker] = []
        self._card_labels: dict[int, QLabel] = {}
        self._cards: list[QFrame] = []
        self._thumb_worker = ThumbnailWorker(app.thumbnail_cache, self)
        self._thumb_worker.result_ready.connect(self._on_thumbnail_ready)
        self._thumb_worker.start()

        self._build()
        self.refresh_results()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        top.addWidget(QLabel(f"当前标本：{self.app.current_voucher or ''}"))
        top.addWidget(QLabel("核心编号"))
        self.query_edit = QLineEdit(self._default_query())
        self.query_edit.setPlaceholderText("输入关键词搜索，如 QD-C、CK、SC008")
        self.query_edit.textChanged.connect(self.schedule_refresh)
        top.addWidget(self.query_edit, stretch=1)
        top.addWidget(QLabel("显示"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(10, 500)
        self.limit_spin.setValue(50)
        self.limit_spin.setSuffix(" 张")
        self.limit_spin.setToolTip("搜索结果最大显示数量")
        self.limit_spin.valueChanged.connect(self._on_limit_changed)
        top.addWidget(self.limit_spin)
        top.addWidget(self._make_button("重新扫描", self.rescan_results))
        top.addWidget(self._make_button("添加选中图片", self.add_selected))
        layout.addLayout(top)

        # Search paths row
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("搜索范围"))
        self.path_combo = QComboBox()
        self.path_combo.setEditable(True)
        self.path_combo.addItems(self._default_search_paths())
        self.path_combo.setToolTip("输入自定义路径（可用 ; 分隔多个路径），或选择预设")
        self.path_combo.currentTextChanged.connect(self.schedule_refresh)
        path_row.addWidget(self.path_combo, stretch=1)
        path_row.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        for label, key in IMAGE_TYPE_CHOICES:
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self.schedule_refresh)
        path_row.addWidget(self.type_combo)
        path_row.addWidget(self._make_button("添加目录", self._add_search_dir))
        path_row.addWidget(self._make_button("恢复默认", self._reset_search_paths))
        path_row.addWidget(self._make_button("清除索引", self._clear_index_cache))
        layout.addLayout(path_row)

        # Results scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { background-color: #f4f6f8; border: 1px solid #a9b3bd; }")
        self.result_container = QWidget()
        self.result_grid = QGridLayout(self.result_container)
        self.result_grid.setSpacing(6)
        self.scroll_area.setWidget(self.result_container)
        layout.addWidget(self.scroll_area, stretch=1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.status_label = QLabel()
        bottom.addWidget(self.status_label, stretch=1)
        bottom.addWidget(self._make_button("添加", self.add_selected))
        bottom.addWidget(self._make_button("关闭", self.close))
        layout.addLayout(bottom)

    @staticmethod
    def _make_button(text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.clicked.connect(slot)
        return btn

    def _default_query(self) -> str:
        if not self.app.current_voucher:
            return ""
        specimen = self.app.store.get_specimen(self.app.current_voucher) or {}
        return default_image_query(specimen)

    def _default_search_paths(self) -> list[str]:
        paths = [DEFAULT_PHOTO_SCOPE, WORKSPACE_SCOPE]
        settings = load_settings()
        for p in settings.search_paths:
            if p not in paths:
                paths.append(p)
        return paths

    def _add_search_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择搜索目录")
        if d:
            if self.path_combo.findText(d) < 0:
                self.path_combo.addItem(d)
            self.path_combo.setCurrentText(d)

    def _reset_search_paths(self) -> None:
        self.path_combo.setCurrentText(DEFAULT_PHOTO_SCOPE)

    def _clear_index_cache(self) -> None:
        clear_image_index()
        self.app.search_index = None
        self.status_label.setText("索引缓存已清除，下次检索将重新扫描。")
        QTimer.singleShot(500, self.refresh_results)

    def _parse_search_roots(self) -> list[str] | None:
        text = self.path_combo.currentText().strip()
        if not text or text == DEFAULT_PHOTO_SCOPE:
            return None
        if text == WORKSPACE_SCOPE:
            return [str(self.app.workspace_root)]
        roots = []
        for part in text.replace("\n", ";").split(";"):
            part = part.strip()
            if part and part not in (DEFAULT_PHOTO_SCOPE, WORKSPACE_SCOPE) and Path(part).is_dir():
                roots.append(part)
        return roots or None

    def _selected_image_type(self) -> str:
        key = self.type_combo.currentData()
        return str(key or "tif")

    def _on_limit_changed(self, value: int) -> None:
        self.result_limit = value
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        if not hasattr(self, "_refresh_timer"):
            self._refresh_timer = QTimer(self)
            self._refresh_timer.setSingleShot(True)
            self._refresh_timer.timeout.connect(self.refresh_results)
        self._refresh_timer.start(250)

    def rescan_results(self) -> None:
        self.refresh_results(force_rebuild=True)

    def refresh_results(self, force_rebuild: bool = False) -> None:
        if not self.app.current_voucher:
            self.close()
            return
        self.selected_indices.clear()
        self.last_selected_index = None
        query = self.query_edit.text().strip()
        if not query:
            self.results = []
            self._clear_cards()
            self.status_label.setText("输入关键词搜索图片，如 QD-C、CK、SC008")
            return
        search_roots = self._parse_search_roots()
        if force_rebuild:
            self.status_label.setText(f"正在重新建立图片索引，并检索 {query}...")
        elif self.app.search_index is not None:
            self.status_label.setText(f"正在检索：{query}...")
        elif image_index_exists(self.app.workspace_root, search_roots):
            self.status_label.setText(f"正在检索索引：{query}...")
        else:
            self.status_label.setText(f"正在建立图片索引，并检索 {query}...")

        linked_paths = [self.app.store.resolve_photo_path(row) for row in self.app.current_photos]
        specimen = self.app.store.get_specimen(self.app.current_voucher) or {}
        classification = self.app.store.get_classification(self.app.current_voucher) or {}
        self._search_token += 1
        token = self._search_token
        for old_worker in list(self._search_workers):
            old_worker.requestInterruption()
        self._clear_cards()
        worker = ImageSearchWorker(
            token=token,
            workspace_root=self.app.workspace_root,
            voucher=self.app.current_voucher,
            specimen=specimen,
            classification=classification,
            linked_paths=linked_paths,
            query=query,
            search_roots=search_roots,
            image_type=self._selected_image_type(),
            search_index=self.app.search_index,
            force_rebuild=force_rebuild,
            limit=self.result_limit,
            parent=self,
        )
        worker.result_ready.connect(self._on_search_ready)
        worker.finished.connect(lambda w=worker: self._discard_search_worker(w))
        self._search_workers.append(worker)
        worker.start()

    def _on_search_ready(self, token: int, results: list[ImageSearchResult], exc: Exception | None) -> None:
        if token != self._search_token:
            return
        if exc is not None:
            self.results = []
            self._clear_cards()
            self.status_label.setText(f"检索失败：{exc}")
            return
        self.results = results
        self.render_results()

    def _discard_search_worker(self, worker: ImageSearchWorker) -> None:
        if worker in self._search_workers:
            self._search_workers.remove(worker)

    def _clear_cards(self) -> None:
        self._thumb_worker.clear_pending()
        for child in self.result_container.children():
            if isinstance(child, QWidget):
                self.result_grid.removeWidget(child)
                child.deleteLater()
        self._render_token += 1
        self._card_labels.clear()
        self._cards = []

    def render_results(self) -> None:
        self._clear_cards()
        columns = 4

        if not self.results:
            query = self.query_edit.text().strip()
            self.status_label.setText(f'未找到与 "{query}" 匹配的图片')
            return

        for index, result in enumerate(self.results):
            r = index // columns
            c = index % columns
            card = self._create_card(index, result)
            self.result_grid.addWidget(card, r, c)
            self._cards.append(card)

        self._update_status()

    def _create_card(self, index: int, result: ImageSearchResult) -> QFrame:
        selected = index in self.selected_indices
        bg = self._card_background(selected, result.is_linked)
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background-color: {bg}; border: 1px solid #ccc; padding: 6px; }}")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(6, 6, 6, 6)

        image_label = QLabel("加载中")
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setFixedSize(180, 135)
        image_label.setStyleSheet("color: #59666b; background-color: #e8eaed;")
        card_layout.addWidget(image_label)

        title = result.file_name
        if result.is_linked:
            title += "  已关联"
        title_label = QLabel(_shorten(title, 26))
        title_label.setStyleSheet("font-weight: bold;")
        card_layout.addWidget(title_label)
        path_label = QLabel(_shorten(result.relative_path, 32))
        path_label.setStyleSheet("color: #59666b;")
        card_layout.addWidget(path_label)
        match_text = "核心编号：" + "、".join(result.matched_keywords[:3]) if result.matched_keywords else "核心编号：无"
        match_label = QLabel(match_text)
        match_label.setStyleSheet("color: #3f4b57;")
        card_layout.addWidget(match_label)

        # Load thumbnail
        token = self._render_token * 1000 + index
        self._thumb_worker.enqueue(result.path, (180, 135), token)
        self._card_labels[token] = image_label

        # Click handling
        card.mousePressEvent = lambda event, idx=index: self._on_card_click(event, idx)
        card.mouseDoubleClickEvent = lambda event, idx=index: self._open_preview(idx)
        card.setContextMenuPolicy(Qt.CustomContextMenu)
        card.customContextMenuRequested.connect(lambda pos, idx=index: self._show_context_menu(idx))

        return card

    def _on_thumbnail_ready(self, token: int, qpixmap: QPixmap | None, exc: Exception | None) -> None:
        label = getattr(self, "_card_labels", {}).get(token)
        if not label or not qpixmap:
            return
        scaled = qpixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled)
        label.setText("")

    def _on_card_click(self, event, index: int) -> None:
        modifiers = event.modifiers()
        if modifiers & Qt.ShiftModifier and self.last_selected_index is not None:
            start, end = sorted((self.last_selected_index, index))
            self.selected_indices.update(range(start, end + 1))
        elif modifiers & Qt.ControlModifier:
            if index in self.selected_indices:
                self.selected_indices.remove(index)
            else:
                self.selected_indices.add(index)
            self.last_selected_index = index
        else:
            self.selected_indices = {index}
            self.last_selected_index = index
        self._update_selection_visuals()

    def _update_selection_visuals(self) -> None:
        for idx, card in enumerate(self._cards):
            result = self.results[idx] if idx < len(self.results) else None
            is_linked = result.is_linked if result else False
            bg = self._card_background(idx in self.selected_indices, is_linked)
            card.setStyleSheet(f"QFrame {{ background-color: {bg}; border: 1px solid #ccc; padding: 6px; }}")
        self._update_status()

    def _show_context_menu(self, index: int) -> None:
        if index not in self.selected_indices:
            self.selected_indices = {index}
            self.last_selected_index = index
            self._update_selection_visuals()
        menu = QMenu(self)
        menu.addAction("添加选中图片", self.add_selected)
        menu.addAction("打开原图", lambda: self._open_preview(index))
        menu.addAction("复制相对路径", lambda: self._copy_relative_path(index))
        menu.exec_(QCursor.pos())

    def _open_preview(self, index: int) -> None:
        if 0 <= index < len(self.results):
            _open_path(self.results[index].path)

    def _copy_relative_path(self, index: int) -> None:
        if 0 <= index < len(self.results):
            QApplication.clipboard().setText(self.results[index].relative_path)

    def add_selected(self) -> None:
        if not self.selected_indices:
            QMessageBox.information(self, "请选择图片", "请先选择要关联的图片。")
            return
        paths = [
            str(self.results[idx].path)
            for idx in sorted(self.selected_indices)
            if 0 <= idx < len(self.results) and not self.results[idx].is_linked
        ]
        if not paths:
            QMessageBox.information(self, "无需添加", "选中的图片已经关联到当前标本。")
            return
        added = self.app.add_photo_paths(paths, ask_for_outside=False)
        self.selected_indices.clear()
        self.last_selected_index = None
        self.refresh_results()
        self.status_label.setText(f"已添加 {added} 张图片。")

    def _card_background(self, selected: bool, linked: bool) -> str:
        if selected:
            return "#d8ebff"
        if linked:
            return "#eceff2"
        return "#ffffff"

    def _update_status(self) -> None:
        linked_count = sum(1 for item in self.results if item.is_linked)
        self.status_label.setText(f"结果 {len(self.results)} 张；已关联 {linked_count} 张；已选 {len(self.selected_indices)} 张")

    def keyPressEvent(self, event) -> None:
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_A:
            self.selected_indices = set(range(len(self.results)))
            self.last_selected_index = len(self.results) - 1 if self.results else None
            self._update_selection_visuals()
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._search_token += 1
        for worker in list(self._search_workers):
            worker.requestInterruption()
            worker.wait(3000)
        self._thumb_worker.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Batch photo confirmation dialog
# ---------------------------------------------------------------------------

class PhotoBatchDialog(QDialog):
    """Show a list of photos before batch-assigning them to a voucher."""

    def __init__(self, voucher: str, photo_paths: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认批量关联照片")
        self.resize(520, 400)
        self._paths = photo_paths
        self._confirmed = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"即将为 <b>{voucher}</b> 关联以下 {len(photo_paths)} 张照片："
        ))
        layout.addWidget(QLabel(
            "（如需取消某些照片，请先关闭此窗口，重新选择）"
        ))

        list_widget = QListWidget()
        for path in photo_paths:
            list_widget.addItem(f"  {Path(path).name}")
        layout.addWidget(list_widget, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        confirm_btn = QPushButton("确定关联")
        confirm_btn.setStyleSheet("QPushButton { font-weight: bold; }")
        confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(confirm_btn)
        layout.addLayout(btn_row)

    def _on_confirm(self) -> None:
        self._confirmed = True
        self.accept()

    @property
    def confirmed(self) -> bool:
        return self._confirmed


# ---------------------------------------------------------------------------
# Ingest summary dialog — paginated voucher-photo audit with password gate
# ---------------------------------------------------------------------------

class IngestSummaryDialog(QDialog):
    """入库汇总预览 — paginated voucher-photo audit with password-protected edits."""

    PAGE_SIZE = 100
    DEFAULT_PASSWORD = "123"

    def __init__(self, app: SpecimenWindow):
        super().__init__(app)
        self.app = app
        self.store = app.store
        self.setWindowTitle("入库汇总预览")
        self.resize(1060, 720)
        self.setMinimumSize(QSize(840, 540))

        self.all_vouchers: list[str] = self.store.list_vouchers()
        self._photo_counts: dict[str, int] = {}
        self._total_photos = 0
        self._build_photo_counts()

        self.filtered_vouchers: list[str] = list(self.all_vouchers)
        self.current_page = 0
        self.selected_voucher: str | None = None
        self._selected_photos: list[dict[str, str]] = []
        self._selected_photo_indices: set[int] = set()
        self._password_verified = False

        self._thumb_worker = ThumbnailWorker(app.thumbnail_cache, self)
        self._thumb_worker.result_ready.connect(self._on_overview_thumbnail)
        self._thumb_worker.start()
        self._card_labels: dict[int, QLabel] = {}
        self._overview_cards: list[QFrame] = []

        self._build_ui()
        self._load_page(0)

    def _build_photo_counts(self) -> None:
        """Build voucher→photo-count dict using the store cache (avoids direct Excel I/O)."""
        counts: dict[str, int] = {}
        for row in self.store.read_rows("photo"):
            v = str(row.get("入库编号*", "")).strip()
            if v:
                counts[v] = counts.get(v, 0) + 1
        self._photo_counts = counts
        self._total_photos = sum(counts.values())

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.filtered_vouchers) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    # ---- UI build ----

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Stats bar
        stats = QHBoxLayout()
        self.stats_label = QLabel()
        stats.addWidget(self.stats_label)
        stats.addStretch()
        layout.addLayout(stats)

        # Search + page nav
        nav = QHBoxLayout()
        nav.addWidget(QLabel("搜索编号"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入编号前缀筛选")
        self.search_edit.textChanged.connect(self._on_search_changed)
        nav.addWidget(self.search_edit, stretch=1)
        self.page_label = QLabel()
        nav.addWidget(self.page_label)
        self._make_btn("上一页", self._prev_page, nav)
        self._make_btn("下一页", self._next_page, nav)
        layout.addLayout(nav)

        # Main splitter: voucher table | photo grid
        splitter = QSplitter(Qt.Horizontal)

        # Left: voucher table
        self.voucher_table = QTableWidget(0, 3)
        self.voucher_table.setHorizontalHeaderLabels(["入库编号", "照片数", "管内编号"])
        self.voucher_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.voucher_table.setSelectionMode(QTableWidget.SingleSelection)
        self.voucher_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.voucher_table.horizontalHeader().setStretchLastSection(True)
        self.voucher_table.itemSelectionChanged.connect(self._on_voucher_selected)
        splitter.addWidget(self.voucher_table)

        # Right: photo grid
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.photo_count_label = QLabel("选择左侧编号查看照片")
        size_row = QHBoxLayout()
        size_row.addWidget(self.photo_count_label, stretch=1)
        size_row.addWidget(QLabel("缩略图"))
        self._thumb_sizes = {"小": (120, 90), "中": (180, 135), "大": (240, 180)}
        self._thumb_size_key = "中"
        for key in ("小", "中", "大"):
            btn = QPushButton(key)
            btn.setFixedWidth(32)
            btn.setCheckable(True)
            btn.setChecked(key == self._thumb_size_key)
            btn.clicked.connect(lambda checked, k=key: self._set_thumb_size(k))
            size_row.addWidget(btn)
        right_layout.addLayout(size_row)
        self.overview_scroll = QScrollArea()
        self.overview_scroll.setWidgetResizable(True)
        self.overview_container = QWidget()
        self.overview_grid = QGridLayout(self.overview_container)
        self.overview_grid.setSpacing(4)
        self.overview_scroll.setWidget(self.overview_container)
        right_layout.addWidget(self.overview_scroll, stretch=1)

        # Actions
        actions = QHBoxLayout()
        self._make_btn("打开原图", self._open_selected_original, actions)
        self._make_btn("替换照片", self._replace_selected_photo, actions)
        self._make_btn("取消关联", self._unlink_selected, actions)
        self._make_btn("移动到...", self._move_selected, actions)
        self._make_btn("跳转到标本", self._jump_to_specimen, actions)
        right_layout.addLayout(actions)
        # Drag-drop support for photo replacement
        self.overview_scroll.setAcceptDrops(True)
        self.overview_scroll.dragEnterEvent = self._overview_drag_enter
        self.overview_scroll.dragMoveEvent = self._overview_drag_move
        self.overview_scroll.dragLeaveEvent = self._overview_drag_leave
        self.overview_scroll.dropEvent = self._overview_drop
        splitter.addWidget(right)

        splitter.setSizes([400, 600])
        layout.addWidget(splitter, stretch=1)

        # Bottom
        bottom = QHBoxLayout()
        bottom.addStretch()
        self._make_btn("关闭", self.close, bottom)
        layout.addLayout(bottom)

        self._update_stats()

    @staticmethod
    def _make_btn(text: str, slot, layout: QHBoxLayout) -> QPushButton:
        btn = QPushButton(text)
        btn.clicked.connect(slot)
        layout.addWidget(btn)
        return btn

    # ---- Pagination ----

    def _update_stats(self) -> None:
        self.stats_label.setText(
            f"入库编号：{len(self.all_vouchers)} 个  |  已入库照片：{self._total_photos} 张"
        )

    def _load_page(self, page: int) -> None:
        self.current_page = max(0, min(page, self.total_pages - 1))
        start = self.current_page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        page_vouchers = self.filtered_vouchers[start:end]

        self.voucher_table.setRowCount(0)
        self.voucher_table.setRowCount(len(page_vouchers))
        for i, voucher in enumerate(page_vouchers):
            self.voucher_table.setItem(i, 0, QTableWidgetItem(voucher))
            count = self._photo_counts.get(voucher, 0)
            self.voucher_table.setItem(i, 1, QTableWidgetItem(str(count)))
            specimen = self.store.get_specimen(voucher) or {}
            tube = str(specimen.get("管内编号*", "") or "")
            self.voucher_table.setItem(i, 2, QTableWidgetItem(tube))

        self.voucher_table.resizeColumnsToContents()
        self.page_label.setText(f"第 {self.current_page + 1} / {self.total_pages} 页")

    def _prev_page(self) -> None:
        if self.current_page > 0:
            self._load_page(self.current_page - 1)

    def _next_page(self) -> None:
        if self.current_page < self.total_pages - 1:
            self._load_page(self.current_page + 1)

    def _on_search_changed(self, text: str) -> None:
        query = text.strip().lower()
        if query:
            self.filtered_vouchers = [
                v for v in self.all_vouchers if v.lower().startswith(query)
            ]
        else:
            self.filtered_vouchers = list(self.all_vouchers)
        self.selected_voucher = None
        self._load_page(0)

    # ---- Voucher selection & photo grid ----

    def _on_voucher_selected(self) -> None:
        rows = self.voucher_table.selectionModel().selectedRows()
        if not rows:
            return
        voucher = self.voucher_table.item(rows[0].row(), 0).text()
        self.selected_voucher = voucher
        self._selected_photos = self.store.get_photos(voucher)
        self._render_photo_grid()

    def _set_thumb_size(self, key: str) -> None:
        self._thumb_size_key = key
        if hasattr(self, "_overview_cards") and self._overview_cards:
            self._render_photo_grid()

    def _render_photo_grid(self) -> None:
        self._thumb_worker.clear_pending()
        for child in self.overview_container.children():
            if isinstance(child, QWidget):
                self.overview_grid.removeWidget(child)
                child.deleteLater()
        self._card_labels.clear()
        self._overview_cards = []
        self._selected_photo_indices.clear()

        photos = self._selected_photos
        tw, th = self._thumb_sizes.get(self._thumb_size_key, (180, 135))
        self.photo_count_label.setText(
            f"{self.selected_voucher} — {len(photos)} 张照片"
        )
        if not photos:
            return

        columns = 4 if tw <= 120 else 3
        for idx, photo in enumerate(photos):
            r, c = idx // columns, idx % columns
            card = QFrame()
            card.setProperty("photo_index", idx)
            card.mousePressEvent = lambda e, i=idx: self._toggle_photo_selection(i)
            card.setStyleSheet("QFrame { background-color: #fff; border: 1px solid #ccc; padding: 4px; }")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(4, 4, 4, 4)

            img_label = QLabel("加载中")
            img_label.setAlignment(Qt.AlignCenter)
            img_label.setFixedSize(tw, th)
            img_label.setStyleSheet("color: #59666b; background-color: #e8eaed;")
            cl.addWidget(img_label)

            name = str(photo.get("文件名", ""))
            name_label = QLabel(name)
            name_label.setWordWrap(True)
            name_label.setMaximumHeight(36 if tw > 160 else 28)
            name_label.setStyleSheet("font-size: 10px; color: #3f4b57; padding: 2px 0;")
            cl.addWidget(name_label)

            path = self.store.resolve_photo_path(photo)
            if path.exists():
                token = idx + 1
                self._thumb_worker.enqueue(path, (tw, th), token)
                self._card_labels[token] = img_label

            self.overview_grid.addWidget(card, r, c)
            self._overview_cards.append(card)

    def _toggle_photo_selection(self, idx: int) -> None:
        if idx in self._selected_photo_indices:
            self._selected_photo_indices.discard(idx)
        else:
            self._selected_photo_indices.add(idx)
        for i, card in enumerate(self._overview_cards):
            if i in self._selected_photo_indices:
                card.setStyleSheet("QFrame { background-color: #d8ebff; border: 2px solid #2a6fbd; padding: 3px; }")
            else:
                card.setStyleSheet("QFrame { background-color: #fff; border: 1px solid #ccc; padding: 4px; }")

    def _on_overview_thumbnail(self, token: int, qpixmap: QPixmap | None, exc: Exception | None) -> None:
        if token < 0:
            return
        label = self._card_labels.get(token)
        if not label or not qpixmap:
            return
        scaled = qpixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled)
        label.setText("")

    # ---- Password gate ----

    def _require_password(self) -> bool:
        if self._password_verified:
            return True
        pw, ok = QInputDialog.getText(
            self, "验证密码", "请输入操作密码：", QLineEdit.Password, ""
        )
        if ok and pw == self.DEFAULT_PASSWORD:
            self._password_verified = True
            return True
        if ok:
            QMessageBox.warning(self, "密码错误", "密码不正确，操作已取消。")
        return False

    # ---- Actions ----

    def _open_selected_original(self) -> None:
        indices = self._selected_photo_indices
        if not indices:
            for i in range(len(self._selected_photos)):
                indices = {i}  # open first if none selected
                break
        for i in indices:
            if i < len(self._selected_photos):
                path = self.store.resolve_photo_path(self._selected_photos[i])
                if path.exists():
                    _open_path(path)

    def _unlink_selected(self) -> None:
        if not self.selected_voucher or not self._selected_photos:
            return
        indices = self._selected_photo_indices
        if not indices:
            QMessageBox.information(self, "提示", "请先在照片上点击选择要取消关联的照片。")
            return
        if not self._require_password():
            return
        count = len(indices)
        reply = QMessageBox.question(
            self, "取消关联",
            f"确定要取消 {self.selected_voucher} 下选中的 {count} 张照片关联吗？\n\n"
            "照片文件不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # Delete in reverse order so smaller indices stay valid
        for photo_idx in sorted(indices, reverse=True):
            self.store.delete_photo(self.selected_voucher, photo_idx)
        self._selected_photos = self.store.get_photos(self.selected_voucher)
        count_after = len(self._selected_photos)
        self._photo_counts[self.selected_voucher] = count_after
        self._total_photos = sum(self._photo_counts.values())
        self._update_stats()
        self._render_photo_grid()
        self._load_page(self.current_page)
        self.app._update_dashboard()
        self.app.refresh_list()

    def _move_selected(self) -> None:
        if not self.selected_voucher or not self._selected_photos:
            return
        indices = self._selected_photo_indices
        if not indices:
            QMessageBox.information(self, "提示", "请先在照片上点击选择要移动的照片。")
            return
        if not self._require_password():
            return
        target, ok = QInputDialog.getText(
            self, "移动到其他编号", "目标入库编号：", QLineEdit.Normal, ""
        )
        if not ok or not target.strip():
            return
        target = target.strip()
        if target not in self.all_vouchers:
            QMessageBox.warning(self, "编号不存在", f"入库编号 {target} 不存在。")
            return
        count = len(indices)
        reply = QMessageBox.question(
            self, "移动照片",
            f"确定将选中的 {count} 张照片从 {self.selected_voucher} 移动到 {target} 吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # Check for conflicts with target before moving
        paths_to_move = []
        for photo_idx in sorted(indices):
            photo = self._selected_photos[photo_idx]
            path = self.store.resolve_photo_path(photo)
            if path.exists():
                paths_to_move.append(path)
        if paths_to_move:
            conflicts = self.store.find_photo_conflicts(paths_to_move, target)
            if conflicts:
                conflict_names = ", ".join(Path(p).name for p in list(conflicts.keys())[:5])
                extra = f" 等{len(conflicts)}张" if len(conflicts) > 5 else ""
                reply = QMessageBox.question(
                    self, "照片冲突",
                    f"以下照片已存在于目标标本 {target} 中：\n{conflict_names}{extra}\n\n"
                    "是否跳过这些照片，仅移动不冲突的照片？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
                conflict_set = {Path(p).resolve() for p in conflicts}
                paths_to_move = [p for p in paths_to_move if p.resolve() not in conflict_set]
        # Move photos to target (reverse order delete so indices stay valid)
        moved = 0
        for photo_idx in sorted(indices, reverse=True):
            photo = self._selected_photos[photo_idx]
            path = self.store.resolve_photo_path(photo)
            if path.exists() and path in paths_to_move:
                try:
                    self.store.add_photo(target, path)
                    moved += 1
                except Exception:
                    continue
            try:
                self.store.delete_photo(self.selected_voucher, photo_idx)
            except Exception:
                pass
        self._selected_photos = self.store.get_photos(self.selected_voucher)
        self._photo_counts[self.selected_voucher] = len(self._selected_photos)
        target_count = len(self.store.get_photos(target))
        self._photo_counts[target] = target_count
        self._total_photos = sum(self._photo_counts.values())
        self._update_stats()
        self._render_photo_grid()
        self._load_page(self.current_page)
        self.app._update_dashboard()
        self.app.refresh_list()

    def _jump_to_specimen(self) -> None:
        if not self.selected_voucher:
            return
        self.app.select_voucher(self.selected_voucher)
        self.close()

    # ---- Photo replacement ----

    def _replace_selected_photo(self) -> None:
        if not self.selected_voucher or not self._selected_photos:
            return
        indices = self._selected_photo_indices
        if len(indices) != 1:
            QMessageBox.information(self, "提示", "请选择一张照片进行替换（只能选一张）。")
            return
        if not self._require_password():
            return
        old_idx = next(iter(indices))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择替换照片", "",
            "图片文件 (*.tif *.tiff *.jpg *.jpeg *.png *.bmp);;所有文件 (*.*)",
        )
        if not paths:
            return
        new_path = Path(paths[0]).resolve()
        if new_path.suffix.lower() not in {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}:
            QMessageBox.warning(self, "格式不支持", "仅支持图片文件。")
            return
        try:
            self.store.delete_photo(self.selected_voucher, old_idx)
            self.store.add_photo(self.selected_voucher, new_path)
        except Exception as exc:
            QMessageBox.critical(self, "替换失败", str(exc))
            return
        self._selected_photos = self.store.get_photos(self.selected_voucher)
        self._photo_counts[self.selected_voucher] = len(self._selected_photos)
        self._total_photos = sum(self._photo_counts.values())
        self._update_stats()
        self._render_photo_grid()
        self._load_page(self.current_page)
        self.app._update_dashboard()
        self.app.refresh_list()
        self.status_label.setText(f"已替换照片：{new_path.name}")

    # ---- Drag-drop photo replacement ----

    def _overview_drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.overview_scroll.setStyleSheet(
                "QScrollArea { background-color: #f4f6f8; border: 2px dashed #2a6fbd; }"
            )

    def _overview_drag_move(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _overview_drag_leave(self, event) -> None:
        self.overview_scroll.setStyleSheet(
            "QScrollArea { background-color: #f4f6f8; border: 1px solid #a9b3bd; }"
        )

    def _overview_drop(self, event) -> None:
        self.overview_scroll.setStyleSheet(
            "QScrollArea { background-color: #f4f6f8; border: 1px solid #a9b3bd; }"
        )
        if not self.selected_voucher or not self._selected_photos:
            event.ignore()
            return
        indices = self._selected_photo_indices
        if len(indices) != 1:
            QMessageBox.information(self, "提示", "请先选择一张要替换的照片（只能选一张），再拖入新照片。")
            event.ignore()
            return
        if not self._require_password():
            event.ignore()
            return
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if not paths:
            event.ignore()
            return
        new_path = Path(paths[0]).resolve()
        if new_path.suffix.lower() not in {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}:
            QMessageBox.warning(self, "格式不支持", "仅支持图片文件。")
            event.ignore()
            return
        old_idx = next(iter(indices))
        try:
            self.store.delete_photo(self.selected_voucher, old_idx)
            self.store.add_photo(self.selected_voucher, new_path)
        except Exception as exc:
            QMessageBox.critical(self, "替换失败", str(exc))
            event.ignore()
            return
        self._selected_photos = self.store.get_photos(self.selected_voucher)
        self._selected_photo_indices.clear()
        self._photo_counts[self.selected_voucher] = len(self._selected_photos)
        self._total_photos = sum(self._photo_counts.values())
        self._update_stats()
        self._render_photo_grid()
        self._load_page(self.current_page)
        self.app._update_dashboard()
        self.app.refresh_list()
        self.status_label.setText(f"已拖入替换照片：{new_path.name}")
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        self._thumb_worker.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Action log dialog
# ---------------------------------------------------------------------------

class ActionLogDialog(QDialog):
    """Show recent undo/redo operations."""

    def __init__(self, app: SpecimenWindow):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("操作记录")
        self.resize(700, 450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("最近操作记录（可撤回/返回）"))
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["时间", "操作类型", "入库编号", "详情"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        rows = app.store._read_plain_rows(app.store.data_dir / "操作记录.xlsx")
        depth = int(app.store.config.get("undo_depth", 200))
        recent = rows[-depth:] if len(rows) > depth else rows
        table.setRowCount(len(recent))
        for i, row in enumerate(reversed(recent)):
            undone = app.store._value(row, "是否撤销") == "是"
            atype = app.store._value(row, "操作类型")
            voucher = app.store._value(row, "入库编号")
            cat = app.store._value(row, "信息类别")
            field = app.store._value(row, "字段名")
            prefix = "[已撤销] " if undone else ""
            table.setItem(i, 0, QTableWidgetItem(app.store._value(row, "时间")))
            table.setItem(i, 1, QTableWidgetItem(prefix + atype))
            table.setItem(i, 2, QTableWidgetItem(voucher))
            detail = f"{cat} {field}" if field else cat
            table.setItem(i, 3, QTableWidgetItem(detail))
        table.resizeColumnsToContents()
        layout.addWidget(table, stretch=1)
        undo_count, redo_count = app._undo_redo_counts()
        layout.addWidget(QLabel(f"可撤回：{undo_count} 条  |  可返回：{redo_count} 条"))
        layout.addWidget(QPushButton("关闭", clicked=self.close))


# Version manager dialog
# ---------------------------------------------------------------------------

class VersionManagerDialog(QDialog):
    def __init__(self, app: SpecimenWindow):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("版本管理")
        self.resize(840, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"软件版本：v{__version__}"))
        layout.addWidget(QLabel(f"工作区：{app.workspace_root}"))

        tabs = QTabWidget()

        # Data versions tab
        data_tab = QWidget()
        data_layout = QVBoxLayout(data_tab)
        data_layout.addWidget(QPushButton("创建当前数据快照", clicked=self._create_snapshot))
        self.data_table = QTableWidget(0, 4)
        self.data_table.setHorizontalHeaderLabels(["版本ID", "时间", "操作类型", "摘要"])
        self.data_table.horizontalHeader().setStretchLastSection(True)
        self.data_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.data_table.setEditTriggers(QTableWidget.NoEditTriggers)
        data_layout.addWidget(self.data_table, stretch=1)
        btn_row = QHBoxLayout()
        btn_row.addWidget(QPushButton("回退到选中版本", clicked=self._restore_snapshot))
        btn_row.addWidget(QPushButton("打开版本目录", clicked=self._open_snapshot_dir))
        data_layout.addLayout(btn_row)
        tabs.addTab(data_tab, "工作区数据版本")
        self._populate_data_versions()

        # Release tab
        release_tab = QWidget()
        release_layout = QVBoxLayout(release_tab)
        self.release_table = QTableWidget(0, 3)
        self.release_table.setHorizontalHeaderLabels(["版本", "exe", "目录"])
        self.release_table.horizontalHeader().setStretchLastSection(True)
        self.release_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.release_table.setEditTriggers(QTableWidget.NoEditTriggers)
        release_layout.addWidget(self.release_table, stretch=1)
        rel_btn_row = QHBoxLayout()
        rel_btn_row.addWidget(QPushButton("打开版本目录", clicked=self._open_release_dir))
        rel_btn_row.addWidget(QPushButton("启动选中版本", clicked=self._launch_release))
        release_layout.addLayout(rel_btn_row)
        tabs.addTab(release_tab, "软件版本")
        self._populate_releases()

        layout.addWidget(tabs, stretch=1)
        layout.addWidget(QPushButton("关闭", clicked=self.close), alignment=Qt.AlignRight)

    def _populate_data_versions(self) -> None:
        rows = self.app.store.list_data_versions()
        self.data_table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self.data_table.setItem(idx, 0, QTableWidgetItem(str(row.get("版本ID", ""))))
            self.data_table.setItem(idx, 1, QTableWidgetItem(str(row.get("时间", ""))))
            self.data_table.setItem(idx, 2, QTableWidgetItem(str(row.get("操作类型", ""))))
            self.data_table.setItem(idx, 3, QTableWidgetItem(str(row.get("摘要", ""))))
            snapshot_path = row.get("快照路径", "")
            if snapshot_path:
                self.data_table.item(idx, 0).setData(Qt.UserRole, str(snapshot_path))

    def _populate_releases(self) -> None:
        releases = list_releases(self.app.workspace_root)
        self.release_table.setRowCount(len(releases))
        for idx, release in enumerate(releases):
            self.release_table.setItem(idx, 0, QTableWidgetItem(release.version))
            self.release_table.setItem(idx, 1, QTableWidgetItem(release.exe_path.name if release.exe_path else ""))
            self.release_table.setItem(idx, 2, QTableWidgetItem(str(release.directory)))
            self.release_table.item(idx, 0).setData(Qt.UserRole, str(release.directory))
            self.release_table.item(idx, 1).setData(Qt.UserRole, str(release.exe_path or ""))

    def _selected_snapshot_path(self) -> Path | None:
        rows = self.data_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "请选择版本", "请先选择一个数据版本。")
            return None
        item = self.data_table.item(rows[0].row(), 0)
        path_str = item.data(Qt.UserRole) if item else ""
        return Path(path_str) if path_str else None

    def _create_snapshot(self) -> None:
        path = self.app.store.create_data_snapshot("手动快照", "用户在版本管理窗口创建")
        QMessageBox.information(self, "快照已创建", str(path))
        self.accept()
        self.app.open_version_manager()

    def _restore_snapshot(self) -> None:
        snapshot = self._selected_snapshot_path()
        if not snapshot:
            return
        if QMessageBox.question(
            self, "确认回退",
            f"回退前会自动保存当前状态。\n确定恢复到 {snapshot.name} 吗？",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.app.store.restore_data_snapshot(snapshot)
        except Exception as exc:
            QMessageBox.critical(self, "回退失败", str(exc))
            return
        self.accept()
        self.app.reload_current()

    def _open_snapshot_dir(self) -> None:
        snapshot = self._selected_snapshot_path()
        if snapshot:
            _open_path(snapshot)

    def _selected_release_paths(self) -> tuple[Path | None, Path | None]:
        rows = self.release_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "请选择版本", "请先选择一个软件版本。")
            return None, None
        r = rows[0].row()
        dir_item = self.release_table.item(r, 0)
        exe_item = self.release_table.item(r, 1)
        directory = Path(dir_item.data(Qt.UserRole)) if dir_item and dir_item.data(Qt.UserRole) else None
        exe = Path(exe_item.data(Qt.UserRole)) if exe_item and exe_item.data(Qt.UserRole) else None
        return directory, exe

    def _open_release_dir(self) -> None:
        directory, _ = self._selected_release_paths()
        if directory:
            _open_path(directory)

    def _launch_release(self) -> None:
        _directory, exe = self._selected_release_paths()
        if not exe:
            QMessageBox.critical(self, "无法启动", "该版本目录中没有可执行文件。")
            return
        if not exe.exists():
            QMessageBox.critical(self, "文件不存在", str(exe))
            return
        from .release_manager import release_roots
        trusted_roots = {r.resolve() for r in release_roots(self.app.workspace_root)}
        if not any(str(exe.resolve()).startswith(str(r)) for r in trusted_roots):
            QMessageBox.critical(self, "安全限制", "可执行文件不在受信任的版本目录内。")
            return
        try:
            if sys.platform != "win32":
                exe.chmod(exe.stat().st_mode | 0o111)
            subprocess.Popen([str(exe), "--workspace", str(self.app.workspace_root)])
        except Exception as exc:
            QMessageBox.critical(self, "启动失败", str(exc))


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, app: SpecimenWindow):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("设置")

        layout = QFormLayout(self)

        self.undo_spin = QSpinBox()
        self.undo_spin.setRange(1, 1000)
        self.undo_spin.setValue(int(app.store.config.get("undo_depth", 200)))
        layout.addRow("操作记录保存步数", self.undo_spin)

        self.quality_combo = QComboBox()
        quality_keys = list(PREVIEW_QUALITY_OPTIONS.keys())
        for key in quality_keys:
            self.quality_combo.addItem(PREVIEW_QUALITY_OPTIONS[key], key)
        current_settings = load_settings()
        current_idx = quality_keys.index(current_settings.preview_quality) if current_settings.preview_quality in quality_keys else 0
        self.quality_combo.setCurrentIndex(current_idx)
        layout.addRow("图片预览质量", self.quality_combo)

        btn_row = QHBoxLayout()
        btn_row.addWidget(QPushButton("保存", clicked=self.accept))
        restore_btn = QPushButton("恢复默认设置")
        restore_btn.clicked.connect(self._restore_defaults)
        btn_row.addWidget(restore_btn)
        layout.addRow(btn_row)

    def _restore_defaults(self) -> None:
        from .app_settings import AppSettings
        defaults = AppSettings()
        self.undo_spin.setValue(200)
        quality_keys = list(PREVIEW_QUALITY_OPTIONS.keys())
        default_idx = quality_keys.index(defaults.preview_quality) if defaults.preview_quality in quality_keys else 0
        self.quality_combo.setCurrentIndex(default_idx)
        # Apply immediately
        self.app.store.set_undo_depth(200)
        save_settings(defaults)
        self.app._cached_preview_quality = defaults.preview_quality
        self.app._show_grid_filenames = defaults.show_grid_filenames
        self.app._show_filename_check.setChecked(defaults.show_grid_filenames)
        QMessageBox.information(self, "已恢复", "所有设置已恢复为默认值。")

    @property
    def undo_depth(self) -> int:
        return self.undo_spin.value()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_app(workspace_root: Path | str | None) -> None:
    if workspace_root is None:
        workspace_root = default_workspace()
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        window = SpecimenWindow(workspace_root)
    except SystemExit:
        QMessageBox.warning(None, "标本入库管理", "未选择工作区，程序将退出。")
        return
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return
    window.show()
    app.exec_()
