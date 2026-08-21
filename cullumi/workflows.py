from __future__ import annotations

import csv
import io
import json
import re
import shutil
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .project_store import (
    Project,
    _is_within,
    connect_db,
    safe_relative_path,
)
from .project_store import (
    atomic_write_json as _atomic_write_json,
)

QUARANTINE_DIR = "_照片筛选隔离"


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


def apply_quarantine(project: Project) -> dict[str, Any]:
    preview = quarantine_preview(project)
    batch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    batch_root = _quarantine_batch_root(project, batch_id)
    manifest: list[dict[str, Any]] = []
    prepared: list[
        tuple[dict[str, Any], dict[str, Any], list[tuple[Path, Path]]]
    ] = []
    for item in preview["items"]:
        source = safe_relative_path(project.root, item["relative_path"], "照片路径")
        entry: dict[str, Any] = {
            "photo_id": int(item["id"]),
            "relative_path": item["relative_path"],
            "status": "pending",
            "size": int(item["size"] or 0) + (
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
            manifest.append(entry)
            continue
        if any(
            candidate.stat().st_size != expected_size
            or abs(candidate.stat().st_mtime - expected_mtime) > 0.01
            for candidate, expected_size, expected_mtime in sources
        ):
            entry["status"] = "changed"
            manifest.append(entry)
            continue
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
        manifest.append(entry)
        prepared.append((entry, item, moves))

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
        for entry, item, moves in prepared:
            if any(not source.exists() for source, _ in moves):
                entry["status"] = "missing"
                _atomic_write_json(manifest_path, manifest)
                continue
            expected = [
                (int(item["size"] or 0), float(item["mtime"] or 0)),
                *(
                    [(int(item["motion_size"] or 0), float(item["motion_mtime"] or 0))]
                    if len(moves) > 1
                    else []
                ),
            ]
            if any(
                source.stat().st_size != expected_size
                or abs(source.stat().st_mtime - expected_mtime) > 0.01
                for (source, _), (expected_size, expected_mtime) in zip(
                    moves, expected
                )
            ):
                entry["status"] = "changed"
                _atomic_write_json(manifest_path, manifest)
                continue
            moved_paths: list[tuple[Path, Path]] = []
            try:
                for source, destination in moves:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    moved_paths.append((source, destination))
            except Exception as error:
                for original, moved in reversed(moved_paths):
                    if moved.exists() and not original.exists():
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(moved), str(original))
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
        companion_paths: dict[int, tuple[Path | None, Path | None, Path | None]] = {}
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
            companion_source = companion_destination = companion_restore = None
            if item.get("companion_relative_path"):
                companion_destination = safe_relative_path(
                    project.root, item["companion_relative_path"], "动态照片恢复目标路径"
                )
            if item.get("companion_quarantine_path"):
                companion_source = safe_relative_path(
                    project.root, item["companion_quarantine_path"], "动态照片隔离文件路径"
                )
                if not _is_within(companion_source, batch_root):
                    raise ValueError("动态照片隔离文件路径超出当前批次")
            if item.get("companion_restore_path"):
                companion_restore = safe_relative_path(
                    project.root, item["companion_restore_path"], "动态照片已恢复文件路径"
                )
            companion_paths[index] = (
                companion_source, companion_destination, companion_restore
            )

        restored = conflicts = missing = 0
        for index, item in enumerate(manifest):
            status = item.get("status")
            source, destination, recorded_restore = paths[index]
            companion_source, companion_destination, companion_restore = companion_paths[index]
            if status == "restored":
                if recorded_restore and recorded_restore.exists():
                    target_rel = recorded_restore.relative_to(project.root.resolve()).as_posix()
                    companion_rel = (
                        companion_restore.relative_to(project.root.resolve()).as_posix()
                        if companion_restore and companion_restore.exists()
                        else ""
                    )
                    if item.get("photo_id"):
                        conn.execute(
                            """UPDATE photos SET status='active',relative_path=?,
                                      motion_relative_path=CASE WHEN ?<>'' THEN ? ELSE motion_relative_path END
                                 WHERE id=?""",
                            (
                                target_rel, companion_rel, companion_rel,
                                int(item["photo_id"]),
                            ),
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
                if companion_source is not None and not companion_source.exists():
                    missing += 1
                    continue
                primary_conflict = destination.exists()
                companion_conflict = bool(
                    companion_destination is not None and companion_destination.exists()
                )
                if primary_conflict or companion_conflict:
                    suffix = f".restored-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
                    destination = destination.with_name(
                        destination.stem + suffix + destination.suffix
                    )
                    if companion_destination is not None:
                        companion_destination = companion_destination.with_name(
                            companion_destination.stem + suffix
                            + companion_destination.suffix
                        )
                    conflicts += int(primary_conflict) + int(companion_conflict)
                destination.parent.mkdir(parents=True, exist_ok=True)
                item["status"] = "restoring"
                item["restore_path"] = destination.relative_to(project.root.resolve()).as_posix()
                if companion_destination is not None:
                    companion_destination.parent.mkdir(parents=True, exist_ok=True)
                    item["companion_restore_path"] = companion_destination.relative_to(
                        project.root.resolve()
                    ).as_posix()
                _atomic_write_json(manifest_path, manifest)
                shutil.move(str(source), str(destination))
                try:
                    if companion_source is not None and companion_destination is not None:
                        shutil.move(str(companion_source), str(companion_destination))
                except Exception:
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(source))
                    raise
                item["status"] = "restored"
                item.pop("error", None)
                _atomic_write_json(manifest_path, manifest)
                restored += 1
            target_rel = destination.relative_to(project.root.resolve()).as_posix()
            companion_rel = (
                companion_destination.relative_to(project.root.resolve()).as_posix()
                if companion_destination is not None
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
