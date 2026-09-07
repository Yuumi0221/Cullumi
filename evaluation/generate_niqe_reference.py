"""Generate golden scores using the unmodified sibling laboratory implementation.

Run with the laboratory Python (which already has OpenCV), passing its root.
This developer utility is never imported or bundled by the application.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    import cv2
    import numpy as np
    from PIL import __version__ as pillow_version

    root = Path(__file__).resolve().parents[1]
    lab = Path(sys.argv[1]).resolve()
    reference = lab / "src/cullumi_quality_lab/niqe.py"
    evaluator = load_module(reference).NiqeEvaluator(lab / "models/niqe/niqe_pris_params.npz")
    images = load_module(root / "tests/niqe_cases.py").reference_cases()
    try:
        result = {
            "reference": "cullumi-quality-lab/src/cullumi_quality_lab/niqe.py",
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "parameters_sha256": hashlib.sha256((lab / "models/niqe/niqe_pris_params.npz").read_bytes()).hexdigest(),
            "numpy": np.__version__, "opencv": cv2.__version__, "pillow": pillow_version,
            # Flat areas can acquire ~1e-13 signed residuals from differing
            # convolution accumulation order. NIQE's AGGD sign counts amplify
            # these; heavily smoothed inputs are not bitwise-portable.
            "tolerance_absolute": 0.001,
            "scores": {name: evaluator.compute(image) for name, image in images.items()},
        }
    finally:
        for image in images.values():
            image.close()
    path = root / "tests/fixtures/niqe_reference.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
