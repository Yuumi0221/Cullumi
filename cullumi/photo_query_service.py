from __future__ import annotations

from contextlib import closing
from typing import Any

from .capture_variants import (
    FORMAT_CATEGORY_IDS,
    active_variant_rows,
    format_category,
    variant_metadata,
    variant_metadata_from_rows,
)
from .classification import (
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    parse_photo_filter,
    photo_filter_where,
)
from .config import ConfigStore
from .project_store import ProjectManager, connect_db
from .similarity import SimilarityGroupCache, quality_score

PHOTO_SORT_EXPRESSIONS = {
    "suggestion": """CASE suggestion
        WHEN 'remove' THEN 0 WHEN 'review' THEN 1
        WHEN 'unreadable' THEN 2 ELSE 3 END""",
    "filename": """LOWER(SUBSTR(
        relative_path,
        LENGTH(RTRIM(relative_path, REPLACE(relative_path, '/', ''))) + 1
      ))""",
    "size": "COALESCE(size, 0)",
    "taken": "taken",
}
PHOTO_SORT_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
SIMILAR_GROUP_PAGE_LIMIT = 500


def photo_sort_order(sort: str, direction: str = "asc") -> str:
    try:
        expression = PHOTO_SORT_EXPRESSIONS[sort]
    except KeyError as error:
        raise ValueError("sort 必须是 suggestion、filename、size 或 taken") from error
    try:
        sql_direction = PHOTO_SORT_DIRECTIONS[direction]
    except KeyError as error:
        raise ValueError("direction 必须是 asc 或 desc") from error
    empty_taken = (
        "CASE WHEN COALESCE(taken, '')='' THEN 1 ELSE 0 END, "
        if sort == "taken"
        else ""
    )
    return (
        f"{empty_taken}{expression} {sql_direction}, "
        "relative_path COLLATE NOCASE, id"
    )


def expanded_similarity_members(
    group: dict[str, Any], variants: dict[int, list[Any]]
) -> list[tuple[Any, int]]:
    members = list(group["members"])
    if group["kind"] != "similar":
        return [(row, int(row["id"])) for row in members]
    recommended_id = int(group["recommended_id"])
    owners: dict[int, int] = {}
    priority = sorted(
        members,
        key=lambda row: (int(row["id"]) != recommended_id),
    )
    for source in priority:
        source_id = int(source["id"])
        for row in variants.get(source_id) or [source]:
            owners.setdefault(int(row["id"]), source_id)

    expanded: list[tuple[Any, int]] = []
    emitted: set[int] = set()
    for source in members:
        source_id = int(source["id"])
        for row in variants.get(source_id) or [source]:
            photo_id = int(row["id"])
            if owners.get(photo_id) != source_id or photo_id in emitted:
                continue
            emitted.add(photo_id)
            expanded.append((row, source_id))
    return expanded


def _matching_similarity_photo_ids(
    conn: Any, search: str
) -> set[int]:
    matching: set[int] = set()
    for row in conn.execute(
        """SELECT p.id,cv.representative_id
             FROM photos p
             LEFT JOIN capture_variant_members cv ON cv.photo_id=p.id
            WHERE p.status='active'
              AND INSTR(CASEFOLD(p.relative_path),?)>0""",
        (search.casefold(),),
    ):
        matching.add(int(row["id"]))
        if row["representative_id"] is not None:
            matching.add(int(row["representative_id"]))
    return matching


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
        variant_extensions: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        data = {key: row[key] for key in row.keys()}
        data.pop("capture_representative_id", None)
        blink_ratio = data.get("blink_closed_ratio", -1)
        if blink_ratio is None or float(blink_ratio) < 0:
            data["blink_closed_ratio"] = None
        data["project_id"] = project_id
        data["format_category"] = format_category(
            data.get("extension"), data.get("relative_path")
        )
        data["variant_extensions"] = list(variant_extensions)
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
        order_by = photo_sort_order(
            query.get("sort", ["suggestion"])[0],
            query.get("direction", ["asc"])[0],
        )
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
        formats = parse_photo_filter(
            query.get("formats", [None])[0],
            FORMAT_CATEGORY_IDS,
            "formats",
        )
        where, params = photo_filter_where(
            query.get("file", ["readable"])[0], decisions, ai_states, formats
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
                    ORDER BY {order_by} LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
            variants = variant_metadata(conn, (int(row["id"]) for row in rows))
        return {
            "total": total,
            "items": [
                self.photo_payload(
                    project_id,
                    row,
                    profile,
                    variants.get(int(row["id"]), []),
                )
                for row in rows
            ],
        }

    def similar_groups(self, query: dict[str, list[str]]) -> dict[str, Any]:
        project_id = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0].strip()
        raw_limit = query.get("limit", [None])[0]
        limit = (
            None
            if raw_limit is None
            else min(SIMILAR_GROUP_PAGE_LIMIT, max(1, int(raw_limit)))
        )
        offset = max(0, int(query.get("offset", ["0"])[0]))
        project = self.manager.from_id(project_id)
        profile = self.config.get_profile(project.profile_id)
        blink_enabled = bool(
            self.config.snapshot().get("blink_detection_enabled", True)
        )
        with closing(connect_db(project.db_path)) as conn:
            participant_ids = (
                _matching_similarity_photo_ids(conn, search)
                if search
                else None
            )
            total, groups = self.similarity_groups.get_page(
                project_id,
                conn,
                profile,
                blink_enabled,
                offset=offset,
                limit=limit,
                participant_ids=participant_ids,
            )
            variant_rows = active_variant_rows(
                conn,
                (
                    int(row["id"])
                    for group in groups
                    for row in group["members"]
                ),
            )
            variants = variant_metadata_from_rows(variant_rows)
        items = []
        for group in groups:
            expanded = expanded_similarity_members(group, variant_rows)
            items.append({
                "id": group["id"],
                "count": len(expanded),
                "capture_count": len(group["members"]),
                "kind": group["kind"],
                "recommended_id": group["recommended_id"],
                "recommended": self.photo_payload(
                    project_id,
                    group["recommended"],
                    profile,
                    variants.get(int(group["recommended"]["id"]), []),
                ),
                "covers": [
                    self.photo_payload(
                        project_id,
                        row,
                        profile,
                        variants.get(int(row["id"]), []),
                    )
                    for row in group["covers"]
                ],
                "face_safe": group["face_safe"],
            })
        return {"total": total, "items": items}

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
            if group:
                variant_rows = active_variant_rows(
                    conn, (int(row["id"]) for row in group["members"])
                )
                expanded = expanded_similarity_members(group, variant_rows)
                variants = variant_metadata_from_rows(variant_rows)
            else:
                expanded = []
                variants = {}
        if not group:
            raise ValueError("相似照片组不存在或已发生变化")
        members = []
        for row, source_id in expanded:
            if search and search not in str(row["relative_path"]).casefold():
                continue
            item = self.photo_payload(
                project_id,
                row,
                profile,
                variants.get(int(row["id"]), []),
            )
            item["group_similarity"] = group["confidence"].get(
                source_id, 0.0
            )
            item["similarity_source_id"] = source_id
            item["is_capture_variant"] = int(row["id"]) != source_id
            members.append(item)
        return {
            "id": group["id"],
            "count": len(expanded),
            "capture_count": len(group["members"]),
            "kind": group["kind"],
            "recommended_id": group["recommended_id"],
            "face_safe": group["face_safe"],
            "members": members,
        }


__all__ = ["PhotoQueryService", "photo_sort_order"]
