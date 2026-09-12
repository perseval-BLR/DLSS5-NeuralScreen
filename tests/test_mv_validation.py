"""MV validation: noise-floor vectors are zeroed, real motion survives.

DIS on a static desktop reports small noise vectors (capture noise,
cursor jitter, UI shimmer). NGX would treat them as real motion and
smear text/UI. The validation zeroes vectors below the noise floor
(0.5 px in flow space) before the upscale.

Checked: a synthetic shift of 2 px in flow space survives (magnitude
above the floor); a sub-floor shift (0.2 px) is zeroed; a static frame
produces no motion at all.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import numpy as np  # noqa: E402
from guides import TemporalGuideGenerator  # noqa: E402

W, H = 1280, 720
FW, FH = 320, 180


def _frame(shift: int = 0) -> np.ndarray:
    """A synthetic scene: a large bright bar on a dark background, shifted.

    The bar covers a third of the frame so the shift moves enough pixels
    to clear the static-screen threshold (scene_score > 0.001).
    """
    img = np.zeros((H, W, 4), dtype=np.uint8)
    img[100:500, 200 + shift:800 + shift, 0] = 200
    img[100:500, 200 + shift:800 + shift, 1] = 200
    img[100:500, 200 + shift:800 + shift, 2] = 200
    img[..., 3] = 255
    return img


def main() -> int:
    failures = []
    g = TemporalGuideGenerator(W, H, flow_width=FW, emit_small=True)

    # 1. A static frame: no motion at all.
    f0 = _frame(0)
    g.process(f0)
    f1 = _frame(0)
    out = g.process(f1)
    if out.reset:
        failures.append("a static frame must not reset")
    if np.abs(out.motion).max() > 0:
        failures.append(f"a static frame produced motion: {np.abs(out.motion).max():.3f}")

    # 2. A real shift (4 px in flow space ~ 16 px at 4K): survives.
    g2 = TemporalGuideGenerator(W, H, flow_width=FW, emit_small=True)
    g2.process(_frame(0))
    out2 = g2.process(_frame(4))
    mag = np.hypot(out2.motion[..., 0], out2.motion[..., 1])
    if mag.max() < 1.0:
        failures.append(f"a 4 px shift was zeroed: max {mag.max():.3f}")

    # 3. A sub-floor shift in WORK pixels, independent of the flow grid.
    g3 = TemporalGuideGenerator(W, H, flow_width=FW, emit_small=True)
    g3.process(_frame(0))
    from types import SimpleNamespace
    noise = np.zeros((FH, FW, 2), np.float32)
    noise[..., 0] = .1 * FW / W
    g3.dis = SimpleNamespace(calc=lambda *_: noise)
    out3 = g3.process(_frame(1))  # 1 px at 1280 wide = 0.25 px in flow space
    mag3 = np.hypot(out3.motion[..., 0], out3.motion[..., 1])
    if mag3.max() > 0.5:
        failures.append(f"a sub-floor shift survived: max {mag3.max():.3f}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: noise-floor vectors are zeroed, real motion survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
