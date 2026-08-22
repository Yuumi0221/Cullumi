from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .classification import classification_percentiles, classify
from .config import ConfigStore, validate_profile
from .project_store import Project, ProjectManager, connect_db
from .scanner import Scanner
from .similarity import SimilarityGroupCache, build_similarity_groups


def save_settings(config: ConfigStore, body: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a settings update as one configuration transaction."""
    updates: dict[str, Any] = {}
    if "theme" in body:
        theme = str(body["theme"])
        if theme not in {"day", "night"}:
            raise ValueError("主题必须为 day 或 night")
        updates["theme"] = theme
    for key in (
        "auto_advance",
        "auto_check_updates",
        "blink_detection_enabled",
    ):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} 必须为布尔值")
            updates[key] = body[key]
    if "motion_cover_writeback" in body:
        writeback = str(body["motion_cover_writeback"])
        if writeback not in {"never", "ask", "always"}:
            raise ValueError("动态照片封面修改设置无效")
        updates["motion_cover_writeback"] = writeback

    cache_path = None
    if "default_cache_root" in body:
        raw_path = body["default_cache_root"]
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("默认缓存位置不能为空")
        cache_path = Path(raw_path).resolve()
        updates["default_cache_root"] = str(cache_path)

    if cache_path:
        cache_path.mkdir(parents=True, exist_ok=True)
    with config.edit() as data:
        data.update(updates)
    return config.snapshot()


def apply_profile(
    config: ConfigStore,
    manager: ProjectManager,
    scanner: Scanner,
    similarity_groups: SimilarityGroupCache,
    project_id: str,
    profile_id: str,
) -> Project:
    """Apply a profile while keeping the database and configuration in sync."""
    project = manager.from_id(project_id)
    profiles = config.profiles()
    if profile_id not in profiles:
        raise ValueError("筛选模式不存在")
    profile = profiles[profile_id]
    with scanner.project_operation(project.project_id, "应用筛选模式"):
        with manager.data_operation(project.project_id):
            with config.lock:
                old_profile_id = config.data["projects"][project.project_id].get(
                    "profile_id", "conservative"
                )
            with closing(connect_db(project.db_path)) as conn:
                config_saved = False
                try:
                    conn.execute(
                        "UPDATE project SET profile_id=?,updated_at=datetime('now') "
                        "WHERE id=1",
                        (profile_id,),
                    )
                    scanner.reclassify(project, conn, profile, commit=False)
                    scanner.rebuild_similarity(project, conn, profile, commit=False)
                    scanner.analyze_blinks(project, conn, profile, commit=False)
                    with config.edit() as data:
                        data["projects"][project.project_id]["profile_id"] = profile_id
                    config_saved = True
                    conn.commit()
                except Exception:
                    conn.rollback()
                    if config_saved:
                        with config.edit() as data:
                            data["projects"][project.project_id][
                                "profile_id"
                            ] = old_profile_id
                    raise
    similarity_groups.invalidate(project.project_id)
    return manager.from_id(project.project_id)


def estimate_profile(
    manager: ProjectManager,
    scanner: Scanner,
    project_id: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Estimate classification and similarity results without mutating a project."""
    validate_profile(profile)
    project = manager.from_id(project_id)
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute("SELECT * FROM photos WHERE status='active'").fetchall()
        percentiles = classification_percentiles(rows, profile)
        counts = {"remove": 0, "review": 0, "keep": 0, "unreadable": 0}
        for row in rows:
            suggestion, _ = classify(row, profile, percentiles)
            counts[suggestion] = counts.get(suggestion, 0) + 1
        with closing(sqlite3.connect(":memory:")) as estimate_conn:
            estimate_conn.row_factory = sqlite3.Row
            conn.backup(estimate_conn)
            scanner.rebuild_similarity(project, estimate_conn, profile)
            estimated_pairs = estimate_conn.execute(
                "SELECT COUNT(*) FROM similar_pairs"
            ).fetchone()[0]
            estimated_groups = len(build_similarity_groups(estimate_conn, profile))
    return {
        "counts": counts,
        "estimated_pairs": estimated_pairs,
        "estimated_groups": estimated_groups,
    }
