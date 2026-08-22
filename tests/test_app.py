from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from PIL import Image

from cullumi import http_api as app
from cullumi import motion_cover_service
from cullumi.config import ConfigStore
from cullumi.motion import ffmpeg_executable
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import Scanner
from cullumi.similarity import SimilarityGroupCache


def application_context(
    config: ConfigStore | None = None,
    manager: ProjectManager | None = None,
    scanner: Scanner | None = None,
    groups: SimilarityGroupCache | None = None,
) -> app.ApplicationContext:
    return app.ApplicationContext(
        config or mock.Mock(spec=ConfigStore),
        manager or mock.Mock(spec=ProjectManager),
        scanner or mock.Mock(spec=Scanner),
        groups or mock.Mock(spec=SimilarityGroupCache),
        "test-token",
        Path("web"),
    )


class AppSafetyTests(unittest.TestCase):
    def test_handler_prefers_the_server_application_context(self):
        config = mock.Mock(spec=ConfigStore)
        manager = mock.Mock(spec=ProjectManager)
        scanner = mock.Mock(spec=Scanner)
        groups = mock.Mock(spec=SimilarityGroupCache)
        server_context = app.ApplicationContext(
            config, manager, scanner, groups, "token", Path("web")
        )
        fallback_context = application_context()
        handler = object.__new__(app.Handler)
        handler.server = mock.Mock(application=server_context)
        with mock.patch.object(app, "APPLICATION", fallback_context):
            self.assertIs(handler.application, server_context)

    def test_photo_payload_recomputes_legacy_zero_quality_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            with closing(connect_db(project.db_path)) as conn:
                conn.execute(
                    """INSERT INTO photos(
                       relative_path,status,error,media_type,cover_revision,
                       sharpness,luminance,dark_clip,bright_clip,contrast,
                       entropy,megapixels,quality_score
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "ordinary.jpg", "active", "", "image", 0,
                        900.0, 110.0, 0.01, 0.01, 60.0, 7.0, 12.0, 0.0,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM photos WHERE relative_path='ordinary.jpg'"
                ).fetchone()

            payload = app.photo_payload(
                project.project_id,
                row,
                application=application_context(config, manager),
            )

            self.assertGreater(payload["quality_score"], 0)
            self.assertNotEqual(payload["quality_score"], row["quality_score"])
            self.assertIsNone(payload["blink_closed_ratio"])

    def test_motion_cover_at_video_end_uses_the_last_real_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            with Image.new("RGB", (320, 240), "navy") as image:
                image.save(photos / "IMG_0010.JPG")
            subprocess.run(
                [
                    ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
                    "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(photos / "IMG_0010.MOV"),
                ],
                check=True,
            )
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            groups = SimilarityGroupCache()
            scanner = Scanner(config, manager, groups)
            scanner.start(project.project_id)
            scanner.threads[project.project_id].join(20)
            with closing(connect_db(project.db_path)) as conn:
                photo_id = int(conn.execute("SELECT id FROM photos").fetchone()[0])
                conn.execute(
                    "UPDATE photos SET motion_still_time_ms=-1 WHERE id=?", (photo_id,)
                )
                conn.commit()
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(
                app,
                "APPLICATION",
                application_context(config, manager, scanner, groups),
            ):
                with mock.patch.object(
                    app, "locate_motion_still_time", return_value=300
                ) as locate:
                    handler.api_motion_locate({
                        "project_id": project.project_id,
                        "photo_id": photo_id,
                    })
                self.assertEqual(
                    handler._send_json.call_args.args[0]["still_time_ms"], 300
                )
                locate.assert_called_once()
                handler.api_motion_cover({
                    "project_id": project.project_id,
                    "photo_id": photo_id,
                    "source": "motion",
                    "time_ms": 500,
                })

            payload = handler._send_json.call_args.args[0]["photo"]
            self.assertEqual(payload["motion"]["cover_source"], "motion")
            self.assertEqual(payload["motion"]["cover_time_ms"], 400)
            self.assertEqual(payload["motion"]["cover_frame_index"], 4)
            self.assertEqual(payload["motion"]["still_time_ms"], 300)
            self.assertEqual((payload["width"], payload["height"]), (160, 120))
            self.assertGreaterEqual(payload["quality_score"], 0)
            self.assertIn(".cover-1.jpg", payload["thumbnail"])

            with mock.patch.object(
                app,
                "APPLICATION",
                application_context(config, manager, scanner, groups),
            ):
                handler.api_motion_cover({
                    "project_id": project.project_id,
                    "photo_id": photo_id,
                    "source": "motion",
                    "time_ms": 200,
                    "write_source": True,
                })

            written = handler._send_json.call_args.args[0]
            self.assertTrue(written["source_written"])
            self.assertTrue(Path(written["source_backup"]).is_file())
            self.assertEqual(written["photo"]["motion"]["cover_source"], "still")
            self.assertEqual(written["photo"]["motion"]["still_time_ms"], 200)
            with Image.open(photos / "IMG_0010.JPG") as image:
                self.assertEqual(image.size, (160, 120))

            before_failed_write = (photos / "IMG_0010.JPG").read_bytes()
            with (
                mock.patch.object(
                    app,
                    "APPLICATION",
                    application_context(config, manager, scanner, groups),
                ),
                mock.patch.object(
                    scanner, "reclassify", side_effect=RuntimeError("reclassify failed")
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "reclassify failed"):
                    handler.api_motion_cover({
                        "project_id": project.project_id,
                        "photo_id": photo_id,
                        "source": "motion",
                        "time_ms": 300,
                        "write_source": True,
                    })
            self.assertEqual(
                (photos / "IMG_0010.JPG").read_bytes(), before_failed_write
            )

            before_failed_analysis = (photos / "IMG_0010.JPG").read_bytes()
            with (
                mock.patch.object(
                    app,
                    "APPLICATION",
                    application_context(config, manager, scanner, groups),
                ),
                mock.patch.object(
                    motion_cover_service,
                    "quality_score",
                    side_effect=RuntimeError("scoring failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "scoring failed"):
                    handler.api_motion_cover({
                        "project_id": project.project_id,
                        "photo_id": photo_id,
                        "source": "motion",
                        "time_ms": 100,
                        "write_source": True,
                    })
            self.assertEqual(
                (photos / "IMG_0010.JPG").read_bytes(), before_failed_analysis
            )

    def test_route_tables_reference_handler_methods(self):
        for routes in (app.GET_ROUTES, app.POST_ROUTES):
            for path, method_name in routes.items():
                self.assertTrue(path.startswith("/api/"))
                self.assertTrue(callable(getattr(app.Handler, method_name)))

    def test_static_route_table_dispatches_to_the_registered_handler(self):
        handler = object.__new__(app.Handler)
        handler.path = "/api/bootstrap"
        handler.api_bootstrap = mock.Mock()

        handler._dispatch(app.GET_ROUTES)

        handler.api_bootstrap.assert_called_once_with()

    def test_post_rejects_a_json_value_that_is_not_an_object(self):
        handler = object.__new__(app.Handler)
        context = application_context()
        handler.server = mock.Mock(application=context)
        handler.path = f"/api/project/open?token={context.token}"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"[]")
        handler._send_json = mock.Mock()

        handler.do_POST()

        handler._send_json.assert_called_once_with(
            {"error": "请求正文必须是 JSON 对象"}, 400
        )

    def test_post_reports_a_missing_required_field(self):
        handler = object.__new__(app.Handler)
        context = application_context()
        handler.server = mock.Mock(application=context)
        handler.path = f"/api/project/open?token={context.token}"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler._send_json = mock.Mock()

        handler.do_POST()

        handler._send_json.assert_called_once_with(
            {"error": "缺少请求字段：root"}, 400
        )

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
                mock.patch.object(
                    app, "APPLICATION", application_context(config, manager)
                ),
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

            payload = app.recent_project_payload(
                project.project_id,
                stored,
                application_context(config, manager),
            )

            self.assertTrue(payload["thumbnail_url"].startswith("/api/thumb?"))
            self.assertTrue(payload["stats_loaded"])

    def test_bootstrap_exposes_configuration_recovery_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text("broken", encoding="utf-8")
            config = ConfigStore(config_path)
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(
                app, "APPLICATION", application_context(config)
            ):
                handler.api_bootstrap()

            payload = handler._send_json.call_args.args[0]
            self.assertEqual(payload["startup_warning"], config.load_warning)
            self.assertIn("已备份", payload["startup_warning"])
            self.assertTrue(payload["settings"]["blink_detection_enabled"])

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

    def test_motion_video_range_request_returns_partial_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "motion.webm"
            path.write_bytes(b"0123456789")
            handler = object.__new__(app.Handler)
            handler.headers = {"Range": "bytes=3-6"}
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()

            handler._send_range_file(path, "video/webm")

            handler.send_response.assert_called_once_with(206)
            self.assertEqual(handler.wfile.getvalue(), b"3456")
            self.assertIn(
                mock.call("Content-Range", "bytes 3-6/10"),
                handler.send_header.call_args_list,
            )
            self.assertIn(
                mock.call("Accept-Ranges", "bytes"),
                handler.send_header.call_args_list,
            )

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
        context = application_context()
        handler.server = mock.Mock(application=context)

        handler.path = "/"
        self.assertFalse(handler._authorized())
        handler.path = f"/?token={context.token}"
        self.assertTrue(handler._authorized())
        handler.path = "/api/bootstrap"
        self.assertFalse(handler._authorized())
        handler.path = f"/api/bootstrap?token={context.token}"
        self.assertTrue(handler._authorized())
        handler.path = "/static/js/app.js"
        self.assertTrue(handler._authorized())

    def test_invalid_settings_do_not_partially_mutate_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ConfigStore(root / "config.json")
            before = json.loads(json.dumps(config.data))
            cache = root / "must-not-be-created"
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(
                app, "APPLICATION", application_context(config)
            ):
                with self.assertRaisesRegex(ValueError, "主题"):
                    handler.api_settings(
                        {"default_cache_root": str(cache), "theme": "invalid"}
                    )
                self.assertEqual(config.data, before)
                self.assertFalse(cache.exists())

                with self.assertRaisesRegex(ValueError, "布尔值"):
                    handler.api_settings({"auto_advance": "false"})
                self.assertEqual(config.data, before)

                with self.assertRaisesRegex(ValueError, "布尔值"):
                    handler.api_settings({"blink_detection_enabled": "false"})
                self.assertEqual(config.data, before)

                with self.assertRaisesRegex(ValueError, "封面修改设置"):
                    handler.api_settings({"motion_cover_writeback": "sometimes"})
                self.assertEqual(config.data, before)

                handler.api_settings({"motion_cover_writeback": "always"})
                self.assertEqual(config.data["motion_cover_writeback"], "always")
                after_writeback_setting = json.loads(json.dumps(config.data))

                with mock.patch.object(
                    config, "save", side_effect=OSError("disk full")
                ):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        handler.api_settings({"theme": "night"})
                self.assertEqual(config.data, after_writeback_setting)

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
                mock.patch.object(
                    app,
                    "APPLICATION",
                    application_context(config, manager, scanner),
                ),
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
                mock.patch.object(
                    app,
                    "APPLICATION",
                    application_context(config, manager, scanner),
                ),
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

    def test_decision_response_returns_canonical_state_and_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            with closing(connect_db(project.db_path)) as conn:
                conn.execute(
                    """INSERT INTO photos(relative_path,status,error,suggestion,decision)
                       VALUES('photo.jpg','active','','review','')"""
                )
                photo_id = int(conn.execute("SELECT id FROM photos").fetchone()[0])
                conn.commit()

            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()
            with mock.patch.object(
                app, "APPLICATION", application_context(config, manager)
            ):
                handler.api_decision({
                    "project_id": project.project_id,
                    "photo_id": photo_id,
                    "decision": "remove",
                })

            payload = handler._send_json.call_args.args[0]
            self.assertEqual(payload["photo_id"], photo_id)
            self.assertEqual(payload["decision"], "remove")
            self.assertEqual(payload["project_counts"]["decisions"], {"remove": 1})
            self.assertEqual(payload["project_counts"]["library_counts"]["remove"], 1)
            with closing(connect_db(project.db_path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT decision FROM photos WHERE id=?", (photo_id,)).fetchone()[0],
                    "remove",
                )

    def test_decision_rejects_a_missing_photo_without_reporting_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            handler = object.__new__(app.Handler)
            handler._send_json = mock.Mock()

            with mock.patch.object(
                app, "APPLICATION", application_context(config, manager)
            ):
                with self.assertRaisesRegex(ValueError, "不存在或当前不可用"):
                    handler.api_decision({
                        "project_id": project.project_id,
                        "photo_id": 999,
                        "decision": "keep",
                    })
            handler._send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
