from __future__ import annotations

import sqlite3
from typing import Any, Iterable

import numpy as np

PHOTO_DECISION_FILTERS = frozenset({"undecided", "keep", "remove"})
PHOTO_AI_FILTERS = frozenset({"remove", "review", "no_suggestion"})
PHOTO_ANALYSIS_COLUMNS = (
    "relative_path", "extension", "size", "mtime", "width", "height",
    "megapixels", "taken", "luminance", "contrast", "dark_clip",
    "bright_clip", "sharpness", "entropy", "phash", "dhash", "sha256",
    "thumbnail", "error", "suggestion", "reason", "status", "analyzed_at",
    "media_type", "motion_kind", "motion_relative_path", "motion_offset",
    "motion_length", "motion_size", "motion_mtime", "motion_asset_id",
    "motion_error", "motion_duration_ms", "motion_fps", "motion_frame_count",
    "motion_width", "motion_height", "motion_sha256", "motion_still_time_ms",
    "cover_source",
    "cover_time_ms", "cover_frame_index", "cover_revision", "quality_score",
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
    return project_photo_counts(conn)["library_counts"]


def project_photo_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build every project/sidebar photo count with a single aggregate scan."""
    readable = "status='active' AND COALESCE(error,'')='' AND suggestion<>'unreadable'"
    unreadable = "status='active' AND (COALESCE(error,'')<>'' OR suggestion='unreadable')"
    row = conn.execute(
        f"""SELECT
              SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) total,
              SUM(CASE WHEN status='active' AND suggestion='remove' THEN 1 ELSE 0 END) suggestion_remove,
              SUM(CASE WHEN status='active' AND suggestion='review' THEN 1 ELSE 0 END) suggestion_review,
              SUM(CASE WHEN status='active' AND suggestion='keep' THEN 1 ELSE 0 END) suggestion_keep,
              SUM(CASE WHEN status='active' AND suggestion='unreadable' THEN 1 ELSE 0 END) suggestion_unreadable,
              SUM(CASE WHEN status='active' AND decision='keep' THEN 1 ELSE 0 END) decision_keep,
              SUM(CASE WHEN status='active' AND decision='remove' THEN 1 ELSE 0 END) decision_remove,
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
    library_counts = {
        key: int(row[key] or 0)
        for key in (
            "readable", "undecided", "keep", "remove", "ai_pending",
            "ai_remove_pending", "unreadable",
        )
    }
    counts = {
        name: int(row[f"suggestion_{name}"] or 0)
        for name in ("remove", "review", "keep", "unreadable")
        if int(row[f"suggestion_{name}"] or 0)
    }
    decisions = {
        name: int(row[f"decision_{name}"] or 0)
        for name in ("keep", "remove")
        if int(row[f"decision_{name}"] or 0)
    }
    return {
        "total": int(row["total"] or 0),
        "library_counts": library_counts,
        "counts": counts,
        "decisions": decisions,
    }

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
    cover_source = (
        row["cover_source"]
        if hasattr(row, "keys") and "cover_source" in row.keys()
        else "still"
    )
    if cover_source != "motion":
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
