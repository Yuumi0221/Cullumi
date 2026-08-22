from __future__ import annotations

import copy
import hashlib
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .fs_utils import is_within

DATABASE_SCHEMA_VERSION = 4
_WAL_CONFIGURED_DATABASES: dict[Path, tuple[int, int]] = {}
_INITIALIZED_DATABASES: dict[Path, tuple[int, int, int, int]] = {}
_DATABASE_CONFIGURATION_LOCK = threading.RLock()


def _database_identity(path: Path) -> tuple[int, int]:
    file_stat = path.stat()
    return int(file_stat.st_dev), int(file_stat.st_ino)


def _database_fingerprint(path: Path) -> tuple[int, int, int, int]:
    """Identify both file replacement and writes that may change the schema version."""
    file_stat = path.stat()
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
    )


def _ensure_wal(
    conn: sqlite3.Connection, path: Path, initialized: bool
) -> bool:
    """Enable persistent WAL once for each database file identity."""
    identity = _database_identity(path)
    with _DATABASE_CONFIGURATION_LOCK:
        if not initialized and _WAL_CONFIGURED_DATABASES.get(path) == identity:
            return False
        conn.execute("PRAGMA journal_mode=WAL")
        _WAL_CONFIGURED_DATABASES[path] = identity
        return True


def safe_relative_path(root: Path, relative_path: str, label: str = "文件路径") -> Path:
    """Resolve an untrusted relative path and keep it inside ``root``."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"{label}为空")
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError(f"{label}必须是相对路径")
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    if target == resolved_root or not is_within(target, resolved_root):
        raise ValueError(f"{label}超出项目目录")
    return target


def project_id_for(root: Path) -> str:
    return hashlib.sha1(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


DATABASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project (
  id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL, profile_id TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path TEXT NOT NULL UNIQUE, extension TEXT, size INTEGER, mtime REAL,
  width INTEGER, height INTEGER, megapixels REAL, taken TEXT,
  luminance REAL, contrast REAL, dark_clip REAL, bright_clip REAL,
  sharpness REAL, entropy REAL, phash TEXT, dhash TEXT, sha256 TEXT,
  thumbnail TEXT, error TEXT DEFAULT '', suggestion TEXT DEFAULT 'keep',
  reason TEXT DEFAULT '', decision TEXT DEFAULT '', status TEXT DEFAULT 'active',
  analyzed_at TEXT,
  media_type TEXT NOT NULL DEFAULT 'image',
  motion_kind TEXT NOT NULL DEFAULT '', motion_relative_path TEXT NOT NULL DEFAULT '',
  motion_offset INTEGER NOT NULL DEFAULT 0, motion_length INTEGER NOT NULL DEFAULT 0,
  motion_size INTEGER NOT NULL DEFAULT 0, motion_mtime REAL NOT NULL DEFAULT 0,
  motion_asset_id TEXT NOT NULL DEFAULT '', motion_error TEXT NOT NULL DEFAULT '',
  motion_duration_ms INTEGER NOT NULL DEFAULT 0, motion_fps REAL NOT NULL DEFAULT 0,
  motion_frame_count INTEGER NOT NULL DEFAULT 0,
  motion_width INTEGER NOT NULL DEFAULT 0, motion_height INTEGER NOT NULL DEFAULT 0,
  motion_sha256 TEXT NOT NULL DEFAULT '', motion_still_time_ms INTEGER NOT NULL DEFAULT -1,
  cover_source TEXT NOT NULL DEFAULT 'still', cover_time_ms INTEGER NOT NULL DEFAULT 0,
  cover_frame_index INTEGER NOT NULL DEFAULT 0, cover_revision INTEGER NOT NULL DEFAULT 0,
  quality_score REAL NOT NULL DEFAULT 0,
  blink_status TEXT NOT NULL DEFAULT 'not_analyzed',
  blink_face_count INTEGER NOT NULL DEFAULT 0,
  blink_closed_face_count INTEGER NOT NULL DEFAULT 0,
  blink_uncertain_face_count INTEGER NOT NULL DEFAULT 0,
  blink_closed_ratio REAL NOT NULL DEFAULT -1,
  blink_confidence REAL NOT NULL DEFAULT 0,
  blink_model_version TEXT NOT NULL DEFAULT '',
  blink_input_fingerprint TEXT NOT NULL DEFAULT '',
  blink_analyzed_at TEXT NOT NULL DEFAULT '',
  blink_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_photos_suggestion ON photos(suggestion);
CREATE INDEX IF NOT EXISTS idx_photos_decision ON photos(decision);
CREATE INDEX IF NOT EXISTS idx_photos_status_decision ON photos(status,decision);
CREATE INDEX IF NOT EXISTS idx_photos_status_error_size ON photos(status,error,size);
CREATE TABLE IF NOT EXISTS similar_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,
  score REAL, kind TEXT, recommended_id INTEGER, face_safe INTEGER DEFAULT 0,
  UNIQUE(a_id,b_id)
);
CREATE TABLE IF NOT EXISTS quarantine_batches (
  id TEXT PRIMARY KEY, created_at TEXT, manifest_path TEXT, count INTEGER,
  total_size INTEGER, restored_at TEXT DEFAULT ''
);
"""

PHOTO_SCHEMA_COLUMNS = {
    "media_type": "TEXT NOT NULL DEFAULT 'image'",
    "motion_kind": "TEXT NOT NULL DEFAULT ''",
    "motion_relative_path": "TEXT NOT NULL DEFAULT ''",
    "motion_offset": "INTEGER NOT NULL DEFAULT 0",
    "motion_length": "INTEGER NOT NULL DEFAULT 0",
    "motion_size": "INTEGER NOT NULL DEFAULT 0",
    "motion_mtime": "REAL NOT NULL DEFAULT 0",
    "motion_asset_id": "TEXT NOT NULL DEFAULT ''",
    "motion_error": "TEXT NOT NULL DEFAULT ''",
    "motion_duration_ms": "INTEGER NOT NULL DEFAULT 0",
    "motion_fps": "REAL NOT NULL DEFAULT 0",
    "motion_frame_count": "INTEGER NOT NULL DEFAULT 0",
    "motion_width": "INTEGER NOT NULL DEFAULT 0",
    "motion_height": "INTEGER NOT NULL DEFAULT 0",
    "motion_sha256": "TEXT NOT NULL DEFAULT ''",
    "motion_still_time_ms": "INTEGER NOT NULL DEFAULT -1",
    "cover_source": "TEXT NOT NULL DEFAULT 'still'",
    "cover_time_ms": "INTEGER NOT NULL DEFAULT 0",
    "cover_frame_index": "INTEGER NOT NULL DEFAULT 0",
    "cover_revision": "INTEGER NOT NULL DEFAULT 0",
    "quality_score": "REAL NOT NULL DEFAULT 0",
    "blink_status": "TEXT NOT NULL DEFAULT 'not_analyzed'",
    "blink_face_count": "INTEGER NOT NULL DEFAULT 0",
    "blink_closed_face_count": "INTEGER NOT NULL DEFAULT 0",
    "blink_uncertain_face_count": "INTEGER NOT NULL DEFAULT 0",
    "blink_closed_ratio": "REAL NOT NULL DEFAULT -1",
    "blink_confidence": "REAL NOT NULL DEFAULT 0",
    "blink_model_version": "TEXT NOT NULL DEFAULT ''",
    "blink_input_fingerprint": "TEXT NOT NULL DEFAULT ''",
    "blink_analyzed_at": "TEXT NOT NULL DEFAULT ''",
    "blink_error": "TEXT NOT NULL DEFAULT ''",
}


def _database_has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"""
    ).fetchone() is not None


def _backup_database_for_migration(
    conn: sqlite3.Connection, path: Path, target_version: int
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(
        f"{path.stem}.pre-v{target_version}-{stamp}-{uuid.uuid4().hex[:8]}{path.suffix}"
    )
    backup_conn: sqlite3.Connection | None = None
    try:
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.commit()
    except Exception as error:
        if backup_conn is not None:
            backup_conn.close()
            backup_conn = None
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("无法创建数据库升级备份，已停止升级") from error
    finally:
        if backup_conn is not None:
            backup_conn.close()
    return backup_path


def _initialize_database(
    conn: sqlite3.Connection, path: Path, existed: bool
) -> bool:
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > DATABASE_SCHEMA_VERSION:
        raise RuntimeError(
            f"项目数据库版本 {current_version} 高于当前支持的 {DATABASE_SCHEMA_VERSION}，"
            "请使用更新版本的 Cullumi 打开"
        )
    if current_version == DATABASE_SCHEMA_VERSION:
        return False
    if existed and _database_has_user_tables(conn):
        _backup_database_for_migration(conn, path, current_version + 1)
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(photos)").fetchall()
    }
    additions = ""
    if existing_columns:
        additions = "\n".join(
            f"ALTER TABLE photos ADD COLUMN {name} {declaration};"
            for name, declaration in PHOTO_SCHEMA_COLUMNS.items()
            if name not in existing_columns
        )
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + DATABASE_SCHEMA_SQL
            + "\n"
            + additions
            + f"\nPRAGMA user_version={DATABASE_SCHEMA_VERSION};\nCOMMIT;"
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return True


def connect_db(path: Path) -> sqlite3.Connection:
    resolved_path = Path(os.path.abspath(path))
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved_path, timeout=30, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        with _DATABASE_CONFIGURATION_LOCK:
            fingerprint = _database_fingerprint(resolved_path)
            existed = fingerprint[2] > 0
            if not existed or _INITIALIZED_DATABASES.get(resolved_path) != fingerprint:
                initialized = _initialize_database(conn, resolved_path, existed)
                _ensure_wal(conn, resolved_path, initialized)
                conn.commit()
                _INITIALIZED_DATABASES[resolved_path] = _database_fingerprint(
                    resolved_path
                )
        return conn
    except Exception:
        conn.close()
        raise


@dataclass
class Project:
    project_id: str
    root: Path
    cache_root: Path
    project_dir: Path
    db_path: Path
    thumb_dir: Path
    profile_id: str
    motion_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.motion_dir is None:
            self.motion_dir = self.project_dir / "motion"


def project_thumbnail_path(project: Project, stored_path: str | Path) -> Path:
    """Resolve relative or legacy absolute thumbnail paths in the current cache."""
    raw = str(stored_path or "").strip()
    if not raw:
        return Path()
    return project.thumb_dir / Path(raw).name


def project_thumbnail_storage_path(path: str | Path) -> str:
    """Store thumbnails relocatably while accepting legacy absolute inputs."""
    raw = str(path or "").strip()
    return (Path("thumbs") / Path(raw).name).as_posix() if raw else ""


def _rewrite_project_thumbnail_paths(project: Project) -> None:
    """Persist the new cache location in a copied project database."""
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute(
            "SELECT id,thumbnail FROM photos WHERE thumbnail<>''"
        ).fetchall()
        updates = [
            (project_thumbnail_storage_path(row["thumbnail"]), row["id"])
            for row in rows
        ]
        if updates:
            conn.executemany("UPDATE photos SET thumbnail=? WHERE id=?", updates)
            conn.commit()
        integrity = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if integrity != ["ok"]:
            raise RuntimeError("迁移后的项目数据库完整性校验失败")


_ACTIVE_DATABASE_FILES = {"project.db", "project.db-wal", "project.db-shm"}


def _is_regenerable_preview(path: Path) -> bool:
    return (
        ".display-" in path.name
        or ".motion-cover-" in path.name
        or path.suffix.lower() == ".webm"
        or path.name.endswith(".tmp")
    )


def _migration_file_manifest(root: Path) -> dict[Path, int]:
    manifest: dict[Path, int] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if len(relative.parts) == 1 and relative.name in _ACTIVE_DATABASE_FILES:
            continue
        if _is_regenerable_preview(relative):
            continue
        manifest[relative] = path.stat().st_size
    return manifest


def _copy_project_cache_without_live_database(source: Path, target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> list[str]:
        relative_dir = Path(directory).relative_to(source)
        ignored = [name for name in names if _is_regenerable_preview(Path(name))]
        if not relative_dir.parts:
            ignored.extend(name for name in names if name in _ACTIVE_DATABASE_FILES)
        return list(dict.fromkeys(ignored))

    shutil.copytree(source, target, ignore=ignore)


def _backup_project_database(source: Path, target: Path) -> None:
    with closing(connect_db(source)) as source_conn:
        with closing(sqlite3.connect(target)) as target_conn:
            source_conn.backup(target_conn)
            target_conn.commit()


class ProjectManager:
    def __init__(self, config: Any):
        self.config = config
        self._data_locks_guard = threading.Lock()
        self._data_locks: dict[str, threading.RLock] = {}

    def _data_lock(self, project_id: str) -> threading.RLock:
        with self._data_locks_guard:
            return self._data_locks.setdefault(project_id, threading.RLock())

    @contextmanager
    def data_operation(self, project_id: str):
        """Serialize writes that must not cross a project cache migration."""
        with self._data_lock(project_id):
            yield

    def open(self, root: str, cache_root: str | None = None) -> Project:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise ValueError("照片文件夹不存在")
        pid = project_id_for(root_path)
        config_data = self.config.snapshot()
        existing = config_data.get("projects", {}).get(pid, {})
        default_cache_root = config_data["default_cache_root"]
        cache = Path(cache_root or existing.get("cache_root") or default_cache_root).resolve()
        project_dir = cache / pid
        overlaps_photos = is_within(project_dir, root_path) or is_within(root_path, project_dir)
        existing_cache = Path(existing["cache_root"]).resolve() if existing.get("cache_root") else None
        if overlaps_photos and existing_cache != cache:
            raise ValueError("缓存位置不能与照片文件夹重叠")
        project_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = project_dir / "thumbs"
        thumb_dir.mkdir(exist_ok=True)
        motion_dir = project_dir / "motion"
        motion_dir.mkdir(exist_ok=True)
        profile_id = existing.get("profile_id", "conservative")
        project = Project(
            pid, root_path, cache, project_dir, project_dir / "project.db",
            thumb_dir, profile_id, motion_dir,
        )
        now = datetime.now().isoformat(timespec="seconds")
        with closing(connect_db(project.db_path)) as conn:
            conn.execute(
                """INSERT INTO project(id,root,profile_id,created_at,updated_at) VALUES(1,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET root=excluded.root,profile_id=excluded.profile_id,updated_at=excluded.updated_at""",
                (str(root_path), profile_id, now, now),
            )
            conn.commit()
        with self.config.edit() as data:
            stored = data.setdefault("projects", {}).setdefault(pid, {})
            stored.update({
                "root": str(root_path), "cache_root": str(cache), "profile_id": profile_id,
                "last_opened": now,
            })
            recent = [pid] + [x for x in data.get("recent_projects", []) if x != pid]
            data["recent_projects"] = recent[:12]
        return project

    def from_id(self, project_id: str) -> Project:
        config_data = self.config.snapshot()
        data = config_data.get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        root = Path(data["root"]).resolve()
        if not root.is_dir():
            raise ValueError("照片文件夹当前不可用")
        if project_id_for(root) != project_id:
            raise ValueError("项目标识与照片目录不匹配")
        cache_root = Path(
            data.get("cache_root") or config_data["default_cache_root"]
        ).resolve()
        project_dir = cache_root / project_id
        return Project(
            project_id,
            root,
            cache_root,
            project_dir,
            project_dir / "project.db",
            project_dir / "thumbs",
            data.get("profile_id", "conservative"),
            project_dir / "motion",
        )

    def project_root(self, project_id: str) -> Path:
        data = self.config.snapshot().get("projects", {}).get(project_id)
        if not data:
            raise ValueError("项目不存在")
        root = Path(data["root"]).resolve()
        if not root.is_dir():
            raise ValueError("照片文件夹当前不可用")
        return root

    def remove_from_recent(self, project_id: str, delete_cache: bool = False) -> dict[str, Any]:
        with self.config.lock:
            data = copy.deepcopy(self.config.data.get("projects", {}).get(project_id))
        if not data:
            raise ValueError("项目不存在")
        deleted_paths: list[str] = []
        if delete_cache:
            root = Path(data["root"]).resolve()
            if project_id_for(root) != project_id:
                raise ValueError("项目标识与照片目录不匹配，已停止删除")
            cache_root = Path(data["cache_root"]).resolve()
            targets = [(cache_root / project_id).resolve()]
            targets.extend(Path(item).resolve() for item in data.get("old_caches", []))
            unique_targets = list(dict.fromkeys(targets))
            for target in unique_targets:
                if target.name.casefold() != project_id.casefold():
                    raise ValueError("项目缓存目录校验失败，已停止删除")
            for target in unique_targets:
                if target.exists():
                    shutil.rmtree(target)
                    deleted_paths.append(str(target))
        with self.config.edit() as config_data:
            config_data["recent_projects"] = [
                item for item in config_data.get("recent_projects", []) if item != project_id
            ]
            if delete_cache:
                config_data.get("projects", {}).pop(project_id, None)
        return {"removed": True, "cache_deleted": delete_cache, "deleted_paths": deleted_paths}

    def migrate_cache(self, project_id: str, new_root: str) -> dict[str, Any]:
        with self.data_operation(project_id):
            return self._migrate_cache_locked(project_id, new_root)

    def _migrate_cache_locked(self, project_id: str, new_root: str) -> dict[str, Any]:
        project = self.from_id(project_id)
        new_cache = Path(new_root).resolve()
        new_dir = new_cache / project_id
        if new_dir == project.project_dir:
            return {
                "changed": False,
                "path": str(new_dir),
                "cache_root": str(new_cache),
            }
        if is_within(new_dir, project.root) or is_within(project.root, new_dir):
            raise ValueError("新缓存位置不能与照片文件夹重叠")
        if new_dir.exists() and any(new_dir.iterdir()):
            raise ValueError("新位置已有同名项目缓存")
        temp = new_dir.with_name(new_dir.name + ".migrating")
        if temp.exists():
            shutil.rmtree(temp)
        temp.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_manifest = _migration_file_manifest(project.project_dir)
            _copy_project_cache_without_live_database(project.project_dir, temp)
            _backup_project_database(project.db_path, temp / "project.db")
            copied_manifest = _migration_file_manifest(temp)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        if source_manifest != copied_manifest:
            shutil.rmtree(temp, ignore_errors=True)
            raise RuntimeError("缓存迁移文件校验失败，原位置保持不变")
        migrated_project = Project(
            project.project_id,
            project.root,
            new_cache,
            new_dir,
            temp / "project.db",
            new_dir / "thumbs",
            project.profile_id,
            new_dir / "motion",
        )
        destination_was_empty = new_dir.exists()
        installed = False
        try:
            _rewrite_project_thumbnail_paths(migrated_project)
            if new_dir.exists():
                new_dir.rmdir()
            temp.rename(new_dir)
            installed = True
            with self.config.edit() as data:
                stored = data["projects"][project_id]
                stored["cache_root"] = str(new_cache)
                old_caches = stored.setdefault("old_caches", [])
                if str(project.project_dir) not in old_caches:
                    old_caches.append(str(project.project_dir))
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            if installed:
                shutil.rmtree(new_dir, ignore_errors=True)
                if destination_was_empty:
                    new_dir.mkdir(parents=True, exist_ok=True)
            raise
        return {
            "changed": True,
            "path": str(new_dir),
            "cache_root": str(new_cache),
            "old_cache": str(project.project_dir),
        }

    def cleanup_old_cache(self, project_id: str, path: str) -> dict[str, Any]:
        with self.config.lock:
            data = copy.deepcopy(self.config.data.get("projects", {}).get(project_id))
        if not data:
            raise ValueError("项目不存在")
        target = Path(path).resolve()
        allowed = [Path(item).resolve() for item in data.get("old_caches", [])]
        if target not in allowed:
            raise ValueError("该目录不在此项目的待清理旧缓存列表中")
        current = Path(data["cache_root"]).resolve() / project_id
        if target == current or target.name != project_id:
            raise ValueError("不能清理当前项目缓存")
        if target.exists():
            shutil.rmtree(target)
        with self.config.edit() as config_data:
            stored = config_data["projects"][project_id]
            stored["old_caches"] = [
                item for item in stored.get("old_caches", [])
                if Path(item).resolve() != target
            ]
        return {"cleaned": True, "path": str(target)}


