from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageDraw

from photoculler import __version__
from photoculler.core import (
    BUILTIN_PROFILES,
    ConfigStore,
    ProjectManager,
    Scanner,
    SimilarityGroupCache,
    apply_quarantine,
    build_similarity_groups,
    classification_percentiles,
    clear_decisions,
    connect_db,
    HEIF_EXTENSIONS,
    import_decisions,
    mark_ai_remove_suggestions,
    parse_photo_filter,
    photo_filter_where,
    photo_library_counts,
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    quarantine_preview,
    restore_batch,
    open_image,
    validate_profile,
)


class PhotoCullerTests(unittest.TestCase):
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

    def test_heif_phone_variants_use_tolerant_decoder(self):
        import pillow_heif.options

        self.assertTrue(pillow_heif.options.ALLOW_INCORRECT_HEADERS)
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
            "photoculler.core.build_similarity_groups", wraps=build_similarity_groups
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


if __name__ == "__main__":
    unittest.main()
