"""Packaged NIQE resource, reference-score and multiprocessing smoke check.

Optional --output REPORT.json; only synthetic photos are used.
"""

import argparse
import hashlib
import json
import multiprocessing
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from cullumi.analysis_worker import PhotoAnalysisPool
from cullumi.niqe import NIQE_VERSION, initialize_niqe, model_directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert getattr(sys, "frozen", False), "Run the packaged probe executable"
    directory = model_directory()
    source = json.loads((directory / "SOURCE.json").read_text())
    assert hashlib.sha256((directory / "LICENSE.txt").read_bytes()).hexdigest() == source["license_sha256"]
    evaluator, error = initialize_niqe()
    assert not error, error
    with Image.fromarray(np.random.default_rng(6).integers(0, 256, (288, 384, 3), dtype=np.uint8)) as image:
        score = evaluator.compute(image)
    assert abs(score - 30.261472409374573) < 0.001
    with tempfile.TemporaryDirectory(prefix="cullumi-frozen-pool-") as temp:
        root = Path(temp)
        source = root / "source.jpg"
        with Image.fromarray(np.random.default_rng(3).integers(0, 256, (384, 512, 3), dtype=np.uint8)) as image:
            image.save(source)
        pool = PhotoAnalysisPool()
        try:
            with ThreadPoolExecutor(max_workers=pool.parallel_capacity) as executor:
                tasks = [executor.submit(pool.analyze, source, root / f"thumb-{i}.jpg", parallel=True)
                         for i in range(4)]
                results = [task.result() for task in tasks]
            assert all(not r["error"] and not r["niqe_error"] for r in results), results
            assert all(r["niqe_version"] == NIQE_VERSION for r in results)
            assert len({r["niqe_score"] for r in results}) == 1
            pids = [r._process.pid for r in pool._runners if r._process is not None]
            assert len(pids) == pool.parallel_capacity
        finally:
            pool.close()
    report = {"frozen": True, "resources": str(directory), "reference_score": score,
              "workers": len(pids), "equal_scores": True, "score": results[0]["niqe_score"]}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))



if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
