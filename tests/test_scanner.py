from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from cullumi import analysis_worker, motion
from cullumi.config import ConfigStore
from cullumi.project_store import ProjectManager, connect_db, project_thumbnail_path
from cullumi.scanner import Scanner
from cullumi.settings_service import save_settings


def stalled_worker(requests, responses, memory_limit):
    requests.get()
    time.sleep(30)


def crashed_worker(requests, responses, memory_limit):
    requests.get()
    os._exit(1)


class ScannerIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.config = ConfigStore(self.root / "config.json")
        self.config.data["default_cache_root"] = str(self.root / "cache")
        self.manager = ProjectManager(self.config)
        self.project = self.manager.open(str(self.photos))
        self.scanner = Scanner(self.config, self.manager)
        rng = np.random.default_rng(12)
        for i in range(6):
            with Image.fromarray(rng.integers(0, 256, (288 + i, 512, 3), dtype=np.uint8)) as image:
                image.save(self.photos / f"IMG_{i:04d}.jpg", quality=90)

    def scan(self, scanner=None):
        scanner = scanner or self.scanner
        scanner._run(self.project.project_id, threading.Event())
        self.assertEqual(scanner.get_progress(self.project.project_id)["stage"], "complete",
                         scanner.get_progress(self.project.project_id))

    def rows(self):
        with closing(connect_db(self.project.db_path)) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM photos ORDER BY id")]

    def test_unchanged_rescan_does_not_read_sources_or_write_scores(self):
        self.scan()
        self.scanner.similarity_groups = mock.Mock()
        before = self.rows()
        statements = []
        def traced(path):
            conn = connect_db(path)
            conn.set_trace_callback(statements.append)
            return conn
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.parent == self.photos:
                raise AssertionError("unchanged original opened")
            return original_open(path, *args, **kwargs)
        with mock.patch("cullumi.scanner.connect_db", side_effect=traced), \
                mock.patch.object(Path, "open", guarded), \
                mock.patch.object(self.scanner, "analyze_photo", side_effect=AssertionError("decoded")), \
                mock.patch.object(self.scanner, "_rebuild_relationships", side_effect=AssertionError("rebuilt")):
            self.scan()
        self.assertEqual(before, self.rows())
        writes = [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT"))]
        self.assertEqual(writes, [])
        self.scanner.similarity_groups.invalidate.assert_not_called()

    def test_niqe_only_refresh_preserves_hash_thumbnail_and_blink(self):
        self.scan()
        with closing(connect_db(self.project.db_path)) as conn:
            conn.execute("UPDATE photos SET niqe_version='old',sha256='cached-sha',blink_status='open',blink_input_fingerprint='cached-eyes',decision='keep' WHERE id=1")
            conn.commit()
        before = self.rows()[0]
        thumb = project_thumbnail_path(self.project, before["thumbnail"])
        content, mtime = thumb.read_bytes(), thumb.stat().st_mtime_ns
        with mock.patch.object(self.scanner, "analyze_photo", wraps=self.scanner.analyze_photo) as analyze:
            self.scan()
            self.assertEqual(analyze.call_count, 1)
            self.assertTrue(analyze.call_args.kwargs["niqe_only"])
        after = self.rows()[0]
        for key in ("sha256", "blink_status", "blink_input_fingerprint", "cover_revision", "decision"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual((content, mtime), (thumb.read_bytes(), thumb.stat().st_mtime_ns))

    def test_blink_only_refresh_updates_completed_stage_once(self):
        self.scan()
        def refresh(_project, conn, _profile, _cancel):
            conn.execute("UPDATE photos SET blink_input_fingerprint='refreshed' WHERE id=1")
            conn.commit()
            return 1
        with mock.patch.object(self.scanner, "blink_rescan_required", return_value=True), \
                mock.patch.object(self.scanner, "analyze_blinks", side_effect=refresh), \
                mock.patch.object(self.scanner, "reclassify", wraps=self.scanner.reclassify) as reclassify:
            self.scan()
            reclassify.assert_called_once()
        with mock.patch.object(self.scanner, "_rebuild_relationships", side_effect=AssertionError("rebuilt")):
            self.scan()

    def test_missing_thumbnail_and_changed_file_only_decode_affected_photos(self):
        self.scan()
        before = self.rows()
        project_thumbnail_path(self.project, before[0]["thumbnail"]).unlink()
        path = self.photos / before[1]["relative_path"]
        with path.open("ab") as stream:
            stream.write(b"changed")
        with mock.patch.object(self.scanner, "analyze_photo", wraps=self.scanner.analyze_photo) as analyze:
            self.scan()
        self.assertEqual(analyze.call_count, 2)
        self.scan()

    def test_cancelled_grouping_is_retried(self):
        def cancel_grouping(_id, _project, _conn, _profile, cancel):
            cancel.set()
        with mock.patch.object(self.scanner, "_rebuild_relationships", side_effect=cancel_grouping):
            self.scanner._run(self.project.project_id, threading.Event())
        self.assertEqual(self.scanner.get_progress(self.project.project_id)["stage"], "cancelled")
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scan_stage_state").fetchone()[0], 0)
        with mock.patch.object(self.scanner, "_rebuild_relationships", wraps=self.scanner._rebuild_relationships) as rebuild:
            self.scan()
            rebuild.assert_called_once()

    def test_motion_sidecar_changes_invalidate_cache_without_reopening_jpeg_metadata(self):
        video = self.photos / "IMG_0000.MOV"
        video.write_bytes(b"video")
        with mock.patch("cullumi.scanner.probe_motion", return_value={"motion_duration_ms": 100}) as probe, \
                mock.patch("cullumi.scanner.locate_motion_still_time", return_value=0):
            self.scan()
            self.assertEqual(probe.call_count, 1)
            with mock.patch.object(motion, "_xmp_prefix", side_effect=AssertionError("cached JPEG read")):
                self.scan()
                video.write_bytes(b"changed-video")
                self.scan()
                self.assertEqual(probe.call_count, 2)
                video.unlink()
                self.scan()
        self.assertEqual(self.rows()[0]["media_type"], "image")

    def test_parallel_scan_cancellation_terminates_active_workers(self):
        with mock.patch.object(analysis_worker, "parallel_worker_count", return_value=2):
            pool = analysis_worker.PhotoAnalysisPool()
        self.addCleanup(pool.close)
        for runner in pool._runners:
            runner._worker_main = stalled_worker
        self.scanner.analysis_runner = pool
        save_settings(self.config, {"fast_analysis": True})
        cancel = threading.Event()
        thread = threading.Thread(target=self.scanner._run, args=(self.project.project_id, cancel))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while sum(r._process is not None for r in pool._runners) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(sum(r._process is not None for r in pool._runners), 2)
            cancel.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(r._process is None for r in pool._runners))
            self.assertEqual(self.scanner.get_progress(self.project.project_id)["stage"], "cancelled")
        finally:
            cancel.set()
            thread.join(5)

    def test_crashed_worker_does_not_poison_pool(self):
        with mock.patch.object(analysis_worker, "parallel_worker_count", return_value=1):
            pool = analysis_worker.PhotoAnalysisPool()
        self.addCleanup(pool.close)
        pool._runners[0]._worker_main = crashed_worker
        source = next(self.photos.iterdir())
        failed = pool.analyze(source, self.root / "thumb.jpg", parallel=True)
        self.assertIn("异常退出", failed["error"])
        pool._runners[0]._worker_main = analysis_worker._worker_main
        recovered = pool.analyze(source, self.root / "thumb.jpg", parallel=True)
        self.assertFalse(recovered["error"])

    def test_parallel_results_keep_discovery_order_and_cached_scan_is_idle(self):
        self.scan()
        before = self.rows()
        cache = self.root / "parallel-cache"
        self.project = replace(self.project, project_dir=cache, db_path=cache / "project.db",
                               thumb_dir=cache / "thumbs", motion_dir=cache / "motion")
        self.project.thumb_dir.mkdir(parents=True)
        self.scanner.manager = mock.Mock(from_id=mock.Mock(return_value=self.project))
        with mock.patch.object(analysis_worker, "parallel_worker_count", return_value=2):
            pool = analysis_worker.PhotoAnalysisPool()
        self.addCleanup(pool.close)
        self.scanner.analysis_runner = pool
        save_settings(self.config, {"fast_analysis": True})
        self.scan()
        after = self.rows()
        for a, b in zip(before, after, strict=True):
            for key in ("id", "relative_path", "niqe_score", "sharpness", "phash", "quality_score", "suggestion"):
                self.assertEqual(a[key], b[key], key)
        with mock.patch.object(pool, "analyze", side_effect=AssertionError("cached task dispatched")):
            self.scan()

    def test_parallel_queue_is_bounded_and_cancel_stops_dispatch(self):
        cancel, started, release = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def compute(*args):
            calls.append(args[1])
            started.set()
            release.wait(3)
            return {}
        files = list(self.photos.iterdir()) * 10
        with mock.patch.object(self.scanner, "_changed_photo_values", side_effect=compute):
            tasks = self.scanner._analysis_tasks(self.project, files, {}, {},
                                                self.config.get_profile(self.project.profile_id), cancel, 2)
            _index, _path, future = next(tasks)
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertLessEqual(len(calls), 2)
            cancel.set()
            release.set()
            tasks.close()
            future.result()
        self.assertLessEqual(len(calls), 4)

    def test_v6_migration_preserves_decisions_and_invalidates_raw_only(self):
        self.scan()
        path = self.project.db_path
        with closing(connect_db(path)) as conn:
            conn.execute("UPDATE photos SET decision='keep'")
            conn.execute("INSERT INTO photos(relative_path,extension,error) VALUES('camera.raf','.raf','')")
            conn.execute("ALTER TABLE photos DROP COLUMN analysis_version")
            conn.execute("ALTER TABLE photos DROP COLUMN motion_detection_version")
            conn.execute("DROP TABLE scan_stage_state")
            conn.execute("PRAGMA user_version=6")
            conn.commit()
        with closing(connect_db(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertTrue(self.scanner.preprocessing_rescan_required(conn))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photos WHERE decision='keep'").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scan_stage_state").fetchone()[0], 0)
            conn.execute("DELETE FROM photos WHERE extension='.raf'")
            self.assertFalse(self.scanner.preprocessing_rescan_required(conn))
        backup = next(path.parent.glob("project.pre-v7-*.db"))
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
