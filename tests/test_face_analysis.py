from __future__ import annotations

import copy
import hashlib
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from cullumi.config import BUILTIN_PROFILES, ConfigStore, validate_profile
from cullumi.face_analysis import (
    MODEL_SHA256,
    MODEL_VERSION,
    FaceAnalyzer,
    FaceDetection,
    empty_blink_values,
)
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import ScanCancelled, Scanner
from cullumi.similarity import build_similarity_groups


def face(x: float, y: float, score: float = 0.95) -> FaceDetection:
    landmarks = np.asarray(
        [
            [x + 12, y + 14],
            [x + 36, y + 14],
            [x + 24, y + 25],
            [x + 15, y + 37],
            [x + 34, y + 37],
        ],
        dtype=np.float32,
    )
    return FaceDetection(x, y, 48, 48, landmarks, score)


class EyeSession:
    def __init__(self, probabilities: list[float]):
        self.probabilities = np.asarray(probabilities, dtype=np.float32)

    def run(self, _outputs, _inputs):
        return [self.probabilities]


class FakeFaceAnalyzer:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def input_fingerprint(self, _thumbnail, row, _thresholds):
        return f"fingerprint-{int(row['cover_revision'] or 0)}"

    def analyze(self, _thumbnail, row, _profile):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model unavailable")
        result = empty_blink_values("open")
        result.update(
            {
                "blink_face_count": 1,
                "blink_confidence": 0.96,
                "blink_model_version": MODEL_VERSION,
                "blink_input_fingerprint": self.input_fingerprint(
                    None, row, None
                ),
            }
        )
        return result


class FaceAnalysisTests(unittest.TestCase):
    def test_shipped_models_match_the_pinned_checksums(self):
        model_root = Path(__file__).resolve().parent.parent / "models"
        for filename, expected in MODEL_SHA256.items():
            with (model_root / filename).open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            self.assertEqual(actual, expected)

    def test_yunet_decoder_uses_stride_coordinates_and_nms(self):
        outputs = {}
        for stride in (8, 16, 32):
            count = (640 // stride) ** 2
            outputs[f"cls_{stride}"] = np.zeros((1, count, 1), np.float32)
            outputs[f"obj_{stride}"] = np.zeros((1, count, 1), np.float32)
            outputs[f"bbox_{stride}"] = np.zeros((1, count, 4), np.float32)
            outputs[f"kps_{stride}"] = np.zeros((1, count, 10), np.float32)
        index = 80 + 2
        outputs["cls_8"][0, index, 0] = 1
        outputs["obj_8"][0, index, 0] = 1

        detections = FaceAnalyzer.decode_yunet(outputs, 0.85)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].width, 8)
        self.assertEqual(detections[0].height, 8)
        self.assertEqual(detections[0].x, 12)
        self.assertEqual(detections[0].y, 4)

    def test_multi_face_results_aggregate_closed_and_open_people(self):
        with tempfile.TemporaryDirectory() as temporary:
            thumbnail = Path(temporary) / "thumb.jpg"
            Image.new("RGB", (100, 100), (120, 100, 90)).save(thumbnail)
            analyzer = FaceAnalyzer(Path(temporary))
            faces = [face(100, 100), face(300, 300)]
            row = {
                "cover_source": "still",
                "cover_time_ms": 0,
                "cover_revision": 0,
            }
            with (
                mock.patch.object(
                    analyzer,
                    "_sessions",
                    return_value=(object(), EyeSession([0.95, 0.92, 0.1, 0.2])),
                ),
                mock.patch.object(analyzer, "_detect_faces", return_value=faces),
            ):
                result = analyzer.analyze(
                    thumbnail, row, BUILTIN_PROFILES["balanced"]
                )

        self.assertEqual(result["blink_status"], "closed")
        self.assertEqual(result["blink_face_count"], 2)
        self.assertEqual(result["blink_closed_face_count"], 1)
        self.assertEqual(result["blink_uncertain_face_count"], 0)
        self.assertEqual(result["blink_closed_ratio"], 0.5)

    def test_detailed_analysis_exposes_faces_only_for_offline_evaluation(self):
        with tempfile.TemporaryDirectory() as temporary:
            thumbnail = Path(temporary) / "thumb.jpg"
            Image.new("RGB", (100, 100), (120, 100, 90)).save(thumbnail)
            analyzer = FaceAnalyzer(Path(temporary))
            faces = [face(100, 100), face(300, 300)]
            row = {
                "cover_source": "still",
                "cover_time_ms": 0,
                "cover_revision": 0,
            }
            with (
                mock.patch.object(
                    analyzer,
                    "_sessions",
                    return_value=(object(), EyeSession([0.95, 0.92, 0.1, 0.2])),
                ),
                mock.patch.object(analyzer, "_detect_faces", return_value=faces),
            ):
                regular = analyzer.analyze(
                    thumbnail, row, BUILTIN_PROFILES["balanced"]
                )
                detailed = analyzer.analyze_detailed(
                    thumbnail, row, BUILTIN_PROFILES["balanced"]
                )

        self.assertNotIn("faces", regular)
        self.assertEqual(
            [observation["status"] for observation in detailed["faces"]],
            ["open", "closed"],
        )
        self.assertEqual(
            detailed["faces"][0]["eye_open_probabilities"],
            [0.95, 0.92],
        )
        self.assertEqual(detailed["faces"][1]["confidence"], 0.9)

    def test_ocec_eye_crop_preserves_rgb_channel_order(self):
        image = Image.new("RGB", (100, 100), (204, 102, 51))
        try:
            crop = FaceAnalyzer._eye_crop(
                image,
                np.asarray([50, 50], dtype=np.float32),
                30,
                0,
            )
        finally:
            image.close()
        means = crop.mean(axis=(1, 2))
        np.testing.assert_allclose(means, [0.8, 0.4, 0.2], atol=0.01)

    def test_threshold_validation_and_old_profile_completion(self):
        old_profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
        del old_profile["similarity"]["blink"]
        with tempfile.TemporaryDirectory() as temporary:
            config = ConfigStore(Path(temporary) / "config.json")
            old_profile.update({"id": "", "name": "旧模式", "builtin": False})
            saved = config.save_custom_profile(old_profile)
        self.assertEqual(
            saved["similarity"]["blink"],
            BUILTIN_PROFILES["balanced"]["similarity"]["blink"],
        )

        invalid = copy.deepcopy(BUILTIN_PROFILES["balanced"])
        invalid["similarity"]["blink"]["min_eye_distance_px"] = 12.5
        with self.assertRaisesRegex(ValueError, "整数"):
            validate_profile(invalid)
        invalid = copy.deepcopy(BUILTIN_PROFILES["balanced"])
        invalid["similarity"]["blink"].update(
            {"open_confidence_min": 0.5, "closed_confidence_min": 0.5}
        )
        with self.assertRaisesRegex(ValueError, "之和"):
            validate_profile(invalid)


class BlinkScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        photos = root / "photos"
        photos.mkdir()
        self.config = ConfigStore(root / "config.json")
        self.config.data["default_cache_root"] = str(root / "cache")
        self.config.save()
        self.manager = ProjectManager(self.config)
        self.project = self.manager.open(str(photos))
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            Image.new("RGB", (80, 60), (110, 120, 130)).save(
                self.project.thumb_dir / name
            )
        with closing(connect_db(self.project.db_path)) as conn:
            conn.executemany(
                "INSERT INTO photos(relative_path,thumbnail,status,error) "
                "VALUES(?,?,'active','')",
                [("a.jpg", "a.jpg"), ("b.jpg", "b.jpg"), ("c.jpg", "c.jpg")],
            )
            conn.execute(
                "INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id) "
                "VALUES(1,2,0.9,'similar',1)"
            )
            conn.execute(
                "INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id) "
                "VALUES(2,3,1,'exact',2)"
            )
            conn.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def test_incremental_analysis_uses_only_non_exact_candidates_and_cache(self):
        analyzer = FakeFaceAnalyzer()
        scanner = Scanner(self.config, self.manager, face_analyzer=analyzer)
        profile = BUILTIN_PROFILES["balanced"]
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(
                scanner.analyze_blinks(self.project, conn, profile), 2
            )
            self.assertEqual(
                scanner.analyze_blinks(self.project, conn, profile), 0
            )
            statuses = dict(
                conn.execute(
                    "SELECT relative_path,blink_status FROM photos"
                ).fetchall()
            )
        self.assertEqual(analyzer.calls, 2)
        self.assertEqual(
            statuses,
            {"a.jpg": "open", "b.jpg": "open", "c.jpg": "not_analyzed"},
        )

    def test_rescan_requirement_uses_cache_fingerprints_without_model_loading(self):
        analyzer = FakeFaceAnalyzer()
        scanner = Scanner(self.config, self.manager, face_analyzer=analyzer)
        profile = BUILTIN_PROFILES["balanced"]
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertTrue(scanner.blink_rescan_required(self.project, conn, profile))
            scanner.analyze_blinks(self.project, conn, profile)
            self.assertFalse(scanner.blink_rescan_required(self.project, conn, profile))
            conn.execute(
                "UPDATE photos SET cover_revision=cover_revision+1 WHERE id=1"
            )
            conn.commit()
            self.assertTrue(scanner.blink_rescan_required(self.project, conn, profile))
        self.assertEqual(analyzer.calls, 2)

    def test_disabled_or_cancelled_analysis_never_loads_the_model(self):
        analyzer = FakeFaceAnalyzer()
        scanner = Scanner(self.config, self.manager, face_analyzer=analyzer)
        with self.config.edit() as data:
            data["blink_detection_enabled"] = False
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(
                scanner.analyze_blinks(
                    self.project, conn, BUILTIN_PROFILES["balanced"]
                ),
                0,
            )
        self.assertEqual(analyzer.calls, 0)

        with self.config.edit() as data:
            data["blink_detection_enabled"] = True
        cancelled = threading.Event()
        cancelled.set()
        with closing(connect_db(self.project.db_path)) as conn:
            with self.assertRaises(ScanCancelled):
                scanner.analyze_blinks(
                    self.project,
                    conn,
                    BUILTIN_PROFILES["balanced"],
                    cancelled,
                )
        self.assertEqual(analyzer.calls, 0)

    def test_model_failure_is_recorded_without_raising(self):
        scanner = Scanner(
            self.config,
            self.manager,
            face_analyzer=FakeFaceAnalyzer(fail=True),
        )
        with closing(connect_db(self.project.db_path)) as conn:
            analyzed = scanner.analyze_blinks(
                self.project, conn, BUILTIN_PROFILES["balanced"]
            )
            rows = conn.execute(
                "SELECT blink_status,blink_error FROM photos WHERE id IN (1,2)"
            ).fetchall()
        self.assertEqual(analyzed, 2)
        self.assertTrue(all(row["blink_status"] == "error" for row in rows))
        self.assertTrue(all("model unavailable" in row["blink_error"] for row in rows))

    def test_recommendation_prefers_open_faces_and_toggle_restores_quality(self):
        profile = BUILTIN_PROFILES["balanced"]
        with closing(connect_db(self.project.db_path)) as conn:
            conn.execute("DELETE FROM similar_pairs")
            conn.execute(
                "UPDATE photos SET sharpness=10,blink_status='open',"
                "blink_face_count=1,blink_uncertain_face_count=0 WHERE id=1"
            )
            conn.execute(
                "UPDATE photos SET sharpness=10000,blink_status='closed',"
                "blink_face_count=1,blink_closed_face_count=1,"
                "blink_closed_ratio=1,blink_uncertain_face_count=0 WHERE id=2"
            )
            conn.execute(
                "INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id) "
                "VALUES(1,2,0.9,'similar',2)"
            )
            conn.commit()
            enabled = build_similarity_groups(conn, profile, True)[0]
            disabled = build_similarity_groups(conn, profile, False)[0]
        self.assertEqual(enabled["recommended_id"], 1)
        self.assertEqual(disabled["recommended_id"], 2)


if __name__ == "__main__":
    unittest.main()
