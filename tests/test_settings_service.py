from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from cullumi.config import ConfigStore
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import Scanner
from cullumi.settings_service import save_profile, save_settings
from cullumi.similarity import SimilarityGroupCache


class SettingsServiceTests(unittest.TestCase):
    def test_settings_are_validated_before_filesystem_or_config_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ConfigStore(root / "config.json")
            cache = root / "new-cache"
            before = config.snapshot()

            with self.assertRaisesRegex(ValueError, "主题"):
                save_settings(
                    config,
                    {"default_cache_root": str(cache), "theme": "invalid"},
                )

            self.assertEqual(config.snapshot(), before)
            self.assertFalse(cache.exists())

    def test_valid_settings_are_committed_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ConfigStore(root / "config.json")
            cache = root / "new-cache"

            saved = save_settings(
                config,
                {
                    "default_cache_root": str(cache),
                    "theme": "night",
                    "auto_advance": False,
                },
            )

            self.assertTrue(cache.is_dir())
            self.assertEqual(saved["default_cache_root"], str(cache.resolve()))
            self.assertEqual(saved["theme"], "night")
            self.assertFalse(saved["auto_advance"])

    def test_active_profile_blink_change_reapplies_and_rolls_back_on_failure(self):
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
            groups = SimilarityGroupCache()
            profile = config.get_profile("conservative")
            profile["id"] = ""
            profile["name"] = "测试模式"
            saved = config.save_custom_profile(profile)
            with config.edit() as data:
                data["projects"][project.project_id]["profile_id"] = saved["id"]
            with closing(connect_db(project.db_path)) as conn:
                conn.execute(
                    "UPDATE project SET profile_id=? WHERE id=1", (saved["id"],)
                )
                conn.commit()

            changed = config.get_profile(saved["id"])
            changed["similarity"]["blink"]["face_confidence_min"] = 0.9
            with (
                mock.patch.object(scanner, "reclassify") as reclassify,
                mock.patch.object(scanner, "rebuild_similarity") as rebuild,
                mock.patch.object(scanner, "analyze_blinks") as analyze,
            ):
                save_profile(
                    config, manager, scanner, groups, changed, project.project_id
                )
            reclassify.assert_not_called()
            rebuild.assert_not_called()
            analyze.assert_called_once()

            original = config.get_profile(saved["id"])
            failing = config.get_profile(saved["id"])
            failing["similarity"]["blink"]["face_confidence_min"] = 0.91
            with mock.patch.object(
                scanner, "analyze_blinks", side_effect=RuntimeError("分析失败")
            ):
                with self.assertRaisesRegex(RuntimeError, "分析失败"):
                    save_profile(
                        config,
                        manager,
                        scanner,
                        groups,
                        failing,
                        project.project_id,
                    )
            self.assertEqual(config.get_profile(saved["id"]), original)
