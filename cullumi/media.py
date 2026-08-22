from __future__ import annotations

import hashlib
import io
import math
import os
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import from_pillow, open_heif, register_heif_opener

    register_heif_opener()
except Exception:
    from_pillow = None
    open_heif = None

try:
    import rawpy
except Exception:
    rawpy = None


HEIF_EXTENSIONS = {".heic", ".heics", ".heif", ".heifs", ".hif"}
RAW_EXTENSIONS = {
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef"
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp",
    *HEIF_EXTENSIONS, *RAW_EXTENSIONS,
}
DISPLAY_PREVIEW_EXTENSIONS = HEIF_EXTENSIONS | RAW_EXTENSIONS | {".tif", ".tiff"}
DISPLAY_PREVIEW_MAX_SIZE = (2560, 2560)
VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts",
    ".3gp", ".webm",
}




def _dct_matrix(n: int) -> np.ndarray:
    matrix = np.empty((n, n), dtype=np.float32)
    factor = math.pi / (2 * n)
    for k in range(n):
        scale = math.sqrt(1 / n) if k == 0 else math.sqrt(2 / n)
        for i in range(n):
            matrix[k, i] = scale * math.cos((2 * i + 1) * k * factor)
    return matrix


DCT32 = _dct_matrix(32)


def _phash(gray: Image.Image) -> str:
    arr = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    coeff = DCT32 @ arr @ DCT32.T
    block = coeff[:8, :8]
    median = float(np.median(block[1:, :]))
    value = 0
    for bit in (block > median).ravel():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _dhash(gray: Image.Image) -> str:
    arr = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    value = 0
    for bit in (arr[:, 1:] > arr[:, :-1]).ravel():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _open_raw(path: Path) -> Image.Image:
    if rawpy is None:
        raise RuntimeError("RAW 解码组件未安装")
    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as embedded:
                    return embedded.convert("RGB")
            source = Image.fromarray(thumb.data)
            try:
                return source.convert("RGB")
            finally:
                source.close()
        except Exception:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
            source = Image.fromarray(rgb)
            try:
                return source.convert("RGB")
            finally:
                source.close()


def _open_heif(path: Path) -> Image.Image:
    if open_heif is None:
        raise RuntimeError("HEIC/HEIF 解码组件未安装")
    container = open_heif(path, convert_hdr_to_8bit=True, reload_size=True)
    if not len(container):
        raise UnidentifiedImageError("HEIC/HEIF 文件中没有可读取的照片")

    # Prefer the declared primary image, but tolerate phone containers whose
    # primary item is damaged while another full-size image remains readable.
    indices = [container.primary_index] + [
        index for index in range(len(container)) if index != container.primary_index
    ]
    errors: list[Exception] = []
    for index in indices:
        try:
            return container[index].to_pillow()
        except Exception as error:
            errors.append(error)
    raise UnidentifiedImageError(f"HEIC/HEIF 解码失败：{errors[0]}") from errors[0]


def open_image(path: Path) -> tuple[Image.Image, str]:
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _open_raw(path), ""
    if path.suffix.lower() in HEIF_EXTENSIONS:
        source = _open_heif(path)
        try:
            exif = source.getexif()
            taken = str(exif.get(36867, "") or exif.get(306, ""))
            oriented = ImageOps.exif_transpose(source)
            try:
                return oriented.convert("RGB"), taken
            finally:
                if oriented is not source:
                    oriented.close()
        finally:
            source.close()
    with Image.open(path) as source:
        source.load()
        exif = source.getexif()
        taken = str(exif.get(36867, "") or exif.get(306, ""))
        oriented = ImageOps.exif_transpose(source)
        try:
            return oriented.convert("RGB"), taken
        finally:
            if oriented is not source:
                oriented.close()


def display_preview_path(source: Path, thumbnail: Path) -> Path:
    """Return a cache path tied to the source file's current contents."""
    stat = source.stat()
    fingerprint = hashlib.sha1(
        f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
    ).hexdigest()[:12]
    return thumbnail.with_name(f"{thumbnail.stem}.display-{fingerprint}.jpg")


def ensure_display_preview(source: Path, thumbnail: Path) -> Path:
    """Build and atomically cache a browser-friendly, high-resolution preview."""
    target = display_preview_path(source, thumbnail)
    if target.is_file():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    image: Image.Image | None = None
    try:
        image, _ = open_image(source)
        image.thumbnail(DISPLAY_PREVIEW_MAX_SIZE, Image.Resampling.LANCZOS)
        image.save(temporary, "JPEG", quality=92, optimize=True)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        if image is not None:
            image.close()

    prefix = f"{thumbnail.stem}.display-"
    for candidate in target.parent.iterdir():
        if (
            candidate != target
            and candidate.name.startswith(prefix)
            and candidate.name.endswith(".jpg")
        ):
            try:
                candidate.unlink()
            except OSError:
                pass
    return target


def analyze_photo(
    path: Path,
    thumb_path: Path,
    stat: os.stat_result | None = None,
) -> dict[str, Any]:
    base = {
        "extension": path.suffix.lower(), "size": 0, "mtime": 0,
        "width": 0, "height": 0, "megapixels": 0, "taken": "",
        "luminance": None, "contrast": None, "dark_clip": None, "bright_clip": None,
        "sharpness": None, "entropy": None, "phash": "", "dhash": "",
        "sha256": "", "thumbnail": str(thumb_path), "error": "",
    }
    image: Image.Image | None = None
    preview: Image.Image | None = None
    gray: Image.Image | None = None
    temporary = thumb_path.with_suffix(thumb_path.suffix + ".tmp")
    try:
        stat = stat or path.stat()
        base.update({"size": stat.st_size, "mtime": stat.st_mtime})
        image, taken = open_image(path)
        width, height = image.size
        preview = image
        image = None
        preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(preview)
        arr = np.asarray(gray, dtype=np.float32)
        center = arr[1:-1, 1:-1]
        lap = -4 * center + arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
        hist = np.bincount(arr.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
        probs = hist[hist > 0] / hist.sum()
        entropy = float(-(probs * np.log2(probs)).sum())
        metrics = {
            "width": width, "height": height, "megapixels": round(width * height / 1_000_000, 3),
            "taken": taken, "luminance": round(float(arr.mean()), 3),
            "contrast": round(float(arr.std()), 3), "dark_clip": round(float((arr <= 8).mean()), 5),
            "bright_clip": round(float((arr >= 247).mean()), 5),
            "sharpness": round(float(lap.var()), 3), "entropy": round(entropy, 4),
            "phash": _phash(gray), "dhash": _dhash(gray),
        }
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        preview.save(temporary, "JPEG", quality=86, optimize=True)
        temporary.replace(thumb_path)
        base.update(metrics)
    except Exception as error:
        base["error"] = str(error)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        for resource in (gray, preview, image):
            if resource is not None:
                resource.close()
    return base


