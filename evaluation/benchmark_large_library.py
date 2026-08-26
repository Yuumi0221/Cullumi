from __future__ import annotations

import argparse
import copy
import ctypes
import gc
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import threading
import time
import tracemalloc
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from cullumi.capture_variants import rebuild_capture_variants  # noqa: E402
from cullumi.config import BUILTIN_PROFILES  # noqa: E402
from cullumi.photo_query_service import PhotoQueryService  # noqa: E402
from cullumi.project_store import Project, connect_db  # noqa: E402
from cullumi.scanner import ScanCancelled, Scanner  # noqa: E402
from cullumi.settings_service import estimate_profile  # noqa: E402
from cullumi.similarity import (  # noqa: E402
    SimilarityGroupCache,
    build_similarity_groups,
)

if os.name == "nt":
    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]


    _KERNEL32 = ctypes.windll.kernel32
    _PSAPI = ctypes.windll.psapi
    _KERNEL32.GetCurrentProcess.restype = ctypes.c_void_p
    _PSAPI.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    _PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int


@dataclass(frozen=True)
class BenchmarkScale:
    discovery_files: int = 100_000
    photos: int = 50_000
    similar_edges: int = 10_000
    exact_photos: int = 5_000
    exact_size_groups: int = 100
    variant_pairs: int = 1_000


class BenchmarkConfig:
    def snapshot(self) -> dict[str, Any]:
        return {
            "projects": {},
            "blink_detection_enabled": False,
        }

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return copy.deepcopy(BUILTIN_PROFILES[profile_id])


class BenchmarkManager:
    def __init__(self, project: Project) -> None:
        self.project = project

    def from_id(self, project_id: str) -> Project:
        if project_id != self.project.project_id:
            raise ValueError("benchmark project does not exist")
        return self.project


class QueryCounter:
    def __init__(self) -> None:
        self.total = 0
        self.by_operation: dict[str, int] = {}

    def __call__(self, statement: str) -> None:
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        self.total += 1
        self.by_operation[operation] = self.by_operation.get(operation, 0) + 1

    def report(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_operation": dict(sorted(self.by_operation.items())),
        }


def _resident_bytes() -> int | None:
    if os.name == "nt":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if _PSAPI.GetProcessMemoryInfo(
            _KERNEL32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
        return None
    statm = Path("/proc/self/statm")
    if statm.is_file():
        try:
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (IndexError, OSError, ValueError):
            return None
    return None


def measure(operation: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    gc.collect()
    rss_start = _resident_bytes()
    rss_peak = rss_start
    stop = threading.Event()

    def sample_memory() -> None:
        nonlocal rss_peak
        while not stop.wait(0.01):
            value = _resident_bytes()
            if value is not None and (rss_peak is None or value > rss_peak):
                rss_peak = value

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
    finally:
        elapsed = time.perf_counter() - started
        _current, python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stop.set()
        sampler.join()
        rss_end = _resident_bytes()
        if rss_end is not None and (rss_peak is None or rss_end > rss_peak):
            rss_peak = rss_end
    return result, {
        "elapsed_seconds": round(elapsed, 6),
        "python_peak_bytes": python_peak,
        "rss_start_bytes": rss_start,
        "rss_peak_bytes": rss_peak,
        "rss_peak_increase_bytes": (
            max(0, rss_peak - rss_start)
            if rss_peak is not None and rss_start is not None
            else None
        ),
    }


def _new_project(root: Path, cache: Path, project_id: str) -> Project:
    project_dir = cache / project_id
    thumb_dir = project_dir / "thumbs"
    motion_dir = project_dir / "motion"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    motion_dir.mkdir(parents=True, exist_ok=True)
    return Project(
        project_id,
        root,
        cache,
        project_dir,
        project_dir / "project.db",
        thumb_dir,
        "balanced",
        motion_dir,
    )


def _create_discovery_fixture(root: Path, file_count: int) -> None:
    directory_count = max(1, min(200, math.ceil(file_count / 500)))
    created = 0
    for directory_index in range(directory_count):
        directory = root / f"album-{directory_index:04d}"
        directory.mkdir(parents=True)
        remaining_directories = directory_count - directory_index
        in_directory = math.ceil((file_count - created) / remaining_directories)
        for file_index in range(in_directory):
            (directory / f"IMG_{created + file_index:08d}.jpg").touch()
        created += in_directory


def _path_digest(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def benchmark_discovery(base: Path, file_count: int) -> dict[str, Any]:
    root = base / "discovery-library"
    cache = base / "discovery-cache"
    root.mkdir()
    _create_discovery_fixture(root, file_count)
    project = _new_project(root, cache, "discovery")
    scanner = Scanner(BenchmarkConfig(), object())
    cancel = threading.Event()

    result, metrics = measure(lambda: scanner._discover(project, cancel))
    summary = {
        "discovered_total": result.discovered_total,
        "photo_count": len(result.photos),
        "video_count": result.video_count,
        "unsupported_count": result.unsupported_count,
        "path_digest": _path_digest(result.photos, root),
    }

    cancellation_started = threading.Event()
    cancellation_finished = threading.Event()
    cancellation_error: list[str] = []
    cancellation_event = threading.Event()

    def cancelled_discovery() -> None:
        cancellation_started.set()
        try:
            scanner._discover(project, cancellation_event)
        except ScanCancelled:
            pass
        except Exception as error:  # pragma: no cover - diagnostic path
            cancellation_error.append(str(error))
        finally:
            cancellation_finished.set()

    thread = threading.Thread(target=cancelled_discovery)
    thread.start()
    cancellation_started.wait(2)
    time.sleep(0.01)
    cancel_started = time.perf_counter()
    cancellation_event.set()
    cancellation_finished.wait(5)
    cancellation_seconds = time.perf_counter() - cancel_started
    thread.join(1)
    return {
        **metrics,
        "sql_queries": {"total": 0, "by_operation": {}},
        "cancellation_response_seconds": round(cancellation_seconds, 6),
        "cancellation_completed": cancellation_finished.is_set(),
        "cancellation_error": cancellation_error,
        "summary": summary,
    }


PHOTO_INSERT_SQL = """INSERT INTO photos(
    id,relative_path,extension,size,mtime,width,height,megapixels,taken,
    luminance,contrast,dark_clip,bright_clip,sharpness,entropy,phash,dhash,
    sha256,thumbnail,error,status,media_type,motion_sha256
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _photo_row(photo_id: int) -> tuple[Any, ...]:
    hash_value = (photo_id * 0x9E3779B185EBCA87) & ((1 << 64) - 1)
    return (
        photo_id,
        f"album-{photo_id:08d}/IMG_{photo_id:08d}.jpg",
        ".jpg",
        2_000_000 + photo_id % 10_000,
        1_700_000_000 + photo_id,
        6000,
        4000,
        24.0,
        f"2025:01:{1 + photo_id % 28:02d} 12:{photo_id % 60:02d}:00",
        105.0 + photo_id % 20,
        35.0 + photo_id % 15,
        0.01,
        0.01,
        180.0 + photo_id % 200,
        6.0 + (photo_id % 10) / 10,
        f"{hash_value:016x}",
        f"{hash_value ^ 0xA5A5A5A5A5A5A5A5:016x}",
        f"sha-{photo_id:08d}",
        "",
        "",
        "active",
        "image",
        "",
    )


def _populate_photo_database(project: Project, photo_count: int, edge_count: int) -> None:
    with closing(connect_db(project.db_path)) as conn:
        for start in range(1, photo_count + 1, 1_000):
            stop = min(photo_count + 1, start + 1_000)
            conn.executemany(PHOTO_INSERT_SQL, (_photo_row(photo_id) for photo_id in range(start, stop)))
        usable_edges = min(edge_count, photo_count // 2)
        conn.executemany(
            """INSERT INTO similar_pairs(
                   a_id,b_id,score,kind,recommended_id,face_safe
               ) VALUES(?,?,?,?,?,?)""",
            (
                (
                    edge_index * 2 + 1,
                    edge_index * 2 + 2,
                    0.8 + (edge_index % 20) / 100,
                    "similar",
                    edge_index * 2 + 1,
                    edge_index % 2,
                )
                for edge_index in range(usable_edges)
            ),
        )
        conn.commit()


def _add_capture_variants(project: Project, count: int) -> None:
    copied_columns = (
        "mtime",
        "width",
        "height",
        "megapixels",
        "taken",
        "luminance",
        "contrast",
        "dark_clip",
        "bright_clip",
        "sharpness",
        "entropy",
        "phash",
        "dhash",
        "thumbnail",
        "error",
        "suggestion",
        "reason",
        "status",
        "analyzed_at",
        "media_type",
        "cover_source",
        "quality_score",
    )
    insert_columns = ("relative_path", "extension", "size", *copied_columns)
    with closing(connect_db(project.db_path)) as conn:
        rows = conn.execute(
            f"""SELECT relative_path,size,{','.join(copied_columns)}
                  FROM photos ORDER BY id LIMIT ?""",
            (count,),
        ).fetchall()
        conn.executemany(
            f"""INSERT INTO photos({','.join(insert_columns)})
                 VALUES({','.join('?' for _ in insert_columns)})""",
            (
                (
                    str(Path(row["relative_path"]).with_suffix(".cr3")),
                    ".cr3",
                    int(row["size"] or 0) * 5,
                    *(row[column] for column in copied_columns),
                )
                for row in rows
            ),
        )
        rebuild_capture_variants(conn)
        conn.commit()


def _groups_summary(groups: list[dict[str, Any]]) -> dict[str, Any]:
    digest = hashlib.sha256()
    member_count = 0
    for group in groups:
        member_count += len(group["member_ids"])
        stable = {
            "id": group["id"],
            "member_ids": group["member_ids"],
            "recommended_id": group["recommended_id"],
            "confidence": group["confidence"],
            "kind": group["kind"],
            "face_safe": group["face_safe"],
            "sort_key": group["sort_key"],
        }
        digest.update(
            json.dumps(
                stable,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return {
        "group_count": len(groups),
        "member_count": member_count,
        "result_digest": digest.hexdigest(),
    }


def benchmark_similarity_groups(project: Project) -> dict[str, Any]:
    profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
    with closing(connect_db(project.db_path)) as conn:
        queries = QueryCounter()
        conn.set_trace_callback(queries)
        groups, metrics = measure(lambda: build_similarity_groups(conn, profile))
        conn.set_trace_callback(None)
    return {
        **metrics,
        "sql_queries": queries.report(),
        "summary": _groups_summary(groups),
    }


def measure_query_service(
    operation: Callable[[], Any],
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    queries = QueryCounter()

    def traced_connect_db(path: Path):
        conn = connect_db(path)
        conn.set_trace_callback(queries)
        return conn

    with mock.patch(
        "cullumi.photo_query_service.connect_db", new=traced_connect_db
    ):
        result, metrics = measure(operation)
    return result, metrics, queries.report()


def benchmark_photo_queries(project: Project) -> dict[str, Any]:
    service = PhotoQueryService(
        BenchmarkConfig(),
        BenchmarkManager(project),
        SimilarityGroupCache(),
        "benchmark-token",
    )

    with closing(connect_db(project.db_path)) as conn:
        photo_count = int(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0])
    deep_offset = max(0, photo_count - 120)

    def load_pages() -> list[dict[str, Any]]:
        pages = []
        for sort, direction, offset in (
            ("suggestion", "asc", 0),
            ("filename", "asc", deep_offset),
            ("size", "desc", deep_offset),
            ("taken", "desc", deep_offset),
        ):
            pages.append(
                service.photos(
                    {
                        "project_id": [project.project_id],
                        "sort": [sort],
                        "direction": [direction],
                        "offset": [str(offset)],
                        "limit": ["120"],
                        "decisions": ["all"],
                        "ai_states": ["all"],
                        "formats": ["all"],
                    }
                )
            )
        return pages

    pages, metrics, queries = measure_query_service(load_pages)
    payload = json.dumps(pages, ensure_ascii=False, separators=(",", ":"))
    return {
        **metrics,
        "sql_queries": queries,
        "response_bytes": len(payload.encode("utf-8")),
        "summary": {
            "totals": [page["total"] for page in pages],
            "page_sizes": [len(page["items"]) for page in pages],
            "first_ids": [
                int(page["items"][0]["id"]) if page["items"] else 0
                for page in pages
            ],
        },
    }


def benchmark_similarity_api(project: Project) -> dict[str, Any]:
    config = BenchmarkConfig()
    groups = SimilarityGroupCache()
    service = PhotoQueryService(
        config,
        BenchmarkManager(project),
        groups,
        "benchmark-token",
    )
    profile = config.get_profile(project.profile_id)
    with closing(connect_db(project.db_path)) as conn:
        groups.count(project.project_id, conn, profile, False)
    result, metrics, queries = measure_query_service(
        lambda: service.similar_groups(
            {
                "project_id": [project.project_id],
                "limit": ["120"],
                "offset": ["0"],
                "search": [""],
            }
        )
    )
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    summary_payload = [
        {
            "id": item["id"],
            "count": item["count"],
            "capture_count": item["capture_count"],
            "recommended_id": item["recommended_id"],
        }
        for item in result["items"]
    ]
    result_digest = hashlib.sha256(
        json.dumps(
            summary_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **metrics,
        "sql_queries": queries,
        "response_bytes": len(payload.encode("utf-8")),
        "summary": {
            "total": result["total"],
            "page_size": len(result["items"]),
            "result_digest": result_digest,
        },
    }


def benchmark_profile_estimate(project: Project) -> dict[str, Any]:
    profile = copy.deepcopy(BUILTIN_PROFILES["balanced"])
    manager = BenchmarkManager(project)
    scanner = Scanner(BenchmarkConfig(), manager)
    result, metrics = measure(
        lambda: estimate_profile(manager, scanner, project.project_id, profile)
    )
    return {
        **metrics,
        "sql_queries": None,
        "summary": result,
    }


def _create_exact_fixture(
    project: Project, photo_count: int, size_group_count: int
) -> None:
    size_group_count = max(1, min(photo_count, size_group_count))
    rows: list[tuple[Any, ...]] = []
    for index in range(photo_count):
        group = index % size_group_count
        size = group + 1
        relative = f"duplicates/group-{group:04d}-{index:06d}.jpg"
        path = project.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([group % 251]) * size)
        rows.append((relative, size))
    with closing(connect_db(project.db_path)) as conn:
        conn.executemany(
            """INSERT INTO photos(
                   relative_path,size,error,status,media_type
               ) VALUES(?,?,'','active','image')""",
            rows,
        )
        conn.commit()


def benchmark_exact_duplicates(
    base: Path, photo_count: int, size_group_count: int
) -> dict[str, Any]:
    root = base / "exact-library"
    cache = base / "exact-cache"
    root.mkdir()
    project = _new_project(root, cache, "exact")
    _create_exact_fixture(project, photo_count, size_group_count)
    scanner = Scanner(BenchmarkConfig(), object())
    with closing(connect_db(project.db_path)) as conn:
        queries = QueryCounter()
        conn.set_trace_callback(queries)
        unavailable, metrics = measure(
            lambda: scanner._exact_hashes(project, conn, threading.Event())
        )
        conn.set_trace_callback(None)
        summary = dict(
            conn.execute(
                """SELECT COUNT(*) photos,
                          COUNT(DISTINCT sha256) distinct_hashes,
                          SUM(CASE WHEN sha256='' THEN 1 ELSE 0 END) unhashed
                   FROM photos"""
            ).fetchone()
        )
    return {
        **metrics,
        "sql_queries": queries.report(),
        "summary": {
            **summary,
            "unavailable": unavailable,
            "size_groups": size_group_count,
        },
    }


def compare_results(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    def ratio(scenario: str, metric: str) -> float | None:
        if scenario not in current["scenarios"] or scenario not in baseline["scenarios"]:
            return None
        previous = baseline["scenarios"][scenario].get(metric)
        value = current["scenarios"][scenario].get(metric)
        if previous in (None, 0) or value is None:
            return None
        return round(value / previous, 4)

    summaries_match = {
        name: current["scenarios"][name]["summary"]
        == baseline["scenarios"][name]["summary"]
        for name in current["scenarios"]
        if name in baseline.get("scenarios", {})
    }
    exact_current = current["scenarios"].get("exact_duplicates")
    exact_baseline = baseline["scenarios"].get("exact_duplicates")
    return {
        "same_scale": current["scale"] == baseline.get("scale"),
        "summaries_match": summaries_match,
        "discovery_elapsed_ratio": ratio("discovery", "elapsed_seconds"),
        "similarity_python_peak_ratio": ratio(
            "similarity_groups", "python_peak_bytes"
        ),
        "similarity_rss_peak_increase_ratio": ratio(
            "similarity_groups", "rss_peak_increase_bytes"
        ),
        "profile_estimate_python_peak_ratio": ratio(
            "profile_estimate", "python_peak_bytes"
        ),
        "exact_select_queries_before": (
            exact_baseline["sql_queries"]["by_operation"].get("SELECT", 0)
            if exact_baseline and exact_current
            else None
        ),
        "exact_select_queries_after": (
            exact_current["sql_queries"]["by_operation"].get("SELECT", 0)
            if exact_baseline and exact_current
            else None
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run reproducible Cullumi large-library performance scenarios."
    )
    parser.add_argument("--discovery-files", type=int, default=100_000)
    parser.add_argument("--photos", type=int, default=50_000)
    parser.add_argument("--similar-edges", type=int, default=10_000)
    parser.add_argument("--exact-photos", type=int, default=5_000)
    parser.add_argument("--exact-size-groups", type=int, default=100)
    parser.add_argument("--variant-pairs", type=int, default=1_000)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use a small smoke-test scale instead of the supplied counts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/performance-results/latest.json"),
    )
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--only",
        choices=(
            "all",
            "discovery",
            "similarity_groups",
            "similarity_api",
            "photo_queries",
            "profile_estimate",
            "exact_duplicates",
        ),
        default="all",
        help="Run one scenario when investigating a specific regression.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scale = (
        BenchmarkScale(2_000, 2_000, 500, 500, 20, 100)
        if args.quick
        else BenchmarkScale(
            args.discovery_files,
            args.photos,
            args.similar_edges,
            args.exact_photos,
            args.exact_size_groups,
            args.variant_pairs,
        )
    )
    if min(asdict(scale).values()) < 1:
        raise ValueError("all benchmark counts must be positive")

    with tempfile.TemporaryDirectory(prefix="cullumi-perf-") as temporary:
        base = Path(temporary)
        scenarios: dict[str, Any] = {}
        if args.only in {"all", "discovery"}:
            scenarios["discovery"] = benchmark_discovery(base, scale.discovery_files)
        if args.only in {
            "all",
            "similarity_groups",
            "similarity_api",
            "photo_queries",
            "profile_estimate",
        }:
            photo_root = base / "photo-library"
            photo_cache = base / "photo-cache"
            photo_root.mkdir()
            project = _new_project(photo_root, photo_cache, "photos")
            _populate_photo_database(project, scale.photos, scale.similar_edges)
            _add_capture_variants(project, scale.variant_pairs)
            if args.only in {"all", "similarity_groups"}:
                scenarios["similarity_groups"] = benchmark_similarity_groups(project)
            if args.only in {"all", "profile_estimate"}:
                scenarios["profile_estimate"] = benchmark_profile_estimate(project)
            if args.only in {"all", "photo_queries"}:
                scenarios["photo_queries"] = benchmark_photo_queries(project)
            if args.only in {"all", "similarity_api"}:
                scenarios["similarity_api"] = benchmark_similarity_api(project)
        if args.only in {"all", "exact_duplicates"}:
            scenarios["exact_duplicates"] = benchmark_exact_duplicates(
                base, scale.exact_photos, scale.exact_size_groups
            )

    report: dict[str, Any] = {
        "schema": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "scale": asdict(scale),
        "scenarios": scenarios,
    }
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        report["comparison"] = compare_results(report, baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
