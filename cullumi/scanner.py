from __future__ import annotations

import hashlib
import heapq
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .analysis_worker import AnalysisCancelled, PhotoAnalysisRunner
from .classification import (
    CLASSIFICATION_COLUMNS,
    PHOTO_ANALYSIS_COLUMNS,
    PHOTO_UPSERT_SQL,
    classification_percentiles,
    classify,
)
from .config import ConfigStore
from .face_analysis import (
    MODEL_VERSION,
    BlinkThresholds,
    FaceAnalyzer,
    empty_blink_values,
)
from .fs_utils import is_within
from .media import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    analyze_photo,
)
from .motion import (
    MotionAsset,
    ensure_motion_video,
    extract_motion_frame,
    locate_motion_still_time,
    motion_asset_from_row,
    motion_fingerprint,
    paired_motion_asset,
    probe_motion,
)
from .project_store import (
    Project,
    ProjectManager,
    connect_db,
    project_thumbnail_path,
    project_thumbnail_storage_path,
    safe_relative_path,
)
from .similarity import (
    SimilarityGroupCache,
    SimilarityPair,
    _structure_similarity,
    _structure_vector,
    filename_sequence,
    hamming,
    hamming_candidate_pairs,
    parse_taken,
    quality_score,
)
from .workflows import QUARANTINE_DIR, import_decisions

DATABASE_BATCH_SIZE = 500
DISCOVERY_PROGRESS_CHECK_INTERVAL = 64
SCAN_EXISTING_COLUMNS = (
    "id",
    "relative_path",
    "size",
    "mtime",
    "taken",
    "thumbnail",
    "error",
    "status",
    "media_type",
    "motion_kind",
    "motion_relative_path",
    "motion_offset",
    "motion_length",
    "motion_size",
    "motion_mtime",
    "motion_error",
    "motion_sha256",
    "motion_still_time_ms",
    "cover_source",
    "cover_time_ms",
    "cover_revision",
)
EXACT_HASH_COLUMNS = (
    "id",
    "relative_path",
    "sha256",
    "media_type",
    "motion_kind",
    "motion_relative_path",
    "motion_offset",
    "motion_length",
    "motion_asset_id",
    "motion_sha256",
)
SIMILARITY_REBUILD_COLUMNS = (
    "id",
    "relative_path",
    "taken",
    "width",
    "height",
    "thumbnail",
    "phash",
    "dhash",
    "sha256",
    "motion_sha256",
    "media_type",
    "sharpness",
    "luminance",
    "dark_clip",
    "bright_clip",
    "contrast",
    "entropy",
    "megapixels",
)


def _fetch_batches(cursor: sqlite3.Cursor, size: int = DATABASE_BATCH_SIZE):
    while batch := cursor.fetchmany(size):
        yield batch


def _batched_update(
    conn: sqlite3.Connection,
    statement: str,
    rows: list[tuple[Any, ...]],
) -> None:
    for offset in range(0, len(rows), DATABASE_BATCH_SIZE):
        conn.executemany(statement, rows[offset : offset + DATABASE_BATCH_SIZE])


class ScanCancelled(Exception):
    """Internal control flow used to stop every scan stage consistently."""


@dataclass(frozen=True)
class DiscoveryResult:
    photos: list[Path]
    discovered_total: int
    unsupported_count: int
    video_count: int
    unsupported_extensions: dict[str, int]
    videos: list[Path] = field(default_factory=list)
    inaccessible_count: int = 0

def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled


class _SimilarityPlanner:
    """Build similarity edges while keeping each matching concern isolated."""

    def __init__(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None,
        target_ids: set[int] | None,
    ) -> None:
        self.project = project
        self.conn = conn
        self.profile = profile
        self.sim = profile["similarity"]
        self.cancel = cancel
        self.target_ids = target_ids
        self.rows = self._load_rows()
        self.derived = {
            int(row["id"]): {
                "sequence": filename_sequence(Path(row["relative_path"]).name),
                "taken": parse_taken(row["taken"]),
                "aspect": row["width"] / max(1, row["height"]),
            }
            for row in self.rows
        }
        self.quality_scores: dict[int, float] = {}
        self.structure_vectors: dict[int, tuple[np.ndarray, float] | None] = {}
        self.exact_pair_keys: set[tuple[int, int]] = set()
        self.exact_identity_by_id: dict[int, tuple[str, str]] = {}
        self.similar_heaps: dict[
            int, list[tuple[float, int, int, int, SimilarityPair]]
        ] = {}

    def _load_rows(self) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        cursor = self.conn.execute(
            f"""SELECT {','.join(SIMILARITY_REBUILD_COLUMNS)} FROM photos
                WHERE status='active' AND error=''"""
        )
        for batch in _fetch_batches(cursor):
            rows.extend(batch)
        return rows

    def _row_quality(self, row: sqlite3.Row) -> float:
        photo_id = int(row["id"])
        if photo_id not in self.quality_scores:
            self.quality_scores[photo_id] = quality_score(row, self.profile)
        return self.quality_scores[photo_id]

    def _row_structure(
        self, row: sqlite3.Row
    ) -> tuple[np.ndarray, float] | None:
        photo_id = int(row["id"])
        if photo_id not in self.structure_vectors:
            self.structure_vectors[photo_id] = _structure_vector(
                project_thumbnail_path(self.project, row["thumbnail"])
            )
        return self.structure_vectors[photo_id]

    def _existing_exact_pair_keys(self) -> None:
        if self.target_ids is None:
            return
        self.exact_pair_keys.update(
            (int(row["a_id"]), int(row["b_id"]))
            for row in self.conn.execute(
                "SELECT a_id,b_id FROM similar_pairs WHERE kind='exact'"
            )
        )

    def _exact_groups(self) -> dict[tuple[str, str], list[sqlite3.Row]]:
        by_hash: dict[tuple[str, str], list[sqlite3.Row]] = {}
        if not self.sim.get("exact_duplicates", True):
            return by_hash
        for row in self.rows:
            _check_cancelled(self.cancel)
            motion_hash = str(row["motion_sha256"] or "")
            if not row["sha256"] or (
                row["media_type"] == "motion_photo" and not motion_hash
            ):
                continue
            identity = (str(row["sha256"]), motion_hash)
            by_hash.setdefault(identity, []).append(row)
            self.exact_identity_by_id[int(row["id"])] = identity
        return by_hash

    def _append_exact_pairs(
        self,
        pairs: list[SimilarityPair],
        by_hash: dict[tuple[str, str], list[sqlite3.Row]],
    ) -> None:
        if self.target_ids is not None or not self.sim.get("exact_duplicates", True):
            return
        for group in by_hash.values():
            _check_cancelled(self.cancel)
            if len(group) < 2:
                continue
            best = max(group, key=self._row_quality)
            for row in group:
                if row["id"] == best["id"]:
                    continue
                key = tuple(sorted((best["id"], row["id"])))
                self.exact_pair_keys.add(key)
                pairs.append(
                    SimilarityPair(
                        key[0], key[1], 1.0, "exact", int(best["id"]), 0
                    )
                )

    def _directory_groups(self) -> list[list[sqlite3.Row]]:
        directories: dict[str, dict[tuple[str, str, str], sqlite3.Row]] = {}
        for row in self.rows:
            photo_id = int(row["id"])
            directory = str(Path(row["relative_path"]).parent).casefold()
            exact_identity = self.exact_identity_by_id.get(photo_id)
            candidate_key = (
                ("exact", exact_identity[0], exact_identity[1])
                if exact_identity
                else ("photo", str(photo_id), "")
            )
            candidates = directories.setdefault(directory, {})
            current = candidates.get(candidate_key)
            if current is None or self._row_quality(row) > self._row_quality(current):
                candidates[candidate_key] = row
        return [list(candidates.values()) for candidates in directories.values()]

    def _retain_similar_edge(self, edge: SimilarityPair) -> None:
        left, right, score = edge.a_id, edge.b_id, edge.score
        for photo_id, other_id in ((left, right), (right, left)):
            heap = self.similar_heaps.setdefault(photo_id, [])
            item = (score, -other_id, left, right, edge)
            if len(heap) < 8:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)

    def _is_target_pair(self, left: int, right: int) -> bool:
        return self.target_ids is None or bool(
            {left, right}.intersection(self.target_ids)
        )

    def _compare_pair(self, a: sqlite3.Row, b: sqlite3.Row) -> None:
        a_id, b_id = int(a["id"]), int(b["id"])
        if not self._is_target_pair(a_id, b_id):
            return
        if (
            self.exact_identity_by_id.get(a_id) is not None
            and self.exact_identity_by_id.get(a_id)
            == self.exact_identity_by_id.get(b_id)
        ):
            return
        aspect_a = self.derived[a_id]["aspect"]
        aspect_b = self.derived[b_id]["aspect"]
        if (
            abs(aspect_a - aspect_b) / max(aspect_a, aspect_b)
            > self.sim["aspect_tolerance"]
        ):
            return
        ph = hamming(a["phash"], b["phash"])
        dh = hamming(a["dhash"], b["dhash"])
        key = tuple(sorted((a_id, b_id)))
        if (
            ph > self.sim["phash_max"]
            or dh > self.sim["dhash_max"]
            or key in self.exact_pair_keys
        ):
            return
        structure = _structure_similarity(
            self._row_structure(a), self._row_structure(b)
        )
        if structure < self.sim["structure_min"]:
            return
        recommended = a if self._row_quality(a) >= self._row_quality(b) else b
        score = 0.45 * (1 - ph / 64) + 0.25 * (1 - dh / 64) + 0.30 * structure
        self._retain_similar_edge(
            SimilarityPair(
                key[0],
                key[1],
                score,
                "similar",
                int(recommended["id"]),
                int(self.sim.get("face_safe", True)),
            )
        )

    def _compare_indexed_group(self, group: list[sqlite3.Row]) -> None:
        index_field = (
            "phash"
            if self.sim["phash_max"] <= self.sim["dhash_max"]
            else "dhash"
        )
        radius = self.sim[f"{index_field}_max"]
        hashes = [str(row[index_field] or "") for row in group]
        for left, right in hamming_candidate_pairs(
            hashes, radius, max_neighbors=64
        ):
            _check_cancelled(self.cancel)
            self._compare_pair(group[left], group[right])

    def _photos_are_nearby(self, a: sqlite3.Row, b: sqlite3.Row) -> tuple[bool, int]:
        derived_a = self.derived[int(a["id"])]
        derived_b = self.derived[int(b["id"])]
        seq_a = derived_a["sequence"]
        seq_b = derived_b["sequence"]
        seq_gap = abs(seq_b - seq_a) if seq_a >= 0 and seq_b >= 0 else 999999
        ta, tb = derived_a["taken"], derived_b["taken"]
        time_gap = (
            abs(ta - tb) / 60 if ta is not None and tb is not None else None
        )
        nearby = seq_gap <= self.sim["sequence_gap"] or (
            time_gap is not None and time_gap <= self.sim["time_window_minutes"]
        )
        return nearby, seq_gap

    def _compare_sequential_group(self, group: list[sqlite3.Row]) -> None:
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                _check_cancelled(self.cancel)
                nearby, sequence_gap = self._photos_are_nearby(left, right)
                if not nearby:
                    if sequence_gap > self.sim["sequence_gap"]:
                        break
                    continue
                self._compare_pair(left, right)

    def _compare_directory(self, group: list[sqlite3.Row]) -> None:
        self.structure_vectors.clear()
        group.sort(
            key=lambda row: (
                self.derived[int(row["id"])]["sequence"], row["relative_path"]
            )
        )
        if self.sim.get("allow_cross_time_high_confidence"):
            self._compare_indexed_group(group)
        else:
            self._compare_sequential_group(group)

    def _selected_similar_pairs(self) -> list[SimilarityPair]:
        selected: dict[tuple[int, int], SimilarityPair] = {}
        for heap in self.similar_heaps.values():
            for item in heap:
                edge = item[-1]
                selected[(edge.a_id, edge.b_id)] = edge
        return [selected[key] for key in sorted(selected)]

    def plan(self) -> list[SimilarityPair]:
        _check_cancelled(self.cancel)
        pairs: list[SimilarityPair] = []
        self._existing_exact_pair_keys()
        exact_groups = self._exact_groups()
        self._append_exact_pairs(pairs, exact_groups)
        for group in self._directory_groups():
            self._compare_directory(group)
        pairs.extend(self._selected_similar_pairs())
        _check_cancelled(self.cancel)
        return pairs


class Scanner:
    def __init__(
        self,
        config: ConfigStore,
        manager: ProjectManager,
        similarity_groups: SimilarityGroupCache | None = None,
        face_analyzer: FaceAnalyzer | None = None,
        analysis_runner: PhotoAnalysisRunner | None = None,
    ):
        self.config = config
        self.manager = manager
        self.similarity_groups = similarity_groups
        self.face_analyzer = face_analyzer
        self.analysis_runner = analysis_runner
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

    def analyze_photo(
        self,
        path: Path,
        thumbnail: Path,
        cancel: threading.Event | None = None,
        stat: os.stat_result | None = None,
    ) -> dict[str, Any]:
        if self.analysis_runner is None:
            return analyze_photo(path, thumbnail, stat)
        try:
            return self.analysis_runner.analyze(path, thumbnail, cancel)
        except AnalysisCancelled as error:
            if cancel is not None:
                raise ScanCancelled from error
            raise RuntimeError("图片分析服务已停止") from error

    def _report_discovery_progress(
        self,
        project_id: str,
        discovered_total: int,
        photo_count: int,
        video_count: int,
        inaccessible_count: int,
        current_directory: str,
        last_progress: float,
    ) -> float:
        now = time.monotonic()
        if now - last_progress < 0.2:
            return last_progress
        self._set(
            project_id,
            current=discovered_total,
            discovered_total=discovered_total,
            photo_count=photo_count,
            video_count=video_count,
            inaccessible_count=inaccessible_count,
            current_directory=current_directory,
        )
        return now

    def _discover(self, project: Project, cancel: threading.Event) -> DiscoveryResult:
        stored = self.config.snapshot().get("projects", {}).get(project.project_id, {})
        excluded = [project.root / QUARANTINE_DIR, project.project_dir]
        excluded.extend(Path(item) for item in stored.get("old_caches", []))
        excluded = [path.resolve() for path in excluded if is_within(path, project.root)]
        project_root = project.root.resolve()
        excluded_keys = {
            os.path.normcase(os.path.abspath(path)) for path in excluded
        }

        def path_key(path: str | Path) -> str:
            return os.path.normcase(os.path.abspath(path))

        is_junction = getattr(os.path, "isjunction", lambda _path: False)

        photos: list[Path] = []
        videos: list[Path] = []
        discovered_total = 0
        unsupported_count = 0
        video_count = 0
        inaccessible_count = 0
        unsupported_extensions: dict[str, int] = {}
        pending = [project_root]
        last_progress = 0.0
        current_directory = "."
        while pending:
            _check_cancelled(cancel)
            current_path = pending.pop()
            try:
                current_directory = current_path.relative_to(project_root).as_posix() or "."
            except ValueError:
                inaccessible_count += 1
                continue
            directories: list[Path] = []
            try:
                with os.scandir(current_path) as entries:
                    for entry_index, entry in enumerate(entries, 1):
                        if entry_index % DISCOVERY_PROGRESS_CHECK_INTERVAL == 0:
                            _check_cancelled(cancel)
                            last_progress = self._report_discovery_progress(
                                project.project_id,
                                discovered_total,
                                len(photos),
                                video_count,
                                inaccessible_count,
                                current_directory,
                                last_progress,
                            )
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if is_junction(entry.path):
                                    continue
                                if path_key(entry.path) not in excluded_keys:
                                    directories.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            inaccessible_count += 1
                            continue

                        path = Path(entry.path)
                        discovered_total += 1
                        extension = path.suffix.lower()
                        if extension in IMAGE_EXTENSIONS:
                            photos.append(path)
                        else:
                            unsupported_count += 1
                            key = extension or "无扩展名"
                            unsupported_extensions[key] = (
                                unsupported_extensions.get(key, 0) + 1
                            )
                            if extension in VIDEO_EXTENSIONS:
                                video_count += 1
                                videos.append(path)

                    _check_cancelled(cancel)
            except OSError:
                inaccessible_count += 1
            pending.extend(reversed(directories))

        self._set(
            project.project_id,
            current=discovered_total,
            discovered_total=discovered_total,
            photo_count=len(photos),
            video_count=video_count,
            inaccessible_count=inaccessible_count,
            current_directory=current_directory,
        )
        return DiscoveryResult(
            photos=photos,
            videos=videos,
            discovered_total=discovered_total,
            unsupported_count=unsupported_count,
            video_count=video_count,
            unsupported_extensions=unsupported_extensions,
            inaccessible_count=inaccessible_count,
        )

    def _prepare_scan(
        self, project_id: str, cancel: threading.Event
    ) -> tuple[Project, dict[str, Any], list[Path], dict[Path, MotionAsset]]:
        """Discover files and pair motion sidecars before database work begins."""
        project = self.manager.from_id(project_id)
        profile = self.config.get_profile(project.profile_id)
        self._set(
            project_id,
            stage="discovering",
            current=0,
            total=0,
            file="",
            discovered_total=0,
            photo_count=0,
            video_count=0,
            inaccessible_count=0,
            current_directory=".",
        )
        discovery = self._discover(project, cancel)
        files = discovery.photos
        sidecars = {
            (path.parent.resolve(), path.stem.casefold()): path
            for path in discovery.videos
            if path.suffix.lower() in {".mov", ".m4v", ".mp4"}
        }
        motion_assets = {
            path: asset
            for path in files
            if (asset := paired_motion_asset(path, sidecars)) is not None
        }
        matched_sidecars = {
            asset.path.resolve()
            for asset in motion_assets.values()
            if asset.kind == "apple_sidecar"
        }
        unsupported_count = max(
            0, discovery.unsupported_count - len(matched_sidecars)
        )
        video_count = max(0, discovery.video_count - len(matched_sidecars))
        unsupported_extensions = dict(discovery.unsupported_extensions)
        for sidecar in matched_sidecars:
            extension = sidecar.suffix.lower()
            remaining = unsupported_extensions.get(extension, 0) - 1
            if remaining > 0:
                unsupported_extensions[extension] = remaining
            else:
                unsupported_extensions.pop(extension, None)
        self._set(
            project_id,
            stage="analyzing",
            total=len(files),
            current=0,
            discovered_total=discovery.discovered_total,
            unsupported_count=unsupported_count,
            video_count=video_count,
            unsupported_extensions=unsupported_extensions,
            unavailable_count=0,
            inaccessible_count=discovery.inaccessible_count,
            file="",
            current_directory="",
        )
        return project, profile, files, motion_assets

    def _finish_scan(
        self,
        project_id: str,
        project: Project,
        files: list[Path],
        unavailable_count: int,
        cancel: threading.Event,
    ) -> None:
        """Run post-database imports and publish the terminal scan state."""
        _check_cancelled(cancel)
        self._auto_import_csv(project)
        self._set(
            project_id,
            stage="complete",
            done=True,
            current=len(files),
            total=len(files),
            unavailable_count=unavailable_count,
            file="",
        )

    def _confirm_exact_duplicates(
        self,
        project_id: str,
        project: Project,
        conn: sqlite3.Connection,
        unavailable_count: int,
        cancel: threading.Event,
    ) -> int:
        """Confirm byte-identical candidates after incremental analysis."""
        _check_cancelled(cancel)
        self._set(project_id, stage="hashing", current=0, total=0, file="")
        unavailable_count += self._exact_hashes(project, conn, cancel)
        self._set(project_id, unavailable_count=unavailable_count)
        return unavailable_count

    def _rebuild_relationships(
        self,
        project_id: str,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        """Rebuild similarity topology and dependent classifications."""
        _check_cancelled(cancel)
        self._set(project_id, stage="grouping", current=0, total=0, file="")
        self.rebuild_similarity(project, conn, profile, cancel)
        _check_cancelled(cancel)
        self.analyze_blinks(
            project,
            conn,
            profile,
            cancel,
            progress_project_id=project_id,
        )
        _check_cancelled(cancel)
        self.reclassify(project, conn, profile, cancel)

    @staticmethod
    def _existing_photos(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        existing: dict[str, sqlite3.Row] = {}
        cursor = conn.execute(
            f"SELECT {','.join(SCAN_EXISTING_COLUMNS)} FROM photos"
        )
        for batch in _fetch_batches(cursor):
            existing.update((str(row["relative_path"]), row) for row in batch)
        return existing

    @staticmethod
    def _motion_storage_values(
        project: Project, asset: MotionAsset | None
    ) -> dict[str, Any]:
        if asset is not None:
            return asset.storage_values(project.root)
        return {
            "media_type": "image",
            "motion_kind": "",
            "motion_relative_path": "",
            "motion_offset": 0,
            "motion_length": 0,
            "motion_size": 0,
            "motion_mtime": 0,
            "motion_asset_id": "",
        }

    @staticmethod
    def _motion_state_matches(
        old: sqlite3.Row | None, asset_values: dict[str, Any]
    ) -> tuple[bool, bool]:
        identity_same = bool(
            old
            and old["media_type"] == asset_values["media_type"]
            and old["motion_kind"] == asset_values["motion_kind"]
            and old["motion_relative_path"] == asset_values["motion_relative_path"]
        )
        unchanged = bool(
            identity_same
            and int(old["motion_offset"] or 0) == asset_values["motion_offset"]
            and int(old["motion_length"] or 0) == asset_values["motion_length"]
            and int(old["motion_size"] or 0) == asset_values["motion_size"]
            and abs(float(old["motion_mtime"] or 0) - asset_values["motion_mtime"])
            < 0.001
        )
        return identity_same, unchanged

    @staticmethod
    def _photo_is_current(
        project: Project,
        old: sqlite3.Row | None,
        stat: os.stat_result | None,
        motion_same: bool,
    ) -> bool:
        thumbnail = (
            project_thumbnail_path(project, old["thumbnail"])
            if old and old["thumbnail"]
            else None
        )
        return bool(
            old
            and stat is not None
            and old["size"] == stat.st_size
            and abs(old["mtime"] - stat.st_mtime) < 0.001
            and motion_same
            and not old["error"]
            and not old["motion_error"]
            and thumbnail is not None
            and thumbnail.is_file()
            and (
                old["media_type"] != "motion_photo"
                or int(
                    old["motion_still_time_ms"]
                    if old["motion_still_time_ms"] is not None
                    else -1
                )
                >= 0
            )
        )

    @staticmethod
    def _probe_motion_values(
        path: Path,
        asset: MotionAsset | None,
        asset_values: dict[str, Any],
        old: sqlite3.Row | None,
        motion_same: bool,
    ) -> dict[str, Any]:
        motion_values = {
            **asset_values,
            "motion_error": "",
            "motion_duration_ms": 0,
            "motion_fps": 0,
            "motion_frame_count": 0,
            "motion_width": 0,
            "motion_height": 0,
            "motion_sha256": old["motion_sha256"] if old and motion_same else "",
            "motion_still_time_ms": -1 if asset else 0,
        }
        if asset is None:
            return motion_values
        try:
            motion_values.update(probe_motion(asset))
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            motion_values["motion_error"] = str(error)
        if motion_values["motion_error"]:
            return motion_values
        try:
            motion_values["motion_still_time_ms"] = locate_motion_still_time(
                path,
                asset,
                int(motion_values["motion_duration_ms"]),
                float(motion_values["motion_fps"]),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            motion_values["motion_still_time_ms"] = 0
        return motion_values

    def _preserved_motion_cover(
        self,
        project: Project,
        path: Path,
        asset: MotionAsset | None,
        old: sqlite3.Row | None,
        motion_identity_same: bool,
        motion_values: dict[str, Any],
        thumbnail: Path,
        stat: os.stat_result | None,
        cancel: threading.Event,
    ) -> tuple[dict[str, Any] | None, int, int]:
        if not (
            asset
            and old
            and motion_identity_same
            and old["cover_source"] == "motion"
            and not motion_values["motion_error"]
        ):
            return None, 0, 0
        cover_time_ms = min(
            int(old["cover_time_ms"] or 0),
            max(0, int(motion_values["motion_duration_ms"]) - 1),
        )
        try:
            motion_dir = project.motion_dir or project.project_dir / "motion"
            video = ensure_motion_video(asset, motion_dir)
            frame = motion_dir / (
                f"{motion_fingerprint(asset)}.motion-cover-{cover_time_ms}.jpg"
            )
            extract_motion_frame(video, cover_time_ms, frame)
            metrics = self.analyze_photo(frame, thumbnail, cancel)
            source_stat = stat or path.stat()
            metrics.update(
                {
                    "extension": path.suffix.lower(),
                    "size": source_stat.st_size,
                    "mtime": source_stat.st_mtime,
                    "taken": old["taken"],
                }
            )
            frame_index = round(
                cover_time_ms * float(motion_values["motion_fps"]) / 1000
            )
            return metrics, cover_time_ms, frame_index
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            motion_values["motion_error"] = str(error)
            return None, 0, 0

    def _changed_photo_values(
        self,
        project: Project,
        path: Path,
        rel: str,
        stat: os.stat_result | None,
        old: sqlite3.Row | None,
        asset: MotionAsset | None,
        asset_values: dict[str, Any],
        motion_identity_same: bool,
        motion_same: bool,
        profile: dict[str, Any],
        cancel: threading.Event,
    ) -> dict[str, Any]:
        thumb_name = hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".jpg"
        thumbnail = project.thumb_dir / thumb_name
        motion_values = self._probe_motion_values(
            path, asset, asset_values, old, motion_same
        )
        metrics, cover_time_ms, cover_frame_index = self._preserved_motion_cover(
            project,
            path,
            asset,
            old,
            motion_identity_same,
            motion_values,
            thumbnail,
            stat,
            cancel,
        )
        cover_source = "motion" if metrics is not None else "still"
        if metrics is None:
            metrics = self.analyze_photo(path, thumbnail, cancel, stat)
        if metrics.get("thumbnail"):
            metrics["thumbnail"] = project_thumbnail_storage_path(
                metrics["thumbnail"]
            )
        values = {
            "relative_path": rel,
            **metrics,
            **motion_values,
            "cover_source": cover_source,
            "cover_time_ms": cover_time_ms,
            "cover_frame_index": cover_frame_index,
            "cover_revision": int(old["cover_revision"] or 0) + 1 if old else 0,
            "quality_score": 0,
            "suggestion": "keep",
            "reason": "",
            "status": "active",
            "analyzed_at": datetime.now().isoformat(timespec="microseconds"),
            **empty_blink_values(),
        }
        values["quality_score"] = round(
            max(0.0, min(1.0, quality_score(values, profile))) * 100, 1
        )
        values["suggestion"], values["reason"] = classify(values, profile)
        return values

    def _scan_photo(
        self,
        project_id: str,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        path: Path,
        asset: MotionAsset | None,
        old: sqlite3.Row | None,
        index: int,
        unavailable_count: int,
        cancel: threading.Event,
    ) -> tuple[bool, int]:
        rel = path.relative_to(project.root).as_posix()
        unavailable_delta = 0
        try:
            stat = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            self._set(
                project_id,
                current=index,
                file=rel,
                unavailable_count=unavailable_count + 1,
            )
            return False, 1
        except OSError:
            stat = None
            unavailable_delta = 1
        asset_values = self._motion_storage_values(project, asset)
        motion_identity_same, motion_same = self._motion_state_matches(
            old, asset_values
        )
        if self._photo_is_current(project, old, stat, motion_same):
            if old is not None and old["status"] != "active":
                conn.execute(
                    "UPDATE photos SET status='active' WHERE id=?", (old["id"],)
                )
            self._set(project_id, current=index)
            return True, unavailable_delta
        values = self._changed_photo_values(
            project,
            path,
            rel,
            stat,
            old,
            asset,
            asset_values,
            motion_identity_same,
            motion_same,
            profile,
            cancel,
        )
        if not path.is_file():
            unavailable_delta += 1
            self._set(
                project_id,
                current=index,
                file=rel,
                unavailable_count=unavailable_count + unavailable_delta,
            )
            return False, unavailable_delta
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
            unavailable_count=unavailable_count + unavailable_delta,
        )
        return True, unavailable_delta

    def _scan_database(
        self,
        project_id: str,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        files: list[Path],
        motion_assets: dict[Path, MotionAsset],
        cancel: threading.Event,
    ) -> int:
        existing = self._existing_photos(conn)
        seen: set[str] = set()
        unavailable_count = 0
        for index, path in enumerate(files, 1):
            _check_cancelled(cancel)
            rel = path.relative_to(project.root).as_posix()
            was_seen, unavailable_delta = self._scan_photo(
                project_id,
                project,
                conn,
                profile,
                path,
                motion_assets.get(path),
                existing.get(rel),
                index,
                unavailable_count,
                cancel,
            )
            unavailable_count += unavailable_delta
            if was_seen:
                seen.add(rel)
        missing = [
            (rel,)
            for rel, row in existing.items()
            if rel not in seen and row["status"] == "active"
        ]
        conn.executemany(
            "UPDATE photos SET status='missing' WHERE relative_path=?", missing
        )
        conn.commit()
        return unavailable_count

    def _run(self, project_id: str, cancel: threading.Event) -> None:
        try:
            project, profile, files, motion_assets = self._prepare_scan(
                project_id, cancel
            )
            with closing(connect_db(project.db_path)) as conn:
                unavailable_count = self._scan_database(
                    project_id,
                    project,
                    conn,
                    profile,
                    files,
                    motion_assets,
                    cancel,
                )
                unavailable_count = self._confirm_exact_duplicates(
                    project_id,
                    project,
                    conn,
                    unavailable_count,
                    cancel,
                )
                self._rebuild_relationships(
                    project_id, project, conn, profile, cancel
                )
            self._finish_scan(
                project_id, project, files, unavailable_count, cancel
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
        updates: list[tuple[str, int]] = []
        motion_updates: list[tuple[str, int]] = []
        missing_ids: list[tuple[int]] = []
        unavailable = 0
        projected = ",".join(f"photos.{column}" for column in EXACT_HASH_COLUMNS)
        candidates = conn.execute(
            f"""SELECT {projected} FROM photos
                JOIN (
                  SELECT size FROM photos
                  WHERE status='active' AND error=''
                  GROUP BY size HAVING COUNT(*)>1
                ) duplicate_sizes ON duplicate_sizes.size=photos.size
                WHERE photos.status='active'
                ORDER BY photos.size,photos.id"""
        )
        for batch in _fetch_batches(candidates):
            for row in batch:
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
                if not row["sha256"]:
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
                if row["media_type"] == "motion_photo" and not row["motion_sha256"]:
                    try:
                        asset = motion_asset_from_row(project.root, row)
                        motion_digest = hashlib.sha256()
                        with asset.path.open("rb") as motion_source:
                            if asset.offset:
                                motion_source.seek(asset.offset)
                            remaining = asset.length or asset.path.stat().st_size
                            while remaining:
                                _check_cancelled(cancel)
                                block = motion_source.read(min(1024 * 1024, remaining))
                                if not block:
                                    break
                                motion_digest.update(block)
                                remaining -= len(block)
                        motion_updates.append((motion_digest.hexdigest(), int(row["id"])))
                    except (OSError, ValueError):
                        unavailable += 1
        _batched_update(conn, "UPDATE photos SET sha256=? WHERE id=?", updates)
        _batched_update(
            conn,
            "UPDATE photos SET motion_sha256=? WHERE id=?", motion_updates
        )
        _batched_update(
            conn,
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
        photo_ids: set[int] | None = None,
    ) -> None:
        percentile_mode = profile["quality"].get("threshold_mode") == "percentile"
        percentiles = (
            classification_percentiles(
                conn.execute(
                    "SELECT sharpness FROM photos WHERE status='active'"
                ),
                profile,
            )
            if percentile_mode
            else None
        )
        selected_ids = None if percentile_mode else photo_ids
        if selected_ids == set():
            return
        queries: list[tuple[str, list[int]]] = []
        if selected_ids is None:
            queries.append(("status='active'", []))
        else:
            ordered_ids = sorted(int(photo_id) for photo_id in selected_ids)
            for offset in range(0, len(ordered_ids), DATABASE_BATCH_SIZE):
                chunk = ordered_ids[offset : offset + DATABASE_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                queries.append((f"status='active' AND id IN ({placeholders})", chunk))
        updates: list[tuple[str, str, float, int]] = []
        columns = ",".join(CLASSIFICATION_COLUMNS)
        for where, params in queries:
            cursor = conn.execute(f"SELECT {columns} FROM photos WHERE {where}", params)
            for batch in _fetch_batches(cursor):
                for row in batch:
                    _check_cancelled(cancel)
                    suggestion, reason = classify(row, profile, percentiles)
                    score = round(
                        max(0.0, min(1.0, quality_score(row, profile))) * 100, 1
                    )
                    updates.append((suggestion, reason, score, int(row["id"])))
        _batched_update(
            conn,
            "UPDATE photos SET suggestion=?,reason=?,quality_score=? WHERE id=?",
            updates,
        )
        if commit:
            conn.commit()

    @staticmethod
    def _blink_candidate_rows(
        conn: sqlite3.Connection,
        photo_ids: set[int] | None = None,
    ) -> list[sqlite3.Row]:
        params: list[Any] = []
        id_filter = ""
        if photo_ids is not None:
            if not photo_ids:
                return []
            placeholders = ",".join("?" for _ in photo_ids)
            id_filter = f" AND photos.id IN ({placeholders})"
            params.extend(sorted(photo_ids))
        return conn.execute(
            f"""SELECT DISTINCT photos.* FROM photos
                JOIN (
                  SELECT a_id AS photo_id FROM similar_pairs WHERE kind='similar'
                  UNION
                  SELECT b_id AS photo_id FROM similar_pairs WHERE kind='similar'
                ) candidates ON candidates.photo_id=photos.id
                WHERE photos.status='active' AND photos.error=''
                  AND photos.thumbnail<>''{id_filter}
                ORDER BY photos.id""",
            params,
        ).fetchall()

    def blink_rescan_required(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
    ) -> bool:
        """Check cached blink fingerprints without creating ONNX sessions."""
        if not self.config.snapshot().get("blink_detection_enabled", True):
            return False
        thresholds = BlinkThresholds.from_profile(profile)
        reusable_statuses = {"open", "closed", "uncertain", "no_face"}
        for row in self._blink_candidate_rows(conn):
            if (
                row["blink_status"] not in reusable_statuses
                or row["blink_model_version"] != MODEL_VERSION
            ):
                return True
            try:
                fingerprint_builder = self.face_analyzer or FaceAnalyzer
                fingerprint = fingerprint_builder.input_fingerprint(
                    project_thumbnail_path(project, row["thumbnail"]),
                    row,
                    thresholds,
                )
            except OSError:
                return True
            if row["blink_input_fingerprint"] != fingerprint:
                return True
        return False

    def analyze_blinks(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None = None,
        commit: bool = True,
        progress_project_id: str | None = None,
        photo_ids: set[int] | None = None,
    ) -> int:
        """Incrementally analyze non-exact similar candidates from thumbnails."""
        if not self.config.snapshot().get("blink_detection_enabled", True):
            return 0
        if self.face_analyzer is None:
            return 0
        rows = self._blink_candidate_rows(conn, photo_ids)
        if progress_project_id is not None:
            self._set(
                progress_project_id,
                stage="blink_detection",
                current=0,
                total=len(rows),
            )
        thresholds = BlinkThresholds.from_profile(profile)
        reusable_statuses = {"open", "closed", "uncertain", "no_face"}
        analyzed = 0
        for index, row in enumerate(rows, 1):
            _check_cancelled(cancel)
            thumbnail = project_thumbnail_path(project, row["thumbnail"])
            fingerprint = ""
            try:
                fingerprint = self.face_analyzer.input_fingerprint(
                    thumbnail, row, thresholds
                )
                if (
                    row["blink_status"] in reusable_statuses
                    and row["blink_model_version"] == MODEL_VERSION
                    and row["blink_input_fingerprint"] == fingerprint
                ):
                    if progress_project_id is not None:
                        self._set(progress_project_id, current=index)
                    continue
                result = self.face_analyzer.analyze(thumbnail, row, profile)
            except Exception as error:
                result = empty_blink_values("error", str(error)[:1000])
                result["blink_input_fingerprint"] = fingerprint
            columns = tuple(result)
            assignments = ",".join(f"{column}=?" for column in columns)
            conn.execute(
                f"UPDATE photos SET {assignments} WHERE id=?",
                [*[result[column] for column in columns], int(row["id"])],
            )
            analyzed += 1
            if commit and index % 20 == 0:
                conn.commit()
            if progress_project_id is not None:
                self._set(
                    progress_project_id,
                    current=index,
                    file=str(row["relative_path"]),
                )
        if commit:
            conn.commit()
        return analyzed

    def plan_similarity_pairs(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None = None,
        photo_ids: set[int] | None = None,
    ) -> list[SimilarityPair]:
        target_ids = None if photo_ids is None else {int(value) for value in photo_ids}
        if target_ids == set():
            return []
        return _SimilarityPlanner(
            project, conn, profile, cancel, target_ids
        ).plan()

    def rebuild_similarity(
        self,
        project: Project,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        cancel: threading.Event | None = None,
        commit: bool = True,
        photo_ids: set[int] | None = None,
    ) -> None:
        target_ids = None if photo_ids is None else {int(value) for value in photo_ids}
        if target_ids == set():
            return
        pairs = self.plan_similarity_pairs(
            project,
            conn,
            profile,
            cancel,
            target_ids,
        )

        def replace_pairs() -> None:
            if target_ids is None:
                conn.execute("DELETE FROM similar_pairs")
            else:
                placeholders = ",".join("?" for _ in target_ids)
                params = sorted(target_ids)
                conn.execute(
                    f"""DELETE FROM similar_pairs WHERE kind='similar'
                        AND (a_id IN ({placeholders}) OR b_id IN ({placeholders}))""",
                    [*params, *params],
                )
            conn.executemany(
                "INSERT OR IGNORE INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe) VALUES(?,?,?,?,?,?)",
                pairs,
            )
        if commit:
            with conn:
                replace_pairs()
        else:
            replace_pairs()

    @staticmethod
    def related_photo_ids(
        conn: sqlite3.Connection,
        photo_ids: set[int],
        kind: str = "similar",
    ) -> set[int]:
        if not photo_ids:
            return set()
        placeholders = ",".join("?" for _ in photo_ids)
        params = sorted(photo_ids)
        rows = conn.execute(
            f"""SELECT a_id,b_id FROM similar_pairs WHERE kind=?
                AND (a_id IN ({placeholders}) OR b_id IN ({placeholders}))""",
            [kind, *params, *params],
        )
        related = set(photo_ids)
        for row in rows:
            related.update((int(row["a_id"]), int(row["b_id"])))
        return related

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
