from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .media import HEIF_EXTENSIONS, IMAGE_EXTENSIONS, RAW_EXTENSIONS

FORMAT_CATEGORY_ORDER = ("raw", "jpeg", "heif", "png", "other")
FORMAT_CATEGORY_IDS = frozenset(FORMAT_CATEGORY_ORDER)
FORMAT_CATEGORY_LABELS = {
    "raw": "RAW",
    "jpeg": "JPEG",
    "heif": "HEIF",
    "png": "PNG",
    "other": "其他",
}
JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})
PNG_EXTENSIONS = frozenset({".png"})
OTHER_IMAGE_EXTENSIONS = frozenset(
    IMAGE_EXTENSIONS - RAW_EXTENSIONS - HEIF_EXTENSIONS - JPEG_EXTENSIONS - PNG_EXTENSIONS
)
FORMAT_EXTENSIONS = {
    "raw": frozenset(RAW_EXTENSIONS),
    "jpeg": JPEG_EXTENSIONS,
    "heif": frozenset(HEIF_EXTENSIONS),
    "png": PNG_EXTENSIONS,
    "other": OTHER_IMAGE_EXTENSIONS,
}
SQLITE_PARAMETER_BATCH = 500


def normalize_extension(extension: Any, relative_path: Any = "") -> str:
    value = str(extension or "").strip().lower()
    if value and not value.startswith("."):
        value = f".{value}"
    if not value and relative_path:
        value = Path(str(relative_path)).suffix.lower()
    return value


def format_category(extension: Any, relative_path: Any = "") -> str:
    value = normalize_extension(extension, relative_path)
    if value in RAW_EXTENSIONS:
        return "raw"
    if value in JPEG_EXTENSIONS:
        return "jpeg"
    if value in HEIF_EXTENSIONS:
        return "heif"
    if value in PNG_EXTENSIONS:
        return "png"
    return "other"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _capture_key(row: Any) -> tuple[str, str]:
    relative = Path(str(_row_value(row, "relative_path", "")))
    return str(relative.parent).casefold(), relative.stem.casefold()


def _representative_sort_key(row: Any) -> tuple[Any, ...]:
    extension = normalize_extension(
        _row_value(row, "extension"), _row_value(row, "relative_path")
    )
    is_raw = extension in RAW_EXTENSIONS
    readable = not bool(str(_row_value(row, "error", "") or ""))
    tier = (
        3
        if readable and not is_raw
        else 2
        if readable and is_raw
        else 1
        if not is_raw
        else 0
    )
    width = int(_row_value(row, "width", 0) or 0)
    height = int(_row_value(row, "height", 0) or 0)
    megapixels = float(_row_value(row, "megapixels", 0) or 0)
    pixels = max(width * height, int(megapixels * 1_000_000))
    size = int(_row_value(row, "size", 0) or 0)
    relative_path = str(_row_value(row, "relative_path", ""))
    photo_id = int(_row_value(row, "id", 0) or 0)
    return (-tier, -pixels, -size, relative_path.casefold(), relative_path, photo_id)


def build_capture_variant_memberships(rows: Iterable[Any]) -> dict[int, int]:
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        extension = normalize_extension(
            _row_value(row, "extension"), _row_value(row, "relative_path")
        )
        if extension not in IMAGE_EXTENSIONS:
            continue
        grouped[_capture_key(row)].append(row)

    memberships: dict[int, int] = {}
    for members in grouped.values():
        extensions = {
            normalize_extension(
                _row_value(row, "extension"), _row_value(row, "relative_path")
            )
            for row in members
        }
        if not extensions.intersection(RAW_EXTENSIONS) or not (
            extensions - RAW_EXTENSIONS
        ):
            continue
        representative = min(members, key=_representative_sort_key)
        representative_id = int(_row_value(representative, "id"))
        memberships.update(
            (int(_row_value(row, "id")), representative_id) for row in members
        )
    return memberships


def rebuild_capture_variants(
    conn: sqlite3.Connection, *, prune_similar: bool = False
) -> dict[int, int]:
    rows = conn.execute(
        """SELECT id,relative_path,extension,error,width,height,megapixels,size
             FROM photos WHERE status='active'"""
    ).fetchall()
    memberships = build_capture_variant_memberships(rows)
    conn.execute("DELETE FROM capture_variant_members")
    conn.executemany(
        """INSERT INTO capture_variant_members(photo_id,representative_id)
           VALUES(?,?)""",
        sorted(memberships.items()),
    )
    if prune_similar:
        conn.execute(
            """DELETE FROM similar_pairs
                 WHERE kind='similar' AND (
                   a_id IN (
                     SELECT photo_id FROM capture_variant_members
                      WHERE photo_id<>representative_id
                   ) OR b_id IN (
                     SELECT photo_id FROM capture_variant_members
                      WHERE photo_id<>representative_id
                   )
                 )"""
        )
    return memberships


def variant_memberships(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        int(row["photo_id"]): int(row["representative_id"])
        for row in conn.execute(
            "SELECT photo_id,representative_id FROM capture_variant_members"
        )
    }


def representative_photo_ids(
    conn: sqlite3.Connection, photo_ids: Iterable[int]
) -> set[int]:
    requested = {int(photo_id) for photo_id in photo_ids}
    if not requested:
        return set()
    mapped: dict[int, int] = {}
    ordered = sorted(requested)
    for offset in range(0, len(ordered), SQLITE_PARAMETER_BATCH):
        batch = ordered[offset : offset + SQLITE_PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        mapped.update(
            (int(row["photo_id"]), int(row["representative_id"]))
            for row in conn.execute(
                f"""SELECT photo_id,representative_id
                       FROM capture_variant_members
                      WHERE photo_id IN ({placeholders})""",
                batch,
            )
        )
    return {mapped.get(photo_id, photo_id) for photo_id in requested}


def active_variant_photo_ids(
    conn: sqlite3.Connection, photo_id: int, sync_variants: bool
) -> list[int]:
    photo_id = int(photo_id)
    if not sync_variants:
        return [photo_id]
    membership = conn.execute(
        """SELECT representative_id FROM capture_variant_members
            WHERE photo_id=?""",
        (photo_id,),
    ).fetchone()
    if not membership:
        return [photo_id]
    return [
        int(row["photo_id"])
        for row in conn.execute(
            """SELECT cv.photo_id
                 FROM capture_variant_members cv
                 JOIN photos p ON p.id=cv.photo_id
                WHERE cv.representative_id=? AND p.status='active'
                ORDER BY cv.photo_id""",
            (int(membership["representative_id"]),),
        )
    ]


def active_variant_rows(
    conn: sqlite3.Connection, photo_ids: Iterable[int]
) -> dict[int, list[sqlite3.Row]]:
    requested = list(dict.fromkeys(int(photo_id) for photo_id in photo_ids))
    if not requested:
        return {}
    memberships: dict[int, int] = {}
    for offset in range(0, len(requested), SQLITE_PARAMETER_BATCH):
        batch = requested[offset : offset + SQLITE_PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        memberships.update(
            (int(row["photo_id"]), int(row["representative_id"]))
            for row in conn.execute(
                f"""SELECT photo_id,representative_id
                       FROM capture_variant_members
                      WHERE photo_id IN ({placeholders})""",
                batch,
            )
        )
    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    representative_ids = sorted(set(memberships.values()))
    for offset in range(0, len(representative_ids), SQLITE_PARAMETER_BATCH):
        batch = representative_ids[offset : offset + SQLITE_PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""SELECT p.*,
                       cv.representative_id capture_representative_id
                   FROM capture_variant_members cv
                   JOIN photos p ON p.id=cv.photo_id
                  WHERE cv.representative_id IN ({placeholders})
                    AND p.status='active'
                  ORDER BY cv.representative_id,
                           CASE WHEN cv.photo_id=cv.representative_id
                                THEN 0 ELSE 1 END,
                           p.relative_path COLLATE NOCASE,p.id""",
            batch,
        ):
            groups[int(row["capture_representative_id"])].append(row)
    return {
        photo_id: groups.get(representative_id, [])
        for photo_id, representative_id in memberships.items()
    }


def active_variant_groups(
    conn: sqlite3.Connection,
) -> tuple[dict[int, int], dict[int, list[sqlite3.Row]]]:
    memberships: dict[int, int] = {}
    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """SELECT cv.photo_id,cv.representative_id,p.decision
             FROM capture_variant_members cv
             JOIN photos p ON p.id=cv.photo_id
            WHERE p.status='active'
            ORDER BY cv.representative_id,cv.photo_id"""
    ):
        photo_id = int(row["photo_id"])
        representative_id = int(row["representative_id"])
        memberships[photo_id] = representative_id
        groups[representative_id].append(row)
    return memberships, dict(groups)


def _display_extension(extension: Any, relative_path: Any = "") -> str:
    value = normalize_extension(extension, relative_path)
    return value.removeprefix(".").upper()


def _display_extension_key(value: str) -> tuple[int, str]:
    category = format_category(value)
    return FORMAT_CATEGORY_ORDER.index(category), value


def variant_metadata(
    conn: sqlite3.Connection, photo_ids: Iterable[int]
) -> dict[int, list[str]]:
    requested = list(dict.fromkeys(int(photo_id) for photo_id in photo_ids))
    if not requested:
        return {}
    memberships: dict[int, int] = {}
    for offset in range(0, len(requested), SQLITE_PARAMETER_BATCH):
        batch = requested[offset : offset + SQLITE_PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        memberships.update(
            (int(row["photo_id"]), int(row["representative_id"]))
            for row in conn.execute(
                f"""SELECT photo_id,representative_id
                       FROM capture_variant_members
                      WHERE photo_id IN ({placeholders})""",
                batch,
            )
        )
    representative_ids = sorted(set(memberships.values()))
    extensions: dict[int, set[str]] = defaultdict(set)
    for offset in range(0, len(representative_ids), SQLITE_PARAMETER_BATCH):
        batch = representative_ids[offset : offset + SQLITE_PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""SELECT cv.representative_id,p.extension,p.relative_path
                   FROM capture_variant_members cv
                   JOIN photos p ON p.id=cv.photo_id
                  WHERE cv.representative_id IN ({placeholders})
                    AND p.status='active'""",
            batch,
        ):
            value = _display_extension(row["extension"], row["relative_path"])
            if value:
                extensions[int(row["representative_id"])].add(value)
    return {
        photo_id: sorted(
            extensions.get(representative_id, set()), key=_display_extension_key
        )
        for photo_id, representative_id in memberships.items()
    }


def format_category_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = dict.fromkeys(FORMAT_CATEGORY_ORDER, 0)
    for row in conn.execute(
        """SELECT extension,MIN(relative_path) relative_path,COUNT(*) count
             FROM photos WHERE status='active'
             GROUP BY LOWER(COALESCE(extension,''))"""
    ):
        category = format_category(row["extension"], row["relative_path"])
        counts[category] += int(row["count"] or 0)
    return [
        {
            "id": category,
            "label": FORMAT_CATEGORY_LABELS[category],
            "count": counts[category],
        }
        for category in FORMAT_CATEGORY_ORDER
        if counts[category]
    ]


def format_filter_clause(categories: set[str]) -> tuple[str, list[str]]:
    if categories == FORMAT_CATEGORY_IDS:
        return "", []
    extensions = sorted(
        extension
        for category in categories
        for extension in FORMAT_EXTENSIONS[category]
    )
    if not extensions:
        return "0", []
    placeholders = ",".join("?" for _ in extensions)
    return f"LOWER(COALESCE(extension,'')) IN ({placeholders})", extensions


__all__ = [
    "FORMAT_CATEGORY_IDS",
    "FORMAT_CATEGORY_LABELS",
    "FORMAT_CATEGORY_ORDER",
    "active_variant_groups",
    "active_variant_photo_ids",
    "active_variant_rows",
    "build_capture_variant_memberships",
    "format_category",
    "format_category_counts",
    "format_filter_clause",
    "normalize_extension",
    "rebuild_capture_variants",
    "representative_photo_ids",
    "variant_memberships",
    "variant_metadata",
]
