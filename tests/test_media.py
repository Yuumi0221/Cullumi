from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from cullumi.media import (
    DISPLAY_PREVIEW_EXTENSIONS,
    DISPLAY_PREVIEW_MAX_SIZE,
    MotionAsset,
    analyze_photo,
    embedded_motion_asset,
    ensure_display_preview,
    ensure_motion_video,
    extract_motion_frame,
    paired_motion_asset,
    probe_motion,
)


class MediaPreviewTests(unittest.TestCase):
    def test_standard_android_motion_photo_xmp_locates_appended_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "motion.jpg"
            video = b"video-payload"
            xmp = (
                b'<rdf:Description GCamera:MotionPhoto="1" '
                b'GCamera:MicroVideoOffset="13"/>'
            )
            path.write_bytes(b"jpeg" + xmp + video)

            asset = embedded_motion_asset(path)

            self.assertIsNotNone(asset)
            self.assertEqual(asset.kind, "android_embedded")
            self.assertEqual(asset.length, len(video))
            self.assertEqual(path.read_bytes()[asset.offset :], video)

    def test_ordinary_jpeg_with_mp4_bytes_is_not_guessed_as_motion_photo(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ordinary.jpg"
            path.write_bytes(b"jpeg-data-ftyp-isom-video")

            self.assertIsNone(embedded_motion_asset(path))

    def test_live_photo_sidecar_pairs_by_folder_and_stem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo = root / "IMG_0001.HEIC"
            video = root / "IMG_0001.MOV"
            photo.touch()
            video.touch()

            asset = paired_motion_asset(
                photo, {(root.resolve(), "img_0001"): video}
            )

            self.assertIsNotNone(asset)
            self.assertEqual(asset.kind, "apple_sidecar")
            self.assertEqual(asset.path, video)

    def test_motion_video_can_be_probed_transcoded_and_sampled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mov"
            from cullumi.media import ffmpeg_executable

            subprocess.run(
                [
                    ffmpeg_executable(), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
                    "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(source),
                ],
                check=True,
            )
            asset = MotionAsset("apple_sidecar", source)

            metadata = probe_motion(asset)
            video = ensure_motion_video(asset, root / "cache")
            frame = extract_motion_frame(video, 200, root / "frame.jpg")

            self.assertEqual(metadata["motion_width"], 160)
            self.assertEqual(metadata["motion_height"], 120)
            self.assertGreater(metadata["motion_frame_count"], 1)
            self.assertTrue(video.is_file())
            with Image.open(frame) as image:
                self.assertEqual(image.size, (160, 120))

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
