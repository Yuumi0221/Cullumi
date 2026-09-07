"""Deterministic RGB fixtures shared by the laboratory oracle and unit tests."""

import io

import numpy as np
from PIL import Image, ImageFilter


def reference_cases():
    cases = {}
    for seed, shape in [(6, (288, 384)), (1, (384, 512)), (19, (512, 341)), (21, (96, 192)), (25, (401, 509))]:
        array = np.random.default_rng(seed).integers(0, 256, (*shape, 3), dtype=np.uint8)
        cases[f"noise-{seed}"] = Image.fromarray(array)
    base = cases["noise-1"]
    for radius in (0.5, 1.5, 3):
        cases[f"blur-{radius}"] = base.filter(ImageFilter.GaussianBlur(radius))
    for quality in (10, 60):
        buffer = io.BytesIO()
        base.save(buffer, "JPEG", quality=quality)
        buffer.seek(0)
        with Image.open(buffer) as im:
            cases[f"jpeg-{quality}"] = im.convert("RGB")
    return cases
