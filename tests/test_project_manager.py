from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import cullumi.core as core
from cullumi.core import (
    ConfigStore,
    Project,
    ProjectManager,
    connect_db,
    project_thumbnail_path,
    project_thumbnail_storage_path,
)


class ProjectManagerReliabilityTests(unittest.TestCase):
    def test_reopening_project_preserves_pending_old_cache_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))

            migration = manager.migrate_cache(project.project_id, str(root / "cache-b"))
            manager.open(str(photos))

            stored = config.snapshot()["projects"][project.project_id]
            self.assertEqual(stored["old_caches"], [migration["old_cache"]])
            self.assertEqual(stored["cache_root"], str((root / "cache-b").resolve()))

    def test_configuration_snapshot_cannot_mutate_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ConfigStore(Path(temporary) / "config.json")
            snapshot = config.snapshot()
            snapshot["projects"]["injected"] = {"root": "outside"}

            self.assertNotIn("injected", config.data["projects"])

    def test_legacy_thumbnail_path_resolves_inside_current_cache(self) -> None:
        root = Path("D:/photos")
        project = Project(
            "project-id",
            root,
            Path("E:/new-cache"),
            Path("E:/new-cache/project-id"),
            Path("E:/new-cache/project-id/project.db"),
            Path("E:/new-cache/project-id/thumbs"),
            "conservative",
        )

        resolved = project_thumbnail_path(
            project, Path("C:/old-cache/project-id/thumbs/cover.jpg")
        )

        self.assertEqual(resolved, project.thumb_dir / "cover.jpg")
        self.assertEqual(
            project_thumbnail_storage_path(resolved), "thumbs/cover.jpg"
        )

    def test_cache_migration_uses_sqlite_backup_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))

            with mock.patch.object(
                core, "_backup_project_database", wraps=core._backup_project_database
            ) as backup:
                manager.migrate_cache(project.project_id, str(root / "cache-b"))

            backup.assert_called_once()

    def test_cache_migration_captures_committed_wal_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            source_conn = connect_db(project.db_path)
            try:
                source_conn.execute(
                    """INSERT INTO photos(relative_path,error,status,decision)
                       VALUES('committed.jpg','','active','keep')"""
                )
                source_conn.commit()
                manager.migrate_cache(project.project_id, str(root / "cache-b"))
                migrated = manager.from_id(project.project_id)
                target_conn = connect_db(migrated.db_path)
                try:
                    row = target_conn.execute(
                        "SELECT decision FROM photos WHERE relative_path='committed.jpg'"
                    ).fetchone()
                finally:
                    target_conn.close()
            finally:
                source_conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["decision"], "keep")

    def test_cache_migration_size_mismatch_keeps_original_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            (project.thumb_dir / "cover.jpg").write_bytes(b"thumbnail")
            original_manifest = core._migration_file_manifest

            def mismatched_manifest(path: Path) -> dict[Path, int]:
                manifest = original_manifest(path)
                if path.name.endswith(".migrating") and manifest:
                    first = next(iter(manifest))
                    manifest[first] += 1
                return manifest

            target = root / "cache-b"
            with mock.patch.object(
                core, "_migration_file_manifest", side_effect=mismatched_manifest
            ):
                with self.assertRaisesRegex(RuntimeError, "文件校验失败"):
                    manager.migrate_cache(project.project_id, str(target))

            stored = config.snapshot()["projects"][project.project_id]
            self.assertEqual(stored["cache_root"], str((root / "cache-a").resolve()))
            self.assertTrue(project.db_path.is_file())
            self.assertFalse((target / project.project_id).exists())
            self.assertFalse((target / f"{project.project_id}.migrating").exists())

    def test_project_data_write_blocks_cache_migration_until_it_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            started = threading.Event()
            finished = threading.Event()
            errors: list[Exception] = []

            def migrate() -> None:
                started.set()
                try:
                    manager.migrate_cache(project.project_id, str(root / "cache-b"))
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()

            with manager.data_operation(project.project_id):
                thread = threading.Thread(target=migrate)
                thread.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.1))

            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
