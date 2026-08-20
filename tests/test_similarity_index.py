from __future__ import annotations

import copy
import itertools
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from PIL import Image

import cullumi.core as core
from cullumi.core import BUILTIN_PROFILES, ConfigStore, ProjectManager, Scanner, connect_db
from cullumi.similarity import hamming, hamming_candidate_pairs


class SimilarityIndexTests(unittest.TestCase):
    def test_candidate_index_matches_brute_force_hamming_results(self) -> None:
        randomizer = random.Random(20260820)
        hashes = [f"{randomizer.getrandbits(64):016x}" for _ in range(180)]
        hashes.extend([hashes[5], hashes[12], ""])

        for radius in (0, 6, 14, 64):
            expected = [
                (left, right)
                for left in range(len(hashes))
                for right in range(left + 1, len(hashes))
                if hamming(hashes[left], hashes[right]) <= radius
            ]
            self.assertEqual(
                list(hamming_candidate_pairs(hashes, radius)),
                expected,
                f"radius={radius}",
            )

    def test_random_photo_hashes_avoid_quadratic_candidate_growth(self) -> None:
        randomizer = random.Random(7)
        hashes = [f"{randomizer.getrandbits(64):016x}" for _ in range(1000)]
        with mock.patch(
            "cullumi.similarity.np.bitwise_count", wraps=core.np.bitwise_count
        ) as vectorized_count:
            candidates = list(hamming_candidate_pairs(hashes, 14))
        all_pairs = len(hashes) * (len(hashes) - 1) // 2

        self.assertGreater(vectorized_count.call_count, 0)
        self.assertLess(len(candidates), all_pairs // 100)

    def test_indexed_similarity_rebuild_matches_full_pair_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = ConfigStore(root / "config.json")
            config.data["default_cache_root"] = str(root / "cache")
            config.save()
            manager = ProjectManager(config)
            project = manager.open(str(photos))
            scanner = Scanner(config, manager)
            thumbnail = project.thumb_dir / "shared.jpg"
            with Image.new("RGB", (16, 16), "gray") as image:
                image.putpixel((0, 0), (255, 255, 255))
                image.save(thumbnail, "JPEG")

            randomizer = random.Random(19)
            hashes: list[tuple[int, int]] = []
            for index in range(80):
                phash = randomizer.getrandbits(64)
                dhash = randomizer.getrandbits(64)
                if index % 10 == 1:
                    previous_phash, previous_dhash = hashes[-1]
                    phash = previous_phash ^ 0b10101
                    dhash = previous_dhash ^ 0b1001
                hashes.append((phash, dhash))

            with closing(connect_db(project.db_path)) as source:
                source.executemany(
                    """INSERT INTO photos(
                           relative_path,status,error,width,height,phash,dhash,thumbnail,
                           sharpness,luminance,contrast,dark_clip,bright_clip,entropy,megapixels
                       ) VALUES(?, 'active', '', 1200, 800, ?, ?, ?, 100, 110, 40, 0, 0, 6, 2)""",
                    [
                        (
                            f"IMG_{index:04d}.jpg",
                            f"{phash:016x}",
                            f"{dhash:016x}",
                            str(thumbnail),
                        )
                        for index, (phash, dhash) in enumerate(hashes)
                    ],
                )
                source.commit()
                indexed = sqlite3.connect(":memory:")
                brute = sqlite3.connect(":memory:")
                indexed.row_factory = sqlite3.Row
                brute.row_factory = sqlite3.Row
                source.backup(indexed)
                source.backup(brute)

            profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
            profile["similarity"]["exact_duplicates"] = False
            profile["similarity"]["structure_min"] = -1
            scanner.rebuild_similarity(project, indexed, profile)
            with mock.patch.object(
                core,
                "hamming_candidate_pairs",
                side_effect=lambda values, radius: itertools.combinations(
                    range(len(values)), 2
                ),
            ):
                scanner.rebuild_similarity(project, brute, profile)

            columns = "a_id,b_id,score,kind,recommended_id,face_safe"
            indexed_rows = [
                tuple(row)
                for row in indexed.execute(
                    f"SELECT {columns} FROM similar_pairs ORDER BY a_id,b_id"
                )
            ]
            brute_rows = [
                tuple(row)
                for row in brute.execute(
                    f"SELECT {columns} FROM similar_pairs ORDER BY a_id,b_id"
                )
            ]
            indexed.close()
            brute.close()
            self.assertEqual(indexed_rows, brute_rows)


if __name__ == "__main__":
    unittest.main()
