from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True)
class MotionAsset:
    kind: str
    path: Path
    offset: int = 0
    length: int = 0
    asset_id: str = ""
    presentation_timestamp_us: int = -1

    def storage_values(self, root: Path) -> dict[str, Any]:
        stat = self.path.stat()
        relative = self.path.relative_to(root).as_posix()
        return {
            "media_type": "motion_photo",
            "motion_kind": self.kind,
            "motion_relative_path": relative,
            "motion_offset": self.offset,
            "motion_length": self.length,
            "motion_size": self.length or stat.st_size,
            "motion_mtime": stat.st_mtime,
            "motion_asset_id": self.asset_id,
        }


def _xmp_prefix(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    with path.open("rb") as source:
        return source.read(limit).decode("utf-8", "ignore")


def embedded_motion_asset(path: Path) -> MotionAsset | None:
    """Read standard Google/Samsung Motion Photo XMP without signature guessing."""
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return None
    try:
        xmp = _xmp_prefix(path)
        marked = re.search(
            r"(?:GCamera|Camera|Samsung):(?:MotionPhoto|MicroVideo)\s*=\s*[\"']1[\"']",
            xmp,
            re.IGNORECASE,
        )
        if not marked:
            return None
        legacy = re.search(
            r"(?:MicroVideoOffset|MotionPhotoOffset)\s*=\s*[\"'](\d+)[\"']",
            xmp,
            re.IGNORECASE,
        )
        length = int(legacy.group(1)) if legacy else 0
        if not length:
            for element in re.findall(r"<[^>]+>", xmp):
                if re.search(r"Semantic\s*=\s*[\"']MotionPhoto[\"']", element, re.I):
                    found = re.search(r"Length\s*=\s*[\"'](\d+)[\"']", element, re.I)
                    if found:
                        length = int(found.group(1))
                        break
        size = path.stat().st_size
        if length <= 0 or length >= size:
            return None
        timestamp_match = re.search(
            r"(?:GCamera|Camera):(?:MotionPhoto|MicroVideo)PresentationTimestampUs"
            r"\s*=\s*[\"'](-?\d+)[\"']",
            xmp,
            re.IGNORECASE,
        )
        timestamp_us = int(timestamp_match.group(1)) if timestamp_match else -1
        return MotionAsset(
            "android_embedded", path, size - length, length,
            presentation_timestamp_us=timestamp_us,
        )
    except (OSError, ValueError):
        return None


def paired_motion_asset(photo: Path, sidecars: dict[tuple[Path, str], Path]) -> MotionAsset | None:
    embedded = embedded_motion_asset(photo)
    if embedded:
        return embedded
    key = (photo.parent.resolve(), photo.stem.casefold())
    sidecar = sidecars.get(key)
    if sidecar and sidecar.resolve() != photo.resolve():
        return MotionAsset("apple_sidecar", sidecar)
    return None


def motion_asset_from_row(root: Path, row: Any) -> MotionAsset:
    relative = str(row["motion_relative_path"] or row["relative_path"])
    path = (root / Path(relative)).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError("动态照片视频路径超出项目目录")
    return MotionAsset(
        str(row["motion_kind"]),
        path,
        int(row["motion_offset"] or 0),
        int(row["motion_length"] or 0),
        str(row["motion_asset_id"] or ""),
    )


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:
        raise RuntimeError("动态照片视频组件未安装") from error


def _motion_input(asset: MotionAsset):
    if not asset.offset:
        return None, asset.path
    temporary = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    temporary_path = Path(temporary.name)
    try:
        with asset.path.open("rb") as source:
            source.seek(asset.offset)
            remaining = asset.length
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    break
                temporary.write(block)
                remaining -= len(block)
    finally:
        temporary.close()
    return temporary_path, temporary_path


def probe_motion(asset: MotionAsset) -> dict[str, Any]:
    temporary, input_path = _motion_input(asset)
    try:
        result = subprocess.run(
            [ffmpeg_executable(), "-hide_banner", "-i", str(input_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = result.stderr
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        video_match = re.search(
            r"Stream[^\n]*Video:[^\n]*?\b(\d{2,5})x(\d{2,5})\b[^\n]*?(\d+(?:\.\d+)?)\s*fps",
            output,
        )
        if not duration_match or not video_match:
            raise RuntimeError("无法读取动态照片的视频信息")
        hours, minutes, seconds = duration_match.groups()
        duration = (int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        fps = float(video_match.group(3))
        asset_id_match = re.search(
            r"com\.apple\.quicktime\.content\.identifier\s*:\s*([^\r\n]+)", output,
            re.I,
        )
        return {
            "motion_duration_ms": max(1, round(duration * 1000)),
            "motion_fps": fps,
            "motion_frame_count": max(1, round(duration * fps)),
            "motion_width": int(video_match.group(1)),
            "motion_height": int(video_match.group(2)),
            "motion_asset_id": asset_id_match.group(1).strip() if asset_id_match else asset.asset_id,
            "motion_error": "",
        }
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def motion_fingerprint(asset: MotionAsset) -> str:
    stat = asset.path.stat()
    payload = f"{asset.path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{asset.offset}:{asset.length}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def ensure_motion_video(asset: MotionAsset, motion_dir: Path) -> Path:
    motion_dir.mkdir(parents=True, exist_ok=True)
    # Keep the cache version in the filename so previously generated, silent
    # WebM files are never reused after audio support is added.
    target = motion_dir / f"{motion_fingerprint(asset)}.av.webm"
    if target.is_file() and target.stat().st_size:
        return target
    temporary_source, input_path = _motion_input(asset)
    temporary_target = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp.webm")
    try:
        command = [
            ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libvpx-vp9",
            "-crf", "34", "-b:v", "0", "-deadline", "good", "-cpu-used", "4",
            "-c:a", "libopus", "-b:a", "96k",
            str(temporary_target),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode or not temporary_target.is_file() or not temporary_target.stat().st_size:
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "动态照片视频转换失败")
        temporary_target.replace(target)
        return target
    finally:
        temporary_target.unlink(missing_ok=True)
        if temporary_source:
            temporary_source.unlink(missing_ok=True)


def _cover_match_image(image: Image.Image, size: int = 64) -> np.ndarray:
    gray = image.convert("L")
    try:
        fitted = ImageOps.fit(
            gray, (size, size), method=Image.Resampling.LANCZOS
        )
        try:
            pixels = np.asarray(fitted, dtype=np.float32)
            return (pixels - pixels.mean()) / max(1.0, float(pixels.std()))
        finally:
            fitted.close()
    finally:
        gray.close()


def locate_motion_still_time(
    photo: Path,
    asset: MotionAsset,
    duration_ms: int,
    fps: float,
) -> int:
    """Locate the still image in the motion track, using metadata or pixels."""
    frame_duration_ms = max(1, math.ceil(1000 / fps)) if fps > 0 else 1
    last_frame_ms = max(0, int(duration_ms) - frame_duration_ms)
    if asset.presentation_timestamp_us >= 0:
        return min(last_frame_ms, round(asset.presentation_timestamp_us / 1000))
    if duration_ms <= 0 or fps <= 0:
        return 0

    still: Image.Image | None = None
    temporary_source: Path | None = None
    try:
        still, _ = open_image(photo)
        reference = _cover_match_image(still)
        temporary_source, input_path = _motion_input(asset)
        size = reference.shape[0]
        completed = subprocess.run(
            [
                ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
                "-i", str(input_path), "-map", "0:v:0",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=increase,"
                f"crop={size}:{size},format=gray",
                "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ],
            capture_output=True,
            timeout=45,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        frame_size = size * size
        frame_count = len(completed.stdout) // frame_size
        if completed.returncode or frame_count <= 0:
            return 0
        frames = np.frombuffer(
            completed.stdout[: frame_count * frame_size], dtype=np.uint8
        ).reshape(frame_count, size, size).astype(np.float32)
        means = frames.mean(axis=(1, 2), keepdims=True)
        deviations = np.maximum(1.0, frames.std(axis=(1, 2), keepdims=True))
        normalized = (frames - means) / deviations
        scores = np.mean((normalized - reference) ** 2, axis=(1, 2))
        best_index = int(np.argmin(scores))
        return min(last_frame_ms, max(0, round(best_index * 1000 / fps)))
    finally:
        if still is not None:
            still.close()
        if temporary_source:
            temporary_source.unlink(missing_ok=True)


def extract_motion_frame(video: Path, time_ms: int, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp.jpg")
    try:
        completed = subprocess.run(
            [
                ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video), "-ss", f"{time_ms / 1000:.3f}",
                "-frames:v", "1", "-q:v", "2", str(temporary),
            ],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode or not temporary.is_file():
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "无法提取动态照片封面")
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def extract_motion_asset_frame(
    asset: MotionAsset, time_ms: int, target: Path
) -> Path:
    temporary_source, input_path = _motion_input(asset)
    try:
        return extract_motion_frame(input_path, time_ms, target)
    finally:
        if temporary_source:
            temporary_source.unlink(missing_ok=True)


def _jpeg_metadata_and_image(data: bytes) -> tuple[list[bytes], bytes]:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("源文件不是有效的 JPEG")
    position = 2
    metadata: list[bytes] = []
    while position + 4 <= len(data) and data[position] == 0xFF:
        start = position
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD9, 0xDA}:
            return metadata, data[start:]
        if marker in {0x01, *range(0xD0, 0xD8)}:
            return metadata, data[start:]
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        end = position + length
        if length < 2 or end > len(data):
            break
        segment = data[start:end]
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            metadata.append(segment)
            position = end
            continue
        return metadata, data[start:]
    raise ValueError("JPEG 段结构损坏")


def _motion_xmp_timestamp(segment: bytes, time_ms: int) -> bytes:
    if b"MotionPhoto" not in segment and b"MicroVideo" not in segment:
        return segment
    timestamp = str(max(0, int(time_ms)) * 1000).encode("ascii")
    pattern = re.compile(
        rb"((?:MotionPhoto|MicroVideo)PresentationTimestampUs\s*=\s*[\"'])"
        rb"-?\d+([\"'])",
        re.IGNORECASE,
    )
    updated, count = pattern.subn(rb"\g<1>" + timestamp + rb"\g<2>", segment)
    if count:
        result = updated
    else:
        prefix_match = re.search(
            rb"([A-Za-z][A-Za-z0-9_-]*):(?:MotionPhoto|MicroVideo)\s*=",
            segment,
            re.IGNORECASE,
        )
        prefix = prefix_match.group(1) if prefix_match else b"GCamera"
        attribute = (
            b" " + prefix + b':MotionPhotoPresentationTimestampUs="' + timestamp + b'"'
        )
        result = re.sub(
            rb"(<rdf:Description\b)", rb"\1" + attribute, segment, count=1
        )
    declared_length = len(result) - 2
    if declared_length > 0xFFFF:
        raise ValueError("Motion Photo XMP 段过大，无法安全写回")
    return result[:2] + declared_length.to_bytes(2, "big") + result[4:]


def _write_jpeg_motion_cover(
    photo: Path,
    frame: Path,
    asset: MotionAsset,
    time_ms: int,
    target: Path,
) -> None:
    source_data = photo.read_bytes()
    still_end = asset.offset if asset.kind == "android_embedded" else len(source_data)
    original_metadata, _ = _jpeg_metadata_and_image(source_data[:still_end])
    _, frame_image = _jpeg_metadata_and_image(frame.read_bytes())
    metadata = [
        _motion_xmp_timestamp(segment, time_ms)
        if asset.kind == "android_embedded"
        else segment
        for segment in original_metadata
    ]
    payload = b"\xff\xd8" + b"".join(metadata) + frame_image
    if asset.kind == "android_embedded":
        payload += source_data[asset.offset : asset.offset + asset.length]
    target.write_bytes(payload)


def _write_heif_motion_cover(photo: Path, frame: Path, target: Path) -> None:
    if open_heif is None or from_pillow is None:
        raise RuntimeError("HEIC/HEIF 写回组件未安装")
    original = open_heif(photo, convert_hdr_to_8bit=True, reload_size=True)
    original_info = dict(original[original.primary_index].info)
    with Image.open(frame) as source:
        source.load()
        converted = source.convert("RGB")
        try:
            encoded = from_pillow(converted)
        finally:
            converted.close()
    for key in ("exif", "xmp", "metadata", "icc_profile"):
        value = original_info.get(key)
        if value:
            encoded.info[key] = value
    encoded.save(target, quality=92)


def restore_motion_source(backup: Path, photo: Path) -> None:
    temporary = photo.with_name(
        f".{photo.name}.cullumi-restore-{uuid.uuid4().hex}.tmp{photo.suffix}"
    )
    try:
        shutil.copy2(backup, temporary)
        temporary.replace(photo)
    finally:
        temporary.unlink(missing_ok=True)


def write_motion_cover_source(
    photo: Path,
    frame: Path,
    asset: MotionAsset,
    time_ms: int,
    backup_root: Path,
    revision: int,
) -> dict[str, Any]:
    """Safely replace the source still while retaining Live/Motion metadata."""
    suffix = photo.suffix.lower()
    if suffix not in {".jpg", ".jpeg", *HEIF_EXTENSIONS}:
        raise ValueError("原片写回目前仅支持 JPEG、HEIC 和 HEIF 动态照片")
    temporary = photo.with_name(
        f".{photo.name}.cullumi-{uuid.uuid4().hex}.tmp{photo.suffix}"
    )
    backup_dir = backup_root / hashlib.sha1(
        str(photo.resolve()).casefold().encode("utf-8")
    ).hexdigest()[:16]
    backup = backup_dir / f"cover-{revision}-{uuid.uuid4().hex[:8]}-{photo.name}"
    replaced = False
    try:
        if suffix in {".jpg", ".jpeg"}:
            _write_jpeg_motion_cover(photo, frame, asset, time_ms, temporary)
        else:
            if asset.kind == "android_embedded":
                raise ValueError("内嵌式 Motion Photo 必须使用 JPEG 容器")
            _write_heif_motion_cover(photo, frame, temporary)
        image, _ = open_image(temporary)
        image.close()
        embedded = embedded_motion_asset(temporary) if asset.kind == "android_embedded" else None
        if asset.kind == "android_embedded" and (
            embedded is None or embedded.length != asset.length
        ):
            raise RuntimeError("写回后的 Motion Photo 视频结构校验失败")
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(photo, backup)
        temporary.replace(photo)
        replaced = True
        written_asset = embedded_motion_asset(photo) if asset.kind == "android_embedded" else asset
        return {
            "backup": backup,
            "asset": written_asset or asset,
            "stat": photo.stat(),
        }
    except Exception:
        if replaced and backup.is_file():
            restore_motion_source(backup, photo)
        raise
    finally:
        temporary.unlink(missing_ok=True)


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


