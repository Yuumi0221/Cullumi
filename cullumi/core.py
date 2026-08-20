from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

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
from .project_store import (
    DATABASE_SCHEMA_VERSION,
    Project,
    ProjectManager,
    _is_within,
    connect_db,
    project_id_for,
    project_thumbnail_path,
    project_thumbnail_storage_path,
    safe_relative_path,
)
from .media import (
    DISPLAY_PREVIEW_EXTENSIONS,
    DISPLAY_PREVIEW_MAX_SIZE,
    HEIF_EXTENSIONS,
    IMAGE_EXTENSIONS,
    RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    analyze_photo,
    display_preview_path,
    ensure_display_preview,
    open_heif,
    open_image,
)
from .workflows import (
    QUARANTINE_DIR,
    apply_quarantine,
    clear_decisions,
    export_decisions,
    import_decisions,
    mark_ai_remove_suggestions,
    quarantine_preview,
    restore_batch,
)

APP_NAME = "Cullumi"
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
class ScanCancelled(Exception):
    """Internal control flow used to stop every scan stage consistently."""

def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled

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
