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

# 解码硬上限（像素数）。标本扫描原图常达数亿像素，全分辨率解码进内存 + 多张并发会耗尽
# 内存导致整机卡死。任何超过此上限的图，在做 exif/convert/缩放等会分配全尺寸缓冲的操作
# 之前，先按整数倍降采样到上限以内。约 24MP，足够生成任何缩略图/预览。
_MAX_DECODE_PIXELS = 24_000_000


class ThumbnailCache:
    def __init__(self, workspace_root: Path | str, memory_limit_bytes: int = DEFAULT_MEMORY_CACHE_BYTES):
        self.workspace_root = Path(workspace_root).resolve()
        self.cache_dir = self.workspace_root / "数据" / "缩略图缓存"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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

    def _get_from_memory(self, key: str) -> Image.Image | None:
        with self._lock:
            item = self._memory_cache.get(key)
            if item is None:
                return None
            image, byte_count = item
            self._memory_cache.move_to_end(key)
            return image.copy()

    def _put_in_memory(self, key: str, image: Image.Image) -> None:
        cached = image.copy()
        byte_count = _image_byte_count(cached)
        with self._lock:
            old = self._memory_cache.pop(key, None)
            if old is not None:
                self._memory_cache_bytes -= old[1]
            self._memory_cache[key] = (cached, byte_count)
            self._memory_cache_bytes += byte_count
            while self._memory_cache_bytes > self.memory_limit_bytes and len(self._memory_cache) > 1:
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
    if path.suffix.lower() in {".tif", ".tiff"}:
        image = _load_tiff(path, max_size=max_size)
        if image is not None:
            return image
    with Image.open(path) as image:
        # draft() 让 JPEG 在解码阶段就按目标尺寸降比例解码（对其它格式是 no-op）；
        # 之后 _downsample_if_huge 兜底处理超大图，避免全分辨率中间缓冲导致内存爆。
        if max_size:
            image.draft("RGB", max_size)
        image = _downsample_if_huge(image, max_size)
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if max_size and (image.width > max_size[0] or image.height > max_size[1]):
            image.thumbnail(max_size, Image.LANCZOS)
        return image


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
