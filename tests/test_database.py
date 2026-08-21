from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from cullumi import project_store
from cullumi.project_store import DATABASE_SCHEMA_VERSION, connect_db


class DatabaseMigrationTests(unittest.TestCase):
    def test_wal_configuration_is_reused_for_the_same_database_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.db"
            project_store._WAL_CONFIGURED_DATABASES.pop(path.resolve(), None)
            project_store._INITIALIZED_DATABASES.pop(path.resolve(), None)
            with mock.patch.object(
                project_store,
                "_ensure_wal",
                wraps=project_store._ensure_wal,
            ) as ensure_wal:
                first = connect_db(path)
                first.close()
                identity = project_store._WAL_CONFIGURED_DATABASES[path.resolve()]

                second = connect_db(path)
                second.close()
            ensure_wal.assert_called_once()
            self.assertEqual(
                project_store._WAL_CONFIGURED_DATABASES[path.resolve()], identity
            )

    def test_concurrent_connections_share_initialized_database_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.db"

            def read_version(_: int) -> int:
                connection = connect_db(path)
                try:
                    return int(connection.execute("PRAGMA user_version").fetchone()[0])
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=8) as pool:
                versions = list(pool.map(read_version, range(32)))

            self.assertEqual(versions, [DATABASE_SCHEMA_VERSION] * 32)

    def test_replaced_database_at_the_same_path_is_configured_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "project.db"
            replacement = root / "replacement.db"
            connect_db(path).close()
            replacement_conn = connect_db(replacement)
            replacement_conn.execute("PRAGMA journal_mode=DELETE")
            replacement_conn.close()
            previous_identity = project_store._WAL_CONFIGURED_DATABASES[path.resolve()]
            path.unlink()
            replacement.replace(path)

            reopened = connect_db(path)
            mode = reopened.execute("PRAGMA journal_mode").fetchone()[0]
            reopened.close()
            self.assertEqual(mode.lower(), "wal")
            self.assertNotEqual(
                project_store._WAL_CONFIGURED_DATABASES[path.resolve()],
                previous_identity,
            )

    def test_new_database_gets_current_schema_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.db"
            conn = connect_db(path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            conn.close()

            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertIn("idx_photos_status_decision", indexes)
            self.assertIn("idx_photos_status_error_size", indexes)
            self.assertEqual(list(path.parent.glob("project.pre-v*.db")), [])

    def test_legacy_database_is_backed_up_and_migrated_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "project.db"
            conn = connect_db(path)
            conn.execute(
                "INSERT INTO photos(relative_path,status,decision,error,size) VALUES(?,?,?,?,?)",
                ("kept.jpg", "active", "keep", "", 123),
            )
            conn.execute("DROP INDEX idx_photos_status_decision")
            conn.execute("DROP INDEX idx_photos_status_error_size")
            conn.execute("PRAGMA user_version=0")
            conn.commit()
            conn.close()

            migrated = connect_db(path)
            migrated_version = migrated.execute("PRAGMA user_version").fetchone()[0]
            migrated_row = tuple(
                migrated.execute(
                    "SELECT decision,size FROM photos WHERE relative_path='kept.jpg'"
                ).fetchone()
            )
            migrated.close()
            self.assertEqual(migrated_version, DATABASE_SCHEMA_VERSION)
            self.assertEqual(migrated_row, ("keep", 123))

            backups = list(root.glob("project.pre-v1-*.db"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                backup.execute(
                    "SELECT decision,size FROM photos WHERE relative_path='kept.jpg'"
                ).fetchone(),
                ("keep", 123),
            )
            backup.close()

    def test_newer_database_version_is_rejected_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.db"
            conn = connect_db(path)
            conn.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION + 1}")
            conn.commit()
            conn.close()

            with self.assertRaisesRegex(RuntimeError, "高于当前支持"):
                connect_db(path)

            unchanged = sqlite3.connect(path)
            self.assertEqual(
                unchanged.execute("PRAGMA user_version").fetchone()[0],
                DATABASE_SCHEMA_VERSION + 1,
            )
            unchanged.close()


if __name__ == "__main__":
    unittest.main()
