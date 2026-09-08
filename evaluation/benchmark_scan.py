"""Real scan benchmark. Source photos are read-only; all caches live in a temp directory.

python -m evaluation.benchmark_scan SAMPLE_DIR --output evaluation/performance-results/scan.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import ExitStack, closing
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from unittest import mock

import numpy as np

from cullumi.analysis_worker import PhotoAnalysisPool
from cullumi.project_store import connect_db
from cullumi.scanner import Scanner
from evaluation.benchmark_large_library import (
    BenchmarkConfig,
    BenchmarkManager,
    _new_project,
)
from evaluation.benchmark_niqe import memory_bytes


class ScanConfig(BenchmarkConfig):
    def __init__(self, fast):
        self.fast = fast

    def snapshot(self):
        return {"projects": {}, "fast_analysis": self.fast}


def stable_results(project):
    with closing(connect_db(project.db_path)) as conn:
        photos = [tuple(row) for row in conn.execute(
            """SELECT id,relative_path,width,height,sharpness,luminance,dark_clip,bright_clip,
               contrast,entropy,phash,dhash,niqe_score,niqe_error,quality_score,suggestion,decision
               FROM photos ORDER BY id"""
        )]
        pairs = [tuple(row) for row in conn.execute(
            "SELECT a_id,b_id,kind,score,recommended_id FROM similar_pairs ORDER BY a_id,b_id"
        )]
    return hashlib.sha256(json.dumps([photos, pairs], separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def _usage_api():
    # ctypes caches pointer types globally. Define these once, not once per
    # sample, otherwise the monitor itself appears to leak process memory.
    import ctypes
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "peak", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile"
            )
        ]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO)]
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    return ctypes, kernel, Memory, IO


def process_usage(pid):
    """Measure live parent/worker working sets and OS read counters on Windows."""
    if os.name != "nt":
        return None
    ctypes, kernel, Memory, IO = _usage_api()
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        return None
    try:
        memory, io = Memory(), IO()
        memory.cb = ctypes.sizeof(memory)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
            return None
        kernel.GetProcessIoCounters(handle, ctypes.byref(io))
        return {"working_bytes": memory.working, "read_bytes": io.read_bytes}
    finally:
        kernel.CloseHandle(handle)


def run_scan(scanner, project, pool):
    stages, timings, io_seen = {}, defaultdict(list), {}
    peak = {"parent": 0, "workers_total": 0}
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            pids = []
            for runner in pool._runners:
                process = runner._process
                if process is not None:
                    try:
                        pids.append(process.pid)
                    except ValueError:
                        pass
            workers = []
            for pid in [os.getpid(), *pids]:
                usage = process_usage(pid)
                if usage:
                    io_seen[pid] = max(io_seen.get(pid, 0), usage["read_bytes"])
                    if pid == os.getpid():
                        peak["parent"] = max(peak["parent"], usage["working_bytes"])
                    else:
                        workers.append(usage["working_bytes"])
            peak["workers_total"] = max(peak["workers_total"], sum(workers))
            stop.wait(0.025)

    initial_pids = [os.getpid(), *[r._process.pid for r in pool._runners if r._process is not None]]
    initial_io = {pid: (process_usage(pid) or {}).get("read_bytes", 0) for pid in initial_pids}
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    with ExitStack() as patches:
        for name in ("_prepare_scan", "_scan_database", "_confirm_exact_duplicates", "_rebuild_relationships"):
            original = getattr(scanner, name)
            def timed(*args, _name=name, _function=original, **kwargs):
                start = time.perf_counter()
                try:
                    return _function(*args, **kwargs)
                finally:
                    stages[_name] = time.perf_counter() - start
            patches.enter_context(mock.patch.object(scanner, name, timed))
        analyze = scanner.analyze_photo
        def measured(source, *args, **kwargs):
            start = time.perf_counter()
            result = analyze(source, *args, **kwargs)
            timings[source.suffix.lower()].append((time.perf_counter() - start) * 1000)
            return result
        patches.enter_context(mock.patch.object(scanner, "analyze_photo", measured))
        start = time.perf_counter()
        try:
            scanner._run(project.project_id, threading.Event())
        finally:
            elapsed = time.perf_counter() - start
            stop.set()
            monitor.join()
    progress = scanner.get_progress(project.project_id)
    assert progress["stage"] == "complete", progress
    for pid, count in initial_io.items():
        io_seen[pid] = max(0, io_seen.get(pid, 0) - count)
    return {
        "seconds": elapsed, "stages_seconds": stages, "analysis_calls": sum(map(len, timings.values())),
        "by_format_ms": {ext: {"count": len(values), "p50": float(np.percentile(values, 50)),
                               "p95": float(np.percentile(values, 95))} for ext, values in timings.items()},
        "peak_working_bytes": peak, "sampled_os_read_bytes": sum(io_seen.values()),
        "note": "OS counters include imports and caches; short-lived process reads may be undersampled.",
        "result_digest": stable_results(project),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    root = args.samples.resolve()
    config = ScanConfig(False)
    reports = []
    with tempfile.TemporaryDirectory(prefix="cullumi-scan-benchmark-") as temp:
        base = Path(temp)
        for run in range(args.repeats):
            for fast in ((False, True) if run % 2 == 0 else (True, False)):
                project = _new_project(root, base / f"run-{run}-{fast}", "benchmark")
                config.fast = fast
                pool = PhotoAnalysisPool()
                scanner = Scanner(config, BenchmarkManager(project), analysis_runner=pool)
                discovery = scanner._discover(project, threading.Event())
                if args.limit:
                    discovery = replace(discovery, photos=discovery.photos[:args.limit])
                before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in discovery.photos}
                # The immutable discovery fixture selects sources, never copies/modifies them.
                with mock.patch.object(scanner, "_discover", return_value=discovery):
                    try:
                        print(f"run={run} fast={fast} photos={len(discovery.photos)} first scan", flush=True)
                        first = run_scan(scanner, project, pool)
                        warm = run_scan(scanner, project, pool)
                        assert warm["analysis_calls"] == 0
                        assert first["result_digest"] == warm["result_digest"]
                        with closing(connect_db(project.db_path)) as conn:
                            conn.execute("UPDATE photos SET niqe_version='benchmark-refresh' WHERE id=1")
                            conn.commit()
                        refresh = run_scan(scanner, project, pool)
                        with closing(connect_db(project.db_path)) as conn:
                            conn.execute("UPDATE photos SET mtime=0 WHERE id=1")
                            conn.commit()
                        changed = run_scan(scanner, project, pool)
                        assert changed["analysis_calls"] == 1
                        reports.append({"run": run, "fast": fast, "workers": pool.parallel_capacity if fast else 1,
                                        "first": first, "warm": warm, "single_niqe_refresh": refresh,
                                        "single_invalidated_source": changed})
                        print(json.dumps({"first_s": first["seconds"], "warm_s": warm["seconds"], "refresh_s": refresh["seconds"]}), flush=True)
                    finally:
                        pool.close()
                assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in discovery.photos}
    assert len({r["first"]["result_digest"] for r in reports}) == 1
    report = {"runs": reports, "source_stat_unchanged": True, "single_parallel_results_equal": True,
              "blink_enabled": False, "memory_after": memory_bytes(),
              "cache_note": "OS disk cache was not flushed; discovery measured separately. Stage timings include monitoring."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
