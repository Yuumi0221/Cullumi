from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from photoculler.updates import (
    check_for_update,
    download_release_asset,
    select_release_asset,
    version_key,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class UpdateTests(unittest.TestCase):
    def test_version_and_windows_asset_selection(self):
        self.assertEqual(version_key("v1.3.0"), (1, 3, 0))
        self.assertGreater(version_key("1.10.0"), version_key("1.9.9"))
        asset = select_release_asset([
            {"name": "source.zip", "browser_download_url": "https://github.com/source.zip"},
            {"name": "照片筛选器-v1.4.0-Windows-便携版.zip", "browser_download_url": "https://github.com/app.zip"},
            {"name": "installer.msi", "browser_download_url": "https://github.com/app.msi"},
        ])
        self.assertEqual(asset["name"], "照片筛选器-v1.4.0-Windows-便携版.zip")
        self.assertIsNone(select_release_asset([
            {"name": "source.zip", "browser_download_url": "https://github.com/source.zip"},
        ]))

    def test_check_for_update_uses_latest_release_asset(self):
        release = {
            "tag_name": "v1.4.0",
            "name": "Photo Culler 1.4.0",
            "html_url": "https://github.com/Yuumi0221/photo-culler/releases/tag/v1.4.0",
            "assets": [{
                "name": "photo-culler-v1.4.0-windows.zip",
                "browser_download_url": "https://github.com/Yuumi0221/photo-culler/releases/download/v1.4.0/app.zip",
            }],
        }

        def opener(request, timeout):
            self.assertEqual(timeout, 12)
            self.assertIn("api.github.com", request.full_url)
            return FakeResponse(json.dumps(release).encode("utf-8"))

        result = check_for_update("1.3.0", opener=opener)
        self.assertTrue(result["update_available"])
        self.assertTrue(result["download_available"])
        self.assertEqual(result["latest_version"], "1.4.0")

    def test_download_release_asset_uses_unique_downloads_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            (destination / "update.zip").write_bytes(b"old")

            def opener(request, timeout):
                self.assertEqual(timeout, 30)
                return FakeResponse(b"new update")

            result = download_release_asset(
                "https://github.com/Yuumi0221/photo-culler/releases/download/v1.4.0/update.zip",
                "update.zip",
                destination=destination,
                opener=opener,
            )
            self.assertEqual(result.name, "update (1).zip")
            self.assertEqual(result.read_bytes(), b"new update")
            self.assertFalse((destination / "update (1).zip.part").exists())


if __name__ == "__main__":
    unittest.main()
