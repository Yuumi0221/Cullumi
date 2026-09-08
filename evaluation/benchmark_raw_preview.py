"""Compare full and scaled RAW embedded JPEG decoding on read-only local samples."""

import argparse
import copy
import json
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

import numpy as np

from cullumi import media
from cullumi.classification import classify
from cullumi.config import BUILTIN_PROFILES
from cullumi.project_store import connect_db
from cullumi.scanner import Scanner
from cullumi.similarity import quality_score
from evaluation.benchmark_large_library import (
    BenchmarkConfig,
    BenchmarkManager,
    _new_project,
)


def metric_differences(rows):
    metrics = ("width", "height", "megapixels", "size", "luminance", "contrast",
               "dark_clip", "bright_clip", "sharpness", "entropy", "niqe_score")
    maxima = {key: max(abs(r["full"]["metrics"][key] - r["scaled"]["metrics"][key])
                       for r in rows) for key in metrics}
    crossings = []
    for name, profile in BUILTIN_PROFILES.items():
        for metric in profile["quality"]["enabled"]:
            isolated = copy.deepcopy(profile)
            isolated["quality"]["enabled"] = {key: key == metric for key in profile["quality"]["enabled"]}
            isolated["quality"]["match_mode"] = "any"
            for row in rows:
                before = classify(row["full"]["metrics"], isolated)[0]
                after = classify(row["scaled"]["metrics"], isolated)[0]
                if before != after:
                    crossings.append({"name": row["name"], "profile": name, "metric": metric,
                                      "before": before, "after": after})
    return {"max_metric_absolute_differences": maxima, "individual_threshold_crossings": crossings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.samples.resolve()
    paths = sorted(p for p in root.rglob("*") if p.suffix.lower() in media.RAW_EXTENSIONS)
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    original_raw = media._open_raw
    rows, pairs, projects = [], {}, {}
    with tempfile.TemporaryDirectory(prefix="cullumi-raw-compare-") as temp:
        for mode in ("full", "scaled"):
            project = _new_project(root, Path(temp) / mode, mode)
            projects[mode] = project
        for index, path in enumerate(paths):
            values = {}
            for mode in (("full", "scaled") if index % 2 == 0 else ("scaled", "full")):
                project = projects[mode]
                thumbnail = project.thumb_dir / f"{index}.jpg"
                def decode(source, size):
                    return original_raw(source, None if mode == "full" else size)
                with mock.patch.object(media, "_open_raw", side_effect=decode):
                    start = time.perf_counter()
                    result = media.analyze_photo(path, thumbnail)
                    elapsed = (time.perf_counter() - start) * 1000
                assert not result["error"], result
                result["relative_path"] = path.relative_to(root).as_posix()
                result["cover_source"] = "still"
                with closing(connect_db(project.db_path)) as conn:
                    columns = list(result)
                    conn.execute(f"INSERT INTO photos({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(result.values()))
                    conn.commit()
                values[mode] = {"ms": elapsed, "metrics": result,
                                "profiles": {key: {"suggestion": classify(result, profile)[0],
                                                   "quality": quality_score(result, profile) * 100}
                                             for key, profile in BUILTIN_PROFILES.items()}}
            raw_diff = abs(values["full"]["metrics"]["niqe_score"] - values["scaled"]["metrics"]["niqe_score"])
            rows.append({"name": str(path.relative_to(root)), "niqe_absolute_difference": raw_diff, **values})
        for mode, project in projects.items():
            scanner = Scanner(BenchmarkConfig(), BenchmarkManager(project))
            with closing(connect_db(project.db_path)) as conn:
                pairs[mode] = {
                    name: [tuple(pair) for pair in scanner.plan_similarity_pairs(project, conn, profile, threading.Event())]
                    for name, profile in BUILTIN_PROFILES.items()
                }
    maximum = max(row["niqe_absolute_difference"] for row in rows)
    report = {
        "raw_count": len(paths), "max_niqe_absolute_difference": maximum,
        **metric_differences(rows),
        "accepted": maximum <= 0.2,
        "analysis_ms": {mode: {"p50": float(np.percentile([r[mode]["ms"] for r in rows], 50)),
                                "p95": float(np.percentile([r[mode]["ms"] for r in rows], 95))}
                        for mode in ("full", "scaled")},
        "suggestion_changes": {name: sum(r["full"]["profiles"][name]["suggestion"] != r["scaled"]["profiles"][name]["suggestion"] for r in rows)
                               for name in BUILTIN_PROFILES},
        "raw_only_similarity_pairs": pairs,
        "note": "Similarity comparison is restricted to the available RAW samples; all per-photo metrics and thresholds are recorded.",
        "rows": rows,
    }
    assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("rows", "raw_only_similarity_pairs")}, indent=2))
    assert report["accepted"], "RAW preview change exceeds the accepted NIQE tolerance"


if __name__ == "__main__":
    main()
