from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cullumi.config import ConfigStore
from cullumi.settings_service import save_settings


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
