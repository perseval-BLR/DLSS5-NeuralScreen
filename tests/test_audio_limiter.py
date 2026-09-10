"""The soft limiter keeps the recording from clipping.

The system mix can hand back peaks above 0 dBFS (measured up to +7.9 dB on
the bench), and AAC clips those peaks into distortion. The limiter folds
them toward 1.0 with a tanh tail. Checks:

  * a quiet signal passes through bit-for-bit untouched (no-op below the
    threshold);
  * a hot signal comes back with every sample inside [-1, 1] - nothing
    left to clip;
  * the limiter is monotonic: louder in, louder out - no pumping or
    ducking of the whole recording;
  * the fold is sign-preserving and symmetric.
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
from audio import LoopbackCapture  # noqa: E402

TH = LoopbackCapture.LIMIT_THRESHOLD


def main() -> int:
    failures = []

    # 1. Below the threshold: the same array comes back.
    quiet = np.array([-0.5, -0.1, 0.0, 0.1, 0.5, TH * 0.999], dtype=np.float32)
    out = LoopbackCapture._limit(quiet)
    if not np.array_equal(out, quiet):
        failures.append("a quiet signal must pass through untouched")

    # 2. A hot signal ends up inside [-1, 1].
    hot = np.array([-2.0, -1.2, -TH, 0.0, TH, 1.2, 2.0, 7.9], dtype=np.float32)
    out = LoopbackCapture._limit(hot)
    if float(np.abs(out).max()) > 1.0:
        failures.append(f"the limiter left a sample above 1.0: {out}")
    if not np.all(np.isfinite(out)):
        failures.append("the limiter produced NaN/Inf")

    # 3. Monotonic: louder in, louder out (strictly, on the hot side).
    x = np.linspace(0.0, 2.0, 2001, dtype=np.float32)
    y = LoopbackCapture._limit(x)
    if np.any(np.diff(y) < 0):
        failures.append("the limiter is not monotonic - it would pump")

    # 4. Sign-preserving and symmetric.
    if not np.array_equal(y, -LoopbackCapture._limit(-x)):
        failures.append("the limiter is not symmetric")

    # 5. The fold is continuous at the threshold (no step).
    eps = 1e-4
    below = LoopbackCapture._limit(np.array([TH - eps], dtype=np.float32))[0]
    above = LoopbackCapture._limit(np.array([TH + eps], dtype=np.float32))[0]
    if abs(float(above) - float(below)) > 2 * eps:
        failures.append("the limiter jumps at the threshold")

    # 6. The stereo path applies the limiter too (the real pipeline).
    stereo = np.array([[0.0, 0.0], [2.0, -2.0]], dtype=np.float32)
    out = LoopbackCapture._to_stereo(stereo, 1.0)
    if float(np.abs(out).max()) > 1.0:
        failures.append("_to_stereo must run the limiter as well")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"OK: limiter folds peaks above {TH:.2f} toward 1.0, "
          f"quiet passes untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
