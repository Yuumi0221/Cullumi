from __future__ import annotations

import hashlib
import math
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .analysis_refresh import changed_photo_plan, execute_refresh
from .classification import classify, project_photo_counts
from .config import ConfigStore, profile_niqe_enabled
from .motion import (
    ensure_motion_video,
    extract_motion_asset_frame,
    extract_motion_frame,
    locate_motion_still_time,
    motion_asset_from_row,
    motion_fingerprint,
    restore_motion_source,
    write_motion_cover_source,
)
from .project_store import (
    ProjectManager,
    connect_db,
    project_thumbnail_storage_path,
    safe_relative_path,
)
from .scanner import Scanner
from .similarity import SimilarityGroupCache, quality_score


@dataclass(frozen=True)
class MotionCoverUpdate:
    row: Any
    profile: dict[str, Any]
    project_counts: dict[str, Any]
    source_written: bool
    source_backup: str


def locate_motion_cover(
    manager: ProjectManager,
    scanner: Scanner,
    project_id: str,
    photo_id: int,
) -> int:
    project = manager.from_id(project_id)
    with manager.data_operation(project_id), scanner.project_operation(
        project_id, "定位动态照片封面"
    ):
        with closing(connect_db(project.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE id=? AND status='active'", (photo_id,)
            ).fetchone()
            if not row or row["media_type"] != "motion_photo":
                raise ValueError("动态照片不存在")
            still_time_ms = int(row["motion_still_time_ms"] or 0)
            if still_time_ms >= 0:
                return still_time_ms
            photo = safe_relative_path(
                project.root, row["relative_path"], "照片路径"
            )
            asset = motion_asset_from_row(project.root, row)
            still_time_ms = locate_motion_still_time(
                photo,
                asset,
                int(row["motion_duration_ms"] or 0),
                float(row["motion_fps"] or 0),
            )
            conn.execute(
                "UPDATE photos SET motion_still_time_ms=? WHERE id=?",
                (still_time_ms, photo_id),
            )
            conn.commit()
    return still_time_ms


def recommend_motion_cover(
    config: ConfigStore,
    manager: ProjectManager,
    scanner: Scanner,
    project_id: str,
    photo_id: int,
) -> dict[str, Any]:
    project = manager.from_id(project_id)
    profile = config.get_profile(project.profile_id)
    with closing(connect_db(project.db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM photos WHERE id=? AND status='active'", (photo_id,)
        ).fetchone()
    if not row or row["media_type"] != "motion_photo":
        raise ValueError("动态照片不存在")
    duration = int(row["motion_duration_ms"] or 0)
    if duration <= 0:
        raise ValueError("动态照片时长无效")
    asset = motion_asset_from_row(project.root, row)
    motion_dir = project.motion_dir or project.project_dir / "motion"
    video = ensure_motion_video(asset, motion_dir)
    count = min(15, max(2, int(row["motion_frame_count"] or 2)))
    times = {
        round(index * max(0, duration - 1) / (count - 1))
        for index in range(count)
    }
    if row["cover_source"] == "motion":
        times.add(int(row["cover_time_ms"] or 0))
    candidates = _motion_cover_candidates(
        scanner, video, sorted(times), row, profile
    )
    recommended = max(
        candidates,
        key=lambda item: (item["quality_score"], -item["time_ms"]),
    )
    return {"recommended": recommended, "candidates": candidates}


def _motion_cover_candidates(
    scanner: Scanner,
    video: Path,
    times: list[int],
    row: Any,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as temporary:
        temp_root = Path(temporary)
        for index, time_ms in enumerate(times):
            frame = temp_root / f"frame-{index}.jpg"
            thumb = temp_root / f"thumb-{index}.jpg"
            extract_motion_frame(video, time_ms, frame)
            metrics = scanner.analyze_photo(
                frame, thumb, niqe_enabled=profile_niqe_enabled(profile)
            )
            metrics.update({"cover_source": "motion", "size": row["size"]})
            score = round(
                max(0.0, min(1.0, quality_score(metrics, profile))) * 100, 1
            )
            candidates.append(
                {
                    "time_ms": time_ms,
                    "frame_index": round(
                        time_ms * float(row["motion_fps"] or 0) / 1000
                    ),
                    "quality_score": score,
                }
            )
    return candidates


MOTION_COVER_METRIC_COLUMNS = (
    "niqe_score", "niqe_error", "niqe_version",
    "width", "height", "megapixels", "luminance", "contrast",
    "dark_clip", "bright_clip", "sharpness", "entropy", "phash",
    "dhash", "thumbnail", "suggestion", "reason", "quality_score",
    "size", "mtime", "sha256", "motion_offset", "motion_length",
    "motion_size", "motion_mtime", "motion_still_time_ms",
    "cover_source", "cover_time_ms", "cover_frame_index",
)


@dataclass(frozen=True)
class _PreparedCover:
    metrics: dict[str, Any]
    thumbnail: Path
    selected_time: int
    frame_index: int
    frame: Path | None
    asset: Any | None
    original: Path | None


def _prepare_cover(
    scanner: Scanner,
    project: Any,
    row: Any,
    source: str,
    time_ms: int,
    write_source: bool,
    revision: int,
    profile: dict[str, Any],
) -> _PreparedCover:
    thumb_name = (
        f"{hashlib.sha1(str(row['relative_path']).encode('utf-8')).hexdigest()}"
        f".cover-{revision}.jpg"
    )
    thumbnail = project.thumb_dir / thumb_name
    selected_time = frame_index = 0
    frame = asset = original = None
    if source == "motion":
        duration = int(row["motion_duration_ms"] or 0)
        if duration <= 0 or time_ms < 0 or time_ms > duration:
            raise ValueError("所选封面时间超出动态照片范围")
        fps = float(row["motion_fps"] or 0)
        frame_duration_ms = max(1, math.ceil(1000 / fps)) if fps > 0 else 1
        selected_time = min(time_ms, max(0, duration - frame_duration_ms))
        asset = motion_asset_from_row(project.root, row)
        motion_dir = project.motion_dir or project.project_dir / "motion"
        video = ensure_motion_video(asset, motion_dir)
        frame = motion_dir / (
            f"{motion_fingerprint(asset)}.motion-cover-{selected_time}.jpg"
        )
        if write_source:
            extract_motion_asset_frame(asset, selected_time, frame)
        else:
            extract_motion_frame(video, selected_time, frame)
        metrics = scanner.analyze_photo(
            frame, thumbnail, niqe_enabled=profile_niqe_enabled(profile)
        )
        frame_index = round(selected_time * fps / 1000)
    else:
        original = safe_relative_path(project.root, row["relative_path"], "照片路径")
        metrics = scanner.analyze_photo(
            original, thumbnail, niqe_enabled=profile_niqe_enabled(profile)
        )
    if metrics["error"]:
        thumbnail.unlink(missing_ok=True)
        raise RuntimeError(metrics["error"])
    return _PreparedCover(
        metrics, thumbnail, selected_time, frame_index, frame, asset, original
    )


def _write_prepared_cover_source(
    project: Any,
    row: Any,
    prepared: _PreparedCover,
    write_source: bool,
    revision: int,
) -> tuple[dict[str, Any] | None, Path | None]:
    if not write_source:
        return None, prepared.original
    original = safe_relative_path(project.root, row["relative_path"], "照片路径")
    assert prepared.frame is not None and prepared.asset is not None
    result = write_motion_cover_source(
        original,
        prepared.frame,
        prepared.asset,
        prepared.selected_time,
        project.project_dir / "source-backups",
        revision,
    )
    return result, original


def _cover_values(
    project: Any,
    row: Any,
    profile: dict[str, Any],
    source: str,
    prepared: _PreparedCover,
    writeback: dict[str, Any] | None,
) -> dict[str, Any]:
    values = dict(prepared.metrics)
    values.update({
        "extension": row["extension"],
        "size": row["size"],
        "mtime": row["mtime"],
        "taken": row["taken"],
        "sha256": row["sha256"],
        "motion_offset": row["motion_offset"],
        "motion_length": row["motion_length"],
        "motion_size": row["motion_size"],
        "motion_mtime": row["motion_mtime"],
        "motion_still_time_ms": row["motion_still_time_ms"],
        "cover_source": "still" if writeback else source,
        "cover_time_ms": 0 if writeback else prepared.selected_time,
        "cover_frame_index": 0 if writeback else prepared.frame_index,
        "thumbnail": project_thumbnail_storage_path(prepared.thumbnail),
        "quality_score": 0,
    })
    if writeback:
        source_stat = writeback["stat"]
        asset_values = writeback["asset"].storage_values(project.root)
        values.update({
            "size": source_stat.st_size,
            "mtime": source_stat.st_mtime,
            "sha256": "",
            "motion_offset": asset_values["motion_offset"],
            "motion_length": asset_values["motion_length"],
            "motion_size": asset_values["motion_size"],
            "motion_mtime": asset_values["motion_mtime"],
            "motion_still_time_ms": prepared.selected_time,
        })
    values["quality_score"] = round(
        max(0.0, min(1.0, quality_score(values, profile))) * 100, 1
    )
    values["suggestion"], values["reason"] = classify(values, profile)
    return values


def _restore_failed_cover(
    prepared: _PreparedCover,
    writeback: dict[str, Any] | None,
    original: Path | None,
) -> None:
    prepared.thumbnail.unlink(missing_ok=True)
    if writeback and original is not None:
        restore_motion_source(writeback["backup"], original)


def _persist_cover(
    scanner: Scanner,
    project: Any,
    conn: Any,
    profile: dict[str, Any],
    photo_id: int,
    revision: int,
    values: dict[str, Any],
) -> None:
    assignments = ",".join(
        f"{column}=?" for column in MOTION_COVER_METRIC_COLUMNS
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        f"UPDATE photos SET {assignments},cover_revision=?,analyzed_at=? WHERE id=?",
        [
            *[values[column] for column in MOTION_COVER_METRIC_COLUMNS],
            revision,
            datetime.now().isoformat(timespec="microseconds"),
            photo_id,
        ],
    )
    execute_refresh(
        scanner,
        project,
        conn,
        profile,
        changed_photo_plan(photo_id),
    )
    conn.commit()


def update_motion_cover(
    config: ConfigStore,
    manager: ProjectManager,
    scanner: Scanner,
    similarity_groups: SimilarityGroupCache,
    project_id: str,
    photo_id: int,
    source: str,
    time_ms: int,
    write_source: bool,
) -> MotionCoverUpdate:
    if source not in {"still", "motion"}:
        raise ValueError("封面来源无效")
    if write_source and source != "motion":
        raise ValueError("只有动态帧可以修改原图")
    project = manager.from_id(project_id)
    profile = config.get_profile(project.profile_id)
    with manager.data_operation(project_id), scanner.project_operation(
        project_id, "修改动态照片封面"
    ):
        with closing(connect_db(project.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE id=? AND status='active'", (photo_id,)
            ).fetchone()
            if not row or row["media_type"] != "motion_photo":
                raise ValueError("动态照片不存在")
            revision = int(row["cover_revision"] or 0) + 1
            prepared = _prepare_cover(
                scanner,
                project,
                row,
                source,
                time_ms,
                write_source,
                revision,
                profile,
            )
            writeback = None
            original = prepared.original
            try:
                writeback, original = _write_prepared_cover_source(
                    project, row, prepared, write_source, revision
                )
                values = _cover_values(
                    project, row, profile, source, prepared, writeback
                )
                _persist_cover(
                    scanner, project, conn, profile, photo_id, revision, values
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                _restore_failed_cover(prepared, writeback, original)
                raise
            updated = conn.execute(
                "SELECT * FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            counts = project_photo_counts(conn)
    similarity_groups.invalidate(project_id)
    return MotionCoverUpdate(
        row=updated,
        profile=profile,
        project_counts=counts,
        source_written=bool(writeback),
        source_backup=(str(writeback["backup"]) if writeback else ""),
    )

__all__ = [
    "MotionCoverUpdate",
    "locate_motion_cover",
    "recommend_motion_cover",
    "update_motion_cover",
]
