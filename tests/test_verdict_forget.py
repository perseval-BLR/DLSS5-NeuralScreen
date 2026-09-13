"""The feature-18 verdict dies with the worker that gave it.

st.gpu_ok is latched: refresh_gpu_ok() decides once and returns immediately
ever after, which is what keeps it off the per-frame path. The latch belongs
to ONE worker process though - a replacement calls CreateFeature again and
can answer differently.

Only rebuild_pipeline used to clear it, so the monitor and window switches
were right and the four restart paths were not: a card that started working
kept the red dot, and a pipeline that came back BROKEN kept the green one -
while TECHNICAL.md tells the user that dot is the thing to trust.

A live RNSZ resize is the exception that has to stay an exception:
resize_window_live keeps the same process, so the verdict it gave still
stands and must NOT be forgotten (that would put the dot back to "unknown"
for no reason, every time a captured window is resized).

Checked:
* forget_verdict clears both the verdict and the alert latch;
* every restart_worker() call site forgets the verdict right after;
* resize_window_live does not;
* the guides' bypass contract: clearing previous_gray makes the next real
  frame a scene cut rather than a correlation against a stale screen.
"""
import os
import re
import sys
from types import SimpleNamespace

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import numpy as np  # noqa: E402
import channels  # noqa: E402
from guides import TemporalGuideGenerator  # noqa: E402


def source(name: str) -> str:
    with open(os.path.join(BASE, name), encoding="utf-8") as fh:
        return fh.read()


def restart_sites_forget(src: str, window: int = 14) -> list:
    """Line numbers of restart_worker() calls with no forget_verdict after."""
    lines = src.splitlines()
    missing = []
    for i, line in enumerate(lines):
        if "restart_worker(" not in line or "def restart_worker" in line:
            continue
        after = "\n".join(lines[i:i + window])
        if "forget_verdict" not in after:
            missing.append(i + 1)
    return missing


def main() -> int:
    failures = []

    # 1. It clears both fields.
    st = SimpleNamespace(gpu_ok=True, gpu_alerted=True)
    channels.forget_verdict(st)
    if st.gpu_ok is not None:
        failures.append(f"gpu_ok is {st.gpu_ok!r}, want None")
    if st.gpu_alerted is not False:
        failures.append(f"gpu_alerted is {st.gpu_alerted!r}, want False")

    # 2. Every restart path forgets the verdict.
    for name in ("main.py", "pipeline.py"):
        missing = restart_sites_forget(source(name))
        if missing:
            failures.append(f"{name}: restart_worker at line(s) {missing} "
                            f"does not forget the verdict")

    # 3. A live resize keeps it: the worker survives an RNSZ.
    pipeline_src = source("pipeline.py")
    match = re.search(r"def resize_window_live\(.*?(?=\ndef )", pipeline_src,
                      re.S)
    if match is None:
        failures.append("resize_window_live not found in pipeline.py")
    elif "forget_verdict" in match.group(0):
        failures.append("resize_window_live forgets the verdict, but RNSZ "
                        "keeps the same worker process")

    # 4. The bypass contract in guides: no history -> a scene cut, no motion.
    g = TemporalGuideGenerator(640, 360, flow_width=320, emit_small=True)
    frame = np.zeros((360, 640, 4), dtype=np.uint8)
    frame[..., 3] = 255
    frame[80:280, 100:500, :3] = 210
    g.process(frame)                      # history exists now
    g.previous_gray = None                # what the bypass branch does
    moved = frame.copy()
    moved[80:280, 100:500, :3] = 0
    moved[80:280, 180:580, :3] = 210      # a large, unmistakable move
    out = g.process(moved)
    if not out.reset:
        failures.append("a cleared history must report a scene cut")
    if np.abs(out.motion).max() != 0:
        failures.append("a cleared history must produce no motion, got "
                        f"{np.abs(out.motion).max():.3f}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the verdict dies with its worker, a live resize keeps it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
