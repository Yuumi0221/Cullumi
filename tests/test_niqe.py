from __future__ import annotations

import copy
import hashlib
import io
import json
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import numpy as np
from niqe_cases import reference_cases
from PIL import Image

from cullumi import analysis_worker, media, niqe, project_store
from cullumi.classification import classify
from cullumi.config import (
    BUILTIN_PROFILES,
    ConfigStore,
    normalize_profile,
    validate_profile,
)
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import Scanner
from cullumi.similarity import build_similarity_groups, quality_score

ROOT = Path(__file__).resolve().parents[1]


def good_photo(**changes):
    return {
        "error": "", "sharpness": 1500, "luminance": 110, "dark_clip": 0,
        "bright_clip": 0, "contrast": 60, "entropy": 7, "megapixels": 12,
        "size": 1_000_000, "niqe_score": 3, "niqe_error": "", **changes,
    }


class NiqeTests(unittest.TestCase):
    def test_parameter_and_license_integrity(self):
        directory = niqe.model_directory()
        source = json.loads((directory / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertEqual(source["license"], "Apache-2.0")
        for name, key in [("niqe_pris_params.npz", "parameters_sha256"), ("LICENSE.txt", "license_sha256")]:
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), source[key])
        self.assertEqual(source["parameters_sha256"], niqe.PARAMETERS_SHA256)
        self.assertIn("Apache License", (directory / "LICENSE.txt").read_text())

    def test_matches_laboratory_opencv_golden_scores(self):
        oracle = json.loads((ROOT / "tests/fixtures/niqe_reference.json").read_text())
        evaluator, error = niqe.initialize_niqe()
        self.assertFalse(error)
        cases = reference_cases()
        try:
            for name, image in cases.items():
                with self.subTest(name=name):
                    score = evaluator.compute(image)
                    self.assertAlmostEqual(score, oracle["scores"][name], delta=oracle["tolerance_absolute"])
                    self.assertEqual(score, evaluator.compute(image))
        finally:
            for image in cases.values():
                image.close()

    def test_separable_convolution_matches_full_7_by_7_replicated_kernel(self):
        evaluator, _ = niqe.initialize_niqe()
        image = np.random.default_rng(1).uniform(0, 255, (37, 61))
        padded = np.pad(image, 3, mode="edge")
        reference = sum(evaluator.window[y, x] * padded[y:y+37, x:x+61] for y in range(7) for x in range(7))
        np.testing.assert_allclose(niqe._convolve(image, evaluator.kernel), reference, atol=1e-10, rtol=0)

    def test_half_size_uses_matlab_keys_kernel(self):
        image = np.random.default_rng(12).normal(size=(96, 192))
        kernel = np.array([-3, -9, 29, 111, 111, 29, -9, -3]) / 256
        self.assertAlmostEqual(niqe._half_size(image)[10, 10], kernel @ image[17:25, 17:25] @ kernel)

    def test_small_constant_and_sparse_texture_fail_safely(self):
        sparse = np.full((384, 512, 3), 128, np.uint8)
        sparse[20:24, 20:24] = 200
        images = [Image.new("RGB", (32, 32)), Image.new("RGB", (512, 384), "gray"), Image.fromarray(sparse)]
        for image in images:
            with image:
                result = niqe.evaluate_preview(image)
                self.assertIsNone(result["niqe_score"])
                self.assertTrue(result["niqe_error"])
                self.assertEqual(result["niqe_version"], niqe.NIQE_INPUT_FAILURE_VERSION)
                self.assertTrue(niqe.niqe_is_current(result))

    def test_initializer_caches_only_success(self):
        for broken, expected_calls in ((False, 1), (True, 3)):
            niqe.initialize_niqe.cache_clear()
            with mock.patch.object(niqe, "NiqeEvaluator", side_effect=ValueError("broken") if broken else None) as evaluator:
                for _ in range(3):
                    niqe.initialize_niqe()
                self.assertEqual(evaluator.call_count, expected_calls)
            niqe.initialize_niqe.cache_clear()

    def test_transient_failures_retry_and_are_not_cached_as_complete(self):
        preview = Image.new("RGB", (512, 288), "gray")
        try:
            evaluator = mock.Mock()
            evaluator.compute.return_value = 4.25
            niqe.initialize_niqe.cache_clear()
            with mock.patch.object(
                niqe, "NiqeEvaluator", side_effect=[MemoryError("temporary"), evaluator]
            ) as constructor:
                result = niqe.evaluate_preview(preview)
            self.assertEqual(result["niqe_score"], 4.25)
            self.assertEqual(constructor.call_count, 2)
            self.assertTrue(niqe.niqe_is_current(result))

            evaluator.compute.reset_mock()
            evaluator.compute.side_effect = RuntimeError("temporary compute failure")
            result = niqe.evaluate_preview(preview)
            self.assertIsNone(result["niqe_score"])
            self.assertEqual(result["niqe_version"], "")
            self.assertFalse(niqe.niqe_is_current(result))
            self.assertEqual(evaluator.compute.call_count, 2)

            evaluator.compute.reset_mock(side_effect=True)
            evaluator.compute.return_value = 4.5
            recovered = niqe.evaluate_preview(preview)
            self.assertEqual(recovered["niqe_score"], 4.5)
            self.assertTrue(niqe.niqe_is_current(recovered))
        finally:
            preview.close()
            niqe.initialize_niqe.cache_clear()

    def test_worker_initializes_before_first_request_and_only_once(self):
        requests, responses = queue.Queue(), queue.Queue()
        requests.put((1, "a.jpg", "thumb.jpg", True))
        requests.put((2, "b.jpg", "thumb.jpg", True))
        requests.put(None)
        with mock.patch.object(analysis_worker, "initialize_niqe") as initialize, mock.patch.object(analysis_worker, "analyze_photo") as analyze:
            analyze.side_effect = lambda *_, **__: {
                "initialized": initialize.call_count
            }
            analysis_worker._worker_main(requests, responses, 0)
            initialize.assert_called_once()
            self.assertEqual(responses.get()[1], {"initialized": 1})
            self.assertEqual(responses.get()[1], {"initialized": 1})

    def test_disabled_worker_does_not_initialize_or_compute_niqe(self):
        requests, responses = queue.Queue(), queue.Queue()
        requests.put((1, "a.jpg", "thumb.jpg", False))
        requests.put(None)
        with (
            mock.patch.object(analysis_worker, "initialize_niqe") as initialize,
            mock.patch.object(analysis_worker, "analyze_photo") as analyze,
        ):
            analyze.return_value = {"niqe_score": None}
            analysis_worker._worker_main(requests, responses, 0)
            initialize.assert_not_called()
            analyze.assert_called_once_with(
                Path("a.jpg"), Path("thumb.jpg"), niqe_enabled=False
            )
            self.assertEqual(responses.get()[1], {"niqe_score": None})

    def test_corrupt_parameters_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "parameters.npz"
            path.write_bytes(b"broken")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                niqe.NiqeEvaluator(path)

    def test_portable_resource_path_and_packaging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(niqe.model_directory(), root / "models/niqe")
            with mock.patch.object(sys, "_MEIPASS", str(root), create=True):
                self.assertEqual(niqe.model_directory(), root / "models/niqe")
                niqe.NiqeEvaluator(niqe.model_directory() / "niqe_pris_params.npz")
        spec = (ROOT / "Cullumi.spec").read_text()
        self.assertIn('(\"models\", \"models\")', spec)
        self.assertIn('"SOURCE.json", "LICENSE.txt"', spec)

    def test_analysis_uses_one_existing_preview_and_failure_is_not_unreadable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for suffix in (".jpg", ".raf", ".heic"):
                source = root / ("source" + suffix)
                source.write_bytes(b"decoder-fixture")
                decoded = Image.new("RGB", (1600, 900), "teal")
                evaluator = mock.Mock()
                def compute(preview):
                    self.assertIs(preview, decoded)
                    self.assertEqual(preview.size, (512, 288))
                    raise niqe.NiqeInputError("insufficient texture")
                evaluator.compute.side_effect = compute
                with mock.patch.object(media, "open_image", return_value=(decoded, "")) as decoder, mock.patch.object(niqe, "initialize_niqe", return_value=(evaluator, "")):
                    result = media.analyze_photo(source, root / "thumb.jpg")
                decoder.assert_called_once_with(source, (512, 512))
                evaluator.compute.assert_called_once()
                self.assertEqual(result["error"], "")
                self.assertIsNone(result["niqe_score"])
                self.assertIn("texture", result["niqe_error"])
                self.assertEqual(source.read_bytes(), b"decoder-fixture")
                self.assertTrue((root / "thumb.jpg").is_file())

    def test_raw_embedded_preview_and_heif_decoder_continue_to_feed_niqe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with Image.fromarray(np.random.default_rng(27).integers(0, 256, (384, 512, 3), dtype=np.uint8)) as image:
                buffer = io.BytesIO()
                image.save(buffer, "JPEG")
                raw_path = root / "sample.raf"
                raw_path.write_bytes(b"raw-fixture")
                raw = mock.MagicMock()
                raw.__enter__.return_value = raw
                raw.extract_thumb.return_value = mock.Mock(format=media.rawpy.ThumbFormat.JPEG, data=buffer.getvalue())
                with mock.patch.object(media.rawpy, "imread", return_value=raw) as decoder:
                    result = media.analyze_photo(raw_path, root / "raw-thumb.jpg")
                decoder.assert_called_once_with(str(raw_path))
                raw.extract_thumb.assert_called_once()
                raw.postprocess.assert_not_called()
                self.assertEqual(result["error"], "")
                self.assertIsNotNone(result["niqe_score"])
                heif_path = root / "sample.heic"
                media.from_pillow(image).save(heif_path)
                with mock.patch.object(media, "_open_heif", wraps=media._open_heif) as decoder:
                    result = media.analyze_photo(heif_path, root / "heif-thumb.jpg")
                decoder.assert_called_once_with(heif_path)
                self.assertEqual(result["error"], "")
                self.assertIsNotNone(result["niqe_score"])

    def test_quality_mapping_missing_weights_and_validation(self):
        profile = copy.deepcopy(BUILTIN_PROFILES["conservative"])
        self.assertAlmostEqual(sum(profile["quality"]["weights"].values()), 1)
        self.assertGreater(quality_score(good_photo(niqe_score=3), profile), quality_score(good_photo(niqe_score=7), profile))
        missing = good_photo(niqe_score=None)
        legacy = copy.deepcopy(profile)
        legacy["quality"]["weights"] = {k: v / 0.8 for k, v in profile["quality"]["weights"].items() if k != "niqe"}
        self.assertAlmostEqual(quality_score(missing, profile), quality_score(missing, legacy))
        for value, error in [(0, "failed"), (float("nan"), ""), (-1, ""), (float("inf"), "")]:
            self.assertEqual(quality_score(good_photo(niqe_score=value, niqe_error=error), profile), quality_score(missing, profile))
        for key, value in [("niqe_review", 9), ("niqe_quality_good", 8), ("niqe_quality_bad", 1), ("niqe_remove", float("nan"))]:
            invalid = copy.deepcopy(profile)
            invalid["quality"][key] = value
            with self.assertRaises(ValueError):
                validate_profile(invalid)
        invalid = copy.deepcopy(profile)
        invalid["quality"]["weights"]["niqe"] = -0.2
        with self.assertRaises(ValueError):
            validate_profile(invalid)
        normalized = normalize_profile(legacy)
        self.assertAlmostEqual(normalized["quality"]["weights"]["niqe"], 0.2)
        self.assertEqual(normalized, normalize_profile(normalized))

    def test_safety_rules_in_any_and_all_modes(self):
        profile = copy.deepcopy(BUILTIN_PROFILES["conservative"])
        self.assertEqual(classify(good_photo(niqe_score=4.99), profile)[0], "keep")
        self.assertEqual(classify(good_photo(niqe_score=5), profile)[0], "review")
        self.assertEqual(classify(good_photo(niqe_score=8), profile)[0], "remove")
        self.assertEqual(classify(good_photo(niqe_score=100), profile)[0], "remove")
        self.assertEqual(classify(good_photo(niqe_score=None), profile)[0], "keep")
        profile["quality"]["enabled"]["niqe"] = False
        self.assertEqual(classify(good_photo(niqe_score=100), profile)[0], "keep")

        profile = copy.deepcopy(BUILTIN_PROFILES["conservative"])
        profile["quality"]["match_mode"] = "all"
        self.assertEqual(classify(good_photo(niqe_score=8), profile)[0], "review")
        self.assertEqual(
            classify(good_photo(niqe_score=8, sharpness=1), profile)[0],
            "remove",
        )
        profile = BUILTIN_PROFILES["conservative"]
        for severe in ({"sharpness": 1}, {"luminance": 0, "dark_clip": 1}, {"bright_clip": 1}):
            self.assertEqual(classify(good_photo(niqe_score=0, **severe), profile)[0], "remove")


class NiqeDatabaseTests(unittest.TestCase):
    def test_profile_toggle_controls_analysis_and_rescan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            photos = root / "photos"
            photos.mkdir()
            source = photos / "texture.png"
            with reference_cases()["noise-6"] as image:
                image.save(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            disabled = copy.deepcopy(config.get_profile("conservative"))
            disabled.update({"id": "", "name": "NIQE 关闭"})
            disabled["quality"]["enabled"]["niqe"] = False
            saved = config.save_custom_profile(disabled)
            with config.edit() as data:
                data["projects"][project.project_id]["profile_id"] = saved["id"]
            scanner = Scanner(config, manager)

            def scan() -> None:
                scanner.start(project.project_id)
                scanner.threads[project.project_id].join(20)
                self.assertEqual(
                    scanner.progress[project.project_id]["stage"], "complete"
                )

            with mock.patch.object(
                scanner, "analyze_photo", wraps=scanner.analyze_photo
            ) as analyze:
                scan()
                self.assertFalse(analyze.call_args.kwargs["niqe_enabled"])
                with closing(connect_db(project.db_path)) as conn:
                    row = conn.execute("SELECT * FROM photos").fetchone()
                    self.assertEqual(row["niqe_version"], "")
                    self.assertFalse(
                        scanner.niqe_rescan_required(conn, saved)
                    )

                enabled = copy.deepcopy(saved)
                enabled["quality"]["enabled"]["niqe"] = True
                config.save_custom_profile(enabled)
                scan()
                self.assertTrue(analyze.call_args.kwargs["niqe_enabled"])
                with closing(connect_db(project.db_path)) as conn:
                    row = conn.execute("SELECT * FROM photos").fetchone()
                    self.assertEqual(row["niqe_version"], niqe.NIQE_VERSION)
                    self.assertFalse(
                        scanner.niqe_rescan_required(conn, enabled)
                    )
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(), original_hash
            )

    def test_v5_migration_preserves_decisions_and_backs_up(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.db"
            conn = connect_db(path)
            conn.execute("INSERT INTO photos(relative_path,decision) VALUES('kept.jpg','keep')")
            for column in ("niqe_score", "niqe_error", "niqe_version"):
                conn.execute(f"ALTER TABLE photos DROP COLUMN {column}")
            conn.execute("PRAGMA user_version=5")
            conn.commit()
            conn.close()
            project_store._INITIALIZED_DATABASES.pop(path.resolve(), None)
            with connect_db(path) as migrated:
                row = migrated.execute("SELECT * FROM photos").fetchone()
                self.assertEqual(row["decision"], "keep")
                self.assertIsNone(row["niqe_score"])
                self.assertEqual(row["niqe_version"], "")
                self.assertTrue(
                    Scanner(mock.Mock(), mock.Mock()).niqe_rescan_required(
                        migrated, BUILTIN_PROFILES["conservative"]
                    )
                )
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 7)
            migrated.close()
            backups = list(path.parent.glob("project.pre-v6-*.db"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertEqual(backup.execute("SELECT decision FROM photos").fetchone()[0], "keep")
            backup.close()

    def test_incremental_scan_reuses_success_and_failure_and_refreshes_old_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            photos = root / "photos"
            photos.mkdir()
            for name, image in [("texture.png", reference_cases()["noise-6"]), ("plain.png", Image.new("RGB", (512, 384), "gray"))]:
                with image:
                    image.save(photos / name)
            original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in photos.iterdir()}
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            scanner = Scanner(config, manager)
            def scan():
                scanner.start(project.project_id)
                scanner.threads[project.project_id].join(20)
                self.assertEqual(scanner.progress[project.project_id]["stage"], "complete")
            with mock.patch.object(scanner, "analyze_photo", wraps=scanner.analyze_photo) as analyze:
                scan()
                self.assertEqual(analyze.call_count, 2)
                conn = connect_db(project.db_path)
                profile = config.get_profile(project.profile_id)
                self.assertFalse(scanner.niqe_rescan_required(conn, profile))
                conn.execute("UPDATE photos SET decision='keep'")
                conn.commit()
                stamps = list(conn.execute("SELECT analyzed_at FROM photos"))
                scan()
                self.assertEqual(analyze.call_count, 2)
                self.assertEqual(stamps, list(conn.execute("SELECT analyzed_at FROM photos")))
                conn.execute("UPDATE photos SET niqe_version='old' WHERE relative_path='texture.png'")
                conn.commit()
                self.assertTrue(scanner.niqe_rescan_required(conn, profile))
                scan()
                self.assertEqual(analyze.call_count, 3)
                conn.execute("UPDATE photos SET niqe_score=NULL,niqe_error='' WHERE relative_path='plain.png'")
                conn.commit()
                scan()
                self.assertEqual(analyze.call_count, 4)
                scan()
                self.assertEqual(analyze.call_count, 4)
                conn.execute(
                    "UPDATE photos SET niqe_version=? WHERE relative_path='plain.png'",
                    (niqe.NIQE_VERSION,),
                )
                conn.commit()
                self.assertTrue(scanner.niqe_rescan_required(conn, profile))
                scan()
                self.assertEqual(analyze.call_count, 5)
                self.assertEqual(
                    conn.execute(
                        "SELECT niqe_version FROM photos WHERE relative_path='plain.png'"
                    ).fetchone()[0],
                    niqe.NIQE_INPUT_FAILURE_VERSION,
                )
                scan()
                self.assertEqual(analyze.call_count, 5)
                self.assertEqual([r[0] for r in conn.execute("SELECT decision FROM photos")], ["keep", "keep"])
                self.assertFalse(scanner.niqe_rescan_required(conn, profile))
                disabled = copy.deepcopy(profile)
                disabled["quality"]["enabled"]["niqe"] = False
                conn.execute("UPDATE photos SET niqe_version='old'")
                self.assertFalse(scanner.niqe_rescan_required(conn, disabled))
                conn.close()
            self.assertEqual(original, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in photos.iterdir()})

    def test_similarity_recommendations_and_reclassification_use_niqe(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = connect_db(Path(temp) / "project.db")
            for index, score in [(1, 7), (2, 3)]:
                metrics = good_photo(niqe_score=score)
                columns = ["relative_path", *metrics]
                conn.execute(f"INSERT INTO photos({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [f"IMG_{index}.jpg", *metrics.values()])
            conn.execute("INSERT INTO similar_pairs(a_id,b_id,score,kind,recommended_id) VALUES(1,2,0.95,'similar',1)")
            profile = BUILTIN_PROFILES["conservative"]
            groups = build_similarity_groups(conn, profile)
            self.assertEqual(groups[0]["recommended_id"], 2)
            scanner = Scanner(mock.Mock(), mock.Mock())
            scanner.reclassify(mock.Mock(), conn, profile, threading.Event())
            rows = list(conn.execute("SELECT quality_score,suggestion FROM photos ORDER BY id"))
            self.assertGreater(rows[1]["quality_score"], rows[0]["quality_score"])
            self.assertEqual(rows[0]["suggestion"], "review")
            conn.close()
