from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import app
from cullumi.core import ConfigStore, ProjectManager, Scanner, connect_db


class AppSafetyTests(unittest.TestCase):
    def test_bootstrap_defers_recent_project_database_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with (
                mock.patch.object(app, "CONFIG", config),
                mock.patch.object(
                    app,
                    "recent_project_payload",
                    side_effect=AssertionError("bootstrap must not open project databases"),
                ),
            ):
                handler.api_bootstrap()

            recent = handler._send_json.call_args.args[0]["recent_projects"]
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["id"], project.project_id)
            self.assertFalse(recent[0]["stats_loaded"])

    def test_recent_project_thumbnail_survives_cache_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache-a")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            thumbnail = project.thumb_dir / "cover.jpg"
            thumbnail.write_bytes(b"thumbnail")
            with closing(connect_db(project.db_path)) as conn:
                conn.execute(
                    """INSERT INTO photos(relative_path,thumbnail,error,status,mtime,decision)
                       VALUES('cover.jpg',?,'','active',1,'keep')""",
                    (str(thumbnail),),
                )
                conn.commit()

            migration = manager.migrate_cache(project.project_id, str(root / "cache-b"))
            manager.cleanup_old_cache(project.project_id, migration["old_cache"])
            stored = config.snapshot()["projects"][project.project_id]

            with mock.patch.object(app, "MANAGER", manager):
                payload = app.recent_project_payload(project.project_id, stored)

            self.assertTrue(payload["thumbnail_url"].startswith("/api/thumb?"))
            self.assertTrue(payload["stats_loaded"])

    def test_bootstrap_exposes_configuration_recovery_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("broken", encoding="utf-8")
            config = ConfigStore(config_path)
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(app, "CONFIG", config):
                handler.api_bootstrap()

            payload = handler._send_json.call_args.args[0]
            self.assertEqual(payload["startup_warning"], config.load_warning)
            self.assertIn("已备份", payload["startup_warning"])

    def test_api_photo_builds_a_high_resolution_tiff_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.tiff"
            thumbnail = root / "cache" / "thumb.jpg"
            preview = root / "cache" / "thumb.display.jpg"
            project = mock.Mock(root=root, thumb_dir=thumbnail.parent)
            row = {"relative_path": source.name, "thumbnail": str(thumbnail)}
            handler = object.__new__(app.Handler)
            handler._photo_row = mock.Mock(return_value=(project, row))
            handler._send_file = mock.Mock()

            with mock.patch.object(
                app, "ensure_display_preview", return_value=preview
            ) as build_preview:
                handler.api_photo()

            build_preview.assert_called_once_with(source, thumbnail)
            handler._send_file.assert_called_once_with(preview, "image/jpeg")

    def test_api_photo_falls_back_to_thumbnail_when_preview_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.heic"
            thumbnail = root / "cache" / "thumb.jpg"
            project = mock.Mock(root=root, thumb_dir=thumbnail.parent)
            row = {"relative_path": source.name, "thumbnail": str(thumbnail)}
            handler = object.__new__(app.Handler)
            handler._photo_row = mock.Mock(return_value=(project, row))
            handler._send_file = mock.Mock()

            with mock.patch.object(
                app, "ensure_display_preview", side_effect=OSError("offline")
            ):
                handler.api_photo()

            handler._send_file.assert_called_once_with(thumbnail, "image/jpeg")

    def test_send_file_streams_in_bounded_chunks(self):
        class TrackingReader:
            def __init__(self, source):
                self.source = source
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.source.close()

            def fileno(self):
                return self.source.fileno()

            def read(self, size=-1):
                self.read_sizes.append(size)
                return self.source.read(size)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.jpg"
            payload = b"a" * (app.FILE_RESPONSE_CHUNK_SIZE * 2 + 17)
            path.write_bytes(payload)
            source = TrackingReader(path.open("rb"))
            handler = object.__new__(app.Handler)
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()

            with (
                mock.patch.object(Path, "open", return_value=source),
                mock.patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("must not read the whole file"),
                ),
            ):
                handler._send_file(path, "image/jpeg")

            self.assertEqual(handler.wfile.getvalue(), payload)
            self.assertEqual(
                source.read_sizes,
                [
                    app.FILE_RESPONSE_CHUNK_SIZE,
                    app.FILE_RESPONSE_CHUNK_SIZE,
                    17,
                ],
            )
            handler.send_response.assert_called_once_with(200)
            self.assertIn(
                mock.call("Content-Type", "image/jpeg"),
                handler.send_header.call_args_list,
            )
            self.assertIn(
                mock.call("Content-Length", str(len(payload))),
                handler.send_header.call_args_list,
            )
            self.assertIn(
                mock.call("Cache-Control", "private, max-age=3600"),
                handler.send_header.call_args_list,
            )
            handler.end_headers.assert_called_once_with()

    def test_send_file_returns_not_found_when_open_fails(self):
        handler = object.__new__(app.Handler)
        handler.send_error = mock.Mock()
        handler.send_response = mock.Mock()

        handler._send_file(Path("does-not-exist.jpg"))

        handler.send_error.assert_called_once_with(404)
        handler.send_response.assert_not_called()

    def test_root_page_requires_the_application_token(self):
        handler = object.__new__(app.Handler)
        handler.headers = {}

        handler.path = "/"
        self.assertFalse(handler._authorized())
        handler.path = f"/?token={app.TOKEN}"
        self.assertTrue(handler._authorized())
        handler.path = "/api/bootstrap"
        self.assertFalse(handler._authorized())
        handler.path = f"/api/bootstrap?token={app.TOKEN}"
        self.assertTrue(handler._authorized())
        handler.path = "/static/app.js"
        self.assertTrue(handler._authorized())

    def test_invalid_settings_do_not_partially_mutate_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ConfigStore(root / "config.json")
            before = json.loads(json.dumps(config.data))
            cache = root / "must-not-be-created"
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(app, "CONFIG", config):
                with self.assertRaisesRegex(ValueError, "主题"):
                    handler.api_settings(
                        {"default_cache_root": str(cache), "theme": "invalid"}
                    )
                self.assertEqual(config.data, before)
                self.assertFalse(cache.exists())

                with self.assertRaisesRegex(ValueError, "布尔值"):
                    handler.api_settings({"auto_advance": "false"})
                self.assertEqual(config.data, before)

                with mock.patch.object(
                    config, "save", side_effect=OSError("disk full")
                ):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        handler.api_settings({"theme": "night"})
                self.assertEqual(config.data, before)

    def test_failed_profile_apply_rolls_back_database_and_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            scanner = Scanner(config, manager)
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with (
                mock.patch.object(app, "CONFIG", config),
                mock.patch.object(app, "MANAGER", manager),
                mock.patch.object(app, "SCANNER", scanner),
                mock.patch.object(
                    scanner, "rebuild_similarity", side_effect=RuntimeError("failed")
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    handler.api_profile_apply(
                        {"project_id": project.project_id, "profile_id": "balanced"}
                    )

            self.assertEqual(
                config.data["projects"][project.project_id]["profile_id"],
                "conservative",
            )
            conn = connect_db(project.db_path)
            self.assertEqual(conn.execute("SELECT profile_id FROM project").fetchone()[0], "conservative")
            conn.close()

    def test_profile_apply_rolls_back_when_config_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            scanner = Scanner(config, manager)
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with (
                mock.patch.object(app, "CONFIG", config),
                mock.patch.object(app, "MANAGER", manager),
                mock.patch.object(app, "SCANNER", scanner),
                mock.patch.object(config, "save", side_effect=OSError("disk full")),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    handler.api_profile_apply(
                        {"project_id": project.project_id, "profile_id": "balanced"}
                    )

            self.assertEqual(
                config.data["projects"][project.project_id]["profile_id"],
                "conservative",
            )
            conn = connect_db(project.db_path)
            self.assertEqual(conn.execute("SELECT profile_id FROM project").fetchone()[0], "conservative")
            conn.close()


if __name__ == "__main__":
    unittest.main()
