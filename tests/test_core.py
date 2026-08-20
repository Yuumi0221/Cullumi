from __future__ import annotations

import csv
import json
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

import cullumi.core as core
from cullumi import __version__
from cullumi.core import (
    BUILTIN_PROFILES,
    ConfigStore,
    ProjectManager,
    ScanCancelled,
    Scanner,
    SimilarityGroupCache,
    analyze_photo,
    apply_quarantine,
    build_similarity_groups,
    classification_percentiles,
    clear_decisions,
    connect_db,
    HEIF_EXTENSIONS,
    import_decisions,
    mark_ai_remove_suggestions,
    open_heif,
    parse_photo_filter,
    photo_filter_where,
    photo_library_counts,
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    project_id_for,
    quarantine_preview,
    restore_batch,
    open_image,
    validate_profile,
)


class CullumiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.photos = self.base / "photos"
        self.photos.mkdir()
        self.cache = self.base / "cache"
        self.config = ConfigStore(self.base / "config.json")
        self.config.data["default_cache_root"] = str(self.cache)
        self.config.save()
        self.manager = ProjectManager(self.config)
        self.project = self.manager.open(str(self.photos))

    def tearDown(self):
        self.temp.cleanup()

    def make_photo(self, name: str, color=(90, 130, 170), pattern=True):
        image = Image.new("RGB", (640, 480), color)
        if pattern:
            draw = ImageDraw.Draw(image)
            for x in range(0, 640, 40):
                draw.line((x, 0, 640 - x, 479), fill=(230, 220, 190), width=4)
        image.save(self.photos / name, quality=92)

    def scan(self):
        scanner = Scanner(self.config, self.manager)
        scanner.start(self.project.project_id)
        scanner.threads[self.project.project_id].join(20)
        progress = scanner.progress[self.project.project_id]
        self.assertEqual(progress["stage"], "complete", progress)

    def test_profile_validation(self):
        self.assertEqual(__version__, "1.0.0")
        self.assertEqual(self.config.data["theme"], "day")
        self.assertTrue(self.config.data["auto_check_updates"])
        validate_profile(BUILTIN_PROFILES["balanced"])
        self.assertTrue(
            all(
                profile["similarity"]["allow_cross_time_high_confidence"]
                for profile in BUILTIN_PROFILES.values()
            )
        )
        broken = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        broken["name"] = ""
        with self.assertRaises(ValueError):
            validate_profile(broken)
        for update in (
            lambda value: value["quality"].update({"threshold_mode": "sometimes"}),
            lambda value: value["quality"]["enabled"].update({"sharpness": "yes"}),
            lambda value: value["similarity"].update({"exact_duplicates": 1}),
            lambda value: value["quality"].update({"blur_review": float("nan")}),
            lambda value: value["quality"].pop("dark_review"),
        ):
            invalid = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
            update(invalid)
            with self.assertRaises(ValueError):
                validate_profile(invalid)

        custom = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        custom.update({"id": "custom-rollback", "name": "回滚测试"})
        before = json.loads(json.dumps(self.config.data))
        with mock.patch.object(self.config, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.config.save_custom_profile(custom)
        self.assertEqual(self.config.data, before)

        saved = self.config.save_custom_profile(custom)
        with mock.patch.object(self.config, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.config.delete_custom_profile(saved["id"])
        self.assertIn(saved["id"], self.config.data["custom_profiles"])

    def test_heif_phone_variants_use_tolerant_decoder(self):
        self.assertIsNotNone(open_heif)
        self.assertTrue({".heic", ".heics", ".heif", ".heifs", ".hif"} <= HEIF_EXTENSIONS)
        path = self.photos / "phone.heic"
        Image.new("RGB", (64, 48), (30, 80, 120)).save(path, "HEIF", quality=80)
        image, taken = open_image(path)
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (64, 48))
        self.assertEqual(taken, "")
        image.close()

    def test_incremental_scan_retries_previous_decode_errors(self):
        self.make_photo("retry.jpg")
        self.scan()
        conn = connect_db(self.project.db_path)
        conn.execute("UPDATE photos SET error='old decoder failed' WHERE relative_path='retry.jpg'")
        conn.commit()
        conn.close()

        self.scan()
        conn = connect_db(self.project.db_path)
        row = conn.execute("SELECT error,width,height FROM photos WHERE relative_path='retry.jpg'").fetchone()
        conn.close()
        self.assertEqual(row["error"], "")
        self.assertEqual((row["width"], row["height"]), (640, 480))

    def test_builtin_modes_have_more_aggressive_ordered_thresholds(self):
        conservative = BUILTIN_PROFILES["conservative"]
        balanced = BUILTIN_PROFILES["balanced"]
        aggressive = BUILTIN_PROFILES["aggressive"]
        self.assertEqual(
            [
                conservative["quality"]["blur_remove_percentile"],
                balanced["quality"]["blur_remove_percentile"],
                aggressive["quality"]["blur_remove_percentile"],
            ],
            [3, 6, 10],
        )
        self.assertLess(
            conservative["quality"]["blur_review"],
            balanced["quality"]["blur_review"],
        )
        self.assertLess(
            balanced["quality"]["blur_review"],
            aggressive["quality"]["blur_review"],
        )
        self.assertGreater(
            conservative["similarity"]["structure_min"],
            balanced["similarity"]["structure_min"],
        )
        self.assertGreater(
            balanced["similarity"]["structure_min"],
            aggressive["similarity"]["structure_min"],
        )

    def test_percentile_estimate_uses_same_thresholds_as_apply(self):
        profile = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        profile["quality"]["threshold_mode"] = "percentile"
        rows = [
            {"sharpness": value}
            for value in (10.0, 20.0, 30.0, 40.0, 1000.0)
        ]
        percentiles = classification_percentiles(rows, profile)
        self.assertIsNotNone(percentiles)
        self.assertLessEqual(
            percentiles["sharpness_remove"],
            percentiles["sharpness_review"],
        )

    def test_remove_from_recent_preserves_project_and_cache(self):
        marker = self.project.thumb_dir / "keep-me.txt"
        marker.write_text("cache", encoding="utf-8")
        self.assertIn(self.project.project_id, self.config.data["recent_projects"])
        self.manager.remove_from_recent(self.project.project_id)
        self.assertNotIn(self.project.project_id, self.config.data["recent_projects"])
        self.assertIn(self.project.project_id, self.config.data["projects"])
        self.assertTrue(self.photos.is_dir())
        self.assertTrue(self.project.db_path.is_file())
        self.assertTrue(marker.is_file())

    def test_from_id_resolves_project_without_reopening_it(self):
        with mock.patch.object(
            self.manager, "open", side_effect=AssertionError("open must not be called")
        ):
            resolved = self.manager.from_id(self.project.project_id)
        self.assertEqual(resolved, self.project)

    def test_remove_from_recent_can_delete_database_and_thumbnails(self):
        marker = self.project.thumb_dir / "delete-me.txt"
        marker.write_text("cache", encoding="utf-8")
        result = self.manager.remove_from_recent(self.project.project_id, delete_cache=True)
        self.assertTrue(result["cache_deleted"])
        self.assertNotIn(self.project.project_id, self.config.data["recent_projects"])
        self.assertNotIn(self.project.project_id, self.config.data["projects"])
        self.assertTrue(self.photos.is_dir())
        self.assertFalse(self.project.project_dir.exists())

    def test_clear_decisions_only_resets_active_photos(self):
        self.make_photo("keep.jpg")
        self.make_photo("remove.jpg", (120, 80, 60))
        self.scan()
        conn = connect_db(self.project.db_path)
        rows = conn.execute(
            "SELECT id FROM photos WHERE status='active' ORDER BY relative_path"
        ).fetchall()
        conn.execute("UPDATE photos SET decision='keep' WHERE id=?", (rows[0]["id"],))
        conn.execute("UPDATE photos SET decision='remove' WHERE id=?", (rows[1]["id"],))
        conn.commit()
        conn.close()

        self.assertEqual(clear_decisions(self.project), 2)
        conn = connect_db(self.project.db_path)
        decisions = [
            row["decision"]
            for row in conn.execute(
                "SELECT decision FROM photos WHERE status='active' ORDER BY relative_path"
            )
        ]
        conn.close()
        self.assertEqual(decisions, ["", ""])

    def test_mark_ai_remove_suggestions_only_marks_safe_pending_rows(self):
        for index in range(5):
            self.make_photo(f"ai-remove-{index}.jpg", (70 + index * 20, 90, 120))
        self.scan()
        conn = connect_db(self.project.db_path)
        rows = conn.execute("SELECT id FROM photos ORDER BY relative_path").fetchall()
        states = [
            ("", "remove", "", "active"),
            ("keep", "remove", "", "active"),
            ("", "review", "", "active"),
            ("", "remove", "broken", "active"),
            ("", "remove", "", "missing"),
        ]
        for row, (decision, suggestion, error, status) in zip(rows, states):
            conn.execute(
                "UPDATE photos SET decision=?,suggestion=?,error=?,status=? WHERE id=?",
                (decision, suggestion, error, status, row["id"]),
            )
        conn.commit()
        conn.close()

        self.assertEqual(mark_ai_remove_suggestions(self.project), 1)
        conn = connect_db(self.project.db_path)
        actual = [
            row["decision"]
            for row in conn.execute("SELECT decision FROM photos ORDER BY relative_path")
        ]
        conn.close()
        self.assertEqual(actual, ["remove", "keep", "", "", ""])

    def test_photo_library_filters_and_counts_share_state_definitions(self):
        for index in range(5):
            self.make_photo(f"filter-{index}.jpg", (60 + index * 20, 90, 120))
        self.scan()
        conn = connect_db(self.project.db_path)
        rows = conn.execute("SELECT id FROM photos ORDER BY relative_path").fetchall()
        conn.execute(
            "UPDATE photos SET decision='',suggestion='remove',error='',status='active' WHERE id=?",
            (rows[0]["id"],),
        )
        conn.execute(
            "UPDATE photos SET decision='keep',suggestion='review',error='',status='active' WHERE id=?",
            (rows[1]["id"],),
        )
        conn.execute(
            "UPDATE photos SET decision='remove',suggestion='keep',error='',status='active' WHERE id=?",
            (rows[2]["id"],),
        )
        conn.execute(
            "UPDATE photos SET decision='',suggestion='unreadable',error='broken',status='active' WHERE id=?",
            (rows[3]["id"],),
        )
        conn.execute(
            "UPDATE photos SET decision='',suggestion='remove',error='',status='missing' WHERE id=?",
            (rows[4]["id"],),
        )
        conn.commit()

        self.assertEqual(
            photo_library_counts(conn),
            {
                "readable": 3,
                "undecided": 1,
                "keep": 1,
                "remove": 1,
                "ai_pending": 1,
                "ai_remove_pending": 1,
                "unreadable": 1,
            },
        )
        where, params = photo_filter_where(
            "readable", {"undecided", "keep"}, {"remove", "review"}
        )
        self.assertEqual(
            conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0],
            2,
        )
        where, params = photo_filter_where("readable", set(), set(PHOTO_AI_FILTERS))
        self.assertEqual(
            conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0],
            0,
        )
        conn.close()

        self.assertEqual(
            parse_photo_filter("all", PHOTO_DECISION_FILTERS, "decisions"),
            set(PHOTO_DECISION_FILTERS),
        )
        self.assertEqual(
            parse_photo_filter("none", PHOTO_AI_FILTERS, "ai_states"),
            set(),
        )
        self.assertEqual(
            parse_photo_filter("remove,review,remove", PHOTO_AI_FILTERS, "ai_states"),
            {"remove", "review"},
        )
        with self.assertRaisesRegex(ValueError, "无效值"):
            parse_photo_filter("remove,unknown", PHOTO_AI_FILTERS, "ai_states")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            parse_photo_filter("", PHOTO_DECISION_FILTERS, "decisions")

    def test_video_only_folder_reports_unsupported_files(self):
        (self.photos / "clip.mov").write_bytes(b"not-a-real-video")
        scanner = Scanner(self.config, self.manager)
        scanner.start(self.project.project_id)
        scanner.threads[self.project.project_id].join(20)
        progress = scanner.progress[self.project.project_id]
        self.assertEqual(progress["stage"], "complete")
        self.assertEqual(progress["total"], 0)
        self.assertEqual(progress["video_count"], 1)
        self.assertEqual(progress["unsupported_extensions"], {".mov": 1})

    def test_scan_incremental_and_exact_duplicate(self):
        self.make_photo("IMG_0001.jpg")
        (self.photos / "IMG_0002.jpg").write_bytes((self.photos / "IMG_0001.jpg").read_bytes())
        self.make_photo("IMG_9000.jpg", (20, 20, 20), False)
        self.scan()
        conn = connect_db(self.project.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM photos WHERE status='active'").fetchone()[0], 3)
        self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM similar_pairs WHERE kind='exact'").fetchone()[0], 1)
        groups = build_similarity_groups(conn, BUILTIN_PROFILES["balanced"])
        exact = next(group for group in groups if group["kind"] == "exact")
        self.assertEqual(len(exact["members"]), 2)
        analyzed = conn.execute("SELECT analyzed_at FROM photos WHERE relative_path='IMG_0001.jpg'").fetchone()[0]
        conn.close()
        time.sleep(0.02)
        self.scan()
        conn = connect_db(self.project.db_path)
        self.assertEqual(conn.execute("SELECT analyzed_at FROM photos WHERE relative_path='IMG_0001.jpg'").fetchone()[0], analyzed)
        conn.close()

    def test_changed_file_replaces_stale_sha_and_exact_pair(self):
        self.make_photo("DUP_0001.bmp")
        (self.photos / "DUP_0002.bmp").write_bytes((self.photos / "DUP_0001.bmp").read_bytes())
        self.scan()
        conn = connect_db(self.project.db_path)
        original = conn.execute(
            "SELECT sha256 FROM photos WHERE relative_path='DUP_0002.bmp'"
        ).fetchone()[0]
        self.assertTrue(original)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM similar_pairs WHERE kind='exact'").fetchone()[0], 1)
        conn.close()

        time.sleep(0.02)
        self.make_photo("DUP_0002.bmp", (180, 30, 20))
        self.scan()
        conn = connect_db(self.project.db_path)
        changed = conn.execute(
            "SELECT sha256 FROM photos WHERE relative_path='DUP_0002.bmp'"
        ).fetchone()[0]
        self.assertTrue(changed)
        self.assertNotEqual(changed, original)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM similar_pairs WHERE kind='exact'").fetchone()[0], 0)
        conn.close()

    def test_missing_photo_returns_to_active_when_it_reappears(self):
        self.make_photo("RETURN.jpg")
        self.scan()
        (self.photos / "RETURN.jpg").unlink()
        self.scan()
        conn = connect_db(self.project.db_path)
        self.assertEqual(
            conn.execute("SELECT status FROM photos WHERE relative_path='RETURN.jpg'").fetchone()[0],
            "missing",
        )
        conn.close()

        self.make_photo("RETURN.jpg")
        self.scan()
        conn = connect_db(self.project.db_path)
        self.assertEqual(
            conn.execute("SELECT status FROM photos WHERE relative_path='RETURN.jpg'").fetchone()[0],
            "active",
        )
        conn.close()

    def test_scan_continues_when_discovered_photo_disappears(self):
        self.make_photo("GONE.jpg")
        self.make_photo("STAYS.jpg", (20, 70, 120))
        self.scan()
        gone = self.photos / "GONE.jpg"
        stays = self.photos / "STAYS.jpg"
        scanner = Scanner(self.config, self.manager)

        def disappearing_discovery(project, cancel):
            gone.unlink()
            return [gone, stays]

        with mock.patch.object(scanner, "_discover", side_effect=disappearing_discovery):
            scanner.start(self.project.project_id)
            scanner.threads[self.project.project_id].join(20)
        progress = scanner.get_progress(self.project.project_id)
        self.assertEqual(progress["stage"], "complete", progress)
        self.assertEqual(progress["unavailable_count"], 1)
        conn = connect_db(self.project.db_path)
        statuses = {
            row["relative_path"]: row["status"]
            for row in conn.execute("SELECT relative_path,status FROM photos")
        }
        conn.close()
        self.assertEqual(statuses, {"GONE.jpg": "missing", "STAYS.jpg": "active"})

    def test_scan_continues_when_photo_disappears_during_hashing(self):
        self.make_photo("HASH_0001.jpg")
        disappearing = self.photos / "HASH_0002.jpg"
        disappearing.write_bytes((self.photos / "HASH_0001.jpg").read_bytes())
        scanner = Scanner(self.config, self.manager)
        exact_hashes = scanner._exact_hashes

        def remove_before_hash(project, conn, cancel):
            disappearing.unlink()
            return exact_hashes(project, conn, cancel)

        with mock.patch.object(scanner, "_exact_hashes", side_effect=remove_before_hash):
            scanner.start(self.project.project_id)
            scanner.threads[self.project.project_id].join(20)
        progress = scanner.get_progress(self.project.project_id)
        self.assertEqual(progress["stage"], "complete", progress)
        self.assertEqual(progress["unavailable_count"], 1)
        conn = connect_db(self.project.db_path)
        row = conn.execute(
            "SELECT status FROM photos WHERE relative_path='HASH_0002.jpg'"
        ).fetchone()
        self.assertEqual(row["status"], "missing")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM similar_pairs").fetchone()[0], 0)
        conn.close()

    def test_scan_marks_photo_missing_when_it_disappears_during_analysis(self):
        self.make_photo("LATE_GONE.jpg")
        self.scan()
        disappearing = self.photos / "LATE_GONE.jpg"
        conn = connect_db(self.project.db_path)
        conn.execute(
            "UPDATE photos SET error='retry' WHERE relative_path='LATE_GONE.jpg'"
        )
        conn.commit()
        conn.close()
        scanner = Scanner(self.config, self.manager)
        real_analyze = core.analyze_photo

        def analyze_then_remove(path, thumbnail, stat=None):
            result = real_analyze(path, thumbnail, stat)
            path.unlink()
            return result

        with mock.patch("cullumi.core.analyze_photo", side_effect=analyze_then_remove):
            scanner.start(self.project.project_id)
            scanner.threads[self.project.project_id].join(20)
        progress = scanner.get_progress(self.project.project_id)
        self.assertEqual(progress["stage"], "complete", progress)
        self.assertEqual(progress["unavailable_count"], 1)
        conn = connect_db(self.project.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT status FROM photos WHERE relative_path='LATE_GONE.jpg'"
            ).fetchone()[0],
            "missing",
        )
        conn.close()

    def test_thumbnail_write_failure_preserves_previous_thumbnail(self):
        self.make_photo("ATOMIC.jpg")
        self.scan()
        conn = connect_db(self.project.db_path)
        thumb_path = Path(
            conn.execute(
                "SELECT thumbnail FROM photos WHERE relative_path='ATOMIC.jpg'"
            ).fetchone()[0]
        )
        conn.close()
        original_thumbnail = thumb_path.read_bytes()
        original_save = Image.Image.save

        def fail_after_write(image, path, *args, **kwargs):
            original_save(image, path, *args, **kwargs)
            raise OSError("simulated thumbnail failure")

        with mock.patch.object(Image.Image, "save", new=fail_after_write):
            metrics = analyze_photo(self.photos / "ATOMIC.jpg", thumb_path)
        self.assertIn("simulated thumbnail failure", metrics["error"])
        self.assertEqual(thumb_path.read_bytes(), original_thumbnail)
        self.assertFalse(thumb_path.with_suffix(thumb_path.suffix + ".tmp").exists())

    def test_cancelled_similarity_rebuild_preserves_previous_pairs(self):
        self.make_photo("CANCEL_0001.jpg")
        (self.photos / "CANCEL_0002.jpg").write_bytes((self.photos / "CANCEL_0001.jpg").read_bytes())
        self.scan()
        scanner = Scanner(self.config, self.manager)
        cancel = threading.Event()
        cancel.set()
        conn = connect_db(self.project.db_path)
        before = [tuple(row) for row in conn.execute("SELECT a_id,b_id,kind FROM similar_pairs ORDER BY id")]
        with self.assertRaises(ScanCancelled):
            scanner.rebuild_similarity(
                self.project, conn, BUILTIN_PROFILES["balanced"], cancel
            )
        after = [tuple(row) for row in conn.execute("SELECT a_id,b_id,kind FROM similar_pairs ORDER BY id")]
        conn.close()
        self.assertEqual(after, before)

    def test_cancelled_scan_is_not_reported_as_complete(self):
        scanner = Scanner(self.config, self.manager)
        cancel = threading.Event()
        cancel.set()
        scanner._run(self.project.project_id, cancel)
        self.assertEqual(scanner.get_progress(self.project.project_id)["stage"], "cancelled")

    def test_nested_project_cache_is_not_scanned_as_photos(self):
        root = self.base / "nested-cache-photos"
        root.mkdir()
        Image.new("RGB", (32, 24), (20, 40, 60)).save(root / "only-photo.jpg")
        project_id = project_id_for(root)
        self.config.data["projects"][project_id] = {
            "root": str(root), "cache_root": str(root), "profile_id": "conservative"
        }
        self.config.save()
        project = self.manager.open(str(root))
        scanner = Scanner(self.config, self.manager)
        for _ in range(2):
            scanner.start(project.project_id)
            scanner.threads[project.project_id].join(20)
            self.assertEqual(scanner.get_progress(project.project_id)["stage"], "complete")
        conn = connect_db(project.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 1)
        conn.close()

    def test_new_project_rejects_cache_overlapping_photo_folder(self):
        root = self.base / "new-overlap-photos"
        root.mkdir()
        with self.assertRaisesRegex(ValueError, "不能与照片文件夹重叠"):
            self.manager.open(str(root), str(root))

    def test_concurrent_scan_starts_create_only_one_worker(self):
        scanner = Scanner(self.config, self.manager)
        release = threading.Event()
        runs: list[str] = []

        def blocked(project_id, cancel):
            runs.append(project_id)
            release.wait(5)

        with mock.patch.object(scanner, "_run", side_effect=blocked):
            callers = [
                threading.Thread(target=scanner.start, args=(self.project.project_id,))
                for _ in range(8)
            ]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(5)
            self.assertEqual(runs, [self.project.project_id])
            with self.assertRaisesRegex(ValueError, "正在执行其他任务"):
                with scanner.project_operation(self.project.project_id, "隔离照片"):
                    pass
            release.set()
            scanner.threads[self.project.project_id].join(5)

        with scanner.project_operation(self.project.project_id):
            self.assertFalse(scanner.start(self.project.project_id))

    def test_similarity_pairs_collapse_into_connected_groups(self):
        for index, color in enumerate(
            ((50, 80, 120), (80, 110, 140), (110, 140, 170), (140, 90, 70), (170, 120, 90)),
            1,
        ):
            self.make_photo(f"IMG_{index:04d}.jpg", color)
        self.scan()
        conn = connect_db(self.project.db_path)
        rows = conn.execute(
            "SELECT id,relative_path FROM photos ORDER BY relative_path"
        ).fetchall()
        ids = [row["id"] for row in rows]
        conn.execute("DELETE FROM similar_pairs")
        conn.executemany(
            """INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe)
               VALUES(?,?,?,?,?,?)""",
            [
                (ids[0], ids[1], 0.90, "similar", ids[1], 0),
                (ids[1], ids[2], 0.80, "similar", ids[2], 1),
                (ids[3], ids[4], 0.95, "similar", ids[3], 0),
            ],
        )
        for index, photo_id in enumerate(ids[:3]):
            conn.execute(
                "UPDATE photos SET taken=?,sharpness=? WHERE id=?",
                (f"2026:01:01 10:00:0{index}", (index + 1) * 1000, photo_id),
            )
        conn.commit()
        profile = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        profile["quality"]["weights"] = {
            "sharpness": 1.0,
            "exposure": 0.0,
            "contrast": 0.0,
            "entropy": 0.0,
            "resolution": 0.0,
        }
        groups = build_similarity_groups(conn, profile)
        self.assertEqual(sorted(len(group["members"]) for group in groups), [2, 3])
        connected = next(group for group in groups if len(group["members"]) == 3)
        self.assertEqual(
            [row["id"] for row in connected["members"]],
            ids[:3],
        )
        self.assertEqual(connected["recommended_id"], ids[2])
        self.assertEqual(connected["covers"][0]["id"], ids[2])
        self.assertAlmostEqual(connected["confidence"][ids[0]], 0.80)
        self.assertTrue(connected["face_safe"])
        self.assertEqual(
            connected["id"],
            next(group for group in build_similarity_groups(conn, profile) if len(group["members"]) == 3)["id"],
        )

        profile["similarity"]["min_group_size"] = 3
        filtered = build_similarity_groups(conn, profile)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(filtered[0]["members"]), 3)
        conn.close()

    def test_similarity_group_cache_reuses_topology_and_hydrates_decisions(self):
        self.make_photo("CACHE_0001.jpg", (50, 80, 120))
        self.make_photo("CACHE_0002.jpg", (80, 110, 140))
        self.scan()
        conn = connect_db(self.project.db_path)
        rows = conn.execute(
            "SELECT id FROM photos ORDER BY relative_path"
        ).fetchall()
        left_id, right_id = (int(row["id"]) for row in rows)
        conn.execute("DELETE FROM similar_pairs")
        conn.execute(
            """INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe)
               VALUES(?,?,?,?,?,?)""",
            (left_id, right_id, 0.9, "similar", left_id, 0),
        )
        conn.commit()
        profile = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        cache = SimilarityGroupCache()

        with mock.patch(
            "cullumi.similarity.build_similarity_groups", wraps=build_similarity_groups
        ) as builder:
            first = cache.get(self.project.project_id, conn, profile)
            second = cache.get(self.project.project_id, conn, profile)
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(first[0]["id"], second[0]["id"])

            conn.execute(
                "UPDATE photos SET decision='keep' WHERE id=?", (right_id,)
            )
            conn.commit()
            fresh = cache.get(self.project.project_id, conn, profile)
            decisions = {int(row["id"]): row["decision"] for row in fresh[0]["members"]}
            self.assertEqual(decisions[right_id], "keep")
            self.assertEqual(builder.call_count, 1)

            changed_profile = json.loads(json.dumps(profile))
            changed_profile["similarity"]["phash_max"] += 1
            cache.get(self.project.project_id, conn, changed_profile)
            self.assertEqual(builder.call_count, 2)

            cache.invalidate(self.project.project_id)
            cache.get(self.project.project_id, conn, profile)
            self.assertEqual(builder.call_count, 3)
        conn.close()

    def test_csv_quarantine_restore_collision(self):
        self.make_photo("旅程 01.jpg")
        self.scan()
        csv_path = self.base / "decisions.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["决定", "路径"])
            writer.writerow(["remove", "旅程 01.jpg"])
            writer.writerow(["keep", "不存在.jpg"])
        result = import_decisions(self.project, csv_path)
        self.assertEqual(result, {"imported": 1, "missing": 1})
        self.assertEqual(quarantine_preview(self.project)["count"], 1)
        batch = apply_quarantine(self.project)
        self.assertEqual(batch["moved"], 1)
        self.assertFalse((self.photos / "旅程 01.jpg").exists())
        self.make_photo("旅程 01.jpg", (255, 0, 0))
        restored = restore_batch(self.project, batch["batch_id"])
        self.assertEqual(restored["restored"], 1)
        self.assertEqual(restored["conflicts"], 1)
        self.assertTrue(any(self.photos.glob("旅程 01.restored-*.jpg")))
        conn = connect_db(self.project.db_path)
        row = conn.execute("SELECT status,decision FROM photos").fetchone()
        conn.close()
        self.assertEqual((row["status"], row["decision"]), ("active", "remove"))

    def test_similarity_rebuild_decodes_each_thumbnail_once(self):
        for index, color in enumerate(((40, 70, 100), (70, 100, 130), (100, 130, 160)), 1):
            self.make_photo(f"PERF_{index:04d}.jpg", color)
        self.scan()
        conn = connect_db(self.project.db_path)
        conn.execute("UPDATE photos SET phash=?,dhash=?,sha256=''", ("0" * 16, "0" * 16))
        conn.commit()
        profile = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        profile["similarity"].update(
            {"exact_duplicates": False, "phash_max": 0, "dhash_max": 0, "structure_min": -1}
        )
        scanner = Scanner(self.config, self.manager)
        with mock.patch(
            "cullumi.core._structure_vector", wraps=core._structure_vector
        ) as loader:
            scanner.rebuild_similarity(self.project, conn, profile)
        self.assertEqual(loader.call_count, 3)
        pairs = conn.execute(
            "SELECT a_id,b_id,score FROM similar_pairs ORDER BY a_id,b_id"
        ).fetchall()
        self.assertEqual(len(pairs), 3)
        photos = {
            int(row["id"]): row
            for row in conn.execute("SELECT id,thumbnail FROM photos")
        }
        for pair in pairs:
            left_path = Path(photos[int(pair["a_id"])]["thumbnail"])
            right_path = Path(photos[int(pair["b_id"])]["thumbnail"])
            with Image.open(left_path) as left, Image.open(right_path) as right:
                left_vector = core.np.asarray(
                    ImageOps.grayscale(left).resize((64, 64)), dtype=core.np.float32
                )
                right_vector = core.np.asarray(
                    ImageOps.grayscale(right).resize((64, 64)), dtype=core.np.float32
                )
            left_vector -= left_vector.mean()
            right_vector -= right_vector.mean()
            denominator = float(
                core.np.linalg.norm(left_vector) * core.np.linalg.norm(right_vector)
            )
            structure = (
                float(core.np.sum(left_vector * right_vector) / denominator)
                if denominator else 0.0
            )
            self.assertAlmostEqual(float(pair["score"]), 0.70 + 0.30 * structure)
        conn.close()

    def test_similarity_cache_hydrates_only_group_member_rows(self):
        for index in range(1, 6):
            self.make_photo(f"QUERY_{index:04d}.jpg", (20 * index, 30, 80))
        self.scan()
        conn = connect_db(self.project.db_path)
        ids = [
            int(row[0])
            for row in conn.execute("SELECT id FROM photos ORDER BY relative_path").fetchall()
        ]
        conn.execute("DELETE FROM similar_pairs")
        conn.execute(
            """INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id,face_safe)
               VALUES(?,?,?,?,?,?)""",
            (ids[0], ids[1], 0.9, "similar", ids[0], 0),
        )
        conn.commit()
        cache = SimilarityGroupCache()
        profile = BUILTIN_PROFILES["balanced"]
        self.assertEqual(cache.count(self.project.project_id, conn, profile), 1)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        hydrated = cache.get(self.project.project_id, conn, profile)
        conn.set_trace_callback(None)
        self.assertEqual(len(hydrated), 1)
        self.assertTrue(any("SELECT * FROM photos WHERE id IN" in sql for sql in statements))
        self.assertFalse(
            any(
                "SELECT * FROM photos WHERE status='active' AND error=''" in sql
                for sql in statements
            )
        )
        conn.close()

    def test_quarantine_batches_are_unique_and_partial_failure_is_recoverable(self):
        self.make_photo("a.jpg")
        self.make_photo("b.jpg", (30, 60, 90))
        self.scan()
        conn = connect_db(self.project.db_path)
        conn.execute("UPDATE photos SET decision='remove'")
        conn.commit()
        conn.close()

        real_move = shutil.move
        calls = 0

        def flaky_move(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated move failure")
            return real_move(source, destination)

        with mock.patch("cullumi.core.shutil.move", side_effect=flaky_move):
            first = apply_quarantine(self.project)
        self.assertEqual((first["moved"], first["skipped"]), (1, 1))
        second = apply_quarantine(self.project)
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        conn = connect_db(self.project.db_path)
        rows = {
            row["relative_path"]: row["status"]
            for row in conn.execute("SELECT relative_path,status FROM photos")
        }
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quarantine_batches").fetchone()[0], 2)
        conn.close()
        self.assertEqual(rows, {"a.jpg": "quarantined", "b.jpg": "quarantined"})
        restored = restore_batch(self.project, first["batch_id"])
        self.assertEqual(restored["restored"], 1)
        self.assertTrue((self.photos / "a.jpg").exists())

    def test_restore_rejects_manifest_path_traversal(self):
        self.make_photo("safe.jpg")
        self.scan()
        conn = connect_db(self.project.db_path)
        conn.execute("UPDATE photos SET decision='remove'")
        conn.commit()
        conn.close()
        batch = apply_quarantine(self.project)
        conn = connect_db(self.project.db_path)
        manifest_path = Path(
            conn.execute(
                "SELECT manifest_path FROM quarantine_batches WHERE id=?", (batch["batch_id"],)
            ).fetchone()[0]
        )
        conn.close()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[0]["quarantine_path"] = "../outside.jpg"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        outside = self.base / "outside.jpg"
        outside.write_bytes(b"do not move")
        with self.assertRaisesRegex(ValueError, "超出项目目录"):
            restore_batch(self.project, batch["batch_id"])
        self.assertEqual(outside.read_bytes(), b"do not move")

    def test_cache_migration_retains_then_cleans_old(self):
        self.make_photo("a.jpg")
        self.scan()
        old = self.project.project_dir
        result = self.manager.migrate_cache(self.project.project_id, str(self.base / "cache2"))
        self.assertTrue(result["changed"])
        self.assertTrue(old.exists())
        self.assertTrue(Path(result["path"]).exists())
        cleaned = self.manager.cleanup_old_cache(self.project.project_id, str(old))
        self.assertTrue(cleaned["cleaned"])
        self.assertFalse(old.exists())

    def test_cache_migration_accepts_existing_empty_project_directory(self):
        target_root = self.base / "cache-empty-target"
        (target_root / self.project.project_id).mkdir(parents=True)
        result = self.manager.migrate_cache(self.project.project_id, str(target_root))
        self.assertTrue(result["changed"])
        self.assertTrue(Path(result["path"]).is_dir())


if __name__ == "__main__":
    unittest.main()
