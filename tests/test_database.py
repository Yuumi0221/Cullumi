from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cullumi.core import DATABASE_SCHEMA_VERSION, connect_db


class DatabaseMigrationTests(unittest.TestCase):
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
