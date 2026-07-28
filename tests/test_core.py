from __future__ import annotations

import csv
import json
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from photoculler.core import (
    BUILTIN_PROFILES,
    ConfigStore,
    ProjectManager,
    Scanner,
    apply_quarantine,
    connect_db,
    import_decisions,
    quarantine_preview,
    restore_batch,
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
        validate_profile(BUILTIN_PROFILES["balanced"])
        broken = json.loads(json.dumps(BUILTIN_PROFILES["balanced"]))
        broken["name"] = ""
        with self.assertRaises(ValueError):
            validate_profile(broken)

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
        analyzed = conn.execute("SELECT analyzed_at FROM photos WHERE relative_path='IMG_0001.jpg'").fetchone()[0]
        conn.close()
        time.sleep(0.02)
        self.scan()
        conn = connect_db(self.project.db_path)
        self.assertEqual(conn.execute("SELECT analyzed_at FROM photos WHERE relative_path='IMG_0001.jpg'").fetchone()[0], analyzed)
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
