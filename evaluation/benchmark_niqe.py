"""Read-only preview NIQE benchmark and optional independent laboratory oracle.

Run from the repository root: python -m evaluation.benchmark_niqe SAMPLE_DIR
--lab-python ../cullumi-quality-lab/.venv/Scripts/python.exe
--lab-root ../cullumi-quality-lab --output evaluation/performance-results/niqe.json
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

from cullumi.media import IMAGE_EXTENSIONS, open_image
from cullumi.niqe import NIQE_VERSION, initialize_niqe


def memory_bytes():
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    handle = ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess())
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return {name: getattr(counters, name) for name in ("WorkingSetSize", "PeakWorkingSetSize", "PrivateUsage")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--lab-python", type=Path)
    parser.add_argument("--lab-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # One decode per sample; the reference receives the exact same in-memory RGB.
    paths = sorted(p for p in args.samples.rglob("*") if p.suffix.lower() in IMAGE_EXTENSIONS)[:args.limit]
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    arrays, rows = {}, []
    for path in paths:
        try:
            with open_image(path, (512, 512))[0] as preview:
                preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
                arrays[str(len(rows))] = np.array(preview)
            rows.append({"name": path.name, "shape": list(arrays[str(len(rows))].shape)})
        except Exception as error:
            rows.append({"name": path.name, "decode_error": str(error)})
    memory_before = memory_bytes()
    started = time.perf_counter()
    evaluator, error = initialize_niqe()
    initialization_ms = (time.perf_counter() - started) * 1000
    if evaluator is None:
        raise RuntimeError(error)
    memory_initialized = memory_bytes()
    times = []
    for index, row in enumerate(rows):
        if str(index) not in arrays:
            continue
        with Image.fromarray(arrays[str(index)]) as preview:
            durations = []
            for repeat in range(args.repeats + 1):
                started = time.perf_counter()
                try:
                    row["score"] = evaluator.compute(preview)
                except Exception as error:
                    row["niqe_error"] = str(error)
                elapsed = (time.perf_counter() - started) * 1000
                if repeat:
                    durations.append(elapsed)
            row["ms"] = float(np.median(durations))
            times.extend(durations)
    memory_finished = memory_bytes()
    if args.lab_python and args.lab_root:
        with tempfile.TemporaryDirectory(prefix="cullumi-niqe-oracle-") as temp:
            previews, results = Path(temp) / "previews.npz", Path(temp) / "reference.json"
            np.savez(previews, **arrays)
            oracle = '''import sys,json,importlib.util
from pathlib import Path
import numpy as np
from PIL import Image
root, inputs, output = map(Path, sys.argv[1:])
spec=importlib.util.spec_from_file_location("lab_niqe",root/"src/cullumi_quality_lab/niqe.py")
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
evaluator=module.NiqeEvaluator(root/"models/niqe/niqe_pris_params.npz")
rows={}
with np.load(inputs,allow_pickle=False) as previews:
    for key in previews.files:
        try:
            with Image.fromarray(previews[key]) as im: rows[key]={"score":evaluator.compute(im)}
        except Exception as e: rows[key]={"error":str(e)}
output.write_text(json.dumps(rows),encoding="utf-8")
'''
            subprocess.run([str(args.lab_python.resolve()), "-c", oracle, str(args.lab_root.resolve()), str(previews), str(results)], check=True)
            reference = json.loads(results.read_text(encoding="utf-8"))
            for index, row in enumerate(rows):
                if str(index) in reference:
                    row["reference"] = reference[str(index)]
                    if "score" in row and "score" in row["reference"]:
                        row["absolute_error"] = abs(row["score"] - row["reference"]["score"])
    assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    report = {
        "version": NIQE_VERSION, "python": platform.python_version(),
        "numpy": np.__version__, "platform": platform.platform(),
        "samples": len(rows), "measurements": len(times), "initialization_ms": initialization_ms,
        "p50_ms": float(np.percentile(times, 50)), "p95_ms": float(np.percentile(times, 95)),
        "mean_ms": float(np.mean(times)), "seconds_per_1000": float(np.mean(times)),
        "memory_before": memory_before, "memory_initialized": memory_initialized,
        "memory_finished": memory_finished, "cache_info": initialize_niqe.cache_info()._asdict(),
        "sample_size_mtime_unchanged": True, "rows": rows,
    }
    if args.lab_root:
        report["reference_code_sha256"] = hashlib.sha256((args.lab_root / "src/cullumi_quality_lab/niqe.py").read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print("maximum_absolute_error", max((r.get("absolute_error", 0) for r in rows), default=0))


if __name__ == "__main__":
    main()
