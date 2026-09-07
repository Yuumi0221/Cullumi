"""PyInstaller smoke entry: find packaged resources and compute without sources."""

import hashlib
import json
import sys

import numpy as np
from PIL import Image

from cullumi.niqe import initialize_niqe, model_directory


def main():
    assert getattr(sys, "frozen", False), "Run the packaged probe executable"
    directory = model_directory()
    source = json.loads((directory / "SOURCE.json").read_text())
    assert hashlib.sha256((directory / "LICENSE.txt").read_bytes()).hexdigest() == source["license_sha256"]
    evaluator, error = initialize_niqe()
    assert not error, error
    with Image.fromarray(np.random.default_rng(6).integers(0, 256, (288, 384, 3), dtype=np.uint8)) as image:
        score = evaluator.compute(image)
    assert abs(score - 30.261472409374573) < 0.001
    print(json.dumps({"frozen": True, "resources": str(directory), "score": score}))


if __name__ == "__main__":
    main()
