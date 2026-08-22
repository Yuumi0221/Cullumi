from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cullumi.fs_utils import atomic_write_json, is_within


class FileSystemUtilityTests(unittest.TestCase):
    def test_is_within_accepts_root_and_descendants_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            self.assertTrue(is_within(root, root))
            self.assertTrue(is_within(root / "nested" / "photo.jpg", root))
            self.assertFalse(is_within(root.parent / "outside.jpg", root))

    def test_atomic_write_json_replaces_payload_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "manifest.json"
            target.write_text('{"old": true}', encoding="utf-8")
            atomic_write_json(target, {"items": ["照片.jpg"]})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"items": ["照片.jpg"]},
            )
            self.assertFalse(target.with_suffix(".json.tmp").exists())
