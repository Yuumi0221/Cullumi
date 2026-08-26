from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .capture_variants import rebuild_capture_variants
from .fs_utils import atomic_write_json, is_within
from .project_store import Project, connect_db, safe_relative_path

QUARANTINE_DIR = "_照片筛选隔离"

def quarantine_preview(project: Project) -> dict[str, Any]:
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute(
            """SELECT id,relative_path,size,mtime,media_type,motion_kind,
                      motion_relative_path,motion_size,motion_mtime
                 FROM photos WHERE decision='remove' AND status='active'
                 ORDER BY relative_path"""
        ).fetchall()
    items = [dict(row) for row in rows]
    return {
        "count": len(items),
        "total_size": sum(
            int(item["size"] or 0)
            + (
                int(item["motion_size"] or 0)
                if item["motion_kind"] == "apple_sidecar"
                and item["motion_relative_path"] != item["relative_path"]
                else 0
            )
            for item in items
        ),
        "items": items,
    }


def _write_manifest_csv(batch_root: Path, manifest: list[dict[str, Any]]) -> None:
    with (batch_root / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "photo_id", "relative_path", "quarantine_path", "restore_path",
            "companion_relative_path", "companion_quarantine_path",
            "companion_restore_path", "status", "size", "error"
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in manifest])


def _quarantine_batch_root(project: Project, batch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", batch_id):
        raise ValueError("隔离批次标识无效")
    quarantine_root = safe_relative_path(project.root, QUARANTINE_DIR, "隔离目录")
    return safe_relative_path(quarantine_root, batch_id, "隔离批次路径")


@dataclass(frozen=True)
class _PreparedQuarantine:
    entry: dict[str, Any]
    item: dict[str, Any]
    moves: list[tuple[Path, Path]]


def _prepare_quarantine_item(
    project: Project, batch_root: Path, item: dict[str, Any]
) -> tuple[dict[str, Any], _PreparedQuarantine | None]:
    source = safe_relative_path(project.root, item["relative_path"], "照片路径")
    entry: dict[str, Any] = {
        "photo_id": int(item["id"]),
        "relative_path": item["relative_path"],
        "status": "pending",
        "size": int(item["size"] or 0)
        + (
            int(item["motion_size"] or 0)
            if item["motion_kind"] == "apple_sidecar"
            and item["motion_relative_path"] != item["relative_path"]
            else 0
        ),
    }
    companion = None
    if (
        item["motion_kind"] == "apple_sidecar"
        and item["motion_relative_path"]
        and item["motion_relative_path"] != item["relative_path"]
    ):
        companion = safe_relative_path(
            project.root, item["motion_relative_path"], "动态照片视频路径"
        )
        entry["companion_relative_path"] = item["motion_relative_path"]
    sources = [(source, int(item["size"] or 0), float(item["mtime"] or 0))]
    if companion:
        sources.append(
            (companion, int(item["motion_size"] or 0), float(item["motion_mtime"] or 0))
        )
    if any(not candidate.exists() for candidate, _, _ in sources):
        entry["status"] = "missing"
        return entry, None
    if any(
        candidate.stat().st_size != expected_size
        or abs(candidate.stat().st_mtime - expected_mtime) > 0.01
        for candidate, expected_size, expected_mtime in sources
    ):
        entry["status"] = "changed"
        return entry, None
    destination = safe_relative_path(batch_root, item["relative_path"], "隔离目标路径")
    entry["quarantine_path"] = destination.relative_to(project.root.resolve()).as_posix()
    moves = [(source, destination)]
    if companion:
        companion_destination = safe_relative_path(
            batch_root, item["motion_relative_path"], "动态照片隔离目标路径"
        )
        entry["companion_quarantine_path"] = companion_destination.relative_to(
            project.root.resolve()
        ).as_posix()
        moves.append((companion, companion_destination))
    return entry, _PreparedQuarantine(entry, item, moves)


def _prepared_quarantine_status(prepared: _PreparedQuarantine) -> str:
    if any(not source.exists() for source, _ in prepared.moves):
        return "missing"
    expected = [
        (int(prepared.item["size"] or 0), float(prepared.item["mtime"] or 0)),
        *(
            [
                (
                    int(prepared.item["motion_size"] or 0),
                    float(prepared.item["motion_mtime"] or 0),
                )
            ]
            if len(prepared.moves) > 1
            else []
        ),
    ]
    changed = any(
        source.stat().st_size != expected_size
        or abs(source.stat().st_mtime - expected_mtime) > 0.01
        for (source, _), (expected_size, expected_mtime) in zip(
            prepared.moves, expected
        )
    )
    return "changed" if changed else ""


def _move_quarantine_assets(moves: list[tuple[Path, Path]]) -> None:
    moved_paths: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved_paths.append((source, destination))
    except Exception:
        for original, moved in reversed(moved_paths):
            if moved.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved), str(original))
        raise


def _record_quarantined_item(
    conn: sqlite3.Connection,
    batch_id: str,
    manifest: list[dict[str, Any]],
    entry: dict[str, Any],
) -> None:
    conn.execute("UPDATE photos SET status='quarantined' WHERE id=?", (entry["photo_id"],))
    moved = [row for row in manifest if row["status"] == "moved"]
    conn.execute(
        "UPDATE quarantine_batches SET count=?,total_size=? WHERE id=?",
        (len(moved), sum(int(row.get("size") or 0) for row in moved), batch_id),
    )
    conn.commit()


def apply_quarantine(project: Project) -> dict[str, Any]:
    preview = quarantine_preview(project)
    batch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    batch_root = _quarantine_batch_root(project, batch_id)
    manifest: list[dict[str, Any]] = []
    prepared: list[_PreparedQuarantine] = []
    for item in preview["items"]:
        entry, candidate = _prepare_quarantine_item(project, batch_root, item)
        manifest.append(entry)
        if candidate is not None:
            prepared.append(candidate)
    batch_root.mkdir(parents=True, exist_ok=False)
    manifest_path = batch_root / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    _write_manifest_csv(batch_root, manifest)
    with closing(connect_db(project.db_path)) as conn:
        conn.execute(
            "INSERT INTO quarantine_batches(id,created_at,manifest_path,count,total_size) VALUES(?,?,?,?,?)",
            (batch_id, datetime.now().isoformat(timespec="seconds"), str(manifest_path), 0, 0),
        )
        conn.commit()
        for candidate in prepared:
            status = _prepared_quarantine_status(candidate)
            if status:
                candidate.entry["status"] = status
                atomic_write_json(manifest_path, manifest)
                continue
            try:
                _move_quarantine_assets(candidate.moves)
            except Exception as error:
                candidate.entry["status"] = "error"
                candidate.entry["error"] = str(error)
            else:
                candidate.entry["status"] = "moved"
            atomic_write_json(manifest_path, manifest)
            if candidate.entry["status"] == "moved":
                _record_quarantined_item(conn, batch_id, manifest, candidate.entry)
        rebuild_capture_variants(conn, prune_similar=True)
        conn.commit()
    _write_manifest_csv(batch_root, manifest)
    moved = [row for row in manifest if row["status"] == "moved"]
    return {
        "batch_id": batch_id,
        "moved": len(moved),
        "skipped": len(manifest) - len(moved),
    }

@dataclass(frozen=True)
class _RestorePaths:
    source: Path | None
    destination: Path
    recorded_restore: Path | None
    companion_source: Path | None
    companion_destination: Path | None
    companion_restore: Path | None


def _load_restore_manifest(
    project: Project, batch_root: Path, stored_path: str
) -> tuple[Path, list[dict[str, Any]]]:
    raw_path = Path(stored_path)
    manifest_path = (
        raw_path.resolve()
        if raw_path.is_absolute()
        else safe_relative_path(project.root, str(raw_path), "清单路径")
    )
    if manifest_path.name != "manifest.json" or not is_within(
        manifest_path, batch_root
    ):
        raise ValueError("隔离清单路径无效")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or any(
        not isinstance(item, dict) for item in manifest
    ):
        raise ValueError("隔离清单格式无效")
    return manifest_path, manifest


def _restore_paths(
    project: Project, batch_root: Path, item: dict[str, Any]
) -> _RestorePaths:
    destination = safe_relative_path(
        project.root, item.get("relative_path", ""), "恢复目标路径"
    )
    source = None
    if item.get("quarantine_path"):
        source = safe_relative_path(
            project.root, item["quarantine_path"], "隔离文件路径"
        )
        if not is_within(source, batch_root):
            raise ValueError("隔离文件路径超出当前批次")
    recorded_restore = (
        safe_relative_path(
            project.root, item["restore_path"], "已恢复文件路径"
        )
        if item.get("restore_path")
        else None
    )
    companion_destination = (
        safe_relative_path(
            project.root,
            item["companion_relative_path"],
            "动态照片恢复目标路径",
        )
        if item.get("companion_relative_path")
        else None
    )
    companion_source = None
    if item.get("companion_quarantine_path"):
        companion_source = safe_relative_path(
            project.root,
            item["companion_quarantine_path"],
            "动态照片隔离文件路径",
        )
        if not is_within(companion_source, batch_root):
            raise ValueError("动态照片隔离文件路径超出当前批次")
    companion_restore = (
        safe_relative_path(
            project.root,
            item["companion_restore_path"],
            "动态照片已恢复文件路径",
        )
        if item.get("companion_restore_path")
        else None
    )
    return _RestorePaths(
        source,
        destination,
        recorded_restore,
        companion_source,
        companion_destination,
        companion_restore,
    )


def _conflict_suffix() -> str:
    return f".restored-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def _renamed_restore_target(path: Path, suffix: str) -> Path:
    return path.with_name(path.stem + suffix + path.suffix)


def _prepare_restore_targets(
    paths: _RestorePaths, status: str
) -> tuple[_RestorePaths, int]:
    if status == "restoring" and paths.recorded_restore is not None:
        resumed = replace(
            paths,
            destination=paths.recorded_restore,
            companion_destination=(
                paths.companion_restore or paths.companion_destination
            ),
        )
        return _resolve_resume_conflicts(resumed)
    primary_conflict = paths.destination.exists()
    companion_conflict = bool(
        paths.companion_destination is not None
        and paths.companion_destination.exists()
    )
    conflicts = int(primary_conflict) + int(companion_conflict)
    if not conflicts:
        return paths, 0
    suffix = _conflict_suffix()
    return (
        replace(
            paths,
            destination=_renamed_restore_target(paths.destination, suffix),
            companion_destination=(
                _renamed_restore_target(paths.companion_destination, suffix)
                if paths.companion_destination is not None
                else None
            ),
        ),
        conflicts,
    )


def _resolve_resume_conflicts(paths: _RestorePaths) -> tuple[_RestorePaths, int]:
    primary_conflict = bool(
        paths.source and paths.source.exists() and paths.destination.exists()
    )
    companion_conflict = bool(
        paths.companion_source
        and paths.companion_source.exists()
        and paths.companion_destination
        and paths.companion_destination.exists()
    )
    conflicts = int(primary_conflict) + int(companion_conflict)
    if not conflicts:
        return paths, 0
    suffix = _conflict_suffix()
    return (
        replace(
            paths,
            destination=(
                _renamed_restore_target(paths.destination, suffix)
                if primary_conflict
                else paths.destination
            ),
            companion_destination=(
                _renamed_restore_target(paths.companion_destination, suffix)
                if companion_conflict and paths.companion_destination is not None
                else paths.companion_destination
            ),
        ),
        conflicts,
    )


def _restore_asset_pairs(paths: _RestorePaths) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if paths.source is not None:
        pairs.append((paths.source, paths.destination))
    if paths.companion_source is not None and paths.companion_destination is not None:
        pairs.append((paths.companion_source, paths.companion_destination))
    return pairs


def _restore_assets_available(paths: _RestorePaths) -> bool:
    pairs = _restore_asset_pairs(paths)
    return bool(pairs) and all(source.exists() or target.exists() for source, target in pairs)


def _record_restoring(
    project: Project,
    manifest_path: Path,
    manifest: list[dict[str, Any]],
    item: dict[str, Any],
    paths: _RestorePaths,
) -> None:
    item["status"] = "restoring"
    item["restore_path"] = paths.destination.relative_to(
        project.root.resolve()
    ).as_posix()
    if paths.companion_destination is not None:
        paths.companion_destination.parent.mkdir(parents=True, exist_ok=True)
        item["companion_restore_path"] = paths.companion_destination.relative_to(
            project.root.resolve()
        ).as_posix()
    paths.destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(manifest_path, manifest)


def _move_restore_assets(paths: _RestorePaths) -> None:
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in _restore_asset_pairs(paths):
            if source.exists():
                if destination.exists():
                    raise FileExistsError(f"恢复目标已存在：{destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            elif not destination.exists():
                raise FileNotFoundError(f"隔离文件不存在：{source}")
            moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        raise


def _update_restored_photo(
    project: Project,
    conn: sqlite3.Connection,
    item: dict[str, Any],
    paths: _RestorePaths,
) -> None:
    target_rel = paths.destination.relative_to(project.root.resolve()).as_posix()
    companion_rel = (
        paths.companion_destination.relative_to(project.root.resolve()).as_posix()
        if paths.companion_destination is not None
        else ""
    )
    if item.get("photo_id"):
        conn.execute(
            """UPDATE photos SET status='active',relative_path=?,
                      motion_relative_path=CASE WHEN ?<>'' THEN ? ELSE motion_relative_path END
                 WHERE id=?""",
            (target_rel, companion_rel, companion_rel, int(item["photo_id"])),
        )
    else:
        conn.execute(
            "UPDATE photos SET status='active',relative_path=? WHERE relative_path=?",
            (target_rel, item["relative_path"]),
        )
    conn.commit()


def _restore_manifest_item(
    project: Project,
    conn: sqlite3.Connection,
    manifest_path: Path,
    manifest: list[dict[str, Any]],
    item: dict[str, Any],
    paths: _RestorePaths,
) -> tuple[int, int, int]:
    status = str(item.get("status") or "")
    if status == "restored":
        if paths.recorded_restore and paths.recorded_restore.exists():
            restored_paths = replace(
                paths,
                destination=paths.recorded_restore,
                companion_destination=(
                    paths.companion_restore
                    if paths.companion_restore
                    and paths.companion_restore.exists()
                    else None
                ),
            )
            _update_restored_photo(project, conn, item, restored_paths)
        return 0, 0, 0
    if status not in {"moved", "pending", "restoring"}:
        return 0, 0, 0
    if status == "restoring" and paths.recorded_restore is None:
        return 0, 0, 0
    targets, conflicts = _prepare_restore_targets(paths, status)
    if not _restore_assets_available(targets):
        return 0, conflicts, 1
    _record_restoring(project, manifest_path, manifest, item, targets)
    _move_restore_assets(targets)
    item["status"] = "restored"
    item.pop("error", None)
    atomic_write_json(manifest_path, manifest)
    _update_restored_photo(project, conn, item, targets)
    return 1, conflicts, 0


def _restore_files_remain(
    manifest: list[dict[str, Any]], paths: list[_RestorePaths]
) -> bool:
    pending_statuses = {"moved", "pending", "restoring"}
    for item, item_paths in zip(manifest, paths):
        if item.get("status") not in pending_statuses:
            continue
        if any(source.exists() for source, _target in _restore_asset_pairs(item_paths)):
            return True
    return False


def restore_batch(project: Project, batch_id: str) -> dict[str, Any]:
    batch_root = _quarantine_batch_root(project, batch_id)
    with closing(connect_db(project.db_path)) as conn:
        batch = conn.execute("SELECT * FROM quarantine_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("隔离批次不存在")
        manifest_path, manifest = _load_restore_manifest(
            project, batch_root, str(batch["manifest_path"])
        )
        paths = [_restore_paths(project, batch_root, item) for item in manifest]
        restored = conflicts = missing = 0
        for item, item_paths in zip(manifest, paths):
            item_restored, item_conflicts, item_missing = _restore_manifest_item(
                project,
                conn,
                manifest_path,
                manifest,
                item,
                item_paths,
            )
            restored += item_restored
            conflicts += item_conflicts
            missing += item_missing
        rebuild_capture_variants(conn, prune_similar=True)
        conn.commit()
        if not _restore_files_remain(manifest, paths):
            conn.execute(
                "UPDATE quarantine_batches SET restored_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), batch_id),
            )
            conn.commit()
    _write_manifest_csv(batch_root, manifest)
    return {"restored": restored, "conflicts": conflicts, "missing": missing}
