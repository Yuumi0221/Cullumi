from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
from PIL import Image, ImageOps

from .classification import PHOTO_ROW_COLUMNS
from .niqe import valid_niqe

SIMILARITY_TOPOLOGY_COLUMNS = (
    "niqe_score", "niqe_error",
    "id",
    "relative_path",
    "taken",
    "sha256",
    "motion_sha256",
    "sharpness",
    "luminance",
    "dark_clip",
    "bright_clip",
    "contrast",
    "entropy",
    "megapixels",
    "blink_status",
    "blink_face_count",
    "blink_closed_face_count",
    "blink_uncertain_face_count",
    "blink_closed_ratio",
)

# Keep the edge table as the outer loop.  Without CROSS JOIN, SQLite can
# choose the status/error index on both photo aliases first, producing an
# O(active_photos²) nested loop before probing similar_pairs.
_SIMILARITY_EDGES_SQL = """SELECT sp.a_id,sp.b_id,sp.score,sp.kind,
                                  sp.recommended_id,sp.face_safe
                           FROM similar_pairs sp
                           CROSS JOIN photos a ON a.id=sp.a_id
                           CROSS JOIN photos b ON b.id=sp.b_id
                           WHERE a.status='active' AND a.error=''
                             AND b.status='active' AND b.error=''"""


class SimilarityPair(NamedTuple):
    a_id: int
    b_id: int
    score: float
    kind: str
    recommended_id: int
    face_safe: int


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count() if a and b else 64


def hamming_candidate_pairs(
    hashes: list[str], radius: float, max_neighbors: int | None = None
) -> Iterator[tuple[int, int]]:
    """Vectorize the hash prefilter with optional per-photo output bounds."""
    if math.isnan(radius):
        return
    parsed: list[int] = []
    valid: list[bool] = []
    wide_hash = False
    for raw_hash in hashes:
        if raw_hash:
            value = int(raw_hash, 16)
            parsed.append(value & ((1 << 64) - 1))
            valid.append(True)
            wide_hash = wide_hash or value < 0 or value.bit_length() > 64
        else:
            parsed.append(0)
            valid.append(False)

    if wide_hash:
        for left in range(len(hashes)):
            candidates: list[tuple[int, int]] = []
            for right in range(left + 1, len(hashes)):
                distance = hamming(hashes[left], hashes[right])
                if distance <= radius:
                    candidates.append((distance, right))
            candidates.sort()
            if max_neighbors is not None:
                candidates = candidates[:max_neighbors]
            for _distance, right in candidates:
                yield left, right
        return

    values = np.asarray(parsed, dtype=np.uint64)
    valid_mask = np.asarray(valid, dtype=np.bool_)
    count = len(values)
    target_elements = 1_000_000
    block_size = max(1, min(256, target_elements // max(1, count)))
    for start in range(0, max(0, count - 1), block_size):
        end = min(count - 1, start + block_size)
        right_start = start + 1
        distances = np.bitwise_count(
            np.bitwise_xor(values[start:end, None], values[None, right_start:])
        )
        matches = distances <= radius
        if radius < 64:
            matches &= valid_mask[start:end, None]
            matches &= valid_mask[None, right_start:]
        else:
            matches |= ~valid_mask[start:end, None]
            matches |= ~valid_mask[None, right_start:]
        for local_left, row_matches in enumerate(matches):
            row_matches[:local_left] = False
            left = start + local_left
            offsets = np.flatnonzero(row_matches)
            if max_neighbors is not None and len(offsets) > max_neighbors:
                row_distances = distances[local_left, offsets]
                order = np.lexsort((offsets, row_distances))[:max_neighbors]
                offsets = offsets[order]
            for offset in offsets:
                yield left, right_start + int(offset)


def _structure_vector(path: Path) -> tuple[np.ndarray, float] | None:
    try:
        with Image.open(path) as image:
            vector = np.asarray(
                ImageOps.grayscale(image).resize((64, 64)), dtype=np.float32
            )
        vector -= vector.mean()
        return vector, float(np.linalg.norm(vector))
    except Exception:
        return None


def _structure_similarity(
    left: tuple[np.ndarray, float] | None,
    right: tuple[np.ndarray, float] | None,
) -> float:
    if left is None or right is None:
        return 0.0
    left_vector, left_norm = left
    right_vector, right_norm = right
    denominator = left_norm * right_norm
    return float(np.sum(left_vector * right_vector) / denominator) if denominator else 0.0


def image_structure(path_a: Path, path_b: Path) -> float:
    return _structure_similarity(_structure_vector(path_a), _structure_vector(path_b))


def quality_score(row: sqlite3.Row | dict[str, Any], profile: dict[str, Any]) -> float:
    q = profile["quality"]
    weights = q["weights"]
    sharpness = min(1.0, math.log1p(max(0, row["sharpness"] or 0)) / 10)
    exposure = 1 - min(
        1.0,
        abs((row["luminance"] if row["luminance"] is not None else 128) - 110) / 140
        + (row["dark_clip"] or 0)
        + (row["bright_clip"] or 0),
    )
    contrast = min(1.0, (row["contrast"] or 0) / 70)
    entropy = min(1.0, (row["entropy"] or 0) / 8)
    resolution = min(1.0, (row["megapixels"] or 0) / 12)
    components = {
        "sharpness": (sharpness, ("sharpness",)),
        "exposure": (exposure, ("luminance", "dark_clip", "bright_clip")),
        "contrast": (contrast, ("contrast",)),
        "entropy": (entropy, ("entropy",)),
        "resolution": (resolution, ("megapixels",)),
    }
    active = {
        key: value for key, (value, fields) in components.items()
        if all(row[field] is not None and math.isfinite(row[field]) for field in fields)
    }
    niqe = valid_niqe(row)
    if niqe is not None and q.get("enabled", {}).get("niqe", True):
        good, bad = q["niqe_quality_good"], q["niqe_quality_bad"]
        active["niqe"] = max(0.0, min(1.0, (bad - niqe) / (bad - good)))
    total = sum(weights.get(key, 0) for key in active)
    return sum(value * weights.get(key, 0) for key, value in active.items()) / total if total else 0.0


def filename_sequence(name: str) -> int:
    match = re.search(r"(\d+)(?!.*\d)", name)
    return int(match.group(1)) if match else -1


def parse_taken(value: str) -> float | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).timestamp()
        except ValueError:
            pass
    return None


def photo_shooting_key(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    taken = parse_taken(str(row["taken"] or ""))
    path = str(row["relative_path"])
    sequence = filename_sequence(Path(path).name)
    if taken is not None:
        return (0, taken, sequence if sequence >= 0 else math.inf, path.casefold())
    return (1, sequence if sequence >= 0 else math.inf, path.casefold())


def blink_recommendation_key(
    row: sqlite3.Row | dict[str, Any],
    profile: dict[str, Any],
    enabled: bool = True,
) -> tuple[Any, ...]:
    """Rank reliable open faces before neutral photos and closed eyes last."""
    score = quality_score(row, profile)
    path = str(row["relative_path"]).casefold()
    if not enabled:
        return (1, 0.0, 0, -score, path)
    face_count = max(0, int(row["blink_face_count"] or 0))
    uncertain_count = max(0, int(row["blink_uncertain_face_count"] or 0))
    reliable_count = max(0, face_count - uncertain_count)
    coverage = reliable_count / face_count if face_count else 0.0
    coverage_min = float(
        profile.get("similarity", {})
        .get("blink", {})
        .get("reliable_coverage_min", 0.8)
    )
    status = str(row["blink_status"] or "not_analyzed")
    if status == "open" and face_count and coverage >= coverage_min:
        category = 0
    elif (
        status == "closed"
        and int(row["blink_closed_face_count"] or 0) > 0
        and coverage >= coverage_min
    ):
        category = 2
    else:
        category = 1
    if category == 2:
        ratio = float(row["blink_closed_ratio"] or 0)
        closed_count = int(row["blink_closed_face_count"] or 0)
        return (category, ratio, closed_count, -score, path)
    return (category, 0.0, 0, -score, path)


def _participant_photos(
    conn: sqlite3.Connection,
    photo_ids: list[int],
    columns: tuple[str, ...],
) -> dict[int, sqlite3.Row]:
    photos: dict[int, sqlite3.Row] = {}
    projection = ",".join(columns)
    for offset in range(0, len(photo_ids), 900):
        chunk = photo_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""SELECT {projection} FROM photos
                WHERE id IN ({placeholders})
                  AND status='active' AND error=''""",
            chunk,
        ):
            photos[int(row["id"])] = row
    return photos


def _collapse_similarity_topology(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    edges: Iterable[SimilarityPair],
    blink_detection_enabled: bool = True,
) -> list[dict[str, Any]]:
    parent: dict[int, int] = {}

    def find(value: int) -> int:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    adjacency: dict[int, list[tuple[int, float]]] = {}
    face_safe_ids: set[int] = set()
    for edge in edges:
        left, right = edge.a_id, edge.b_id
        union(left, right)
        score = float(edge.score or 0)
        adjacency.setdefault(left, []).append((right, score))
        adjacency.setdefault(right, []).append((left, score))
        if edge.face_safe:
            face_safe_ids.update((left, right))

    components: dict[int, list[int]] = {}
    for photo_id in parent:
        components.setdefault(find(photo_id), []).append(photo_id)

    photo_ids = sorted(parent)
    parent.clear()
    photos = _participant_photos(conn, photo_ids, SIMILARITY_TOPOLOGY_COLUMNS)
    del photo_ids

    minimum = max(2, int(profile.get("similarity", {}).get("min_group_size", 2)))
    topology: list[dict[str, Any]] = []
    while components:
        _root, member_ids = components.popitem()
        if len(member_ids) < minimum:
            for photo_id in member_ids:
                photos.pop(photo_id, None)
                adjacency.pop(photo_id, None)
                face_safe_ids.discard(photo_id)
            continue
        members = [photos.pop(photo_id) for photo_id in member_ids]
        shooting_keys = {
            int(row["id"]): photo_shooting_key(row)
            for row in members
        }
        hashes = {
            (str(row["sha256"]), str(row["motion_sha256"] or ""))
            for row in members
            if row["sha256"]
        }
        exact = (
            len(hashes) == 1
            and bool(hashes)
            and all(row["sha256"] for row in members)
        )
        ranked = sorted(
            members,
            key=lambda row: blink_recommendation_key(
                row,
                profile,
                blink_detection_enabled and not exact,
            ),
        )
        recommended = ranked[0]
        recommended_id = int(recommended["id"])

        # Maximum-bottleneck paths retain a meaningful confidence for
        # transitive members relative to the recommended photo.
        confidence = {photo_id: 0.0 for photo_id in member_ids}
        confidence[recommended_id] = 1.0
        pending: list[tuple[float, int]] = [(-1.0, recommended_id)]
        while pending:
            negative_score, current = heapq.heappop(pending)
            current_score = -negative_score
            if current_score < confidence[current]:
                continue
            for neighbor, edge_score in adjacency.pop(current, []):
                candidate = min(current_score, edge_score)
                if candidate > confidence.get(neighbor, 0.0):
                    confidence[neighbor] = candidate
                    heapq.heappush(pending, (-candidate, neighbor))

        stable_ids = ",".join(str(photo_id) for photo_id in sorted(member_ids))
        group_id = "sg-" + hashlib.sha1(stable_ids.encode("ascii")).hexdigest()[:16]
        face_safe = any(photo_id in face_safe_ids for photo_id in member_ids)
        face_safe_ids.difference_update(member_ids)
        topology.append(
            {
                "id": group_id,
                "member_ids": sorted(member_ids),
                "recommended_id": recommended_id,
                "cover_ids": [
                    recommended_id,
                    *[
                        int(row["id"])
                        for row in ranked
                        if int(row["id"]) != recommended_id
                    ],
                ][:4],
                "confidence": confidence,
                "kind": "exact" if exact else "similar",
                "face_safe": face_safe,
                "sort_key": min(shooting_keys.values()),
            }
        )
    topology.sort(key=lambda group: (group["sort_key"], group["id"]))
    return topology


def _build_similarity_topology(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    blink_detection_enabled: bool = True,
) -> list[dict[str, Any]]:
    rows = conn.execute(_SIMILARITY_EDGES_SQL)
    edges = (
        SimilarityPair(
            int(row["a_id"]),
            int(row["b_id"]),
            float(row["score"] or 0),
            str(row["kind"] or ""),
            int(row["recommended_id"] or 0),
            int(row["face_safe"] or 0),
        )
        for row in rows
    )
    return _collapse_similarity_topology(
        conn,
        profile,
        edges,
        blink_detection_enabled,
    )


def _build_similarity_topology_from_pairs(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    pairs: Iterable[SimilarityPair],
    blink_detection_enabled: bool = True,
) -> list[dict[str, Any]]:
    return _collapse_similarity_topology(
        conn,
        profile,
        pairs,
        blink_detection_enabled,
    )


def _hydrate_similarity_groups(
    conn: sqlite3.Connection,
    topology: list[dict[str, Any]],
    *,
    include_cover_ids: bool,
    reuse_groups: bool = False,
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    batch_size = 0
    for group in topology:
        member_count = len(group["member_ids"])
        if batch and batch_size + member_count > 900:
            hydrated.extend(
                _hydrate_similarity_batch(
                    conn, batch, include_cover_ids, reuse_groups
                )
            )
            batch = []
            batch_size = 0
        batch.append(group)
        batch_size += member_count
    if batch:
        hydrated.extend(
            _hydrate_similarity_batch(conn, batch, include_cover_ids, reuse_groups)
        )
    return hydrated


def _hydrate_similarity_batch(
    conn: sqlite3.Connection,
    groups: list[dict[str, Any]],
    include_cover_ids: bool,
    reuse_groups: bool,
) -> list[dict[str, Any]]:
    photo_ids = [
        photo_id for group in groups for photo_id in group["member_ids"]
    ]
    photos = _participant_photos(conn, photo_ids, PHOTO_ROW_COLUMNS)
    hydrated: list[dict[str, Any]] = []
    for group in groups:
        if any(photo_id not in photos for photo_id in group["member_ids"]):
            continue
        member_photos = {
            photo_id: photos.pop(photo_id) for photo_id in group["member_ids"]
        }
        members = list(member_photos.values())
        members.sort(key=photo_shooting_key)
        hydrated_group = group if reuse_groups else dict(group)
        cover_ids = group["cover_ids"]
        if not include_cover_ids:
            hydrated_group.pop("cover_ids")
        hydrated_group.update(
            {
                "members": members,
                "recommended": member_photos[group["recommended_id"]],
                "covers": [member_photos[photo_id] for photo_id in cover_ids],
            }
        )
        hydrated.append(hydrated_group)
    return hydrated


def build_similarity_groups(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    blink_detection_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Collapse active pair relations into deterministic connected photo groups."""
    topology = _build_similarity_topology(
        conn, profile, blink_detection_enabled
    )
    return _hydrate_similarity_groups(
        conn,
        topology,
        include_cover_ids=False,
        reuse_groups=True,
    )


@dataclass(frozen=True)
class _TopologyCacheEntry:
    topology: list[dict[str, Any]]
    index: dict[str, dict[str, Any]]


class SimilarityGroupCache:
    """Cache stable similarity topology while hydrating mutable photo rows."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _TopologyCacheEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _profile_fingerprint(
        profile: dict[str, Any], blink_detection_enabled: bool
    ) -> str:
        payload = json.dumps(
            {
                "profile": profile,
                "blink_detection_enabled": blink_detection_enabled,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _topology(
        self,
        project_id: str,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        blink_detection_enabled: bool = True,
    ) -> list[dict[str, Any]]:
        key = (
            project_id,
            self._profile_fingerprint(profile, blink_detection_enabled),
        )
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                return cached.topology
            topology = _build_similarity_topology(
                conn, profile, blink_detection_enabled
            )
            self._entries[key] = _TopologyCacheEntry(
                topology,
                {group["id"]: group for group in topology},
            )
            return topology

    def _key(
        self,
        project_id: str,
        profile: dict[str, Any],
        blink_detection_enabled: bool,
    ) -> tuple[str, str]:
        return (
            project_id,
            self._profile_fingerprint(profile, blink_detection_enabled),
        )

    def count(
        self,
        project_id: str,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        blink_detection_enabled: bool = True,
    ) -> int:
        return len(
            self._topology(
                project_id, conn, profile, blink_detection_enabled
            )
        )

    def get(
        self,
        project_id: str,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        blink_detection_enabled: bool = True,
    ) -> list[dict[str, Any]]:
        topology = self._topology(
            project_id, conn, profile, blink_detection_enabled
        )
        return self._hydrate(conn, topology)

    def get_page(
        self,
        project_id: str,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        blink_detection_enabled: bool = True,
        *,
        offset: int = 0,
        limit: int | None = None,
        participant_ids: set[int] | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        topology = self._topology(
            project_id, conn, profile, blink_detection_enabled
        )
        selected = (
            topology
            if participant_ids is None
            else [
                group
                for group in topology
                if participant_ids.intersection(group["member_ids"])
            ]
        )
        total = len(selected)
        stop = None if limit is None else offset + limit
        return total, self._hydrate(conn, selected[offset:stop])

    @staticmethod
    def _hydrate(
        conn: sqlite3.Connection,
        topology: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _hydrate_similarity_groups(
            conn,
            topology,
            include_cover_ids=True,
        )

    def get_one(
        self,
        project_id: str,
        group_id: str,
        conn: sqlite3.Connection,
        profile: dict[str, Any],
        blink_detection_enabled: bool = True,
    ) -> dict[str, Any] | None:
        self._topology(project_id, conn, profile, blink_detection_enabled)
        key = self._key(project_id, profile, blink_detection_enabled)
        with self._lock:
            entry = self._entries.get(key)
            group = entry.index.get(group_id) if entry is not None else None
        if group is None:
            return None
        hydrated = self._hydrate(conn, [group])
        return hydrated[0] if hydrated else None

    def invalidate(self, project_id: str) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] == project_id]
            for key in keys:
                del self._entries[key]


__all__ = [
    "SimilarityGroupCache",
    "_structure_similarity",
    "_structure_vector",
    "build_similarity_groups",
    "blink_recommendation_key",
    "filename_sequence",
    "hamming",
    "hamming_candidate_pairs",
    "image_structure",
    "parse_taken",
    "photo_shooting_key",
    "quality_score",
]
