from __future__ import annotations

import hashlib
import math
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

_NUMPY = None
_TIFFFILE = None
_TIFF_IMPORT_ATTEMPTED = False
DEFAULT_MEMORY_CACHE_BYTES = 64 * 1024 * 1024
LOW_MEMORY_CACHE_BYTES = 16 * 1024 * 1024  # 规范化软件设计 2026-05 启动卡死优化:2GB 机器降到 16MB

# 解码硬上限（像素数）。标本扫描原图常达数亿像素，全分辨率解码进内存 + 多张并发会耗尽
# 内存导致整机卡死。任何超过此上限的图，在做 exif/convert/缩放等会分配全尺寸缓冲的操作
# 之前，先按整数倍降采样到上限以内。约 24MP，足够生成任何缩略图/预览。
_MAX_DECODE_PIXELS = 24_000_000


def _default_memory_limit() -> int:
    """根据运行环境选默认缩略图缓存大小。

    规范化软件设计 2026-05 起优先读 settings.memory_profile,
    经 env_detect.memory_profile_params 映射为具体 bytes。
    settings 未配置 / profile 异常 fallback 到原 is_low_memory 二档逻辑。
    """
    try:
        from .app_settings import load_settings
        from .env_detect import memory_profile_params
        profile = load_settings().memory_profile
        return memory_profile_params(profile)["thumb_cache_bytes"]
    except Exception:
        pass
    try:
        from .env_detect import is_low_memory
        return LOW_MEMORY_CACHE_BYTES if is_low_memory() else DEFAULT_MEMORY_CACHE_BYTES
    except Exception:
        return DEFAULT_MEMORY_CACHE_BYTES


class ThumbnailCache:
    def __init__(self, workspace_root: Path | str, memory_limit_bytes: int | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.cache_dir = self.workspace_root / "数据" / "缩略图缓存"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # memory_limit_bytes=None 时按环境自动选(规范化软件设计 2026-05 新增)。
        if memory_limit_bytes is None:
            memory_limit_bytes = _default_memory_limit()
        self.memory_limit_bytes = max(4 * 1024 * 1024, int(memory_limit_bytes))
        self._memory_cache: OrderedDict[str, tuple[Image.Image, int]] = OrderedDict()
        self._memory_cache_bytes = 0
        self._lock = threading.RLock()

    def set_workspace(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.cache_dir = self.workspace_root / "数据" / "缩略图缓存"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.clear_memory_cache()

    def thumbnail(self, source: Path | str, size: tuple[int, int]) -> Image.Image:
        source_path = Path(source).resolve()
        key = self._cache_key(source_path, size)
        cached_image = self._get_from_memory(key)
        if cached_image is not None:
            return cached_image
        cached = self.cache_dir / f"{key}.jpg"
        if cached.exists():
            with Image.open(cached) as image:
                loaded = image.copy()
            self._put_in_memory(key, loaded)
            return loaded.copy()
        image = load_source_image(source_path, max_size=size)
        if image.width > size[0] or image.height > size[1]:
            image.thumbnail(size, Image.LANCZOS)
        image = _prepare_for_cache(image)
        tmp = self.cache_dir / f"{key}.{uuid.uuid4().hex}.tmp.jpg"
        image.save(tmp, "JPEG", quality=86, optimize=True)
        tmp.replace(cached)
        self._put_in_memory(key, image)
        return image.copy()

    def _cache_key(self, source: Path, size: tuple[int, int]) -> str:
        stat = source.stat()
        payload = f"{source}|{stat.st_size}|{stat.st_mtime_ns}|{size[0]}x{size[1]}".encode("utf-8", errors="surrogatepass")
        return hashlib.sha1(payload).hexdigest()

    def clear_memory_cache(self) -> None:
        with self._lock:
            self._memory_cache.clear()
            self._memory_cache_bytes = 0

    def set_memory_limit(self, memory_limit_bytes: int) -> None:
        """更新内存缓存限额，并立即释放超过新限额的旧缩略图。"""
        with self._lock:
            self.memory_limit_bytes = max(4 * 1024 * 1024, int(memory_limit_bytes))
            self._trim_memory_cache()

    def _get_from_memory(self, key: str) -> Image.Image | None:
        with self._lock:
            item = self._memory_cache.get(key)
            if item is None:
                return None
            image, byte_count = item
            self._memory_cache.move_to_end(key)
            return image.copy()

    def _put_in_memory(self, key: str, image: Image.Image) -> None:
        byte_count = _image_byte_count(image)
        with self._lock:
            old = self._memory_cache.pop(key, None)
            if old is not None:
                self._memory_cache_bytes -= old[1]
            # 旧逻辑至少保留一张图片，即使单张已超档位限额；低内存机器切换大图后 RSS
            # 无法降下来。超额单张由当前 QPixmap 显示即可，不再额外常驻 PIL 缓存。
            if byte_count > self.memory_limit_bytes:
                return
            cached = image.copy()
            self._memory_cache[key] = (cached, byte_count)
            self._memory_cache_bytes += byte_count
            self._trim_memory_cache()

    def _trim_memory_cache(self) -> None:
        while self._memory_cache_bytes > self.memory_limit_bytes and self._memory_cache:
            _old_key, (_old_image, old_bytes) = self._memory_cache.popitem(last=False)
            self._memory_cache_bytes -= old_bytes


def _image_byte_count(image: Image.Image) -> int:
    bands = max(1, len(image.getbands()))
    return max(1, image.width * image.height * bands)


def _downsample_if_huge(image: Image.Image, max_size: tuple[int, int] | None) -> Image.Image:
    """对超过 _MAX_DECODE_PIXELS 的图先做整数倍降采样（reduce），再交给后续转换。

    原代码直接对全分辨率图做 exif_transpose / convert / thumbnail —— 这些都会分配全尺寸
    缓冲，超大图会瞬时吃掉数百 MB 内存。reduce() 是高效的整数倍盒式降采样，开销远小于
    全分辨率转换。返回的图保证像素数 <= _MAX_DECODE_PIXELS。
    """
    width, height = image.size
    pixels = width * height
    if pixels <= _MAX_DECODE_PIXELS:
        return image
    factor = math.ceil(math.sqrt(pixels / _MAX_DECODE_PIXELS))
    if max_size and max_size[0] > 0 and max_size[1] > 0:
        # 若已知目标尺寸，可降得更狠（缩略图用途下没必要保留过多分辨率）。
        factor_for_target = min(width // max_size[0], height // max_size[1])
        if factor_for_target > factor:
            factor = factor_for_target
    factor = max(2, int(factor))
    try:
        return image.reduce(factor)
    except Exception:
        # reduce 不可用时退回 thumbnail（仍比全分辨率转换省内存）。
        image.thumbnail((max(1, width // factor), max(1, height // factor)), Image.LANCZOS)
        return image


def load_source_image(path: Path, max_size: tuple[int, int] | None = None) -> Image.Image:
    try:
        return _load_pillow_image(path, max_size=max_size)
    except Exception as pillow_exc:
        # Pillow 可读取的 TIFF 优先使用其按目标尺寸缩放路径；旧逻辑先调用 tifffile
        # materialize 整张数组，预览缩略图时会造成不必要的高峰值。特殊 TIFF 若 Pillow
        # 不支持，再保留 tifffile 作为兼容回退。
        if path.suffix.lower() in {".tif", ".tiff"}:
            image = _load_tiff(path, max_size=max_size)
            if image is not None:
                return image
        raise pillow_exc


def _load_pillow_image(path: Path, max_size: tuple[int, int] | None = None) -> Image.Image:
    # image 变量多次重赋值，源缓冲与中间对象由 with 关闭；返回前 load() 使返回图像
    # 不再依赖文件句柄。
    with Image.open(path) as image:
        target_before_orientation = _target_size_before_orientation(image, max_size)
        # draft() 让 JPEG 在解码阶段按目标尺寸降比例解码（对其它格式是 no-op）。
        if target_before_orientation:
            try:
                image.draft("RGB", target_before_orientation)
            except Exception:
                pass
        image = _downsample_if_huge(image, target_before_orientation)
        # 旧逻辑在这里先 exif_transpose/convert，20MP TIFF 会先生成整图缓冲，再缩到
        # 800x600。现在先在源方向缩到预览目标，再转置，显著降低单图峰值。
        if target_before_orientation and (
            image.width > target_before_orientation[0] or image.height > target_before_orientation[1]
        ):
            image.thumbnail(target_before_orientation, Image.LANCZOS)
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if max_size and (image.width > max_size[0] or image.height > max_size[1]):
            image.thumbnail(max_size, Image.LANCZOS)
        image.load()
        return image


def _target_size_before_orientation(
    image: Image.Image,
    max_size: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if max_size is None:
        return None
    try:
        orientation = int(image.getexif().get(274, 1))
    except Exception:
        orientation = 1
    if orientation in (5, 6, 7, 8):
        return max_size[1], max_size[0]
    return max_size


def _load_tiff(path: Path, max_size: tuple[int, int] | None = None) -> Image.Image | None:
    tifffile, np = _tiff_stack()
    if tifffile is None or np is None:
        return None
    try:
        with tifffile.TiffFile(path) as tif:
            page = _best_tiff_page(tif.pages)
            if page is None:
                return None
            shape = getattr(page, "shape", None)
            stride = 1
            if shape and len(shape) >= 2:
                h, w = _shape_height_width(shape)
                if max_size:
                    stride = max(1, min(h // max(max_size[1], 1), w // max(max_size[0], 1)))
                # 即使没给 max_size，也按解码上限强制降采样：超大单页 TIFF 整页 materialize
                # 会耗尽内存，这里保证 stride 后的像素数落在 _MAX_DECODE_PIXELS 以内。
                if h * w > _MAX_DECODE_PIXELS:
                    cap_stride = math.ceil(math.sqrt((h * w) / _MAX_DECODE_PIXELS))
                    stride = max(stride, cap_stride)
            array = _page_asarray_low_memory(page)
            if stride > 1:
                slices = tuple(
                    slice(None, None, stride) if i < 2 else slice(None)
                    for i in range(array.ndim)
                )
                array = array[slices].copy()
        return _array_to_image(array)
    except Exception:
        return None


def _page_asarray_low_memory(page: object) -> object:
    for kwargs in (
        {"out": "memmap", "maxworkers": 1},
        {"out": "memmap"},
        {"maxworkers": 1},
        {},
    ):
        try:
            return page.asarray(**kwargs)
        except TypeError:
            continue
        except Exception:
            if kwargs:
                continue
            raise
    return page.asarray()


def _best_tiff_page(pages: Iterable[object]) -> object | None:
    candidates = []
    for page in pages:
        shape = getattr(page, "shape", None)
        if not shape:
            continue
        height, width = _shape_height_width(shape)
        candidates.append((height * width, page))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _shape_height_width(shape: object) -> tuple[int, int]:
    dims = tuple(int(dim) for dim in shape)
    if len(dims) >= 3 and dims[0] in (3, 4) and dims[-1] not in (3, 4):
        return dims[-2], dims[-1]
    return dims[0], dims[1]


def _array_to_image(array: object) -> Image.Image:
    _tifffile, np = _tiff_stack()
    arr = np.asarray(array)
    arr = np.squeeze(arr)
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim == 3 and arr.shape[-1] > 4:
        arr = arr[..., :3]
    arr = _to_uint8(arr)
    image = Image.fromarray(arr)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _to_uint8(arr: object) -> object:
    _tifffile, np = _tiff_stack()
    if arr.dtype == np.uint8:
        return arr
    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        if np.issubdtype(arr.dtype, np.unsignedinteger):
            shift = max(0, info.bits - 8)
            if shift:
                return np.asarray(arr >> shift, dtype=np.uint8)
            return np.asarray(arr, dtype=np.uint8)
        scale = 255.0 / max(1, info.max - info.min)
        return np.asarray((arr.astype(np.float32) - info.min) * scale, dtype=np.uint8)
    arr = arr.astype(np.float32)
    min_value = float(np.nanmin(arr))
    max_value = float(np.nanmax(arr))
    if max_value <= min_value:
        return np.zeros(arr.shape, dtype=np.uint8)
    return np.asarray((arr - min_value) / (max_value - min_value) * 255, dtype=np.uint8)


def _tiff_stack() -> tuple[object | None, object | None]:
    global _NUMPY, _TIFFFILE, _TIFF_IMPORT_ATTEMPTED
    if _TIFF_IMPORT_ATTEMPTED:
        return _TIFFFILE, _NUMPY
    _TIFF_IMPORT_ATTEMPTED = True
    try:
        import numpy
        import tifffile
    except Exception:
        _NUMPY = None
        _TIFFFILE = None
    else:
        _NUMPY = numpy
        _TIFFFILE = tifffile
    return _TIFFFILE, _NUMPY


def _prepare_for_cache(image: Image.Image) -> Image.Image:
    if image.mode not in ("RGB", "L"):
        return image.convert("RGB")
    return image
