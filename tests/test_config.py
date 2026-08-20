from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cullumi.core import BUILTIN_PROFILES, ConfigStore


class ConfigRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def damaged_backups(self) -> list[Path]:
        return sorted(self.root.glob("config.damaged-*.json"))

    def test_profile_in_use_must_be_switched_before_it_can_be_deleted(self) -> None:
        config = ConfigStore(self.path)
        profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
        profile.update({"id": "custom-active", "name": "正在使用的模式"})
        saved = config.save_custom_profile(profile)
        with config.edit() as data:
            data["projects"]["project"] = {
                "root": str(self.root / "photos"),
                "profile_id": saved["id"],
            }

        with self.assertRaisesRegex(ValueError, "仍被项目使用"):
            config.delete_custom_profile(saved["id"])

        with config.edit() as data:
            data["projects"]["project"]["profile_id"] = "balanced"
        config.delete_custom_profile(saved["id"])
        self.assertNotIn(saved["id"], config.data["custom_profiles"])

    def test_malformed_json_is_backed_up_and_repaired(self) -> None:
        original = '{"theme": "night"'
        self.path.write_text(original, encoding="utf-8")

        config = ConfigStore(self.path)

        self.assertEqual(config.data["theme"], "day")
        self.assertIn("配置文件损坏", config.load_warning or "")
        backups = self.damaged_backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), config.data)

    def test_each_repair_uses_a_unique_backup_name(self) -> None:
        originals = ["not-json-one", "not-json-two"]
        for original in originals:
            self.path.write_text(original, encoding="utf-8")
            ConfigStore(self.path)

        backups = self.damaged_backups()
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {item.read_text(encoding="utf-8") for item in backups}, set(originals)
        )

    def test_non_object_top_level_is_treated_as_damaged(self) -> None:
        self.path.write_text('["night"]', encoding="utf-8")

        config = ConfigStore(self.path)

        self.assertEqual(config.data["projects"], {})
        self.assertIn("顶层必须是 JSON 对象", config.load_warning or "")
        self.assertEqual(len(self.damaged_backups()), 1)

    def test_valid_sections_are_preserved_while_invalid_types_are_normalized(self) -> None:
        profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
        profile.update({"id": "wrong-id", "builtin": True, "name": "可恢复模式"})
        payload = {
            "version": "one",
            "default_cache_root": str(self.root / "cache"),
            "auto_advance": False,
            "auto_check_updates": "false",
            "theme": " NIGHT ",
            "projects": {
                "good-project": {
                    "root": str(self.root / "photos"),
                    "cache_root": 123,
                    "profile_id": "missing-profile",
                    "old_caches": [str(self.root / "old"), 456],
                    "future_project_value": {"preserved": True},
                },
                "bad-project": 42,
                "missing-root": {"profile_id": "conservative"},
            },
            "recent_projects": ["good-project", 12, "good-project", "missing"],
            "custom_profiles": {
                "custom-good": profile,
                "custom-broken": {"name": "不完整"},
                "balanced": copy.deepcopy(BUILTIN_PROFILES["balanced"]),
            },
            "future_setting": {"preserved": True},
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        config = ConfigStore(self.path)

        self.assertEqual(config.data["version"], 1)
        self.assertFalse(config.data["auto_advance"])
        self.assertTrue(config.data["auto_check_updates"])
        self.assertEqual(config.data["theme"], "night")
        self.assertEqual(config.data["future_setting"], {"preserved": True})
        self.assertEqual(set(config.data["projects"]), {"good-project"})
        project = config.data["projects"]["good-project"]
        self.assertEqual(project["cache_root"], str(self.root / "cache"))
        self.assertEqual(project["profile_id"], "conservative")
        self.assertEqual(project["old_caches"], [str(self.root / "old")])
        self.assertEqual(project["future_project_value"], {"preserved": True})
        self.assertEqual(config.data["recent_projects"], ["good-project"])
        self.assertEqual(set(config.data["custom_profiles"]), {"custom-good"})
        recovered_profile = config.data["custom_profiles"]["custom-good"]
        self.assertEqual(recovered_profile["id"], "custom-good")
        self.assertFalse(recovered_profile["builtin"])
        self.assertIn("原文件已备份", config.load_warning or "")
        self.assertEqual(len(self.damaged_backups()), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), config.data)

    def test_valid_residual_temp_is_recovered_when_main_file_is_missing(self) -> None:
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text('{"theme": "night"}', encoding="utf-8")

        config = ConfigStore(self.path)

        self.assertEqual(config.data["theme"], "night")
        self.assertIn("临时文件恢复", config.load_warning or "")
        self.assertTrue(self.path.is_file())
        self.assertFalse(temp_path.exists())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), config.data)

    def test_valid_temp_can_recover_a_damaged_main_file(self) -> None:
        original = "broken-main"
        self.path.write_text(original, encoding="utf-8")
        self.path.with_suffix(".tmp").write_text(
            '{"theme": "night", "auto_advance": false}', encoding="utf-8"
        )

        config = ConfigStore(self.path)

        self.assertEqual(config.data["theme"], "night")
        self.assertFalse(config.data["auto_advance"])
        self.assertIn("残留的临时文件恢复", config.load_warning or "")
        backups = self.damaged_backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_damaged_main_and_temp_are_both_backed_up_before_defaults_are_saved(self) -> None:
        self.path.write_text("broken-main", encoding="utf-8")
        self.path.with_suffix(".tmp").write_text("broken-temp", encoding="utf-8")

        config = ConfigStore(self.path)

        self.assertEqual(config.data["theme"], "day")
        backups = self.damaged_backups()
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {item.read_text(encoding="utf-8") for item in backups},
            {"broken-main", "broken-temp"},
        )
        self.assertIn("临时配置也已损坏", config.load_warning or "")

    def test_original_is_not_overwritten_when_backup_fails(self) -> None:
        original = "broken-but-important"
        self.path.write_text(original, encoding="utf-8")

        with mock.patch("cullumi.core.shutil.copy2", side_effect=OSError("read only")):
            config = ConfigStore(self.path)

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.damaged_backups(), [])
        self.assertEqual(config.data["theme"], "day")
        self.assertIn("无法备份", config.load_warning or "")
        self.assertIn("未覆盖", config.load_warning or "")

    def test_load_warning_is_read_only(self) -> None:
        config = ConfigStore(self.path)

        self.assertIsNone(config.load_warning)
        with self.assertRaises(AttributeError):
            config.load_warning = "replacement"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
