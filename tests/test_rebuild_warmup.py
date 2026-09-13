"""A rebuild does not pay the cold-start warm-up.

st.effective_warmup is 120 frames. It exists for the LAUNCH: a cold card
can take seconds to produce its first NGX frame, and the frame watchdog
would kill the worker on frame 0 and start a restart storm (RTX 2070 at
~1 FPS, RTX 3060 Ti at ~18 FPS). By the time anything calls
rebuild_pipeline the card has been running the network for a while, so
those frames are a second of veil for nothing.

Measured on a window resize in one-window mode - a browser toggling
fullscreen, which is what the "it dims and animates while I watch a
video" report turned out to be:

    capture closed -> first confirmed frame    1.845 s  (warmup=120)
    the same cycle                             0.972 s  (warmup=10)

do_restart has used RESTART_WARMUP for a full process restart since 1.6,
and that is the same feature being recreated in the same way.

The one thing that must NOT be lost: a pre-Blackwell card is given 4
frames at startup, because for it even the watchdog is the problem
(audit F3 - the first revive brought 120 back and the restarts climbed
to NR OFF). min(), not the constant.

Run:  runtime\\python.exe tests\\test_rebuild_warmup.py
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pipeline  # noqa: E402


class _Display:
    def __init__(self):
        self.menu = types.SimpleNamespace(visible=False, offset=(0, 0),
                                          user_scale=1.0, user_height=None,
                                          state={"theme": "light"},
                                          set_state=lambda *a, **k: None,
                                          set_stats=lambda *a, **k: None)

    def enter_switch_mode(self, *a, **k):
        pass

    def resize(self, *a, **k):
        pass

    def set_visible(self, *a, **k):
        pass

    def is_visible(self):
        return True


def _state(effective_warmup):
    return types.SimpleNamespace(
        width=2560, height=1600, work_w=1664, work_h=1040,
        params={"intensity": 1.0}, effective_warmup=effective_warmup,
        cfg={"profile": "Natural", "lang": "en"}, lang="en",
        display=_Display(), output_rgba=None,
        capture=types.SimpleNamespace(resolution=(2560, 1600)),
        worker=None, worker_logs=[], reader=None, worker_stop=None, shm=None)


def main() -> int:
    failures = []
    seen = []

    saved = (pipeline.start_worker, pipeline.SharedFrameBuffer)
    pipeline.SharedFrameBuffer = lambda w, h: types.SimpleNamespace(
        name="x", close=lambda: None, width=w, height=h)

    def fake_start_worker(params, w, h, warmup, full_w, full_h, shm):
        seen.append(int(warmup))
        return (types.SimpleNamespace(poll=lambda: None), [], None, None)

    pipeline.start_worker = fake_start_worker
    try:
        for effective, want, why in (
                (120, pipeline.RESTART_WARMUP,
                 "the launch warm-up must not be paid again on a rebuild"),
                (4, 4,
                 "a pre-Blackwell card keeps its short warm-up (audit F3)"),
                (10, 10, "an already-short warm-up is left alone")):
            seen.clear()
            st = _state(effective)
            try:
                pipeline.rebuild_pipeline(st, "note")
            except Exception as exc:
                # Everything after start_worker touches the real window and
                # the real channels; the number is already recorded.
                if not seen:
                    failures.append(f"rebuild_pipeline died before starting "
                                    f"the worker: {exc!r}")
                    continue
            got = seen[0] if seen else None
            print(f"    effective_warmup={effective} -> worker warmup={got}")
            if got != want:
                failures.append(f"{why}: expected {want}, got {got}")
    finally:
        pipeline.start_worker, pipeline.SharedFrameBuffer = saved

    if pipeline.RESTART_WARMUP >= 120:
        failures.append("RESTART_WARMUP grew to the cold-start value - the "
                        "rebuild is paying the launch warm-up again")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a rebuild warms up briefly, a cold launch does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
