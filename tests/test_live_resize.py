"""A window resize reconfigures the worker instead of replacing it.

Reported as "with a fullscreen video, seeking or changing the quality makes
the picture dim and animate, as if the mode were switching". It was a mode
switch: a browser toggling fullscreen changes its frame from 3840x2100 to
3840x2160, follow_window sees a real resize, and switch_window used to kill
the worker and start another one. Measured on that exact cycle:

    capture closed -> first frame    1.845 s   (before)
                                     0.972 s   (short warm-up)
                                     0.112 s   (reconfigured live)

and the last one raises no veil at all, because no process died.

What this pins, with no worker and no window:

* a resize takes the live path and switch_window is never called;
* it REFUSES when the worker is not the one capturing - the colour then
  still travels through the shared section, whose colour region is sized
  for the window the pipeline was built for, and a larger window would run
  off the end of it;
* a refusal, at either step, falls back to the full rebuild rather than
  leaving the pipeline half-reconfigured;
* the work size, the guides and the motion channel move together - the
  worker reads exactly work_w*work_h*4 bytes of motion, and the two
  drifting apart is what used to hang the program (see do_restart).

Run:  runtime\\python.exe tests\\test_live_resize.py
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import channels  # noqa: E402
import pipeline  # noqa: E402

FRAME = (3840, 2100)        # the browser, maximised
FULLSCREEN = (3840, 2160)   # the same browser, fullscreen


class _Display:
    def __init__(self):
        self.resized_to = None
        self.menu = types.SimpleNamespace(visible=False)

    def is_visible(self):
        return True

    def set_visible(self, on):
        pass

    def move_to(self, x, y):
        pass

    def raise_topmost(self):
        pass

    def resize(self, w, h):
        self.resized_to = (w, h)

    def is_switch_active(self):
        return False

    def enter_switch_mode(self, *a, **k):
        raise AssertionError("the live path must not raise the veil")


def _state(dda_mode=True):
    return types.SimpleNamespace(
        window_hwnd=0x1234, worker_failed=False, dda_mode=dda_mode,
        present_mode=True, motion_small=True, nr_small=False,
        work_scale=0.65, width=FRAME[0], height=FRAME[1],
        work_w=2496, work_h=1364, params={"intensity": 1.0},
        follow_pos=(0, 0), follow_size=FRAME, follow_resize=None,
        frame_index=7, pts=99, work_frame=object(), last_restart=0.0,
        worker=types.SimpleNamespace(poll=lambda: None),
        reader=types.SimpleNamespace(wait_rack=lambda timeout=0: None,
                                     set_output_size=lambda w, h: None),
        display=_Display(), cfg={"profile": "Natural"}, lang="en")


def _patch(calls, probe=FULLSCREEN, rack_raises=False, probe_raises=False):
    """Stub everything that touches a real worker."""
    saved = {name: getattr(pipeline, name) for name in
             ("send_resize", "switch_window", "TemporalGuideGenerator")}
    saved_ch = {name: getattr(channels, name) for name in
                ("probe_window_capture", "sync_motion_size", "sync_gray",
                 "forget_out", "enable_out_shm", "enable_present")}

    def probe_fn(st, hwnd):
        calls.append("probe")
        if probe_raises:
            raise RuntimeError("no ack")
        return probe

    def resize_fn(*a, **k):
        calls.append("rnsz")
        if rack_raises:
            raise TimeoutError("no RACK")

    pipeline.send_resize = resize_fn
    pipeline.switch_window = lambda st, hwnd: calls.append("restart")
    pipeline.TemporalGuideGenerator = lambda w, h, emit_small=False: \
        calls.append(f"guides:{w}x{h}") or types.SimpleNamespace(
            motion_width=w, motion_height=h)
    channels.probe_window_capture = probe_fn
    for name in ("sync_motion_size", "sync_gray", "forget_out",
                 "enable_out_shm", "enable_present"):
        setattr(channels, name,
                (lambda n: (lambda st: calls.append(n)))(name))
    pipeline.channels = channels
    return saved, saved_ch


def _restore(saved, saved_ch):
    for name, fn in saved.items():
        setattr(pipeline, name, fn)
    for name, fn in saved_ch.items():
        setattr(channels, name, fn)


def main() -> int:
    failures = []

    # 1. The happy path: reconfigured live, no restart, no veil.
    calls = []
    saved, saved_ch = _patch(calls)
    try:
        st = _state()
        ok = pipeline.resize_window_live(st, *FULLSCREEN)
    finally:
        _restore(saved, saved_ch)
    print(f"    live resize: {ok}, steps {calls}")
    if not ok:
        failures.append("a plain resize was refused")
    if "restart" in calls:
        failures.append("the live path fell through to a worker restart")
    for step in ("probe", "rnsz", "sync_motion_size", "sync_gray",
                 "forget_out", "enable_out_shm", "enable_present"):
        if step not in calls:
            failures.append(f"the live path skipped {step}")
    if st.display.resized_to != FULLSCREEN:
        failures.append(f"the overlay was not resized: {st.display.resized_to}")
    if (st.width, st.height) != FULLSCREEN:
        failures.append(f"the pipeline size is {(st.width, st.height)}")
    if st.follow_size != FULLSCREEN:
        failures.append("follow_size was not moved to the new frame - the "
                        "next frame would ask for the same resize again")
    if not any(c.startswith("guides:") for c in calls):
        failures.append("the guides were not rebuilt with the work size")
    guides = next(c for c in calls if c.startswith("guides:"))
    if guides.split(":")[1] != f"{st.work_w}x{st.work_h}":
        failures.append(f"the guides and the work size disagree: {guides} "
                        f"vs {st.work_w}x{st.work_h}")

    # 2. The worker is not capturing: the colour goes through a section
    #    sized for the old window, so this must refuse.
    calls = []
    saved, saved_ch = _patch(calls)
    try:
        ok = pipeline.resize_window_live(_state(dda_mode=False), *FULLSCREEN)
    finally:
        _restore(saved, saved_ch)
    if ok or calls:
        failures.append(f"a resize was done live without worker capture "
                        f"({calls})")

    # 3. Either step refusing means a full rebuild, not half a pipeline.
    for label, kwargs in (("the probe", {"probe_raises": True}),
                          ("RNSZ", {"rack_raises": True})):
        calls = []
        saved, saved_ch = _patch(calls, **kwargs)
        try:
            st = _state()
            ok = pipeline.resize_window_live(st, *FULLSCREEN)
        finally:
            _restore(saved, saved_ch)
        if ok:
            failures.append(f"{label} failed and the resize still reported "
                            f"success")
        if st.display.resized_to is not None:
            failures.append(f"{label} failed but the overlay was resized "
                            f"anyway - the pipeline is half moved")
    # 4. A window too small to process is refused rather than sent on.
    calls = []
    saved, saved_ch = _patch(calls, probe=(32, 32))
    try:
        ok = pipeline.resize_window_live(_state(), 32, 32)
    finally:
        _restore(saved, saved_ch)
    if ok or "rnsz" in calls:
        failures.append("a 32x32 capture went through instead of being refused")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a resize is a reconfiguration, and it refuses rather than guesses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
