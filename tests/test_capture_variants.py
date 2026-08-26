from __future__ import annotations

import copy
import csv
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from cullumi import http_api, project_store
from cullumi.capture_variants import (
    active_variant_rows,
    rebuild_capture_variants,
    variant_metadata,
    variant_metadata_from_rows,
)
from cullumi.config import BUILTIN_PROFILES, ConfigStore
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import Scanner
from cullumi.similarity import SimilarityGroupCache
from cullumi.workflows import (
    apply_quarantine,
    import_decisions,
    mark_ai_remove_suggestions,
    restore_batch,
)


def insert_photo(
    conn,
    relative_path: str,
    *,
    error: str = "",
    decision: str = "",
    suggestion: str = "keep",
    width: int = 4000,
    height: int = 3000,
    size: int = 2_000_000,
    taken: str = "2026:01:01 12:00:00",
    phash: str = "0" * 16,
    dhash: str = "0" * 16,
    sha256: str = "",
) -> int:
    extension = Path(relative_path).suffix.lower()
    cursor = conn.execute(
        """INSERT INTO photos(
             relative_path,extension,size,width,height,megapixels,taken,
             luminance,contrast,dark_clip,bright_clip,sharpness,entropy,
             phash,dhash,sha256,thumbnail,error,suggestion,reason,decision,
             status,analyzed_at,media_type,quality_score
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            relative_path,
            extension,
            size,
            width,
            height,
            width * height / 1_000_000,
            taken,
            110.0,
            60.0,
            0.01,
            0.01,
            900.0,
            7.0,
            phash,
            dhash,
            sha256,
            "",
            error,
            suggestion,
            "",
            decision,
            "active",
            "2026-01-01T12:00:00",
            "image",
            80.0,
        ),
    )
    return int(cursor.lastrowid)


class CaptureVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.config = ConfigStore(self.root / "config.json")
        self.config.data["default_cache_root"] = str(self.root / "cache")
        self.config.save()
        self.manager = ProjectManager(self.config)
        self.project = self.manager.open(str(self.photos))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def application(self) -> http_api.ApplicationContext:
        scanner = Scanner(self.config, self.manager)
        return http_api.ApplicationContext(
            self.config,
            self.manager,
            scanner,
            SimilarityGroupCache(),
            "test-token",
            Path("web"),
        )

    def test_strict_grouping_and_representative_selection(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(conn, "Trip/IMG_0001.JPG", size=3_000_000)
            raw = insert_photo(conn, "Trip/img_0001.CR3", size=20_000_000)
            other_dir = insert_photo(conn, "Other/IMG_0001.NEF")
            jpeg_only = insert_photo(conn, "Trip/IMG_0002.JPG")
            heif_only = insert_photo(conn, "Trip/IMG_0002.HEIF")
            raw_only_a = insert_photo(conn, "Trip/IMG_0003.CR2")
            raw_only_b = insert_photo(conn, "Trip/IMG_0003.NEF")
            lower_res_jpg = insert_photo(
                conn, "Trip/IMG_0004.JPG", width=2000, height=1500
            )
            higher_res_png = insert_photo(
                conn, "Trip/IMG_0004.PNG", width=6000, height=4000
            )
            unreadable_raw = insert_photo(
                conn, "Trip/IMG_0004.ARW", error="broken"
            )
            readable_raw = insert_photo(conn, "Trip/img_0004.NEF")
            unreadable_jpg = insert_photo(
                conn,
                "Trip/IMG_0005.JPG",
                error="broken",
                width=1000,
                height=750,
            )
            larger_unreadable_raw = insert_photo(
                conn,
                "Trip/IMG_0005.CR3",
                error="broken",
                width=8000,
                height=6000,
            )
            unreadable_png = insert_photo(
                conn,
                "Trip/IMG_0006.PNG",
                error="broken",
                width=8000,
                height=6000,
            )
            readable_group_raw = insert_photo(
                conn,
                "Trip/IMG_0006.RAF",
                width=1000,
                height=750,
            )

            memberships = rebuild_capture_variants(conn)
            conn.commit()

        self.assertEqual(memberships[jpg], jpg)
        self.assertEqual(memberships[raw], jpg)
        self.assertNotIn(other_dir, memberships)
        for photo_id in (jpeg_only, heif_only, raw_only_a, raw_only_b):
            self.assertNotIn(photo_id, memberships)
        self.assertEqual(memberships[lower_res_jpg], higher_res_png)
        self.assertEqual(memberships[higher_res_png], higher_res_png)
        self.assertEqual(memberships[unreadable_raw], higher_res_png)
        self.assertEqual(memberships[readable_raw], higher_res_png)
        self.assertEqual(memberships[unreadable_jpg], unreadable_jpg)
        self.assertEqual(memberships[larger_unreadable_raw], unreadable_jpg)
        self.assertEqual(memberships[unreadable_png], readable_group_raw)
        self.assertEqual(memberships[readable_group_raw], readable_group_raw)

    def test_visual_similarity_uses_one_representative_per_capture(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            first_jpg = insert_photo(conn, "IMG_0001.JPG")
            first_raw = insert_photo(conn, "IMG_0001.CR3", size=20_000_000)
            second_jpg = insert_photo(conn, "IMG_0002.JPG")
            second_raw = insert_photo(conn, "IMG_0002.CR3", size=20_000_000)
            rebuild_capture_variants(conn)
            conn.commit()

            profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
            profile["similarity"].update(
                {
                    "exact_duplicates": False,
                    "phash_max": 64,
                    "dhash_max": 64,
                    "structure_min": -1,
                    "allow_cross_time_high_confidence": True,
                }
            )
            Scanner(self.config, self.manager).rebuild_similarity(
                self.project, conn, profile
            )
            pairs = conn.execute(
                "SELECT a_id,b_id,kind FROM similar_pairs ORDER BY a_id,b_id"
            ).fetchall()

        self.assertEqual(
            [(int(row["a_id"]), int(row["b_id"]), row["kind"]) for row in pairs],
            [(first_jpg, second_jpg, "similar")],
        )
        self.assertNotIn(first_raw, {int(pairs[0]["a_id"]), int(pairs[0]["b_id"])})
        self.assertNotIn(second_raw, {int(pairs[0]["a_id"]), int(pairs[0]["b_id"])})

        application = self.application()
        listing = application.photo_queries.similar_groups(
            {"project_id": [self.project.project_id]}
        )
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["capture_count"], 2)
        self.assertEqual(listing["items"][0]["count"], 4)
        detail = application.photo_queries.similar_group(
            {
                "project_id": [self.project.project_id],
                "group_id": [listing["items"][0]["id"]],
            }
        )
        self.assertEqual(detail["capture_count"], 2)
        self.assertEqual(detail["count"], 4)
        self.assertEqual(
            {int(item["id"]) for item in detail["members"]},
            {first_jpg, first_raw, second_jpg, second_raw},
        )
        sources = {
            int(item["id"]): int(item["similarity_source_id"])
            for item in detail["members"]
        }
        self.assertEqual(sources[first_raw], first_jpg)
        self.assertEqual(sources[second_raw], second_jpg)

    def test_exact_duplicates_still_include_nonrepresentative_files(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            insert_photo(conn, "IMG_0001.JPG", sha256="rendered-jpeg")
            raw = insert_photo(
                conn, "IMG_0001.CR3", size=20_000_000, sha256="raw-bytes"
            )
            raw_copy = insert_photo(
                conn,
                "Copies/RAW_COPY.CR3",
                size=20_000_000,
                sha256="raw-bytes",
            )
            rebuild_capture_variants(conn)
            conn.commit()

            Scanner(self.config, self.manager).rebuild_similarity(
                self.project,
                conn,
                copy.deepcopy(BUILTIN_PROFILES["balanced"]),
            )
            exact_pairs = {
                (int(row["a_id"]), int(row["b_id"]))
                for row in conn.execute(
                    "SELECT a_id,b_id FROM similar_pairs WHERE kind='exact'"
                )
            }

        self.assertEqual(exact_pairs, {tuple(sorted((raw, raw_copy)))})

    def test_large_jpg_raw_library_has_no_internal_visual_edges(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            for index in range(250):
                visual_hash = f"{index + 1:016x}"
                insert_photo(
                    conn,
                    f"Shoot/IMG_{index:04d}.JPG",
                    phash=visual_hash,
                    dhash=visual_hash,
                )
                insert_photo(
                    conn,
                    f"Shoot/IMG_{index:04d}.CR3",
                    size=20_000_000,
                    phash=visual_hash,
                    dhash=visual_hash,
                )
            rebuild_capture_variants(conn)
            conn.commit()

            profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
            profile["similarity"].update(
                {
                    "exact_duplicates": False,
                    "phash_max": 0,
                    "dhash_max": 0,
                    "allow_cross_time_high_confidence": True,
                }
            )
            Scanner(self.config, self.manager).rebuild_similarity(
                self.project, conn, profile
            )
            similar_count = conn.execute(
                "SELECT COUNT(*) FROM similar_pairs WHERE kind='similar'"
            ).fetchone()[0]

        self.assertEqual(similar_count, 0)

    def test_manual_decision_sync_can_be_disabled(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(conn, "IMG_0042.JPG", suggestion="review")
            raw = insert_photo(conn, "IMG_0042.CR3", suggestion="review")
            rebuild_capture_variants(conn)
            conn.commit()

        handler = object.__new__(http_api.Handler)
        handler._send_json = mock.Mock()
        with mock.patch.object(http_api, "APPLICATION", self.application()):
            handler.api_decision(
                {
                    "project_id": self.project.project_id,
                    "photo_id": raw,
                    "decision": "remove",
                }
            )
            payload = handler._send_json.call_args.args[0]

        self.assertEqual(
            {item["id"] for item in payload["affected_photos"]}, {jpg, raw}
        )
        self.assertTrue(
            all(item["previous_decision"] == "" for item in payload["affected_photos"])
        )
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(
                {row["decision"] for row in conn.execute("SELECT decision FROM photos")},
                {"remove"},
            )

        handler._send_json.reset_mock()
        with mock.patch.object(http_api, "APPLICATION", self.application()):
            handler.api_decision(
                {
                    "project_id": self.project.project_id,
                    "photo_id": raw,
                    "decision": "",
                }
            )
        payload = handler._send_json.call_args.args[0]
        self.assertTrue(
            all(
                item["previous_decision"] == "remove"
                for item in payload["affected_photos"]
            )
        )
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(
                {row["decision"] for row in conn.execute("SELECT decision FROM photos")},
                {""},
            )

        handler._send_json.reset_mock()
        with mock.patch.object(http_api, "APPLICATION", self.application()):
            handler.api_decision(
                {
                    "project_id": self.project.project_id,
                    "photo_id": raw,
                    "decision": "remove",
                }
            )

        with self.config.edit() as data:
            data["sync_variant_decisions"] = False
        handler._send_json.reset_mock()
        with mock.patch.object(http_api, "APPLICATION", self.application()):
            handler.api_decision(
                {
                    "project_id": self.project.project_id,
                    "photo_id": raw,
                    "decision": "keep",
                }
            )
        payload = handler._send_json.call_args.args[0]
        self.assertEqual([item["id"] for item in payload["affected_photos"]], [raw])
        with closing(connect_db(self.project.db_path)) as conn:
            decisions = {
                int(row["id"]): row["decision"]
                for row in conn.execute("SELECT id,decision FROM photos")
            }
        self.assertEqual(decisions, {jpg: "remove", raw: "keep"})

    def test_ai_batch_does_not_override_a_kept_variant(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(conn, "IMG_0100.JPG", suggestion="remove")
            raw = insert_photo(conn, "IMG_0100.NEF", decision="keep")
            rebuild_capture_variants(conn)
            conn.commit()

        skipped = mark_ai_remove_suggestions(
            self.project, True, detailed=True
        )
        self.assertEqual(skipped["marked"], 0)
        self.assertEqual(skipped["skipped_kept_groups"], 1)

        with closing(connect_db(self.project.db_path)) as conn:
            conn.execute("UPDATE photos SET decision='' WHERE id=?", (raw,))
            conn.commit()
        marked = mark_ai_remove_suggestions(self.project, True, detailed=True)
        self.assertEqual(marked["marked"], 2)
        with closing(connect_db(self.project.db_path)) as conn:
            decisions = {
                int(row["id"]): row["decision"]
                for row in conn.execute("SELECT id,decision FROM photos")
            }
        self.assertEqual(decisions, {jpg: "remove", raw: "remove"})

    def test_csv_variant_conflict_requires_disabling_sync_before_writes(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(conn, "IMG_0200.JPG")
            raw = insert_photo(conn, "IMG_0200.ARW")
            rebuild_capture_variants(conn)
            conn.commit()
        csv_path = self.root / "decisions.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["decision", "path"])
            writer.writerow(["keep", "IMG_0200.JPG"])
            writer.writerow(["remove", "IMG_0200.ARW"])

        conflict = import_decisions(
            self.project, csv_path, True, detailed=True
        )
        self.assertTrue(conflict["requires_sync_disable"])
        self.assertEqual(conflict["conflicting_groups"], 1)
        with closing(connect_db(self.project.db_path)) as conn:
            self.assertEqual(
                {row["decision"] for row in conn.execute("SELECT decision FROM photos")},
                {""},
            )

        imported = import_decisions(
            self.project, csv_path, False, detailed=True
        )
        self.assertFalse(imported["requires_sync_disable"])
        with closing(connect_db(self.project.db_path)) as conn:
            decisions = {
                int(row["id"]): row["decision"]
                for row in conn.execute("SELECT id,decision FROM photos")
            }
        self.assertEqual(decisions, {jpg: "keep", raw: "remove"})

    def test_format_filter_and_variant_payload_are_bulk_hydrated(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(conn, "IMG_0300.JPG")
            raw = insert_photo(conn, "IMG_0300.CR2")
            insert_photo(conn, "standalone.PNG")
            rebuild_capture_variants(conn)
            conn.commit()
            variants = variant_metadata(conn, [jpg, raw])
        self.assertEqual(variants[jpg], ["CR2", "JPG"])
        self.assertEqual(variants[raw], ["CR2", "JPG"])

        application = self.application()
        result = application.photo_queries.photos(
            {
                "project_id": [self.project.project_id],
                "formats": ["raw"],
                "decisions": ["all"],
                "ai_states": ["all"],
            }
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], raw)
        self.assertEqual(result["items"][0]["format_category"], "raw")
        self.assertEqual(result["items"][0]["variant_extensions"], ["CR2", "JPG"])

        combined = application.photo_queries.photos(
            {
                "project_id": [self.project.project_id],
                "formats": ["raw,jpeg"],
                "decisions": ["all"],
                "ai_states": ["all"],
                "search": ["IMG_0300"],
                "limit": ["1"],
                "offset": ["1"],
            }
        )
        self.assertEqual(combined["total"], 2)
        self.assertEqual(len(combined["items"]), 1)
        empty = application.photo_queries.photos(
            {
                "project_id": [self.project.project_id],
                "formats": ["none"],
                "decisions": ["all"],
                "ai_states": ["all"],
            }
        )
        self.assertEqual(empty, {"total": 0, "items": []})

    def test_similar_groups_are_paginated_and_search_raw_paths_casefolded(
        self,
    ) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            groups = []
            for index, folder in enumerate(("旅行", "Straße", "归档")):
                jpg = insert_photo(
                    conn,
                    f"{folder}/IMG_{index:04d}.JPG",
                    taken=f"2026:01:0{index + 1} 12:00:00",
                )
                raw = insert_photo(
                    conn,
                    f"{folder}/IMG_{index:04d}.NEF",
                    size=20_000_000,
                    taken=f"2026:01:0{index + 1} 12:00:00",
                )
                mate = insert_photo(
                    conn,
                    f"{folder}/MATE_{index:04d}.JPG",
                    taken=f"2026:01:0{index + 1} 12:00:01",
                )
                groups.append((jpg, raw, mate))
            rebuild_capture_variants(conn)
            conn.executemany(
                """INSERT INTO similar_pairs(
                     a_id,b_id,score,kind,recommended_id,face_safe
                   ) VALUES(?,?,?,?,?,?)""",
                [
                    (jpg, mate, 0.9, "similar", jpg, 0)
                    for jpg, _raw, mate in groups
                ],
            )
            conn.commit()

        application = self.application()
        all_groups = application.photo_queries.similar_groups(
            {"project_id": [self.project.project_id]}
        )
        first_page = application.photo_queries.similar_groups(
            {
                "project_id": [self.project.project_id],
                "limit": ["2"],
                "offset": ["0"],
            }
        )
        second_page = application.photo_queries.similar_groups(
            {
                "project_id": [self.project.project_id],
                "limit": ["2"],
                "offset": ["2"],
            }
        )
        self.assertEqual(all_groups["total"], 3)
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(second_page["total"], 3)
        self.assertEqual(len(first_page["items"]), 2)
        self.assertEqual(len(second_page["items"]), 1)
        self.assertEqual(
            [item["id"] for item in all_groups["items"]],
            [
                item["id"]
                for item in [*first_page["items"], *second_page["items"]]
            ],
        )

        raw_search = application.photo_queries.similar_groups(
            {
                "project_id": [self.project.project_id],
                "search": ["STRASSE/img_0001.nef"],
                "limit": ["1"],
                "offset": ["0"],
            }
        )
        self.assertEqual(raw_search["total"], 1)
        self.assertEqual(len(raw_search["items"]), 1)
        self.assertEqual(raw_search["items"][0]["count"], 3)
        self.assertIn(
            ["NEF", "JPG"],
            [
                photo["variant_extensions"]
                for photo in raw_search["items"][0]["covers"]
            ],
        )
        exhausted = application.photo_queries.similar_groups(
            {
                "project_id": [self.project.project_id],
                "limit": ["2"],
                "offset": ["99"],
            }
        )
        self.assertEqual(exhausted, {"total": 3, "items": []})

    def test_variant_rows_and_metadata_queries_scale_by_parameter_batch(
        self,
    ) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            photo_ids = []
            for index in range(3):
                photo_ids.extend(
                    (
                        insert_photo(conn, f"IMG_{index:04d}.JPG"),
                        insert_photo(conn, f"IMG_{index:04d}.CR3"),
                    )
                )
            rebuild_capture_variants(conn)
            conn.commit()
            statements = []
            conn.set_trace_callback(statements.append)
            with mock.patch(
                "cullumi.capture_variants.SQLITE_PARAMETER_BATCH", 2
            ):
                rows = active_variant_rows(conn, photo_ids)
                query_count = len(
                    [
                        statement
                        for statement in statements
                        if statement.lstrip().upper().startswith("SELECT")
                    ]
                )
                metadata = variant_metadata_from_rows(rows)

        self.assertEqual(query_count, 5)
        self.assertEqual(
            len(
                [
                    statement
                    for statement in statements
                    if statement.lstrip().upper().startswith("SELECT")
                ]
            ),
            query_count,
        )
        self.assertEqual(set(metadata), set(photo_ids))
        self.assertTrue(
            all(extensions == ["CR3", "JPG"] for extensions in metadata.values())
        )

    def test_photo_library_sort_orders_are_whitelisted_and_paginated(self) -> None:
        with closing(connect_db(self.project.db_path)) as conn:
            alpha = insert_photo(
                conn,
                "Z-folder/alpha.JPG",
                suggestion="keep",
                size=100,
                taken="2024:01:01 10:00:00",
            )
            beta = insert_photo(
                conn,
                "A-folder/beta.JPG",
                suggestion="remove",
                size=300,
                taken="2026:01:01 10:00:00",
            )
            gamma = insert_photo(
                conn,
                "M-folder/gamma.JPG",
                suggestion="review",
                size=200,
                taken="2025:01:01 10:00:00",
            )
            conn.commit()

        application = self.application()

        def sorted_ids(
            sort: str,
            direction: str = "asc",
            *,
            limit: int = 10,
            offset: int = 0,
        ) -> list[int]:
            result = application.photo_queries.photos(
                {
                    "project_id": [self.project.project_id],
                    "sort": [sort],
                    "direction": [direction],
                    "formats": ["all"],
                    "decisions": ["all"],
                    "ai_states": ["all"],
                    "limit": [str(limit)],
                    "offset": [str(offset)],
                }
            )
            return [int(item["id"]) for item in result["items"]]

        self.assertEqual(sorted_ids("suggestion"), [beta, gamma, alpha])
        self.assertEqual(sorted_ids("filename"), [alpha, beta, gamma])
        self.assertEqual(sorted_ids("size"), [alpha, gamma, beta])
        self.assertEqual(sorted_ids("taken"), [alpha, gamma, beta])
        self.assertEqual(sorted_ids("suggestion", "desc"), [alpha, gamma, beta])
        self.assertEqual(sorted_ids("filename", "desc"), [gamma, beta, alpha])
        self.assertEqual(sorted_ids("size", "desc"), [beta, gamma, alpha])
        self.assertEqual(sorted_ids("taken", "desc"), [beta, gamma, alpha])
        self.assertEqual(sorted_ids("filename", limit=1, offset=1), [beta])
        with self.assertRaisesRegex(ValueError, "sort 必须是"):
            sorted_ids("relative_path DESC; DROP TABLE photos")
        with self.assertRaisesRegex(ValueError, "direction 必须是"):
            sorted_ids("filename", "sideways")

    def test_quarantine_and_restore_refresh_variant_memberships(self) -> None:
        jpg_path = self.photos / "IMG_0400.JPG"
        raw_path = self.photos / "IMG_0400.CR3"
        jpg_path.write_bytes(b"jpeg")
        raw_path.write_bytes(b"raw-data")
        with closing(connect_db(self.project.db_path)) as conn:
            jpg = insert_photo(
                conn,
                jpg_path.name,
                decision="remove",
                size=jpg_path.stat().st_size,
            )
            raw = insert_photo(conn, raw_path.name, size=raw_path.stat().st_size)
            conn.execute(
                "UPDATE photos SET mtime=? WHERE id=?",
                (jpg_path.stat().st_mtime, jpg),
            )
            conn.execute(
                "UPDATE photos SET mtime=? WHERE id=?",
                (raw_path.stat().st_mtime, raw),
            )
            rebuild_capture_variants(conn)
            conn.commit()

        quarantined = apply_quarantine(self.project)
        self.assertEqual(quarantined["moved"], 1)
        with closing(connect_db(self.project.db_path)) as conn:
            memberships = {
                int(row["photo_id"]): int(row["representative_id"])
                for row in conn.execute(
                    "SELECT photo_id,representative_id FROM capture_variant_members"
                )
            }
        self.assertEqual(memberships, {})

        restored = restore_batch(self.project, str(quarantined["batch_id"]))
        self.assertEqual(restored["restored"], 1)
        with closing(connect_db(self.project.db_path)) as conn:
            memberships = {
                int(row["photo_id"]): int(row["representative_id"])
                for row in conn.execute(
                    "SELECT photo_id,representative_id FROM capture_variant_members"
                )
            }
        self.assertEqual(memberships, {jpg: jpg, raw: jpg})


class CaptureVariantMigrationTests(unittest.TestCase):
    def test_v4_upgrade_builds_variants_prunes_visual_edges_and_keeps_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.db"
            with closing(connect_db(path)) as conn:
                jpg = insert_photo(conn, "IMG_1000.JPG", decision="keep")
                raw = insert_photo(conn, "IMG_1000.CR3", decision="remove")
                conn.execute(
                    """INSERT INTO similar_pairs(
                         a_id,b_id,score,kind,recommended_id,face_safe
                       ) VALUES(?,?,?,?,?,?)""",
                    (jpg, raw, 1.0, "similar", jpg, 0),
                )
                conn.execute("DROP TABLE capture_variant_members")
                conn.execute("PRAGMA user_version=4")
                conn.commit()
            project_store._INITIALIZED_DATABASES.pop(path.resolve(), None)

            with closing(connect_db(path)) as migrated:
                memberships = {
                    int(row["photo_id"]): int(row["representative_id"])
                    for row in migrated.execute(
                        "SELECT photo_id,representative_id FROM capture_variant_members"
                    )
                }
                decisions = {
                    int(row["id"]): row["decision"]
                    for row in migrated.execute("SELECT id,decision FROM photos")
                }
                similar_count = migrated.execute(
                    "SELECT COUNT(*) FROM similar_pairs WHERE kind='similar'"
                ).fetchone()[0]

            self.assertEqual(memberships, {jpg: jpg, raw: jpg})
            self.assertEqual(decisions, {jpg: "keep", raw: "remove"})
            self.assertEqual(similar_count, 0)
            self.assertEqual(len(list(path.parent.glob("project.pre-v5-*.db"))), 1)


if __name__ == "__main__":
    unittest.main()
