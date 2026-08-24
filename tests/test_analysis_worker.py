from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import cullumi.analysis_worker as worker_module
from cullumi.analysis_worker import AnalysisCancelled, PhotoAnalysisRunner
from cullumi.config import ConfigStore
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import Scanner


def blocking_worker(requests, _responses, _memory_limit) -> None:
    requests.get()
    time.sleep(30)


class PhotoAnalysisRunnerTests(unittest.TestCase):
    def test_worker_decodes_photo_and_recycles_after_task_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jpg"
            thumbnail = root / "thumb.jpg"
            Image.new("RGB", (1600, 900), "teal").save(source, quality=90)
            runner = PhotoAnalysisRunner(
                timeout_seconds=10,
                max_tasks_per_worker=1,
                memory_limit=0,
            )
            try:
                result = runner.analyze(source, thumbnail, threading.Event())
                self.assertEqual(result["error"], "")
                self.assertEqual((result["width"], result["height"]), (1600, 900))
                self.assertTrue(thumbnail.is_file())
                self.assertIsNone(runner._process)
            finally:
                runner.close()

    def test_cancelled_task_never_starts_a_worker(self) -> None:
        runner = PhotoAnalysisRunner(memory_limit=0)
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaises(AnalysisCancelled):
                runner.analyze(Path("unused.jpg"), Path("unused-thumb.jpg"), cancel)
            self.assertIsNone(runner._process)
        finally:
            runner.close()

    def test_scanner_uses_worker_and_commits_successful_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (1200, 800), "purple").save(photos / "photo.jpg")
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            runner = PhotoAnalysisRunner(timeout_seconds=10)
            scanner = Scanner(config, manager, analysis_runner=runner)
            try:
                scanner.start(project.project_id)
                scanner.threads[project.project_id].join(20)
                self.assertEqual(
                    scanner.get_progress(project.project_id)["stage"], "complete"
                )
                conn = connect_db(project.db_path)
                row = conn.execute(
                    "SELECT width,height,error FROM photos"
                ).fetchone()
                conn.close()
                self.assertEqual(tuple(row), (1200, 800, ""))
            finally:
                runner.close()

    def test_default_memory_limit_is_bounded(self) -> None:
        gibibyte = 1024 * 1024 * 1024
        with mock.patch.object(
            worker_module, "_physical_memory_bytes", return_value=4 * gibibyte
        ):
            self.assertEqual(worker_module.default_worker_memory_limit(), int(0.8 * gibibyte))
        with mock.patch.object(
            worker_module, "_physical_memory_bytes", return_value=64 * gibibyte
        ):
            self.assertEqual(
                worker_module.default_worker_memory_limit(),
                worker_module.MAX_WORKER_MEMORY_BYTES,
            )

    def test_active_worker_is_terminated_on_cancellation(self) -> None:
        runner = PhotoAnalysisRunner(
            timeout_seconds=10,
            memory_limit=0,
            worker_main=blocking_worker,
        )
        cancel = threading.Event()
        errors: list[BaseException] = []

        def analyze() -> None:
            try:
                runner.analyze(Path("unused.jpg"), Path("unused-thumb.jpg"), cancel)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=analyze)
        try:
            thread.start()
            deadline = time.monotonic() + 5
            while runner._process is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(runner._process)
            cancel.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], AnalysisCancelled)
            self.assertIsNone(runner._process)
        finally:
            cancel.set()
            thread.join(3)
            runner.close()

    def test_timed_out_worker_is_replaced_for_the_next_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jpg"
            Image.new("RGB", (64, 48), "orange").save(source)
            runner = PhotoAnalysisRunner(
                timeout_seconds=0.1,
                memory_limit=0,
                worker_main=blocking_worker,
            )
            try:
                failed = runner.analyze(source, root / "failed-thumb.jpg")
                self.assertIn("超过", failed["error"])
                self.assertIsNone(runner._process)

                runner.timeout_seconds = 10
                runner._worker_main = worker_module._worker_main
                recovered = runner.analyze(source, root / "thumb.jpg")
                self.assertEqual(recovered["error"], "")
                self.assertTrue((root / "thumb.jpg").is_file())
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
