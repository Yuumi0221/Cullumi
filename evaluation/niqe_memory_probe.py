"""Isolated process memory comparison, with no original photos required.

python -m evaluation.niqe_memory_probe [baseline|niqe]
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from cullumi import media, niqe
from evaluation.benchmark_niqe import memory_bytes


def main():
    mode = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix="cullumi-niqe-memory-") as temp:
        root = Path(temp)
        source = root / "synthetic.jpg"
        image = Image.fromarray(np.random.default_rng(7).integers(0, 256, (341, 512, 3), dtype=np.uint8))
        image.save(source, "JPEG")
        image.close()
        initial = memory_bytes()
        if mode == "niqe":
            niqe.initialize_niqe()
        initialized = memory_bytes()
        snapshots = []
        evaluator = media.evaluate_preview if mode == "niqe" else lambda _: {
            "niqe_score": None, "niqe_error": "baseline", "niqe_version": "",
        }
        with mock.patch.object(media, "evaluate_preview", evaluator):
            for index in range(500):
                result = media.analyze_photo(source, root / "thumb.jpg")
                assert not result["error"]
                if (index + 1) in (1, 50, 100, 250, 500):
                    snapshots.append({"photos": index + 1, **memory_bytes()})
        print(json.dumps({"mode": mode, "initial": initial, "initialized": initialized, "snapshots": snapshots, "cache_info": niqe.initialize_niqe.cache_info()._asdict()}, indent=2))


if __name__ == "__main__":
    main()
