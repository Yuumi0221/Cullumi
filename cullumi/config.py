from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME = "Cullumi"

BLINK_DEFAULTS = {
    "face_confidence_min": 0.85,
    "open_confidence_min": 0.80,
    "closed_confidence_min": 0.80,
    "min_eye_distance_px": 12,
    "reliable_coverage_min": 0.80,
}


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Fill optional profile sections introduced after profile version 1."""
    normalized = copy.deepcopy(profile)
    similarity = normalized.setdefault("similarity", {})
    if not isinstance(similarity, dict):
        return normalized
    blink = similarity.get("blink")
    if not isinstance(blink, dict):
        blink = {}
    similarity["blink"] = {**BLINK_DEFAULTS, **blink}
    return normalized


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
            "blink": copy.deepcopy(BLINK_DEFAULTS),
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
            "blink_detection_enabled": True,
            "motion_cover_writeback": "ask",
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

        for key in (
            "auto_advance",
            "auto_check_updates",
            "blink_detection_enabled",
        ):
            if key in loaded and not isinstance(loaded[key], bool):
                normalized[key] = defaults[key]
                issues.append(f"{key} 类型无效")

        writeback = loaded.get(
            "motion_cover_writeback", defaults["motion_cover_writeback"]
        )
        if isinstance(writeback, str) and writeback in {"never", "ask", "always"}:
            normalized["motion_cover_writeback"] = writeback
        else:
            normalized["motion_cover_writeback"] = defaults["motion_cover_writeback"]
            issues.append("动态照片封面修改设置无效")

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
                profile = normalize_profile(raw_profile)
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
        profile = normalize_profile(profile)
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
    if not isinstance(s, dict):
        raise ValueError("相似照片配置格式无效")
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

    blink = s.get("blink", {})
    if not isinstance(blink, dict):
        raise ValueError("眨眼检测配置格式无效")

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

    blink_ranges = {
        "face_confidence_min": (0.5, 0.99),
        "open_confidence_min": (0.5, 0.99),
        "closed_confidence_min": (0.5, 0.99),
        "min_eye_distance_px": (4, 64),
        "reliable_coverage_min": (0.5, 1.0),
    }
    blink_values: dict[str, float] = {}
    for key, (low, high) in blink_ranges.items():
        value = number(blink, key)
        if not low <= value <= high:
            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
        blink_values[key] = value
    eye_distance = blink["min_eye_distance_px"]
    if type(eye_distance) is not int:
        raise ValueError("min_eye_distance_px 必须是整数")
    if (
        blink_values["open_confidence_min"]
        + blink_values["closed_confidence_min"]
        <= 1
    ):
        raise ValueError("睁眼与闭眼置信度之和必须大于 1")
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

