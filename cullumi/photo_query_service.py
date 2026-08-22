from __future__ import annotations

from contextlib import closing
from typing import Any

from .classification import (
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    parse_photo_filter,
    photo_filter_where,
)
from .config import ConfigStore
from .project_store import ProjectManager, connect_db
from .similarity import SimilarityGroupCache, quality_score


class PhotoQueryService:
    """Read and serialize library and similarity-group photos."""

    def __init__(
        self,
        config: ConfigStore,
        manager: ProjectManager,
        similarity_groups: SimilarityGroupCache,
        token: str,
    ) -> None:
        self.config = config
        self.manager = manager
        self.similarity_groups = similarity_groups
        self.token = token

    def photo_payload(
        self,
        project_id: str,
        row: Any,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = {key: row[key] for key in row.keys()}
        blink_ratio = data.get("blink_closed_ratio", -1)
        if blink_ratio is None or float(blink_ratio) < 0:
            data["blink_closed_ratio"] = None
        data["project_id"] = project_id
        if not str(row["error"] or ""):
            if profile is None:
                project = self.manager.from_id(project_id)
                profile = self.config.get_profile(project.profile_id)
            data["quality_score"] = round(
                max(0.0, min(1.0, quality_score(row, profile))) * 100,
                1,
            )
        else:
            data["quality_score"] = None
        revision = int(row["cover_revision"] or 0)
        suffix = f"&v={revision}"
        data["thumb_url"] = (
            f"/api/thumb?project_id={project_id}&id={row['id']}"
            f"&token={self.token}{suffix}"
        )
        data["photo_url"] = (
            f"/api/photo?project_id={project_id}&id={row['id']}"
            f"&token={self.token}{suffix}"
        )
        if row["media_type"] == "motion_photo":
            data["motion"] = {
                "kind": row["motion_kind"],
                "duration_ms": int(row["motion_duration_ms"] or 0),
                "fps": float(row["motion_fps"] or 0),
                "frame_count": int(row["motion_frame_count"] or 0),
                "width": int(row["motion_width"] or 0),
                "height": int(row["motion_height"] or 0),
                "still_time_ms": int(row["motion_still_time_ms"] or 0),
                "cover_source": row["cover_source"],
                "cover_time_ms": int(row["cover_time_ms"] or 0),
                "cover_frame_index": int(row["cover_frame_index"] or 0),
                "error": row["motion_error"],
                "video_url": (
                    f"/api/motion/video?project_id={project_id}&id={row['id']}"
                    f"&token={self.token}{suffix}"
                ),
            }
        return data

    def photos(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_id = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0]
        limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
        offset = max(0, int(query.get("offset", ["0"])[0]))
        project = self.manager.from_id(project_id)
        profile = self.config.get_profile(project.profile_id)
        decisions = parse_photo_filter(
            query.get("decisions", [None])[0],
            PHOTO_DECISION_FILTERS,
            "decisions",
        )
        ai_states = parse_photo_filter(
            query.get("ai_states", [None])[0],
            PHOTO_AI_FILTERS,
            "ai_states",
        )
        where, params = photo_filter_where(
            query.get("file", ["readable"])[0], decisions, ai_states
        )
        if search:
            where += " AND relative_path LIKE ?"
            params.append(f"%{search}%")
        with closing(connect_db(project.db_path)) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM photos WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT * FROM photos WHERE {where}
                    ORDER BY CASE suggestion
                        WHEN 'remove' THEN 0 WHEN 'review' THEN 1
                        WHEN 'unreadable' THEN 2 ELSE 3 END,
                    relative_path LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "items": [
                self.photo_payload(project_id, row, profile) for row in rows
            ],
        }

    def similar_groups(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_id = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = self.manager.from_id(project_id)
        profile = self.config.get_profile(project.profile_id)
        blink_enabled = bool(
            self.config.snapshot().get("blink_detection_enabled", True)
        )
        with closing(connect_db(project.db_path)) as conn:
            groups = self.similarity_groups.get(
                project_id, conn, profile, blink_enabled
            )
        items = []
        for group in groups:
            if search and not any(
                search in str(row["relative_path"]).casefold()
                for row in group["members"]
            ):
                continue
            items.append({
                "id": group["id"],
                "count": len(group["members"]),
                "kind": group["kind"],
                "recommended_id": group["recommended_id"],
                "recommended": self.photo_payload(
                    project_id, group["recommended"], profile
                ),
                "covers": [
                    self.photo_payload(project_id, row, profile)
                    for row in group["covers"]
                ],
                "face_safe": group["face_safe"],
            })
        return {"total": len(items), "items": items}

    def similar_group(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_id = query.get("project_id", [""])[0]
        group_id = query.get("group_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = self.manager.from_id(project_id)
        profile = self.config.get_profile(project.profile_id)
        blink_enabled = bool(
            self.config.snapshot().get("blink_detection_enabled", True)
        )
        with closing(connect_db(project.db_path)) as conn:
            group = self.similarity_groups.get_one(
                project_id, group_id, conn, profile, blink_enabled
            )
        if not group:
            raise ValueError("相似照片组不存在或已发生变化")
        members = []
        for row in group["members"]:
            if search and search not in str(row["relative_path"]).casefold():
                continue
            item = self.photo_payload(project_id, row, profile)
            item["group_similarity"] = group["confidence"].get(
                int(row["id"]), 0.0
            )
            members.append(item)
        return {
            "id": group["id"],
            "count": len(group["members"]),
            "kind": group["kind"],
            "recommended_id": group["recommended_id"],
            "face_safe": group["face_safe"],
            "members": members,
        }


__all__ = ["PhotoQueryService"]
