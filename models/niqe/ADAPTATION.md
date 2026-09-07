# Cullumi NIQE adaptation

The three upstream files (`niqe_pris_params.npz`, `SOURCE.json`, `LICENSE.txt`)
are copied byte for byte from the sibling cullumi-quality-lab project.
`SOURCE.json` describes that laboratory adaptation, including its OpenCV use.
Cullumi's runtime implementation is `cullumi/niqe.py`, adapted from laboratory
code SHA-256 `209cbf998378f9e3a4a1118a718268dc3fef47fae6fc4ce5ca6bfd2371b8c6fc`.
Original implementation copyright BasicSR Authors; Apache License 2.0 applies.

Cullumi changes use NumPy separable 7×7 Gaussian correlation with replicated
edges and batched AGGD fitting. MATLAB rounded Y, 96-pixel blocks and the
laboratory's antialiased Keys bicubic half-size implementation are preserved.
No OpenCV, SciPy, Torch, training, human labels, network access or photo file
access is used by the evaluator. Evaluation consumes the existing RGB preview
at a maximum side of 512 pixels. The pristine parameters are fixed and verified
against their SHA-256 once at initialization.

Floating-point accumulation differs from OpenCV. Flat regions with residuals
near zero can change AGGD sign counts, so numerical agreement is approximate,
not bitwise. See `evaluation/NIQE_REPORT.md` for measured agreement, performance
and calibration limitations. Bump `NIQE_VERSION` for future changes to the
algorithm, parameters, decoding or preview preprocessing.
