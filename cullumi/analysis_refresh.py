from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project_store import Project
    from .scanner import Scanner


@dataclass(frozen=True)
class AnalysisRefreshPlan:
    reclassify: bool = False
    rebuild_relationships: bool = False
    analyze_blinks: bool = False
    photo_ids: frozenset[int] | None = None


def _relationship_settings(profile: dict[str, Any]) -> dict[str, Any]:
    values = dict(profile.get("similarity", {}))
    values.pop("blink", None)
    return values


def plan_profile_change(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> AnalysisRefreshPlan:
    quality_changed = any(
        previous.get(key) != current.get(key)
        for key in ("quality", "people_conservative")
    )
    relationships_changed = _relationship_settings(previous) != _relationship_settings(
        current
    )
    blink_changed = (
        previous.get("similarity", {}).get("blink")
        != current.get("similarity", {}).get("blink")
    )
    return AnalysisRefreshPlan(
        reclassify=quality_changed,
        rebuild_relationships=relationships_changed,
        analyze_blinks=relationships_changed or blink_changed,
    )


def execute_refresh(
    scanner: Scanner,
    project: Project,
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    plan: AnalysisRefreshPlan,
) -> None:
    photo_ids = set(plan.photo_ids) if plan.photo_ids is not None else None
    if plan.reclassify:
        scanner.reclassify(
            project, conn, profile, commit=False, photo_ids=photo_ids
        )
    if plan.rebuild_relationships:
        scanner.rebuild_similarity(
            project, conn, profile, commit=False, photo_ids=photo_ids
        )
    if plan.analyze_blinks:
        blink_ids = photo_ids
        if photo_ids is not None and plan.rebuild_relationships:
            blink_ids = scanner.related_photo_ids(conn, photo_ids)
        scanner.analyze_blinks(
            project, conn, profile, commit=False, photo_ids=blink_ids
        )


def changed_photo_plan(photo_id: int) -> AnalysisRefreshPlan:
    return AnalysisRefreshPlan(
        reclassify=True,
        rebuild_relationships=True,
        analyze_blinks=True,
        photo_ids=frozenset({photo_id}),
    )


__all__ = [
    "AnalysisRefreshPlan",
    "changed_photo_plan",
    "execute_refresh",
    "plan_profile_change",
]
