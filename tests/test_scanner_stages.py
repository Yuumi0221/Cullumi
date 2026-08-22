from __future__ import annotations

import threading
import unittest
from unittest import mock

from cullumi.scanner import ScanCancelled, Scanner


class ScannerStageTests(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner(mock.Mock(), mock.Mock())
        self.project = mock.Mock()
        self.connection = mock.Mock()
        self.cancel = threading.Event()
        self.cancel.set()

    def test_exact_duplicate_stage_honors_cancellation_before_hashing(self):
        self.scanner._exact_hashes = mock.Mock()

        with self.assertRaises(ScanCancelled):
            self.scanner._confirm_exact_duplicates(
                "project", self.project, self.connection, 0, self.cancel
            )

        self.scanner._exact_hashes.assert_not_called()

    def test_relationship_stage_honors_cancellation_before_rebuild(self):
        self.scanner.rebuild_similarity = mock.Mock()
        self.scanner.reclassify = mock.Mock()

        with self.assertRaises(ScanCancelled):
            self.scanner._rebuild_relationships(
                "project", self.project, self.connection, {}, self.cancel
            )

        self.scanner.rebuild_similarity.assert_not_called()
        self.scanner.reclassify.assert_not_called()
