from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from cullumi.core import (
    DISPLAY_PREVIEW_EXTENSIONS,
    DISPLAY_PREVIEW_MAX_SIZE,
    analyze_photo,
    ensure_display_preview,
)


class MediaPreviewTests(unittest.TestCase):
    def test_analysis_reuses_the_decoded_image_instead_of_copying_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.jpg"
            source_path.write_bytes(b"placeholder")
            decoded = Image.new("RGB", (1600, 900), "teal")
            decoded.copy = mock.Mock(side_effect=AssertionError("full-size copy"))

            with mock.patch("cullumi.media.open_image", return_value=(decoded, "")):
                result = analyze_photo(source_path, root / "thumb.jpg")

            self.assertEqual(result["error"], "")
            decoded.copy.assert_not_called()
            self.assertTrue((root / "thumb.jpg").is_file())

    def test_special_formats_include_tiff_raw_and_heif(self):
        self.assertIn(".tiff", DISPLAY_PREVIEW_EXTENSIONS)
        self.assertIn(".dng", DISPLAY_PREVIEW_EXTENSIONS)
        self.assertIn(".heic", DISPLAY_PREVIEW_EXTENSIONS)

    def test_display_preview_is_high_resolution_cached_and_refreshed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "wide.tiff"
            thumbnail = root / "thumbs" / "wide.jpg"
            with Image.new("RGB", (3000, 120), "navy") as image:
                image.save(source, "TIFF")

            first = ensure_display_preview(source, thumbnail)
            self.assertTrue(first.is_file())
            with Image.open(first) as preview:
                self.assertEqual(preview.format, "JPEG")
                self.assertEqual(max(preview.size), max(DISPLAY_PREVIEW_MAX_SIZE))

            with mock.patch(
                "cullumi.media.open_image",
                side_effect=AssertionError("cached preview should be reused"),
            ):
                self.assertEqual(ensure_display_preview(source, thumbnail), first)

            with Image.new("RGB", (2800, 140), "maroon") as image:
                image.save(source, "TIFF")
            second = ensure_display_preview(source, thumbnail)

            self.assertNotEqual(second, first)
            self.assertTrue(second.is_file())
            self.assertFalse(first.exists())


if __name__ == "__main__":
    unittest.main()
