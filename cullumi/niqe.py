"""CPU NIQE, adapted from BasicSR (Apache-2.0; models/niqe/SOURCE.json).

Original implementation copyright BasicSR Authors. Local adaptation replaces
SciPy convolution and Torch resizing with NumPy; rejects degenerate images.
Cullumi adaptation uses a separable 7×7 convolution with replicated edges.

Uses official pristine statistics, 96px blocks, rounded MATLAB Y luminance,
and two scales with antialiased Keys bicubic downsampling. No label input.
"""

import hashlib
import io
import math
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

PARAMETERS_SHA256 = "2a7c182a68c9e7f1b2e2e5ec723279d6f65d912b6fcaf37eb2bf03d7367c4296"
# Bump on any algorithm, parameters, decoder or preview-preprocessing change.
NIQE_VERSION = "niqe-2a7c182a:numpy-v1:rgb512-lanczos-matlab-y96"


def model_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "models" / "niqe"


def _convolve(image, kernel):
    """Separable Gaussian correlation, equivalent to filter2D BORDER_REPLICATE."""
    for axis in (0, 1):
        padding = [(0, 0), (0, 0)]
        padding[axis] = (3, 3)
        padded = np.pad(image, padding, mode="edge")
        result = np.zeros_like(image)
        for offset, weight in enumerate(kernel):
            slices = [slice(None), slice(None)]
            slices[axis] = slice(offset, offset + image.shape[axis])
            result += padded[tuple(slices)] * weight
        image = result
    return image

_ALPHAS = np.arange(0.2, 10.001, 0.001)
_G1 = np.array([math.gamma(1 / a) for a in _ALPHAS])
_G2 = np.array([math.gamma(2 / a) for a in _ALPHAS])
_G3 = np.array([math.gamma(3 / a) for a in _ALPHAS])
_RATIOS = _G2**2 / (_G1 * _G3)


def _aggd(block):
    """Fit all blocks together, keeping the laboratory's nearest alpha lookup."""
    axes = (-2, -1)
    squared = block**2
    left, right = block < 0, block > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        ls = np.sqrt((squared * left).sum(axis=axes) / left.sum(axis=axes))
        rs = np.sqrt((squared * right).sum(axis=axes) / right.sum(axis=axes))
        gh = ls / rs
        rh = np.mean(np.abs(block), axis=axes) ** 2 / squared.mean(axis=axes)
        normalized = rh * (gh**3 + 1) * (gh + 1) / (gh**2 + 1) ** 2
    upper = np.minimum(np.searchsorted(_RATIOS, normalized), len(_RATIOS) - 1)
    lower = np.maximum(0, upper - 1)
    i = np.where(abs(_RATIOS[lower] - normalized) <= abs(_RATIOS[upper] - normalized), lower, upper)
    factor = np.sqrt(_G1[i] / _G3[i])
    valid = np.isfinite(normalized)
    return tuple(np.where(valid, value, np.nan) for value in (
        _ALPHAS[i], ls * factor, rs * factor, _G2[i] / _G1[i],
    ))


def _features(block):
    alpha, left, right, _ = _aggd(block)
    result = [alpha, (left + right) / 2]
    for shift in ((0, 1), (1, 0), (1, 1), (1, -1)):
        alpha, left, right, ratio = _aggd(block * np.roll(block, shift, axis=(-2, -1)))
        result.extend((alpha, (right - left) * ratio, left, right))
    return np.stack(result, axis=-1)


def _half_size(image):
    """MATLAB-style antialiased bicubic resize by exactly 0.5, symmetric edges."""
    for axis in (0, 1):
        length = image.shape[axis]
        centers = 2 * np.arange(length // 2) + 0.5
        indices = np.floor(centers[:, None]).astype(int) + np.arange(-3, 5)
        x = np.abs((centers[:, None] - indices) / 2)
        weights = np.where(
            x <= 1,
            1.5 * x**3 - 2.5 * x**2 + 1,
            np.where(x <= 2, -0.5 * x**3 + 2.5 * x**2 - 4 * x + 2, 0),
        )
        weights /= weights.sum(axis=1, keepdims=True)
        folded = indices % (2 * length)
        folded = np.where(folded < length, folded, 2 * length - 1 - folded)
        moved = np.moveaxis(image, axis, 0)
        image = np.moveaxis(np.sum(moved[folded] * weights[:, :, None], axis=1), 0, axis)
    return image


class NiqeEvaluator:
    def __init__(self, parameters: Path):
        data = parameters.read_bytes()
        if hashlib.sha256(data).hexdigest() != PARAMETERS_SHA256:
            raise ValueError("NIQE 参数校验失败")
        with np.load(io.BytesIO(data), allow_pickle=False) as params:
            self.mean = params["mu_pris_param"].reshape(36)
            self.cov = params["cov_pris_param"]
            self.window = params["gaussian_window"]
        self.kernel = self.window.sum(axis=0)
        if not np.allclose(np.outer(self.kernel, self.kernel), self.window, atol=1e-15):
            raise ValueError("NIQE Gaussian window is not separable")

    def compute(self, image: Image.Image) -> float:
        if image.mode == "RGB":
            array = np.asarray(image, dtype=np.float64)
        else:
            with image.convert("RGB") as rgb:
                array = np.asarray(rgb, dtype=np.float64)
        luminance = np.round(array @ np.array([65.481, 128.553, 24.966]) / 255 + 16)
        return self.compute_luminance(luminance)

    def compute_luminance(self, luminance) -> float:
        h, w = np.array(luminance.shape) // 96
        if h * w < 2:
            raise ValueError("NIQE needs at least two complete 96×96 blocks")
        img = luminance[: h * 96, : w * 96].astype(np.float64)
        scales = []
        for scale in (1, 2):
            mu = _convolve(img, self.kernel)
            sq = _convolve(img**2, self.kernel)
            normalized = (img - mu) / (np.sqrt(np.abs(sq - mu**2)) + 1)
            size = 96 // scale
            blocks = normalized.reshape(h, size, w, size).transpose(2, 0, 1, 3).reshape(h * w, size, size)
            scales.append(_features(blocks))
            if scale == 1:
                img = _half_size(img)
        features = np.concatenate(scales, axis=1)
        valid = features[np.isfinite(features).all(axis=1)]
        if len(valid) < 2:
            raise ValueError("NIQE has fewer than two valid textured blocks")
        delta = self.mean - np.nanmean(features, axis=0)
        covariance = (self.cov + np.cov(valid, rowvar=False)) / 2
        squared = float(delta @ np.linalg.pinv(covariance) @ delta)
        if not math.isfinite(squared) or squared < -1e-8:
            raise ValueError("NIQE could not produce a finite statistical distance")
        return math.sqrt(max(0, squared))


@lru_cache(maxsize=1)
def initialize_niqe() -> tuple[NiqeEvaluator | None, str]:
    """Load once per process, including failed initialization; no photo I/O."""
    try:
        return NiqeEvaluator(model_directory() / "niqe_pris_params.npz"), ""
    except Exception as error:
        return None, str(error) or type(error).__name__


def evaluate_preview(preview: Image.Image) -> dict:
    result = {"niqe_score": None, "niqe_error": "", "niqe_version": NIQE_VERSION}
    try:
        evaluator, error = initialize_niqe()
        if evaluator is None:
            raise ValueError(error)
        result["niqe_score"] = evaluator.compute(preview)
    except Exception as error:
        result["niqe_error"] = str(error) or type(error).__name__
    return result


def valid_niqe(row) -> float | None:
    """Accept finite nonnegative measurements only, never an error sentinel."""
    if "niqe_score" not in row.keys() or ("niqe_error" in row.keys() and row["niqe_error"]):
        return None
    value = row["niqe_score"]
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def niqe_is_current(row) -> bool:
    return bool(
        "niqe_version" in row.keys()
        and row["niqe_version"] == NIQE_VERSION
        and (valid_niqe(row) is not None or row["niqe_error"])
    )
