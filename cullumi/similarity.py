from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count() if a and b else 64


def hamming_candidate_pairs(
    hashes: list[str], radius: float
) -> Iterator[tuple[int, int]]:
    """Vectorize the exact hash prefilter while keeping memory bounded."""
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
            for right in range(left + 1, len(hashes)):
                if hamming(hashes[left], hashes[right]) <= radius:
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
            for offset in np.flatnonzero(row_matches):
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
    sharpness = math.log1p(max(0, row["sharpness"] or 0)) / 10
    exposure = 1 - min(
        1.0,
        abs((row["luminance"] or 128) - 110) / 140
        + (row["dark_clip"] or 0)
        + (row["bright_clip"] or 0),
    )
    contrast = min(1.0, (row["contrast"] or 0) / 70)
    entropy = min(1.0, (row["entropy"] or 0) / 8)
    resolution = min(1.0, (row["megapixels"] or 0) / 12)
    return (
        sharpness * weights["sharpness"]
        + exposure * weights["exposure"]
        + contrast * weights["contrast"]
        + entropy * weights["entropy"]
        + resolution * weights["resolution"]
    )


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


def build_similarity_groups(
    conn: sqlite3.Connection, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Collapse active pair relations into deterministic connected photo groups."""
    rows = conn.execute(
        "SELECT * FROM photos WHERE status='active' AND error=''"
    ).fetchall()
    photos = {int(row["id"]): row for row in rows}
    edges = conn.execute(
        """SELECT sp.a_id,sp.b_id,sp.score,sp.kind,sp.face_safe
           FROM similar_pairs sp
           JOIN photos a ON a.id=sp.a_id
           JOIN photos b ON b.id=sp.b_id
           WHERE a.status='active' AND b.status='active'"""
    ).fetchall()

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
        left, right = int(edge["a_id"]), int(edge["b_id"])
        if left not in photos or right not in photos:
            continue
        union(left, right)
        score = float(edge["score"] or 0)
        adjacency.setdefault(left, []).append((right, score))
        adjacency.setdefault(right, []).append((left, score))
        if edge["face_safe"]:
            face_safe_ids.update((left, right))

    components: dict[int, list[int]] = {}
    for photo_id in parent:
        components.setdefault(find(photo_id), []).append(photo_id)

    minimum = max(2, int(profile.get("similarity", {}).get("min_group_size", 2)))
    groups: list[dict[str, Any]] = []
    for member_ids in components.values():
        if len(member_ids) < minimum:
            continue
        members = [photos[photo_id] for photo_id in member_ids]
        quality = {
            int(row["id"]): quality_score(row, profile)
            for row in members
        }
        shooting_keys = {
            int(row["id"]): photo_shooting_key(row)
            for row in members
        }
        ranked = sorted(
            members,
            key=lambda row: (
                -quality[int(row["id"])], str(row["relative_path"]).casefold()
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
            for neighbor, edge_score in adjacency.get(current, []):
                candidate = min(current_score, edge_score)
                if candidate > confidence.get(neighbor, 0.0):
                    confidence[neighbor] = candidate
                    heapq.heappush(pending, (-candidate, neighbor))

        hashes = {
            (str(row["sha256"]), str(row["motion_sha256"] or ""))
            for row in members if row["sha256"]
        }
        exact = (
            len(hashes) == 1
            and bool(hashes)
            and all(row["sha256"] for row in members)
        )
        ordered = sorted(members, key=lambda row: shooting_keys[int(row["id"])])
        stable_ids = ",".join(str(photo_id) for photo_id in sorted(member_ids))
        group_id = "sg-" + hashlib.sha1(stable_ids.encode("ascii")).hexdigest()[:16]
        groups.append(
            {
                "id": group_id,
                "member_ids": sorted(member_ids),
                "members": ordered,
                "recommended_id": recommended_id,
                "recommended": recommended,
                "covers": [
                    recommended,
                    *[row for row in ranked if row["id"] != recommended_id],
                ][:4],
                "confidence": confidence,
                "kind": "exact" if exact else "similar",
                "face_safe": any(photo_id in face_safe_ids for photo_id in member_ids),
                "sort_key": min(shooting_keys.values()),
            }
        )
    groups.sort(key=lambda group: (group["sort_key"], group["id"]))
    return groups


class SimilarityGroupCache:
    """Cache stable similarity topology while hydrating mutable photo rows."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _profile_fingerprint(profile: dict[str, Any]) -> str:
        payload = json.dumps(
            profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _topology(
        self, project_id: str, conn: sqlite3.Connection, profile: dict[str, Any]
    ) -> list[dict[str, Any]]:
        key = (project_id, self._profile_fingerprint(profile))
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                return cached
            groups = build_similarity_groups(conn, profile)
            topology = [
                {
                    "id": group["id"],
                    "member_ids": list(group["member_ids"]),
                    "recommended_id": int(group["recommended_id"]),
                    "cover_ids": [int(row["id"]) for row in group["covers"]],
                    "confidence": dict(group["confidence"]),
                    "kind": group["kind"],
                    "face_safe": bool(group["face_safe"]),
                    "sort_key": group["sort_key"],
                }
                for group in groups
            ]
            self._entries[key] = topology
            return topology

    def count(
        self, project_id: str, conn: sqlite3.Connection, profile: dict[str, Any]
    ) -> int:
        return len(self._topology(project_id, conn, profile))

    def get(
        self, project_id: str, conn: sqlite3.Connection, profile: dict[str, Any]
    ) -> list[dict[str, Any]]:
        topology = self._topology(project_id, conn, profile)
        photo_ids = sorted(
            {photo_id for group in topology for photo_id in group["member_ids"]}
        )
        if not photo_ids:
            return []
        rows: list[sqlite3.Row] = []
        for offset in range(0, len(photo_ids), 900):
            chunk = photo_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                conn.execute(
                    f"SELECT * FROM photos WHERE id IN ({placeholders}) AND status='active' AND error=''",
                    chunk,
                ).fetchall()
            )
        photos = {int(row["id"]): row for row in rows}
        hydrated: list[dict[str, Any]] = []
        for group in topology:
            if any(photo_id not in photos for photo_id in group["member_ids"]):
                continue
            members = [photos[photo_id] for photo_id in group["member_ids"]]
            members.sort(key=photo_shooting_key)
            recommended = photos[group["recommended_id"]]
            hydrated.append(
                {
                    **group,
                    "members": members,
                    "recommended": recommended,
                    "covers": [photos[photo_id] for photo_id in group["cover_ids"]],
                }
            )
        return hydrated

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
    "filename_sequence",
    "hamming",
    "hamming_candidate_pairs",
    "image_structure",
    "parse_taken",
    "photo_shooting_key",
    "quality_score",
]
