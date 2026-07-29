from __future__ import annotations

import csv
import hashlib
import heapq
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

try:
    import rawpy
except Exception:
    rawpy = None


APP_NAME = "PhotoCuller"
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp",
    ".heic", ".heif", ".dng", ".cr2", ".cr3", ".nef", ".arw",
    ".raf", ".orf", ".rw2", ".pef",
}
RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef"}
VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts", ".3gp", ".webm",
}
QUARANTINE_DIR = "_照片筛选隔离"


def _profile(
    profile_id: str,
    name: str,
    *,
    blur_review: float,
    blur_remove: float,
    dark_review: float,
    dark_remove: float,
    bright_clip_review: float,
    bright_clip_remove: float,
    contrast_review: float,
    phash: int,
    dhash: int,
    structure: float,
    time_window: int,
    sequence_gap: int,
    blur_review_percentile: float,
    blur_remove_percentile: float,
    aspect_tolerance: float,
) -> dict[str, Any]:
    return {
        "version": 1,
        "id": profile_id,
        "name": name,
        "builtin": True,
        "quality": {
            "enabled": {
                "sharpness": True,
                "luminance": True,
                "dark_clip": True,
                "bright_clip": True,
                "contrast": True,
                "entropy": True,
                "resolution": True,
                "file_size": True,
            },
            "threshold_mode": "absolute",
            "match_mode": "any",
            "blur_review_percentile": blur_review_percentile,
            "blur_remove_percentile": blur_remove_percentile,
            "blur_review": blur_review,
            "blur_remove": blur_remove,
            "dark_review": dark_review,
            "dark_remove": dark_remove,
            "dark_clip_review": 0.55,
            "dark_clip_remove": 0.72,
            "bright_clip_review": bright_clip_review,
            "bright_clip_remove": bright_clip_remove,
            "contrast_review": contrast_review,
            "contrast_remove": max(8.0, contrast_review * 0.65),
            "entropy_review": 4.8,
            "entropy_remove": 3.8,
            "min_megapixels_review": 0.5,
            "min_megapixels_remove": 0.1,
            "min_size_kb_review": 20,
            "min_size_kb_remove": 1,
            "weights": {
                "sharpness": 0.45,
                "exposure": 0.25,
                "contrast": 0.12,
                "entropy": 0.10,
                "resolution": 0.08,
            },
        },
        "similarity": {
            "exact_duplicates": True,
            "phash_max": phash,
            "dhash_max": dhash,
            "structure_min": structure,
            "aspect_tolerance": aspect_tolerance,
            "time_window_minutes": time_window,
            "sequence_gap": sequence_gap,
            "min_group_size": 2,
            # Visual similarity is the primary signal. Capture time and filename
            # sequence remain useful hints, but must not exclude an otherwise
            # high-confidence match in the built-in modes.
            "allow_cross_time_high_confidence": True,
            "face_safe": True,
        },
    }


BUILTIN_PROFILES = {
    "conservative": _profile(
        "conservative", "保守优先", blur_review=150, blur_remove=70,
        dark_review=28, dark_remove=14, bright_clip_review=0.28,
        bright_clip_remove=0.60, contrast_review=22, phash=6, dhash=6,
        structure=0.90, time_window=20, sequence_gap=25,
        blur_review_percentile=8, blur_remove_percentile=3, aspect_tolerance=0.08,
    ),
    "balanced": _profile(
        "balanced", "平衡模式", blur_review=260, blur_remove=120,
        dark_review=38, dark_remove=22, bright_clip_review=0.18,
        bright_clip_remove=0.42, contrast_review=28, phash=11, dhash=10,
        structure=0.80, time_window=45, sequence_gap=50,
        blur_review_percentile=15, blur_remove_percentile=6, aspect_tolerance=0.11,
    ),
    "aggressive": _profile(
        "aggressive", "积极精简", blur_review=420, blur_remove=190,
        dark_review=50, dark_remove=30, bright_clip_review=0.10,
        bright_clip_remove=0.28, contrast_review=36, phash=16, dhash=14,
        structure=0.68, time_window=90, sequence_gap=90,
        blur_review_percentile=25, blur_remove_percentile=10, aspect_tolerance=0.15,
    ),
}


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "config.json"
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        default_cache = (self.path.parent if self.path else app_data_dir()) / "projects"
        defaults = {
            "version": 1,
            "default_cache_root": str(default_cache),
            "auto_advance": True,
            "theme": "day",
            "projects": {},
            "recent_projects": [],
            "custom_profiles": {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                defaults.update(loaded)
            except Exception:
                pass
        return defaults

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    def profiles(self) -> dict[str, Any]:
        profiles = {key: json.loads(json.dumps(value)) for key, value in BUILTIN_PROFILES.items()}
        profiles.update(self.data.get("custom_profiles", {}))
        return profiles

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return self.profiles().get(profile_id, self.profiles()["conservative"])

    def save_custom_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        validate_profile(profile)
        now = datetime.now().isoformat(timespec="seconds")
        profile = json.loads(json.dumps(profile))
        profile["builtin"] = False
        profile["version"] = 1
        profile.setdefault("created_at", now)
        profile["updated_at"] = now
        if not profile.get("id") or profile["id"] in BUILTIN_PROFILES:
            profile["id"] = "custom-" + hashlib.sha1(f"{profile.get('name')}-{time.time()}".encode()).hexdigest()[:10]
        self.data.setdefault("custom_profiles", {})[profile["id"]] = profile
        self.save()
        return profile

    def delete_custom_profile(self, profile_id: str) -> None:
        if profile_id in BUILTIN_PROFILES:
            raise ValueError("内置模式不能删除")
        for project in self.data.get("projects", {}).values():
            if project.get("profile_id") == profile_id:
                raise ValueError("该配置仍被项目使用，请先切换项目模式")
        self.data.get("custom_profiles", {}).pop(profile_id, None)
        self.save()


def validate_profile(profile: dict[str, Any]) -> None:
    name = str(profile.get("name", "")).strip()
    if not name or len(name) > 40:
        raise ValueError("配置名称必须为 1–40 个字符")
    q = profile.get("quality", {})
    s = profile.get("similarity", {})
    ranges = {
        "blur_review": (0, 10000), "blur_remove": (0, 10000),
        "dark_review": (0, 255), "dark_remove": (0, 255),
        "dark_clip_review": (0, 1), "dark_clip_remove": (0, 1),
        "bright_clip_review": (0, 1), "bright_clip_remove": (0, 1),
        "contrast_review": (0, 128), "contrast_remove": (0, 128),
        "entropy_review": (0, 8), "entropy_remove": (0, 8),
        "min_megapixels_review": (0, 500), "min_megapixels_remove": (0, 500),
        "min_size_kb_review": (0, 10_000_000), "min_size_kb_remove": (0, 10_000_000),
    }
    for key, (low, high) in ranges.items():
        value = float(q.get(key, 0))
        if not low <= value <= high:
            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
    for key, default in (("blur_review_percentile", 5), ("blur_remove_percentile", 1)):
        value = float(q.get(key, default))
        if not 0 <= value <= 100:
            raise ValueError(f"{key} 超出允许范围 0–100")
    weights = q.get("weights", {})
    weight_keys = ("sharpness", "exposure", "contrast", "entropy", "resolution")
    for key in weight_keys:
        if not 0 <= float(weights.get(key, 0)) <= 10:
            raise ValueError(f"{key} 评分权重超出允许范围 0–10")
    if sum(float(weights.get(key, 0)) for key in weight_keys) <= 0:
        raise ValueError("评分权重不能全部为零")
    sim_ranges = {
        "phash_max": (0, 64), "dhash_max": (0, 64), "structure_min": (-1, 1),
        "aspect_tolerance": (0, 1), "time_window_minutes": (0, 10080),
        "sequence_gap": (0, 10000), "min_group_size": (2, 1000),
    }
    for key, (low, high) in sim_ranges.items():
        value = float(s.get(key, 0))
        if not low <= value <= high:
            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
    if float(q["blur_remove"]) > float(q["blur_review"]):
        raise ValueError("移除清晰度阈值不能高于复看阈值")
    if float(q["dark_remove"]) > float(q["dark_review"]):
        raise ValueError("严重欠曝阈值不能高于偏暗阈值")
    for review_key, remove_key, label in (
        ("dark_clip_review", "dark_clip_remove", "暗部溢出"),
        ("bright_clip_review", "bright_clip_remove", "高光溢出"),
    ):
        if float(q[review_key]) > float(q[remove_key]):
            raise ValueError(f"{label}复看阈值不能高于移除阈值")
    for remove_key, review_key, label in (
        ("contrast_remove", "contrast_review", "对比度"),
        ("entropy_remove", "entropy_review", "细节"),
        ("min_megapixels_remove", "min_megapixels_review", "分辨率"),
        ("min_size_kb_remove", "min_size_kb_review", "文件大小"),
    ):
        if float(q[remove_key]) > float(q[review_key]):
            raise ValueError(f"{label}移除阈值不能高于复看阈值")
    if float(q.get("blur_remove_percentile", 1)) > float(q.get("blur_review_percentile", 5)):
        raise ValueError("清晰度移除百分位不能高于复看百分位")


def project_id_for(root: Path) -> str:
    return hashlib.sha1(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project (
          id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL, profile_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS photos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          relative_path TEXT NOT NULL UNIQUE, extension TEXT, size INTEGER, mtime REAL,
          width INTEGER, height INTEGER, megapixels REAL, taken TEXT,
          luminance REAL, contrast REAL, dark_clip REAL, bright_clip REAL,
          sharpness REAL, entropy REAL, phash TEXT, dhash TEXT, sha256 TEXT,
          thumbnail TEXT, error TEXT DEFAULT '', suggestion TEXT DEFAULT 'keep',
          reason TEXT DEFAULT '', decision TEXT DEFAULT '', status TEXT DEFAULT 'active',
          analyzed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_photos_suggestion ON photos(suggestion);
        CREATE INDEX IF NOT EXISTS idx_photos_decision ON photos(decision);
        CREATE TABLE IF NOT EXISTS similar_pairs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,
          score REAL, kind TEXT, recommended_id INTEGER, face_safe INTEGER DEFAULT 0,
          UNIQUE(a_id,b_id)
        );
        CREATE TABLE IF NOT EXISTS quarantine_batches (
          id TEXT PRIMARY KEY, created_at TEXT, manifest_path TEXT, count INTEGER,
          total_size INTEGER, restored_at TEXT DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


@dataclass
class Project:
    project_id: str
    root: Path
    cache_root: Path
    project_dir: Path
    db_path: Path
    thumb_dir: Path
    profile_id: str


class ProjectManager:
    def __init__(self, config: ConfigStore):
        self.config = config

    def open(self, root: str, cache_root: str | None = None) -> Project:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise ValueError("照片文件夹不存在")
        pid = project_id_for(root_path)
        existing = self.config.data.setdefault("projects", {}).get(pid, {})
        cache = Path(cache_root or existing.get("cache_root") or self.config.data["default_cache_root"]).resolve()
        project_dir = cache / pid
        project_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = project_dir / "thumbs"
        thumb_dir.mkdir(exist_ok=True)
        profile_id = existing.get("profile_id", "conservative")
        project = Project(pid, root_path, cache, project_dir, project_dir / "project.db", thumb_dir, profile_id)
        conn = connect_db(project.db_path)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO project(id,root,profile_id,created_at,updated_at) VALUES(1,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET root=excluded.root,profile_id=excluded.profile_id,updated_at=excluded.updated_at""",
            (str(root_path), profile_id, now, now),
        )
        conn.commit()
        conn.close()
        self.config.data["projects"][pid] = {
            "root": str(root_path), "cache_root": str(cache), "profile_id": profile_id,
            "last_opened": now,
        }
        recent = [pid] + [x for x in self.config.data.get("recent_projects", []) if x != pid]
        self.config.data["recent_projects"] = recent[:12]
        self.config.save()
        return project

    def from_id(self, project_id: str) -> Project:
        data = self.config.data.get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        return self.open(data["root"], data.get("cache_root"))

    def project_root(self, project_id: str) -> Path:
        data = self.config.data.get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        root = Path(data["root"]).resolve()
        if not root.is_dir():
            raise ValueError("照片文件夹当前不可用")
        return root

    def remove_from_recent(self, project_id: str) -> None:
        if project_id not in self.config.data.get("projects", {}):
            raise ValueError("项目不存在")
        self.config.data["recent_projects"] = [
            item for item in self.config.data.get("recent_projects", []) if item != project_id
        ]
        self.config.save()

    def migrate_cache(self, project_id: str, new_root: str) -> dict[str, Any]:
        project = self.from_id(project_id)
        new_cache = Path(new_root).resolve()
        new_dir = new_cache / project_id
        if new_dir == project.project_dir:
            return {"changed": False, "path": str(new_dir)}
        if new_dir.exists() and any(new_dir.iterdir()):
            raise ValueError("新位置已有同名项目缓存")
        temp = new_dir.with_name(new_dir.name + ".migrating")
        if temp.exists():
            shutil.rmtree(temp)
        temp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(project.project_dir, temp)
        source_files = sorted(p.relative_to(project.project_dir) for p in project.project_dir.rglob("*") if p.is_file())
        copied_files = sorted(p.relative_to(temp) for p in temp.rglob("*") if p.is_file())
        if source_files != copied_files:
            shutil.rmtree(temp, ignore_errors=True)
            raise RuntimeError("缓存迁移校验失败")
        temp.rename(new_dir)
        self.config.data["projects"][project_id]["cache_root"] = str(new_cache)
        self.config.data["projects"][project_id].setdefault("old_caches", []).append(str(project.project_dir))
        self.config.save()
        return {"changed": True, "path": str(new_dir), "old_cache": str(project.project_dir)}

    def cleanup_old_cache(self, project_id: str, path: str) -> dict[str, Any]:
        data = self.config.data.get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        target = Path(path).resolve()
        allowed = [Path(item).resolve() for item in data.get("old_caches", [])]
        if target not in allowed:
            raise ValueError("该目录不在此项目的待清理旧缓存列表中")
        current = Path(data["cache_root"]).resolve() / project_id
        if target == current or target.name != project_id:
            raise ValueError("不能清理当前项目缓存")
        if target.exists():
            shutil.rmtree(target)
        data["old_caches"] = [item for item in data.get("old_caches", []) if Path(item).resolve() != target]
        self.config.save()
        return {"cleaned": True, "path": str(target)}


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
                return Image.open(io.BytesIO(thumb.data)).convert("RGB")
            return Image.fromarray(thumb.data).convert("RGB")
        except Exception:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
            return Image.fromarray(rgb).convert("RGB")


def open_image(path: Path) -> tuple[Image.Image, str]:
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _open_raw(path), ""
    with Image.open(path) as source:
        source.load()
        exif = source.getexif()
        taken = str(exif.get(36867, "") or exif.get(306, ""))
        return ImageOps.exif_transpose(source).convert("RGB"), taken


def analyze_photo(path: Path, thumb_path: Path) -> dict[str, Any]:
    stat = path.stat()
    base = {
        "extension": path.suffix.lower(), "size": stat.st_size, "mtime": stat.st_mtime,
        "width": 0, "height": 0, "megapixels": 0, "taken": "",
        "luminance": None, "contrast": None, "dark_clip": None, "bright_clip": None,
        "sharpness": None, "entropy": None, "phash": "", "dhash": "",
        "thumbnail": str(thumb_path), "error": "",
    }
    try:
        image, taken = open_image(path)
        width, height = image.size
        preview = image.copy()
        preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(preview)
        arr = np.asarray(gray, dtype=np.float32)
        center = arr[1:-1, 1:-1]
        lap = -4 * center + arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
        hist = np.bincount(arr.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
        probs = hist[hist > 0] / hist.sum()
        entropy = float(-(probs * np.log2(probs)).sum())
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        preview.save(thumb_path, "JPEG", quality=86, optimize=True)
        base.update({
            "width": width, "height": height, "megapixels": round(width * height / 1_000_000, 3),
            "taken": taken, "luminance": round(float(arr.mean()), 3),
            "contrast": round(float(arr.std()), 3), "dark_clip": round(float((arr <= 8).mean()), 5),
            "bright_clip": round(float((arr >= 247).mean()), 5),
            "sharpness": round(float(lap.var()), 3), "entropy": round(entropy, 4),
            "phash": _phash(gray), "dhash": _dhash(gray),
        })
    except Exception as error:
        base["error"] = str(error)
    return base


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count() if a and b else 64


def image_structure(path_a: Path, path_b: Path) -> float:
    try:
        with Image.open(path_a) as a, Image.open(path_b) as b:
            aa = np.asarray(ImageOps.grayscale(a).resize((64, 64)), dtype=np.float32)
            bb = np.asarray(ImageOps.grayscale(b).resize((64, 64)), dtype=np.float32)
        aa -= aa.mean()
        bb -= bb.mean()
        denom = float(np.linalg.norm(aa) * np.linalg.norm(bb))
        return float(np.sum(aa * bb) / denom) if denom else 0.0
    except Exception:
        return 0.0


def classify(row: sqlite3.Row | dict[str, Any], profile: dict[str, Any], percentiles: dict[str, float] | None = None) -> tuple[str, str]:
    if row["error"]:
        return "unreadable", "无法读取或文件损坏"
    q = profile["quality"]
    enabled = q.get("enabled", {})
    reasons_remove: list[str] = []
    reasons_review: list[str] = []
    def flag(key: str, value: bool, severe: bool, reason: str) -> None:
        if enabled.get(key, True) and value:
            (reasons_remove if severe else reasons_review).append(reason)

    mode = q.get("threshold_mode", "absolute")
    blur_review = q["blur_review"]
    blur_remove = q["blur_remove"]
    if mode == "percentile" and percentiles:
        blur_review = percentiles.get("sharpness_review", blur_review)
        blur_remove = percentiles.get("sharpness_remove", blur_remove)
    flag("sharpness", row["sharpness"] is not None and row["sharpness"] < blur_remove, True, "严重失焦")
    flag("sharpness", row["sharpness"] is not None and blur_remove <= row["sharpness"] < blur_review, False, "画面偏软")
    dark_severe = row["luminance"] < q["dark_remove"] and row["dark_clip"] > q["dark_clip_remove"]
    dark_review = row["luminance"] < q["dark_review"] and row["dark_clip"] > q["dark_clip_review"]
    flag("luminance", dark_severe, True, "严重欠曝")
    flag("luminance", not dark_severe and dark_review, False, "画面偏暗")
    flag("bright_clip", row["bright_clip"] > q["bright_clip_remove"], True, "高光严重溢出")
    flag("bright_clip", q["bright_clip_review"] < row["bright_clip"] <= q["bright_clip_remove"], False, "高光溢出")
    flag("contrast", row["contrast"] < q["contrast_remove"], True, "对比度极低")
    flag("contrast", q["contrast_remove"] <= row["contrast"] < q["contrast_review"], False, "对比度偏低")
    flag("entropy", row["entropy"] < q["entropy_remove"], True, "细节极少")
    flag("entropy", q["entropy_remove"] <= row["entropy"] < q["entropy_review"], False, "细节偏少")
    flag("resolution", row["megapixels"] < q["min_megapixels_remove"], True, "分辨率过低")
    flag("resolution", q["min_megapixels_remove"] <= row["megapixels"] < q["min_megapixels_review"], False, "分辨率偏低")
    size_kb = row["size"] / 1024
    flag("file_size", size_kb < q["min_size_kb_remove"], True, "文件异常小")
    flag("file_size", q["min_size_kb_remove"] <= size_kb < q["min_size_kb_review"], False, "文件较小")
    match_all = q.get("match_mode") == "all"
    if reasons_remove and (not match_all or len(reasons_remove) >= 2):
        return "remove", "、".join(dict.fromkeys(reasons_remove))
    if reasons_review or reasons_remove:
        return "review", "、".join(dict.fromkeys(reasons_review + reasons_remove))
    return "keep", ""


def classification_percentiles(
    rows: Iterable[sqlite3.Row | dict[str, Any]], profile: dict[str, Any]
) -> dict[str, float] | None:
    if profile["quality"].get("threshold_mode") != "percentile":
        return None
    sharp = np.asarray([row["sharpness"] for row in rows if row["sharpness"] is not None])
    if not len(sharp):
        return None
    return {
        "sharpness_remove": float(
            np.percentile(sharp, profile["quality"].get("blur_remove_percentile", 1))
        ),
        "sharpness_review": float(
            np.percentile(sharp, profile["quality"].get("blur_review_percentile", 5))
        ),
    }


def quality_score(row: sqlite3.Row | dict[str, Any], profile: dict[str, Any]) -> float:
    q = profile["quality"]
    w = q["weights"]
    sharp = math.log1p(max(0, row["sharpness"] or 0)) / 10
    exposure = 1 - min(1.0, abs((row["luminance"] or 128) - 110) / 140 + (row["dark_clip"] or 0) + (row["bright_clip"] or 0))
    contrast = min(1.0, (row["contrast"] or 0) / 70)
    entropy = min(1.0, (row["entropy"] or 0) / 8)
    resolution = min(1.0, (row["megapixels"] or 0) / 12)
    return (
        sharp * w["sharpness"] + exposure * w["exposure"] + contrast * w["contrast"]
        + entropy * w["entropy"] + resolution * w["resolution"]
    )


def filename_sequence(name: str) -> int:
    match = re.search(r"(\d+)(?!.*\d)", name)
    return int(match.group(1)) if match else -1


def parse_taken(value: str) -> float | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).timestamp()
        except ValueError:
            pass
    return None


def photo_shooting_key(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    taken = parse_taken(str(row["taken"] or ""))
    path = str(row["relative_path"])
    sequence = filename_sequence(Path(path).name)
    if taken is not None:
        return (0, taken, sequence if sequence >= 0 else math.inf, path.casefold())
    return (1, sequence if sequence >= 0 else math.inf, path.casefold())


def build_similarity_groups(
    conn: sqlite3.Connection, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Collapse active pair relations into deterministic connected photo groups."""
    rows = conn.execute(
        "SELECT * FROM photos WHERE status='active' AND error=''"
    ).fetchall()
    photos = {int(row["id"]): row for row in rows}
    edges = conn.execute(
        """SELECT sp.a_id,sp.b_id,sp.score,sp.kind,sp.face_safe
           FROM similar_pairs sp
           JOIN photos a ON a.id=sp.a_id
           JOIN photos b ON b.id=sp.b_id
           WHERE a.status='active' AND b.status='active'"""
    ).fetchall()

    parent: dict[int, int] = {}

    def find(value: int) -> int:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    adjacency: dict[int, list[tuple[int, float]]] = {}
    face_safe_ids: set[int] = set()
    for edge in edges:
        left, right = int(edge["a_id"]), int(edge["b_id"])
        if left not in photos or right not in photos:
            continue
        union(left, right)
        score = float(edge["score"] or 0)
        adjacency.setdefault(left, []).append((right, score))
        adjacency.setdefault(right, []).append((left, score))
        if edge["face_safe"]:
            face_safe_ids.update((left, right))

    components: dict[int, list[int]] = {}
    for photo_id in parent:
        components.setdefault(find(photo_id), []).append(photo_id)

    minimum = max(2, int(profile.get("similarity", {}).get("min_group_size", 2)))
    groups: list[dict[str, Any]] = []
    for member_ids in components.values():
        if len(member_ids) < minimum:
            continue
        members = [photos[photo_id] for photo_id in member_ids]
        ranked = sorted(
            members,
            key=lambda row: (-quality_score(row, profile), str(row["relative_path"]).casefold()),
        )
        recommended = ranked[0]
        recommended_id = int(recommended["id"])

        # Maximum-bottleneck path gives every transitive member a meaningful
        # confidence relative to the recommended photo.
        confidence = {photo_id: 0.0 for photo_id in member_ids}
        confidence[recommended_id] = 1.0
        pending: list[tuple[float, int]] = [(-1.0, recommended_id)]
        while pending:
            negative_score, current = heapq.heappop(pending)
            current_score = -negative_score
            if current_score < confidence[current]:
                continue
            for neighbor, edge_score in adjacency.get(current, []):
                candidate = min(current_score, edge_score)
                if candidate > confidence.get(neighbor, 0.0):
                    confidence[neighbor] = candidate
                    heapq.heappush(pending, (-candidate, neighbor))

        hashes = {str(row["sha256"]) for row in members if row["sha256"]}
        exact = len(hashes) == 1 and len(hashes) and all(row["sha256"] for row in members)
        ordered = sorted(members, key=photo_shooting_key)
        stable_ids = ",".join(str(photo_id) for photo_id in sorted(member_ids))
        group_id = "sg-" + hashlib.sha1(stable_ids.encode("ascii")).hexdigest()[:16]
        groups.append(
            {
                "id": group_id,
                "member_ids": sorted(member_ids),
                "members": ordered,
                "recommended_id": recommended_id,
                "recommended": recommended,
                "covers": [recommended, *[row for row in ranked if row["id"] != recommended_id]][:4],
                "confidence": confidence,
                "kind": "exact" if exact else "similar",
                "face_safe": any(photo_id in face_safe_ids for photo_id in member_ids),
                "sort_key": min(photo_shooting_key(row) for row in members),
            }
        )
    groups.sort(key=lambda group: (group["sort_key"], group["id"]))
    return groups


class Scanner:
    def __init__(self, config: ConfigStore, manager: ProjectManager):
        self.config = config
        self.manager = manager
        self.progress: dict[str, dict[str, Any]] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}

    def start(self, project_id: str) -> None:
        if project_id in self.threads and self.threads[project_id].is_alive():
            return
        cancel = threading.Event()
        self.cancel_events[project_id] = cancel
        self.progress[project_id] = {"stage": "starting", "current": 0, "total": 0, "done": False, "error": ""}
        thread = threading.Thread(target=self._run, args=(project_id, cancel), daemon=True)
        self.threads[project_id] = thread
        thread.start()

    def cancel(self, project_id: str) -> None:
        if project_id in self.cancel_events:
            self.cancel_events[project_id].set()

    def _set(self, project_id: str, **values: Any) -> None:
        self.progress.setdefault(project_id, {}).update(values)

    def _run(self, project_id: str, cancel: threading.Event) -> None:
        try:
            project = self.manager.from_id(project_id)
            profile = self.config.get_profile(project.profile_id)
            self._set(project_id, stage="discovering")
            discovered = [
                path for path in project.root.rglob("*")
                if path.is_file() and QUARANTINE_DIR not in path.parts
            ]
            files = [path for path in discovered if path.suffix.lower() in IMAGE_EXTENSIONS]
            unsupported = [path for path in discovered if path.suffix.lower() not in IMAGE_EXTENSIONS]
            unsupported_extensions: dict[str, int] = {}
            for path in unsupported:
                key = path.suffix.lower() or "无扩展名"
                unsupported_extensions[key] = unsupported_extensions.get(key, 0) + 1
            video_count = sum(1 for path in unsupported if path.suffix.lower() in VIDEO_EXTENSIONS)
            self._set(
                project_id,
                stage="analyzing",
                total=len(files),
                current=0,
                discovered_total=len(discovered),
                unsupported_count=len(unsupported),
                video_count=video_count,
                unsupported_extensions=unsupported_extensions,
            )
            conn = connect_db(project.db_path)
            existing = {row["relative_path"]: row for row in conn.execute("SELECT * FROM photos")}
            seen: set[str] = set()
            for index, path in enumerate(files, 1):
                if cancel.is_set():
                    self._set(project_id, stage="cancelled", done=True)
                    conn.close()
                    return
                rel = path.relative_to(project.root).as_posix()
                seen.add(rel)
                stat = path.stat()
                old = existing.get(rel)
                if old and old["size"] == stat.st_size and abs(old["mtime"] - stat.st_mtime) < 0.001 and old["thumbnail"]:
                    self._set(project_id, current=index)
                    continue
                thumb_name = hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".jpg"
                metrics = analyze_photo(path, project.thumb_dir / thumb_name)
                suggestion, reason = classify(metrics, profile)
                values = {
                    "relative_path": rel, **metrics, "suggestion": suggestion, "reason": reason,
                    "analyzed_at": datetime.now().isoformat(timespec="seconds"),
                }
                columns = list(values)
                sql = f"""INSERT INTO photos({','.join(columns)}) VALUES({','.join('?' for _ in columns)})
                          ON CONFLICT(relative_path) DO UPDATE SET
                          {','.join(f'{col}=excluded.{col}' for col in columns if col != 'relative_path')}"""
                conn.execute(sql, [values[col] for col in columns])
                if index % 20 == 0:
                    conn.commit()
                self._set(project_id, current=index, file=rel)
            for rel, row in existing.items():
                if rel not in seen and row["status"] == "active":
                    conn.execute("UPDATE photos SET status='missing' WHERE relative_path=?", (rel,))
            conn.commit()
            self._set(project_id, stage="hashing")
            self._exact_hashes(project, conn, cancel)
            self._set(project_id, stage="grouping")
            self.rebuild_similarity(project, conn, profile, cancel)
            self.reclassify(project, conn, profile)
            conn.close()
            self._auto_import_csv(project)
            self._set(project_id, stage="complete", done=True, current=len(files), total=len(files))
        except Exception as error:
            self._set(project_id, stage="error", done=True, error=str(error))

    def _exact_hashes(self, project: Project, conn: sqlite3.Connection, cancel: threading.Event) -> None:
        sizes = conn.execute(
            "SELECT size,COUNT(*) c FROM photos WHERE status='active' AND error='' GROUP BY size HAVING c>1"
        ).fetchall()
        for size_row in sizes:
            for row in conn.execute("SELECT id,relative_path,sha256 FROM photos WHERE status='active' AND size=?", (size_row["size"],)):
                if cancel.is_set():
                    return
                if row["sha256"]:
                    continue
                path = project.root / row["relative_path"]
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                conn.execute("UPDATE photos SET sha256=? WHERE id=?", (digest.hexdigest(), row["id"]))
        conn.commit()

    def reclassify(self, project: Project, conn: sqlite3.Connection, profile: dict[str, Any]) -> None:
        rows = conn.execute("SELECT * FROM photos WHERE status='active'").fetchall()
        percentiles = classification_percentiles(rows, profile)
        for row in rows:
            suggestion, reason = classify(row, profile, percentiles)
            conn.execute("UPDATE photos SET suggestion=?,reason=? WHERE id=?", (suggestion, reason, row["id"]))
        conn.commit()

    def rebuild_similarity(
        self, project: Project, conn: sqlite3.Connection, profile: dict[str, Any], cancel: threading.Event | None = None
    ) -> None:
        conn.execute("DELETE FROM similar_pairs")
        rows = conn.execute("SELECT * FROM photos WHERE status='active' AND error=''").fetchall()
        by_dir: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_dir.setdefault(str(Path(row["relative_path"]).parent).casefold(), []).append(row)
        sim = profile["similarity"]
        pairs: list[tuple[int, int, float, str, int, int]] = []
        seen: set[tuple[int, int]] = set()
        if sim.get("exact_duplicates", True):
            by_hash: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                if row["sha256"]:
                    by_hash.setdefault(row["sha256"], []).append(row)
            for group in by_hash.values():
                if len(group) < 2:
                    continue
                best = max(group, key=lambda row: quality_score(row, profile))
                for row in group:
                    if row["id"] == best["id"]:
                        continue
                    key = tuple(sorted((best["id"], row["id"])))
                    seen.add(key)
                    pairs.append((key[0], key[1], 1.0, "exact", best["id"], 0))
        for group in by_dir.values():
            group.sort(key=lambda row: (filename_sequence(Path(row["relative_path"]).name), row["relative_path"]))
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    if cancel and cancel.is_set():
                        return
                    seq_a = filename_sequence(Path(a["relative_path"]).name)
                    seq_b = filename_sequence(Path(b["relative_path"]).name)
                    seq_gap = abs(seq_b - seq_a) if seq_a >= 0 and seq_b >= 0 else 999999
                    ta, tb = parse_taken(a["taken"]), parse_taken(b["taken"])
                    time_gap = abs(ta - tb) / 60 if ta is not None and tb is not None else None
                    nearby = seq_gap <= sim["sequence_gap"] or (time_gap is not None and time_gap <= sim["time_window_minutes"])
                    if not nearby and not sim.get("allow_cross_time_high_confidence"):
                        if seq_gap > sim["sequence_gap"]:
                            break
                        continue
                    aspect_a = a["width"] / max(1, a["height"])
                    aspect_b = b["width"] / max(1, b["height"])
                    if abs(aspect_a - aspect_b) / max(aspect_a, aspect_b) > sim["aspect_tolerance"]:
                        continue
                    ph = hamming(a["phash"], b["phash"])
                    dh = hamming(a["dhash"], b["dhash"])
                    if ph > sim["phash_max"] or dh > sim["dhash_max"]:
                        continue
                    structure = image_structure(Path(a["thumbnail"]), Path(b["thumbnail"]))
                    if structure < sim["structure_min"]:
                        continue
                    key = tuple(sorted((a["id"], b["id"])))
                    if key in seen:
                        continue
                    recommended = a if quality_score(a, profile) >= quality_score(b, profile) else b
                    score = 0.45 * (1 - ph / 64) + 0.25 * (1 - dh / 64) + 0.30 * structure
                    pairs.append((key[0], key[1], score, "similar", recommended["id"], int(sim.get("face_safe", True))))
                    seen.add(key)
        conn.executemany(
            "INSERT OR IGNORE INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe) VALUES(?,?,?,?,?,?)",
            pairs,
        )
        conn.commit()

    def _auto_import_csv(self, project: Project) -> None:
        conn = connect_db(project.db_path)
        count = conn.execute("SELECT COUNT(*) FROM photos WHERE decision<>''").fetchone()[0]
        conn.close()
        if count:
            return
        candidates = [project.root / "照片筛选结果.csv", project.root.parent / f"{project.root.name}筛选结果.csv"]
        for candidate in candidates:
            if candidate.exists():
                import_decisions(project, candidate)
                break


def import_decisions(project: Project, csv_path: Path) -> dict[str, int]:
    conn = connect_db(project.db_path)
    imported = missing = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            decision = row.get("决定") or row.get("decision") or ""
            raw_path = (row.get("路径") or row.get("path") or "").replace("\\", "/")
            if decision not in {"keep", "remove"}:
                continue
            candidates = [raw_path]
            prefix = project.root.name + "/"
            if raw_path.startswith(prefix):
                candidates.append(raw_path[len(prefix):])
            target = None
            for rel in candidates:
                found = conn.execute("SELECT id FROM photos WHERE relative_path=?", (rel,)).fetchone()
                if found:
                    target = found["id"]
                    break
            if target:
                conn.execute("UPDATE photos SET decision=? WHERE id=?", (decision, target))
                imported += 1
            else:
                missing += 1
    conn.commit()
    conn.close()
    return {"imported": imported, "missing": missing}


def export_decisions(project: Project) -> str:
    conn = connect_db(project.db_path)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["决定", "路径", "建议", "原因"])
    for row in conn.execute("SELECT decision,relative_path,suggestion,reason FROM photos WHERE decision<>'' ORDER BY relative_path"):
        writer.writerow([row["decision"], row["relative_path"], row["suggestion"], row["reason"]])
    conn.close()
    return "\ufeff" + output.getvalue()


def clear_decisions(project: Project) -> int:
    conn = connect_db(project.db_path)
    cursor = conn.execute(
        "UPDATE photos SET decision='' WHERE status='active' AND decision<>''"
    )
    conn.commit()
    cleared = cursor.rowcount
    conn.close()
    return cleared


def quarantine_preview(project: Project) -> dict[str, Any]:
    conn = connect_db(project.db_path)
    rows = conn.execute(
        "SELECT id,relative_path,size,mtime FROM photos WHERE decision='remove' AND status='active' ORDER BY relative_path"
    ).fetchall()
    conn.close()
    return {"count": len(rows), "total_size": sum(row["size"] for row in rows), "items": [dict(row) for row in rows]}


def apply_quarantine(project: Project) -> dict[str, Any]:
    preview = quarantine_preview(project)
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_root = project.root / QUARANTINE_DIR / batch_id
    manifest: list[dict[str, Any]] = []
    conn = connect_db(project.db_path)
    for item in preview["items"]:
        source = project.root / item["relative_path"]
        if not source.exists():
            manifest.append({"relative_path": item["relative_path"], "status": "missing"})
            continue
        stat = source.stat()
        if stat.st_size != item["size"] or abs(stat.st_mtime - item["mtime"]) > 0.01:
            manifest.append({"relative_path": item["relative_path"], "status": "changed"})
            continue
        destination = batch_root / item["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(destination.stem + f"__{batch_id}" + destination.suffix)
        shutil.move(str(source), str(destination))
        manifest.append({
            "relative_path": item["relative_path"], "quarantine_path": str(destination.relative_to(project.root)),
            "status": "moved", "size": item["size"],
        })
        conn.execute("UPDATE photos SET status='quarantined' WHERE id=?", (item["id"],))
    batch_root.mkdir(parents=True, exist_ok=True)
    manifest_path = batch_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with (batch_root / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "quarantine_path", "status", "size"])
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in writer.fieldnames} for row in manifest])
    moved = [row for row in manifest if row["status"] == "moved"]
    conn.execute(
        "INSERT INTO quarantine_batches(id,created_at,manifest_path,count,total_size) VALUES(?,?,?,?,?)",
        (batch_id, datetime.now().isoformat(timespec="seconds"), str(manifest_path), len(moved), sum(row["size"] for row in moved)),
    )
    conn.commit()
    conn.close()
    return {"batch_id": batch_id, "moved": len(moved), "skipped": len(manifest) - len(moved)}


def restore_batch(project: Project, batch_id: str) -> dict[str, Any]:
    conn = connect_db(project.db_path)
    batch = conn.execute("SELECT * FROM quarantine_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        raise ValueError("隔离批次不存在")
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    restored = conflicts = missing = 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for item in manifest:
        if item.get("status") != "moved":
            continue
        source = project.root / item["quarantine_path"]
        if not source.exists():
            missing += 1
            continue
        destination = project.root / item["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(destination.stem + f".restored-{stamp}" + destination.suffix)
            conflicts += 1
        shutil.move(str(source), str(destination))
        conn.execute("UPDATE photos SET status='active',relative_path=? WHERE relative_path=?", (
            destination.relative_to(project.root).as_posix(), item["relative_path"],
        ))
        restored += 1
    conn.execute("UPDATE quarantine_batches SET restored_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), batch_id))
    conn.commit()
    conn.close()
    return {"restored": restored, "conflicts": conflicts, "missing": missing}
