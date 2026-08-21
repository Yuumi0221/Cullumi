from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .classification import (
    PHOTO_ANALYSIS_COLUMNS,
    PHOTO_UPSERT_SQL,
    classification_percentiles,
    classify,
)
from .config import ConfigStore
from .media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, analyze_photo
from .project_store import (
    Project,
    ProjectManager,
    _is_within,
    connect_db,
    project_thumbnail_path,
    project_thumbnail_storage_path,
    safe_relative_path,
)
from .similarity import (
    SimilarityGroupCache,
    _structure_similarity,
    _structure_vector,
    filename_sequence,
    hamming,
    hamming_candidate_pairs,
    parse_taken,
    quality_score,
)
from .workflows import QUARANTINE_DIR, import_decisions


class ScanCancelled(Exception):
    """Internal control flow used to stop every scan stage consistently."""


@dataclass(frozen=True)
class DiscoveryResult:
    photos: list[Path]
    discovered_total: int
    unsupported_count: int
    video_count: int
    unsupported_extensions: dict[str, int]

def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled

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

    def _discover(self, project: Project, cancel: threading.Event) -> DiscoveryResult:
        stored = self.config.snapshot().get("projects", {}).get(project.project_id, {})
        excluded = [project.root / QUARANTINE_DIR, project.project_dir]
        excluded.extend(Path(item) for item in stored.get("old_caches", []))
        excluded = [path.resolve() for path in excluded if _is_within(path, project.root)]
        project_root = project.root.resolve()

        def is_excluded(path: Path) -> bool:
            resolved = path.resolve()
            return any(resolved == root or root in resolved.parents for root in excluded)

        photos: list[Path] = []
        discovered_total = 0
        unsupported_count = 0
        video_count = 0
        unsupported_extensions: dict[str, int] = {}
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
                    discovered_total += 1
                    extension = path.suffix.lower()
                    if extension in IMAGE_EXTENSIONS:
                        photos.append(path)
                    else:
                        unsupported_count += 1
                        key = extension or "无扩展名"
                        unsupported_extensions[key] = unsupported_extensions.get(key, 0) + 1
                        if extension in VIDEO_EXTENSIONS:
                            video_count += 1
        return DiscoveryResult(
            photos=photos,
            discovered_total=discovered_total,
            unsupported_count=unsupported_count,
            video_count=video_count,
            unsupported_extensions=unsupported_extensions,
        )

    def _run(self, project_id: str, cancel: threading.Event) -> None:
        try:
            project = self.manager.from_id(project_id)
            profile = self.config.get_profile(project.profile_id)
            self._set(project_id, stage="discovering")
            discovery = self._discover(project, cancel)
            files = discovery.photos
            self._set(
                project_id,
                stage="analyzing",
                total=len(files),
                current=0,
                discovered_total=discovery.discovered_total,
                unsupported_count=discovery.unsupported_count,
                video_count=discovery.video_count,
                unsupported_extensions=discovery.unsupported_extensions,
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

