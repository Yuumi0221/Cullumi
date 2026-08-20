from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .similarity import (
    SimilarityGroupCache,
    _structure_similarity,
    _structure_vector,
    build_similarity_groups,
    filename_sequence,
    hamming,
    hamming_candidate_pairs,
    image_structure,
    parse_taken,
    photo_shooting_key,
    quality_score,
)

try:
    from pillow_heif import open_heif, register_heif_opener

    register_heif_opener()
except Exception:
    open_heif = None

try:
    import rawpy
except Exception:
    rawpy = None


APP_NAME = "Cullumi"
DATABASE_SCHEMA_VERSION = 1
HEIF_EXTENSIONS = {".heic", ".heics", ".heif", ".heifs", ".hif"}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp",
    *HEIF_EXTENSIONS, ".dng", ".cr2", ".cr3", ".nef", ".arw",
    ".raf", ".orf", ".rw2", ".pef",
}
RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef"}
DISPLAY_PREVIEW_EXTENSIONS = HEIF_EXTENSIONS | RAW_EXTENSIONS | {".tif", ".tiff"}
DISPLAY_PREVIEW_MAX_SIZE = (2560, 2560)
VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".mts", ".m2ts", ".3gp", ".webm",
}
PHOTO_DECISION_FILTERS = frozenset({"undecided", "keep", "remove"})
PHOTO_AI_FILTERS = frozenset({"remove", "review", "no_suggestion"})
PHOTO_ANALYSIS_COLUMNS = (
    "relative_path", "extension", "size", "mtime", "width", "height",
    "megapixels", "taken", "luminance", "contrast", "dark_clip",
    "bright_clip", "sharpness", "entropy", "phash", "dhash", "sha256",
    "thumbnail", "error", "suggestion", "reason", "status", "analyzed_at",
)
PHOTO_UPSERT_SQL = f"""INSERT INTO photos({','.join(PHOTO_ANALYSIS_COLUMNS)})
    VALUES({','.join('?' for _ in PHOTO_ANALYSIS_COLUMNS)})
    ON CONFLICT(relative_path) DO UPDATE SET
    {','.join(f'{column}=excluded.{column}' for column in PHOTO_ANALYSIS_COLUMNS if column != 'relative_path')}"""


def parse_photo_filter(raw: str | None, allowed: frozenset[str], label: str) -> set[str]:
    """Parse an all/none/comma-separated photo filter without changing stored values."""
    if raw is None or raw == "all":
        return set(allowed)
    if raw == "":
        raise ValueError(f"{label}不能为空；请使用 all 或 none")
    if raw == "none":
        return set()
    values = {value.strip() for value in raw.split(",") if value.strip()}
    invalid = values - allowed
    if invalid:
        raise ValueError(f"{label}包含无效值：{', '.join(sorted(invalid))}")
    if not values:
        raise ValueError(f"{label}不能为空；请使用 all 或 none")
    return values


def photo_filter_where(
    file_state: str,
    decisions: set[str],
    ai_states: set[str],
) -> tuple[str, list[Any]]:
    """Return the canonical SQL predicate shared by photo queries and UI counts."""
    if file_state not in {"readable", "unreadable"}:
        raise ValueError("file 必须是 readable 或 unreadable")
    clauses = ["status='active'"]
    params: list[Any] = []
    if file_state == "readable":
        clauses.append("COALESCE(error,'')=''")
        clauses.append("suggestion<>'unreadable'")
    else:
        clauses.append("(COALESCE(error,'')<>'' OR suggestion='unreadable')")

    if not decisions or not ai_states:
        clauses.append("0")
        return " AND ".join(clauses), params

    if decisions != PHOTO_DECISION_FILTERS:
        stored_decisions = ["" if value == "undecided" else value for value in sorted(decisions)]
        placeholders = ",".join("?" for _ in stored_decisions)
        clauses.append(f"decision IN ({placeholders})")
        params.extend(stored_decisions)

    if ai_states != PHOTO_AI_FILTERS:
        stored_ai = ["keep" if value == "no_suggestion" else value for value in sorted(ai_states)]
        placeholders = ",".join("?" for _ in stored_ai)
        clauses.append(f"suggestion IN ({placeholders})")
        params.extend(stored_ai)
    return " AND ".join(clauses), params


def photo_library_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for every sidebar preset, using the same readable-photo definition."""
    readable = "status='active' AND COALESCE(error,'')='' AND suggestion<>'unreadable'"
    unreadable = "status='active' AND (COALESCE(error,'')<>'' OR suggestion='unreadable')"
    row = conn.execute(
        f"""SELECT
              SUM(CASE WHEN {readable} THEN 1 ELSE 0 END) readable,
              SUM(CASE WHEN {readable} AND decision='' THEN 1 ELSE 0 END) undecided,
              SUM(CASE WHEN {readable} AND decision='keep' THEN 1 ELSE 0 END) keep,
              SUM(CASE WHEN {readable} AND decision='remove' THEN 1 ELSE 0 END) remove,
              SUM(CASE WHEN {readable} AND decision=''
                        AND suggestion IN ('remove','review') THEN 1 ELSE 0 END) ai_pending,
              SUM(CASE WHEN {readable} AND decision=''
                        AND suggestion='remove' THEN 1 ELSE 0 END) ai_remove_pending,
              SUM(CASE WHEN {unreadable} THEN 1 ELSE 0 END) unreadable
           FROM photos"""
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}
QUARANTINE_DIR = "_照片筛选隔离"


class ScanCancelled(Exception):
    """Internal control flow used to stop every scan stage consistently."""


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled


def _is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def safe_relative_path(root: Path, relative_path: str, label: str = "文件路径") -> Path:
    """Resolve an untrusted relative path and keep it inside ``root``."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"{label}为空")
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError(f"{label}必须是相对路径")
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    if target == resolved_root or not _is_within(target, resolved_root):
        raise ValueError(f"{label}超出项目目录")
    return target


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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
        self._load_warning: str | None = None
        self.data, should_save = self._load()
        if should_save:
            try:
                self.save()
            except OSError as error:
                self._append_load_warning(f"修复后的配置暂时无法写入：{error}")

    @property
    def load_warning(self) -> str | None:
        """Describe startup recovery without allowing callers to mutate the state."""
        return self._load_warning

    def _append_load_warning(self, message: str) -> None:
        self._load_warning = (
            f"{self._load_warning}；{message}" if self._load_warning else message
        )

    def _defaults(self) -> dict[str, Any]:
        default_cache = self.path.parent / "projects"
        return {
            "version": 1,
            "default_cache_root": str(default_cache),
            "auto_advance": True,
            "auto_check_updates": True,
            "theme": "day",
            "projects": {},
            "recent_projects": [],
            "custom_profiles": {},
        }

    def _temp_path(self) -> Path:
        return self.path.with_suffix(".tmp")

    def _backup_damaged(self, source: Path) -> Path | None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = self.path.suffix or ".json"
        backup = self.path.with_name(
            f"{self.path.stem}.damaged-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"
        )
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
        except OSError as error:
            self._append_load_warning(
                f"配置内容异常，但原文件无法备份，因此未覆盖它：{error}"
            )
            return None
        return backup

    def _normalize(self, loaded: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        defaults = self._defaults()
        normalized = copy.deepcopy(defaults)
        normalized.update(copy.deepcopy(loaded))
        issues: list[str] = []

        version = loaded.get("version", defaults["version"])
        if type(version) is not int or version < 1:
            normalized["version"] = defaults["version"]
            issues.append("version 类型无效")

        cache_root = loaded.get("default_cache_root", defaults["default_cache_root"])
        if not isinstance(cache_root, str) or not cache_root.strip():
            normalized["default_cache_root"] = defaults["default_cache_root"]
            issues.append("默认缓存位置无效")

        for key in ("auto_advance", "auto_check_updates"):
            if key in loaded and not isinstance(loaded[key], bool):
                normalized[key] = defaults[key]
                issues.append(f"{key} 类型无效")

        theme = loaded.get("theme", defaults["theme"])
        if isinstance(theme, str) and theme.strip().lower() in {"day", "night"}:
            cleaned_theme = theme.strip().lower()
            normalized["theme"] = cleaned_theme
            if cleaned_theme != theme:
                issues.append("主题值已规范化")
        else:
            normalized["theme"] = defaults["theme"]
            issues.append("主题值无效")

        custom_profiles: dict[str, Any] = {}
        raw_profiles = loaded.get("custom_profiles", {})
        if not isinstance(raw_profiles, dict):
            issues.append("自定义模式列表类型无效")
        else:
            for profile_id, raw_profile in raw_profiles.items():
                if (
                    not isinstance(profile_id, str)
                    or not profile_id.strip()
                    or profile_id in BUILTIN_PROFILES
                    or not isinstance(raw_profile, dict)
                ):
                    issues.append("已忽略无效的自定义模式")
                    continue
                profile = copy.deepcopy(raw_profile)
                try:
                    validate_profile(profile)
                except (TypeError, ValueError):
                    issues.append(f"已忽略损坏的自定义模式 {profile_id}")
                    continue
                if profile.get("id") != profile_id or profile.get("builtin") is not False:
                    issues.append(f"已规范化自定义模式 {profile_id}")
                profile["id"] = profile_id
                profile["builtin"] = False
                custom_profiles[profile_id] = profile
        normalized["custom_profiles"] = custom_profiles

        projects: dict[str, Any] = {}
        raw_projects = loaded.get("projects", {})
        if not isinstance(raw_projects, dict):
            issues.append("项目列表类型无效")
        else:
            available_profiles = set(BUILTIN_PROFILES) | set(custom_profiles)
            for project_id, raw_project in raw_projects.items():
                if (
                    not isinstance(project_id, str)
                    or not project_id.strip()
                    or not isinstance(raw_project, dict)
                ):
                    issues.append("已忽略无效的项目记录")
                    continue
                project = copy.deepcopy(raw_project)
                root = project.get("root")
                if not isinstance(root, str) or not root.strip():
                    issues.append(f"已忽略缺少照片目录的项目 {project_id}")
                    continue
                cache = project.get("cache_root")
                if not isinstance(cache, str) or not cache.strip():
                    project["cache_root"] = normalized["default_cache_root"]
                    issues.append(f"已修复项目 {project_id} 的缓存位置")
                profile_id = project.get("profile_id", "conservative")
                if not isinstance(profile_id, str) or profile_id not in available_profiles:
                    project["profile_id"] = "conservative"
                    issues.append(f"已修复项目 {project_id} 的筛选模式")
                old_caches = project.get("old_caches")
                if old_caches is not None:
                    if not isinstance(old_caches, list):
                        project.pop("old_caches", None)
                        issues.append(f"已修复项目 {project_id} 的旧缓存列表")
                    else:
                        cleaned_caches = [
                            item for item in old_caches
                            if isinstance(item, str) and item.strip()
                        ]
                        if cleaned_caches != old_caches:
                            project["old_caches"] = cleaned_caches
                            issues.append(f"已修复项目 {project_id} 的旧缓存列表")
                projects[project_id] = project
        normalized["projects"] = projects

        raw_recent = loaded.get("recent_projects", [])
        if not isinstance(raw_recent, list):
            issues.append("最近项目列表类型无效")
            raw_recent = []
        recent: list[str] = []
        for item in raw_recent:
            if isinstance(item, str) and item in projects and item not in recent:
                recent.append(item)
            else:
                issues.append("已清理无效的最近项目记录")
        normalized["recent_projects"] = recent[:12]
        if len(recent) > 12:
            issues.append("最近项目列表已限制为 12 项")

        return normalized, list(dict.fromkeys(issues))

    def _read_candidate(
        self, source: Path
    ) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            return None, [f"无法读取配置文件：{error}"]
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as error:
            return None, [f"配置 JSON 损坏：{error}"]
        if not isinstance(loaded, dict):
            return None, ["配置文件顶层必须是 JSON 对象"]
        return self._normalize(loaded)

    def _load(self) -> tuple[dict[str, Any], bool]:
        defaults = self._defaults()
        temp = self._temp_path()
        if not self.path.exists():
            if not temp.is_file():
                return defaults, False
            recovered, issues = self._read_candidate(temp)
            if recovered is None:
                backup = self._backup_damaged(temp)
                detail = "、".join(issues)
                if backup:
                    self._append_load_warning(
                        f"残留的临时配置损坏（{detail}），已备份到 {backup}"
                    )
                return defaults, backup is not None
            if issues:
                backup = self._backup_damaged(temp)
                if backup is None:
                    return recovered, False
                self._append_load_warning(
                    f"临时配置含有异常内容（{'、'.join(issues)}），原文件已备份到 {backup}"
                )
            else:
                self._append_load_warning("检测到上次未完成的配置写入，已从临时文件恢复")
            return recovered, True

        loaded, issues = self._read_candidate(self.path)
        if loaded is not None and not issues:
            return loaded, False

        backup = self._backup_damaged(self.path)
        if loaded is not None:
            if backup:
                self._append_load_warning(
                    f"配置中含有异常内容（{'、'.join(issues)}），原文件已备份到 {backup}"
                )
            return loaded, backup is not None

        detail = "、".join(issues)
        if backup:
            self._append_load_warning(f"配置文件损坏（{detail}），原文件已备份到 {backup}")
        if temp.is_file():
            recovered, temp_issues = self._read_candidate(temp)
            if recovered is not None:
                if temp_issues:
                    temp_backup = self._backup_damaged(temp)
                    if temp_backup is None:
                        return recovered, False
                    self._append_load_warning(
                        f"临时配置也含有异常内容（{'、'.join(temp_issues)}），已备份到 {temp_backup}"
                    )
                self._append_load_warning("已从上次残留的临时文件恢复可用配置")
                return recovered, backup is not None
            temp_backup = self._backup_damaged(temp)
            if temp_backup:
                self._append_load_warning(
                    f"残留的临时配置也已损坏（{'、'.join(temp_issues)}），已备份到 {temp_backup}"
                )
            else:
                return defaults, False
        return defaults, backup is not None

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)

    @contextmanager
    def edit(self):
        """Serialize a config mutation and restore memory if persistence fails."""
        with self.lock:
            previous = copy.deepcopy(self.data)
            try:
                yield self.data
                self.save()
            except Exception:
                self.data = previous
                raise

    def profiles(self) -> dict[str, Any]:
        with self.lock:
            profiles = copy.deepcopy(BUILTIN_PROFILES)
            profiles.update(copy.deepcopy(self.data.get("custom_profiles", {})))
            return profiles

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        profiles = self.profiles()
        return profiles.get(profile_id, profiles["conservative"])

    def save_custom_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        validate_profile(profile)
        now = datetime.now().isoformat(timespec="seconds")
        profile = copy.deepcopy(profile)
        profile["builtin"] = False
        profile["version"] = 1
        profile.setdefault("created_at", now)
        profile["updated_at"] = now
        if not profile.get("id") or profile["id"] in BUILTIN_PROFILES:
            profile["id"] = "custom-" + hashlib.sha1(f"{profile.get('name')}-{time.time()}".encode()).hexdigest()[:10]
        with self.edit() as data:
            data.setdefault("custom_profiles", {})[profile["id"]] = profile
        return profile

    def delete_custom_profile(self, profile_id: str) -> None:
        if profile_id in BUILTIN_PROFILES:
            raise ValueError("内置模式不能删除")
        with self.edit() as data:
            for project in data.get("projects", {}).values():
                if project.get("profile_id") == profile_id:
                    raise ValueError("该配置仍被项目使用，请先切换项目模式")
            data.get("custom_profiles", {}).pop(profile_id, None)


def validate_profile(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        raise ValueError("配置格式无效")
    name = str(profile.get("name", "")).strip()
    if not name or len(name) > 40:
        raise ValueError("配置名称必须为 1–40 个字符")
    q = profile.get("quality", {})
    s = profile.get("similarity", {})
    if not isinstance(q, dict) or not isinstance(s, dict):
        raise ValueError("质量或相似度配置格式无效")
    if q.get("threshold_mode", "absolute") not in {"absolute", "percentile"}:
        raise ValueError("threshold_mode 必须为 absolute 或 percentile")
    if q.get("match_mode", "any") not in {"any", "all"}:
        raise ValueError("match_mode 必须为 any 或 all")
    enabled = q.get("enabled", {})
    if not isinstance(enabled, dict) or any(not isinstance(value, bool) for value in enabled.values()):
        raise ValueError("质量检测开关必须为布尔值")
    for key in ("exact_duplicates", "allow_cross_time_high_confidence", "face_safe"):
        if key in s and not isinstance(s[key], bool):
            raise ValueError(f"{key} 必须为布尔值")

    def number(container: dict[str, Any], key: str, default: Any = None) -> float:
        if key not in container and default is None:
            raise ValueError(f"缺少配置项 {key}")
        try:
            value = float(container.get(key, default))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} 必须是数字") from error
        if not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数字")
        return value

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
        value = number(q, key)
        if not low <= value <= high:
            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
    for key, default in (("blur_review_percentile", 5), ("blur_remove_percentile", 1)):
        value = number(q, key, default)
        if not 0 <= value <= 100:
            raise ValueError(f"{key} 超出允许范围 0–100")
    weights = q.get("weights", {})
    if not isinstance(weights, dict):
        raise ValueError("评分权重格式无效")
    weight_keys = ("sharpness", "exposure", "contrast", "entropy", "resolution")
    for key in weight_keys:
        if not 0 <= number(weights, key) <= 10:
            raise ValueError(f"{key} 评分权重超出允许范围 0–10")
    if sum(number(weights, key) for key in weight_keys) <= 0:
        raise ValueError("评分权重不能全部为零")
    sim_ranges = {
        "phash_max": (0, 64), "dhash_max": (0, 64), "structure_min": (-1, 1),
        "aspect_tolerance": (0, 1), "time_window_minutes": (0, 10080),
        "sequence_gap": (0, 10000), "min_group_size": (2, 1000),
    }
    for key, (low, high) in sim_ranges.items():
        value = number(s, key)
        if not low <= value <= high:
            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
    if number(q, "blur_remove") > number(q, "blur_review"):
        raise ValueError("移除清晰度阈值不能高于复看阈值")
    if number(q, "dark_remove") > number(q, "dark_review"):
        raise ValueError("严重欠曝阈值不能高于偏暗阈值")
    for review_key, remove_key, label in (
        ("dark_clip_review", "dark_clip_remove", "暗部溢出"),
        ("bright_clip_review", "bright_clip_remove", "高光溢出"),
    ):
        if number(q, review_key) > number(q, remove_key):
            raise ValueError(f"{label}复看阈值不能高于移除阈值")
    for remove_key, review_key, label in (
        ("contrast_remove", "contrast_review", "对比度"),
        ("entropy_remove", "entropy_review", "细节"),
        ("min_megapixels_remove", "min_megapixels_review", "分辨率"),
        ("min_size_kb_remove", "min_size_kb_review", "文件大小"),
    ):
        if number(q, remove_key) > number(q, review_key):
            raise ValueError(f"{label}移除阈值不能高于复看阈值")
    if number(q, "blur_remove_percentile", 1) > number(q, "blur_review_percentile", 5):
        raise ValueError("清晰度移除百分位不能高于复看百分位")


def project_id_for(root: Path) -> str:
    return hashlib.sha1(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


DATABASE_SCHEMA_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_photos_status_decision ON photos(status,decision);
CREATE INDEX IF NOT EXISTS idx_photos_status_error_size ON photos(status,error,size);
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


def _database_has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"""
    ).fetchone() is not None


def _backup_database_for_migration(
    conn: sqlite3.Connection, path: Path, target_version: int
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(
        f"{path.stem}.pre-v{target_version}-{stamp}-{uuid.uuid4().hex[:8]}{path.suffix}"
    )
    backup_conn: sqlite3.Connection | None = None
    try:
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.commit()
    except Exception as error:
        if backup_conn is not None:
            backup_conn.close()
            backup_conn = None
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("无法创建数据库升级备份，已停止升级") from error
    finally:
        if backup_conn is not None:
            backup_conn.close()
    return backup_path


def _initialize_database(
    conn: sqlite3.Connection, path: Path, existed: bool
) -> None:
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > DATABASE_SCHEMA_VERSION:
        raise RuntimeError(
            f"项目数据库版本 {current_version} 高于当前支持的 {DATABASE_SCHEMA_VERSION}，"
            "请使用更新版本的 Cullumi 打开"
        )
    if current_version == DATABASE_SCHEMA_VERSION:
        return
    if existed and _database_has_user_tables(conn):
        _backup_database_for_migration(conn, path, DATABASE_SCHEMA_VERSION)
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + DATABASE_SCHEMA_SQL
            + f"\nPRAGMA user_version={DATABASE_SCHEMA_VERSION};\nCOMMIT;"
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.is_file() and path.stat().st_size > 0
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _initialize_database(conn, path, existed)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


@dataclass
class Project:
    project_id: str
    root: Path
    cache_root: Path
    project_dir: Path
    db_path: Path
    thumb_dir: Path
    profile_id: str


def project_thumbnail_path(project: Project, stored_path: str | Path) -> Path:
    """Resolve relative or legacy absolute thumbnail paths in the current cache."""
    raw = str(stored_path or "").strip()
    if not raw:
        return Path()
    return project.thumb_dir / Path(raw).name


def project_thumbnail_storage_path(path: str | Path) -> str:
    """Store thumbnails relocatably while accepting legacy absolute inputs."""
    raw = str(path or "").strip()
    return (Path("thumbs") / Path(raw).name).as_posix() if raw else ""


def _rewrite_project_thumbnail_paths(project: Project) -> None:
    """Persist the new cache location in a copied project database."""
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute(
            "SELECT id,thumbnail FROM photos WHERE thumbnail<>''"
        ).fetchall()
        updates = [
            (project_thumbnail_storage_path(row["thumbnail"]), row["id"])
            for row in rows
        ]
        if updates:
            conn.executemany("UPDATE photos SET thumbnail=? WHERE id=?", updates)
            conn.commit()
        integrity = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if integrity != ["ok"]:
            raise RuntimeError("迁移后的项目数据库完整性校验失败")


_ACTIVE_DATABASE_FILES = {"project.db", "project.db-wal", "project.db-shm"}


def _is_regenerable_preview(path: Path) -> bool:
    return ".display-" in path.name or path.name.endswith(".tmp")


def _migration_file_manifest(root: Path) -> dict[Path, int]:
    manifest: dict[Path, int] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if len(relative.parts) == 1 and relative.name in _ACTIVE_DATABASE_FILES:
            continue
        if _is_regenerable_preview(relative):
            continue
        manifest[relative] = path.stat().st_size
    return manifest


def _copy_project_cache_without_live_database(source: Path, target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> list[str]:
        relative_dir = Path(directory).relative_to(source)
        ignored = [name for name in names if _is_regenerable_preview(Path(name))]
        if not relative_dir.parts:
            ignored.extend(name for name in names if name in _ACTIVE_DATABASE_FILES)
        return list(dict.fromkeys(ignored))

    shutil.copytree(source, target, ignore=ignore)


def _backup_project_database(source: Path, target: Path) -> None:
    with closing(connect_db(source)) as source_conn:
        with closing(sqlite3.connect(target)) as target_conn:
            source_conn.backup(target_conn)
            target_conn.commit()


class ProjectManager:
    def __init__(self, config: ConfigStore):
        self.config = config
        self._data_locks_guard = threading.Lock()
        self._data_locks: dict[str, threading.RLock] = {}

    def _data_lock(self, project_id: str) -> threading.RLock:
        with self._data_locks_guard:
            return self._data_locks.setdefault(project_id, threading.RLock())

    @contextmanager
    def data_operation(self, project_id: str):
        """Serialize writes that must not cross a project cache migration."""
        with self._data_lock(project_id):
            yield

    def open(self, root: str, cache_root: str | None = None) -> Project:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise ValueError("照片文件夹不存在")
        pid = project_id_for(root_path)
        config_data = self.config.snapshot()
        existing = config_data.get("projects", {}).get(pid, {})
        default_cache_root = config_data["default_cache_root"]
        cache = Path(cache_root or existing.get("cache_root") or default_cache_root).resolve()
        project_dir = cache / pid
        overlaps_photos = _is_within(project_dir, root_path) or _is_within(root_path, project_dir)
        existing_cache = Path(existing["cache_root"]).resolve() if existing.get("cache_root") else None
        if overlaps_photos and existing_cache != cache:
            raise ValueError("缓存位置不能与照片文件夹重叠")
        project_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = project_dir / "thumbs"
        thumb_dir.mkdir(exist_ok=True)
        profile_id = existing.get("profile_id", "conservative")
        project = Project(pid, root_path, cache, project_dir, project_dir / "project.db", thumb_dir, profile_id)
        now = datetime.now().isoformat(timespec="seconds")
        with closing(connect_db(project.db_path)) as conn:
            conn.execute(
                """INSERT INTO project(id,root,profile_id,created_at,updated_at) VALUES(1,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET root=excluded.root,profile_id=excluded.profile_id,updated_at=excluded.updated_at""",
                (str(root_path), profile_id, now, now),
            )
            conn.commit()
        with self.config.edit() as data:
            stored = data.setdefault("projects", {}).setdefault(pid, {})
            stored.update({
                "root": str(root_path), "cache_root": str(cache), "profile_id": profile_id,
                "last_opened": now,
            })
            recent = [pid] + [x for x in data.get("recent_projects", []) if x != pid]
            data["recent_projects"] = recent[:12]
        return project

    def from_id(self, project_id: str) -> Project:
        config_data = self.config.snapshot()
        data = config_data.get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        root = Path(data["root"]).resolve()
        if not root.is_dir():
            raise ValueError("照片文件夹当前不可用")
        if project_id_for(root) != project_id:
            raise ValueError("项目标识与照片目录不匹配")
        cache_root = Path(
            data.get("cache_root") or config_data["default_cache_root"]
        ).resolve()
        project_dir = cache_root / project_id
        return Project(
            project_id,
            root,
            cache_root,
            project_dir,
            project_dir / "project.db",
            project_dir / "thumbs",
            data.get("profile_id", "conservative"),
        )

    def project_root(self, project_id: str) -> Path:
        data = self.config.snapshot().get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        root = Path(data["root"]).resolve()
        if not root.is_dir():
            raise ValueError("照片文件夹当前不可用")
        return root

    def remove_from_recent(self, project_id: str, delete_cache: bool = False) -> dict[str, Any]:
        with self.config.lock:
            data = copy.deepcopy(self.config.data.get("projects", {}).get(project_id))
        if not data:
            raise ValueError("项目不存在")
        deleted_paths: list[str] = []
        if delete_cache:
            root = Path(data["root"]).resolve()
            if project_id_for(root) != project_id:
                raise ValueError("项目标识与照片目录不匹配，已停止删除")
            cache_root = Path(data["cache_root"]).resolve()
            targets = [(cache_root / project_id).resolve()]
            targets.extend(Path(item).resolve() for item in data.get("old_caches", []))
            unique_targets = list(dict.fromkeys(targets))
            for target in unique_targets:
                if target.name.casefold() != project_id.casefold():
                    raise ValueError("项目缓存目录校验失败，已停止删除")
            for target in unique_targets:
                if target.exists():
                    shutil.rmtree(target)
                    deleted_paths.append(str(target))
        with self.config.edit() as config_data:
            config_data["recent_projects"] = [
                item for item in config_data.get("recent_projects", []) if item != project_id
            ]
            if delete_cache:
                config_data.get("projects", {}).pop(project_id, None)
        return {"removed": True, "cache_deleted": delete_cache, "deleted_paths": deleted_paths}

    def migrate_cache(self, project_id: str, new_root: str) -> dict[str, Any]:
        with self.data_operation(project_id):
            return self._migrate_cache_locked(project_id, new_root)

    def _migrate_cache_locked(self, project_id: str, new_root: str) -> dict[str, Any]:
        project = self.from_id(project_id)
        new_cache = Path(new_root).resolve()
        new_dir = new_cache / project_id
        if new_dir == project.project_dir:
            return {
                "changed": False,
                "path": str(new_dir),
                "cache_root": str(new_cache),
            }
        if _is_within(new_dir, project.root) or _is_within(project.root, new_dir):
            raise ValueError("新缓存位置不能与照片文件夹重叠")
        if new_dir.exists() and any(new_dir.iterdir()):
            raise ValueError("新位置已有同名项目缓存")
        temp = new_dir.with_name(new_dir.name + ".migrating")
        if temp.exists():
            shutil.rmtree(temp)
        temp.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_manifest = _migration_file_manifest(project.project_dir)
            _copy_project_cache_without_live_database(project.project_dir, temp)
            _backup_project_database(project.db_path, temp / "project.db")
            copied_manifest = _migration_file_manifest(temp)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        if source_manifest != copied_manifest:
            shutil.rmtree(temp, ignore_errors=True)
            raise RuntimeError("缓存迁移文件校验失败，原位置保持不变")
        migrated_project = Project(
            project.project_id,
            project.root,
            new_cache,
            new_dir,
            temp / "project.db",
            new_dir / "thumbs",
            project.profile_id,
        )
        destination_was_empty = new_dir.exists()
        installed = False
        try:
            _rewrite_project_thumbnail_paths(migrated_project)
            if new_dir.exists():
                new_dir.rmdir()
            temp.rename(new_dir)
            installed = True
            with self.config.edit() as data:
                stored = data["projects"][project_id]
                stored["cache_root"] = str(new_cache)
                old_caches = stored.setdefault("old_caches", [])
                if str(project.project_dir) not in old_caches:
                    old_caches.append(str(project.project_dir))
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            if installed:
                shutil.rmtree(new_dir, ignore_errors=True)
                if destination_was_empty:
                    new_dir.mkdir(parents=True, exist_ok=True)
            raise
        return {
            "changed": True,
            "path": str(new_dir),
            "cache_root": str(new_cache),
            "old_cache": str(project.project_dir),
        }

    def cleanup_old_cache(self, project_id: str, path: str) -> dict[str, Any]:
        with self.config.lock:
            data = copy.deepcopy(self.config.data.get("projects", {}).get(project_id))
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
        with self.config.edit() as config_data:
            stored = config_data["projects"][project_id]
            stored["old_caches"] = [
                item for item in stored.get("old_caches", [])
                if Path(item).resolve() != target
            ]
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


class Scanner:
    def __init__(
        self,
        config: ConfigStore,
        manager: ProjectManager,
        similarity_groups: SimilarityGroupCache | None = None,
    ):
        self.config = config
        self.manager = manager
        self.similarity_groups = similarity_groups
        self.progress: dict[str, dict[str, Any]] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._operation_locks: dict[str, threading.Lock] = {}

    def _operation_lock(self, project_id: str) -> threading.Lock:
        with self._lock:
            return self._operation_locks.setdefault(project_id, threading.Lock())

    @contextmanager
    def project_operation(self, project_id: str, label: str = "执行该操作"):
        operation_lock = self._operation_lock(project_id)
        if not operation_lock.acquire(blocking=False):
            raise ValueError(f"项目正在执行其他任务，暂时无法{label}")
        try:
            yield
        finally:
            operation_lock.release()

    def start(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self.threads and self.threads[project_id].is_alive():
                return False
            operation_lock = self._operation_locks.setdefault(project_id, threading.Lock())
            if not operation_lock.acquire(blocking=False):
                return False
            if self.similarity_groups:
                self.similarity_groups.invalidate(project_id)
            cancel = threading.Event()
            self.cancel_events[project_id] = cancel
            self.progress[project_id] = {
                "stage": "starting", "current": 0, "total": 0, "done": False, "error": ""
            }
            thread = threading.Thread(
                target=self._run_locked,
                args=(project_id, cancel, operation_lock),
                daemon=True,
            )
            self.threads[project_id] = thread
            try:
                thread.start()
            except Exception:
                operation_lock.release()
                raise
            return True

    def _run_locked(
        self,
        project_id: str,
        cancel: threading.Event,
        operation_lock: threading.Lock,
    ) -> None:
        try:
            self._run(project_id, cancel)
        finally:
            operation_lock.release()

    def cancel(self, project_id: str) -> None:
        with self._lock:
            cancel = self.cancel_events.get(project_id)
            if cancel:
                cancel.set()

    def _set(self, project_id: str, **values: Any) -> None:
        with self._lock:
            self.progress.setdefault(project_id, {}).update(values)

    def get_progress(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.progress.get(project_id, {"stage": "idle", "done": True}))

    def _discover(self, project: Project, cancel: threading.Event) -> list[Path]:
        stored = self.config.snapshot().get("projects", {}).get(project.project_id, {})
        excluded = [project.root / QUARANTINE_DIR, project.project_dir]
        excluded.extend(Path(item) for item in stored.get("old_caches", []))
        excluded = [path.resolve() for path in excluded if _is_within(path, project.root)]
        project_root = project.root.resolve()

        def is_excluded(path: Path) -> bool:
            resolved = path.resolve()
            return any(resolved == root or root in resolved.parents for root in excluded)

        discovered: list[Path] = []
        for current, directories, filenames in os.walk(project.root, followlinks=False):
            _check_cancelled(cancel)
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not is_excluded(current_path / name)
            ]
            for name in filenames:
                path = current_path / name
                resolved = path.resolve()
                if path.is_file() and project_root in resolved.parents:
                    discovered.append(path)
        return discovered

    def _run(self, project_id: str, cancel: threading.Event) -> None:
        try:
            project = self.manager.from_id(project_id)
            profile = self.config.get_profile(project.profile_id)
            self._set(project_id, stage="discovering")
            discovered = self._discover(project, cancel)
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
                unavailable_count=0,
            )
            with closing(connect_db(project.db_path)) as conn:
                existing = {row["relative_path"]: row for row in conn.execute("SELECT * FROM photos")}
                seen: set[str] = set()
                unavailable_count = 0
                for index, path in enumerate(files, 1):
                    _check_cancelled(cancel)
                    rel = path.relative_to(project.root).as_posix()
                    try:
                        stat = path.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        unavailable_count += 1
                        self._set(
                            project_id,
                            current=index,
                            file=rel,
                            unavailable_count=unavailable_count,
                        )
                        continue
                    except OSError:
                        stat = None
                        unavailable_count += 1
                    seen.add(rel)
                    old = existing.get(rel)
                    thumbnail = (
                        project_thumbnail_path(project, old["thumbnail"])
                        if old and old["thumbnail"]
                        else None
                    )
                    if (
                        old
                        and stat is not None
                        and old["size"] == stat.st_size
                        and abs(old["mtime"] - stat.st_mtime) < 0.001
                        and not old["error"]
                        and thumbnail is not None
                        and thumbnail.is_file()
                    ):
                        if old["status"] != "active":
                            conn.execute("UPDATE photos SET status='active' WHERE id=?", (old["id"],))
                        self._set(project_id, current=index)
                        continue
                    thumb_name = hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".jpg"
                    metrics = analyze_photo(path, project.thumb_dir / thumb_name, stat)
                    if not path.is_file():
                        seen.discard(rel)
                        unavailable_count += 1
                        self._set(
                            project_id,
                            current=index,
                            file=rel,
                            unavailable_count=unavailable_count,
                        )
                        continue
                    suggestion, reason = classify(metrics, profile)
                    if metrics.get("thumbnail"):
                        metrics["thumbnail"] = project_thumbnail_storage_path(
                            metrics["thumbnail"]
                        )
                    values = {
                        "relative_path": rel, **metrics, "suggestion": suggestion, "reason": reason,
                        "status": "active",
                        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    conn.execute(
                        PHOTO_UPSERT_SQL,
                        [values[column] for column in PHOTO_ANALYSIS_COLUMNS],
                    )
                    if index % 20 == 0:
                        conn.commit()
                    self._set(
                        project_id,
                        current=index,
                        file=rel,
                        unavailable_count=unavailable_count,
                    )
                missing = [
                    (rel,)
                    for rel, row in existing.items()
                    if rel not in seen and row["status"] == "active"
                ]
                conn.executemany(
                    "UPDATE photos SET status='missing' WHERE relative_path=?",
                    missing,
                )
                conn.commit()
                _check_cancelled(cancel)
                self._set(project_id, stage="hashing")
                unavailable_count += self._exact_hashes(project, conn, cancel)
                self._set(project_id, unavailable_count=unavailable_count)
                _check_cancelled(cancel)
                self._set(project_id, stage="grouping")
                self.rebuild_similarity(project, conn, profile, cancel)
                _check_cancelled(cancel)
                self.reclassify(project, conn, profile, cancel)
            _check_cancelled(cancel)
            self._auto_import_csv(project)
            self._set(
                project_id,
                stage="complete",
                done=True,
                current=len(files),
                total=len(files),
                unavailable_count=unavailable_count,
            )
        except ScanCancelled:
            self._set(project_id, stage="cancelled", done=True)
        except Exception as error:
            self._set(project_id, stage="error", done=True, error=str(error))
        finally:
            if self.similarity_groups:
                self.similarity_groups.invalidate(project_id)

    def _exact_hashes(
        self,
        project: Project,
        conn: sqlite3.Connection,
        cancel: threading.Event,
    ) -> int:
        sizes = conn.execute(
            "SELECT size,COUNT(*) c FROM photos WHERE status='active' AND error='' GROUP BY size HAVING c>1"
        ).fetchall()
        updates: list[tuple[str, int]] = []
        missing_ids: list[tuple[int]] = []
        unavailable = 0
        for size_row in sizes:
            for row in conn.execute("SELECT id,relative_path,sha256 FROM photos WHERE status='active' AND size=?", (size_row["size"],)):
                _check_cancelled(cancel)
                try:
                    path = safe_relative_path(project.root, row["relative_path"])
                except ValueError:
                    missing_ids.append((int(row["id"]),))
                    unavailable += 1
                    continue
                if not path.is_file():
                    missing_ids.append((int(row["id"]),))
                    unavailable += 1
                    continue
                if row["sha256"]:
                    continue
                digest = hashlib.sha256()
                try:
                    with path.open("rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            _check_cancelled(cancel)
                            digest.update(block)
                except (FileNotFoundError, NotADirectoryError):
                    missing_ids.append((int(row["id"]),))
                    unavailable += 1
                    continue
                except OSError:
                    unavailable += 1
                    continue
                updates.append((digest.hexdigest(), int(row["id"])))
        conn.executemany("UPDATE photos SET sha256=? WHERE id=?", updates)
        conn.executemany(
            "UPDATE photos SET status='missing',sha256='' WHERE id=?",
            missing_ids,
        )
        conn.commit()
        return unavailable

    def reclassify(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None = None,
        commit: bool = True,
    ) -> None:
        rows = conn.execute("SELECT * FROM photos WHERE status='active'").fetchall()
        percentiles = classification_percentiles(rows, profile)
        updates: list[tuple[str, str, int]] = []
        for row in rows:
            _check_cancelled(cancel)
            suggestion, reason = classify(row, profile, percentiles)
            updates.append((suggestion, reason, int(row["id"])))
        conn.executemany(
            "UPDATE photos SET suggestion=?,reason=? WHERE id=?",
            updates,
        )
        if commit:
            conn.commit()

    def rebuild_similarity(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None = None,
        commit: bool = True,
    ) -> None:
        _check_cancelled(cancel)
        rows = conn.execute("SELECT * FROM photos WHERE status='active' AND error=''").fetchall()
        by_dir: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_dir.setdefault(str(Path(row["relative_path"]).parent).casefold(), []).append(row)
        sim = profile["similarity"]
        derived = {
            int(row["id"]): {
                "sequence": filename_sequence(Path(row["relative_path"]).name),
                "taken": parse_taken(row["taken"]),
                "aspect": row["width"] / max(1, row["height"]),
            }
            for row in rows
        }
        quality_scores: dict[int, float] = {}
        structure_vectors: dict[int, tuple[np.ndarray, float] | None] = {}

        def row_quality(row: sqlite3.Row) -> float:
            photo_id = int(row["id"])
            if photo_id not in quality_scores:
                quality_scores[photo_id] = quality_score(row, profile)
            return quality_scores[photo_id]

        def row_structure(row: sqlite3.Row) -> tuple[np.ndarray, float] | None:
            photo_id = int(row["id"])
            if photo_id not in structure_vectors:
                structure_vectors[photo_id] = _structure_vector(
                    project_thumbnail_path(project, row["thumbnail"])
                )
            return structure_vectors[photo_id]

        pairs: list[tuple[int, int, float, str, int, int]] = []
        seen: set[tuple[int, int]] = set()
        if sim.get("exact_duplicates", True):
            by_hash: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                _check_cancelled(cancel)
                if row["sha256"]:
                    by_hash.setdefault(row["sha256"], []).append(row)
            for group in by_hash.values():
                _check_cancelled(cancel)
                if len(group) < 2:
                    continue
                best = max(group, key=row_quality)
                for row in group:
                    if row["id"] == best["id"]:
                        continue
                    key = tuple(sorted((best["id"], row["id"])))
                    seen.add(key)
                    pairs.append((key[0], key[1], 1.0, "exact", best["id"], 0))

        def compare_pair(a: sqlite3.Row, b: sqlite3.Row) -> None:
            aspect_a = derived[int(a["id"])]["aspect"]
            aspect_b = derived[int(b["id"])]["aspect"]
            if abs(aspect_a - aspect_b) / max(aspect_a, aspect_b) > sim["aspect_tolerance"]:
                return
            ph = hamming(a["phash"], b["phash"])
            dh = hamming(a["dhash"], b["dhash"])
            if ph > sim["phash_max"] or dh > sim["dhash_max"]:
                return
            key = tuple(sorted((a["id"], b["id"])))
            if key in seen:
                return
            structure = _structure_similarity(row_structure(a), row_structure(b))
            if structure < sim["structure_min"]:
                return
            recommended = a if row_quality(a) >= row_quality(b) else b
            score = 0.45 * (1 - ph / 64) + 0.25 * (1 - dh / 64) + 0.30 * structure
            pairs.append((key[0], key[1], score, "similar", recommended["id"], int(sim.get("face_safe", True))))
            seen.add(key)

        for group in by_dir.values():
            structure_vectors.clear()
            group.sort(
                key=lambda row: (
                    derived[int(row["id"])]["sequence"], row["relative_path"]
                )
            )
            if sim.get("allow_cross_time_high_confidence"):
                index_field = (
                    "phash"
                    if sim["phash_max"] <= sim["dhash_max"]
                    else "dhash"
                )
                radius = sim[f"{index_field}_max"]
                hashes = [str(row[index_field] or "") for row in group]
                for left, right in hamming_candidate_pairs(hashes, radius):
                    _check_cancelled(cancel)
                    compare_pair(group[left], group[right])
                continue
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    _check_cancelled(cancel)
                    derived_a = derived[int(a["id"])]
                    derived_b = derived[int(b["id"])]
                    seq_a = derived_a["sequence"]
                    seq_b = derived_b["sequence"]
                    seq_gap = abs(seq_b - seq_a) if seq_a >= 0 and seq_b >= 0 else 999999
                    ta, tb = derived_a["taken"], derived_b["taken"]
                    time_gap = abs(ta - tb) / 60 if ta is not None and tb is not None else None
                    nearby = seq_gap <= sim["sequence_gap"] or (time_gap is not None and time_gap <= sim["time_window_minutes"])
                    if not nearby and not sim.get("allow_cross_time_high_confidence"):
                        if seq_gap > sim["sequence_gap"]:
                            break
                        continue
                    compare_pair(a, b)
        _check_cancelled(cancel)
        def replace_pairs() -> None:
            conn.execute("DELETE FROM similar_pairs")
            conn.executemany(
                "INSERT OR IGNORE INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe) VALUES(?,?,?,?,?,?)",
                pairs,
            )
        if commit:
            with conn:
                replace_pairs()
        else:
            replace_pairs()

    def _auto_import_csv(self, project: Project) -> None:
        with closing(connect_db(project.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM photos WHERE decision<>''").fetchone()[0]
        if count:
            return
        candidates = [project.root / "照片筛选结果.csv", project.root.parent / f"{project.root.name}筛选结果.csv"]
        for candidate in candidates:
            if candidate.exists():
                import_decisions(project, candidate)
                break


def import_decisions(project: Project, csv_path: Path) -> dict[str, int]:
    imported = missing = 0
    with closing(connect_db(project.db_path)) as conn:
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
    return {"imported": imported, "missing": missing}


def export_decisions(project: Project) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["决定", "路径", "建议", "原因"])
    with closing(connect_db(project.db_path)) as conn:
        for row in conn.execute("SELECT decision,relative_path,suggestion,reason FROM photos WHERE decision<>'' ORDER BY relative_path"):
            writer.writerow([row["decision"], row["relative_path"], row["suggestion"], row["reason"]])
    return "\ufeff" + output.getvalue()


def clear_decisions(project: Project) -> int:
    with closing(connect_db(project.db_path)) as conn:
        cursor = conn.execute(
            "UPDATE photos SET decision='' WHERE status='active' AND decision<>''"
        )
        conn.commit()
        return cursor.rowcount


def mark_ai_remove_suggestions(project: Project) -> int:
    """Mark only active, readable, undecided AI-remove suggestions for removal."""
    with closing(connect_db(project.db_path)) as conn:
        cursor = conn.execute(
            """UPDATE photos SET decision='remove'
               WHERE status='active' AND COALESCE(error,'')=''
                 AND suggestion='remove' AND decision=''"""
        )
        conn.commit()
        return cursor.rowcount


def quarantine_preview(project: Project) -> dict[str, Any]:
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute(
            "SELECT id,relative_path,size,mtime FROM photos WHERE decision='remove' AND status='active' ORDER BY relative_path"
        ).fetchall()
    return {
        "count": len(rows),
        "total_size": sum(int(row["size"] or 0) for row in rows),
        "items": [dict(row) for row in rows],
    }


def _write_manifest_csv(batch_root: Path, manifest: list[dict[str, Any]]) -> None:
    with (batch_root / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "photo_id", "relative_path", "quarantine_path", "restore_path", "status", "size", "error"
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in manifest])


def _quarantine_batch_root(project: Project, batch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", batch_id):
        raise ValueError("隔离批次标识无效")
    quarantine_root = safe_relative_path(project.root, QUARANTINE_DIR, "隔离目录")
    return safe_relative_path(quarantine_root, batch_id, "隔离批次路径")


def apply_quarantine(project: Project) -> dict[str, Any]:
    preview = quarantine_preview(project)
    batch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    batch_root = _quarantine_batch_root(project, batch_id)
    manifest: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, Any], dict[str, Any], Path, Path]] = []
    for item in preview["items"]:
        source = safe_relative_path(project.root, item["relative_path"], "照片路径")
        entry: dict[str, Any] = {
            "photo_id": int(item["id"]),
            "relative_path": item["relative_path"],
            "status": "pending",
            "size": int(item["size"] or 0),
        }
        if not source.exists():
            entry["status"] = "missing"
            manifest.append(entry)
            continue
        stat = source.stat()
        if stat.st_size != item["size"] or abs(stat.st_mtime - item["mtime"]) > 0.01:
            entry["status"] = "changed"
            manifest.append(entry)
            continue
        destination = safe_relative_path(batch_root, item["relative_path"], "隔离目标路径")
        entry["quarantine_path"] = destination.relative_to(project.root.resolve()).as_posix()
        manifest.append(entry)
        prepared.append((entry, item, source, destination))

    batch_root.mkdir(parents=True, exist_ok=False)
    manifest_path = batch_root / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    _write_manifest_csv(batch_root, manifest)
    with closing(connect_db(project.db_path)) as conn:
        conn.execute(
            "INSERT INTO quarantine_batches(id,created_at,manifest_path,count,total_size) VALUES(?,?,?,?,?)",
            (batch_id, datetime.now().isoformat(timespec="seconds"), str(manifest_path), 0, 0),
        )
        conn.commit()
        for entry, item, source, destination in prepared:
            if not source.exists():
                entry["status"] = "missing"
                _atomic_write_json(manifest_path, manifest)
                continue
            stat = source.stat()
            if stat.st_size != item["size"] or abs(stat.st_mtime - item["mtime"]) > 0.01:
                entry["status"] = "changed"
                _atomic_write_json(manifest_path, manifest)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(source), str(destination))
            except Exception as error:
                if not source.exists() and destination.exists():
                    entry["status"] = "moved"
                else:
                    entry["status"] = "error"
                    entry["error"] = str(error)
                    _atomic_write_json(manifest_path, manifest)
                    continue
            else:
                entry["status"] = "moved"
            _atomic_write_json(manifest_path, manifest)
            conn.execute("UPDATE photos SET status='quarantined' WHERE id=?", (entry["photo_id"],))
            moved = [row for row in manifest if row["status"] == "moved"]
            conn.execute(
                "UPDATE quarantine_batches SET count=?,total_size=? WHERE id=?",
                (len(moved), sum(int(row.get("size") or 0) for row in moved), batch_id),
            )
            conn.commit()
    _write_manifest_csv(batch_root, manifest)
    moved = [row for row in manifest if row["status"] == "moved"]
    return {"batch_id": batch_id, "moved": len(moved), "skipped": len(manifest) - len(moved)}


def restore_batch(project: Project, batch_id: str) -> dict[str, Any]:
    batch_root = _quarantine_batch_root(project, batch_id)
    with closing(connect_db(project.db_path)) as conn:
        batch = conn.execute("SELECT * FROM quarantine_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("隔离批次不存在")
        raw_manifest_path = Path(batch["manifest_path"])
        manifest_path = (
            raw_manifest_path.resolve()
            if raw_manifest_path.is_absolute()
            else safe_relative_path(project.root, str(raw_manifest_path), "清单路径")
        )
        if manifest_path.name != "manifest.json" or not _is_within(manifest_path, batch_root):
            raise ValueError("隔离清单路径无效")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, list):
            raise ValueError("隔离清单格式无效")

        paths: dict[int, tuple[Path | None, Path, Path | None]] = {}
        for index, item in enumerate(manifest):
            if not isinstance(item, dict):
                raise ValueError("隔离清单格式无效")
            destination = safe_relative_path(project.root, item.get("relative_path", ""), "恢复目标路径")
            source = None
            if item.get("quarantine_path"):
                source = safe_relative_path(project.root, item["quarantine_path"], "隔离文件路径")
                if not _is_within(source, batch_root):
                    raise ValueError("隔离文件路径超出当前批次")
            restore_path = None
            if item.get("restore_path"):
                restore_path = safe_relative_path(project.root, item["restore_path"], "已恢复文件路径")
            paths[index] = (source, destination, restore_path)

        restored = conflicts = missing = 0
        for index, item in enumerate(manifest):
            status = item.get("status")
            source, destination, recorded_restore = paths[index]
            if status == "restored":
                if recorded_restore and recorded_restore.exists():
                    target_rel = recorded_restore.relative_to(project.root.resolve()).as_posix()
                    if item.get("photo_id"):
                        conn.execute(
                            "UPDATE photos SET status='active',relative_path=? WHERE id=?",
                            (target_rel, int(item["photo_id"])),
                        )
                    conn.commit()
                continue
            if status == "restoring" and recorded_restore and source is not None:
                if not source.exists() and recorded_restore.exists():
                    destination = recorded_restore
                    item["status"] = "restored"
                    _atomic_write_json(manifest_path, manifest)
                elif source.exists():
                    destination = recorded_restore
                else:
                    missing += 1
                    continue
            elif status not in {"moved", "pending"}:
                continue
            if item.get("status") != "restored":
                if source is None or not source.exists():
                    missing += 1
                    continue
                if destination.exists():
                    suffix = f".restored-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
                    destination = destination.with_name(destination.stem + suffix + destination.suffix)
                    conflicts += 1
                destination.parent.mkdir(parents=True, exist_ok=True)
                item["status"] = "restoring"
                item["restore_path"] = destination.relative_to(project.root.resolve()).as_posix()
                _atomic_write_json(manifest_path, manifest)
                shutil.move(str(source), str(destination))
                item["status"] = "restored"
                item.pop("error", None)
                _atomic_write_json(manifest_path, manifest)
                restored += 1
            target_rel = destination.relative_to(project.root.resolve()).as_posix()
            if item.get("photo_id"):
                conn.execute(
                    "UPDATE photos SET status='active',relative_path=? WHERE id=?",
                    (target_rel, int(item["photo_id"])),
                )
            else:
                conn.execute(
                    "UPDATE photos SET status='active',relative_path=? WHERE relative_path=?",
                    (target_rel, item["relative_path"]),
                )
            conn.commit()

        remaining = False
        for index, item in enumerate(manifest):
            source = paths[index][0]
            if item.get("status") in {"moved", "pending", "restoring"} and source and source.exists():
                remaining = True
                break
        if not remaining:
            conn.execute(
                "UPDATE quarantine_batches SET restored_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), batch_id),
            )
            conn.commit()
    _write_manifest_csv(batch_root, manifest)
    return {"restored": restored, "conflicts": conflicts, "missing": missing}
