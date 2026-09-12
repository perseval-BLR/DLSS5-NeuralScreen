"""Audit F2: follow_monitor must survive its first call in a fresh session.

startup.bring_up initialises follow_pos/follow_resize but NOT mon_resize,
while pipeline.follow_monitor reads st.mon_resize. The slot only comes
into existence after a poll that ran with size == current (that branch
assigns None). A resolution change that lands before that first poll -
or a change while worker_failed, when the guard returns early and never
initialises - makes the next call raise AttributeError, and an exception
there (no try/except around follow_monitor) exits the whole program via
main()'s top-level handler.

Expected: a state built the way bring_up builds it drives follow_monitor
without raising; exactly one rebuild happens; the pending size clears.
[audit F2/F9]

Run:  runtime\\python.exe tests\\test_follow_monitor_init.py
"""
import shutil
import sys
import tempfile
import types
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

import pipeline  # noqa: E402


def main() -> int:
    failures = []

    # The state as startup.bring_up builds it. This is a hand-made replica
    # and a replica drifts, so it is NOT what keeps bring_up honest -
    # test_state_contract does that, by walking main()'s __slots__ against
    # what bring_up actually assigns. What is checked here is the other
    # half: that follow_monitor survives its first call on that shape.
    #
    # The gap this was written for: mon_resize is assigned on the "nothing
    # changed" path and READ on the other one, so the first call on a
    # screen whose size already disagrees with the config raised
    # AttributeError under __slots__ (audit F2).
    st = types.SimpleNamespace(
        window_hwnd=None, worker_failed=False, running=True,
        width=2560, height=1440, mon_w=2560, mon_h=1440,
        monitor=0, work_scale=0.65, work_w=1664, work_h=936,
        follow_pos=None, follow_resize=None, mon_resize=None,
        capture=types.SimpleNamespace(devicename=r"\\.\DISPLAY1",
                                      resolution=(2560, 1440),
                                      close=lambda: None),
        display=types.SimpleNamespace(alert=lambda *a, **k: None),
    )

    calls = {"teardown": 0, "rebuild": 0, "refresh": 0}

    class _Cap:
        def __init__(self, monitor_idx=0):
            self.devicename = r"\\.\DISPLAY1"
            self.resolution = (3840, 2160)
            self.monitor_idx = monitor_idx
        def close(self):
            pass

    real = {
        "size": pipeline.monitor_size,
        "teardown": pipeline.teardown_pipeline,
        "rebuild": pipeline.rebuild_pipeline,
        "refresh": pipeline._refresh_dxcam_factory,
        "screen": pipeline.ScreenCapture,
    }
    try:
        # The FIRST call of the session already sees a changed size.
        pipeline.monitor_size = lambda name: (3840, 2160)
        pipeline.teardown_pipeline = lambda s: calls.__setitem__(
            "teardown", calls["teardown"] + 1)
        pipeline.rebuild_pipeline = lambda s, note: calls.__setitem__(
            "rebuild", calls["rebuild"] + 1)
        pipeline._refresh_dxcam_factory = lambda: calls.__setitem__(
            "refresh", calls["refresh"] + 1)
        pipeline.ScreenCapture = _Cap

        try:
            # Two ticks: the first records the size and returns; the second
            # (after the 0.5 s settle) rebuilds. Both must not raise.
            pipeline.follow_monitor(st)
            import time
            st.mon_resize = ((3840, 2160),
                             time.monotonic() - 1.0)  # pretend it settled
            pipeline.follow_monitor(st)
        except AttributeError as exc:
            failures.append(f"F2: follow_monitor raised AttributeError on a "
                            f"bring_up-shaped state: {exc}")
        except Exception as exc:
            failures.append(f"follow_monitor raised {type(exc).__name__}: {exc}")
    finally:
        pipeline.monitor_size = real["size"]
        pipeline.teardown_pipeline = real["teardown"]
        pipeline.rebuild_pipeline = real["rebuild"]
        pipeline._refresh_dxcam_factory = real["refresh"]
        pipeline.ScreenCapture = real["screen"]

    if not failures:
        if calls["rebuild"] != 1:
            failures.append(f"expected exactly one rebuild, got {calls}")
        if calls["teardown"] != 1:
            failures.append(f"expected exactly one teardown, got {calls}")
        if st.mon_resize is not None:
            failures.append(f"the pending size must clear, got {st.mon_resize!r}")
        if (st.width, st.height) != (3840, 2160):
            failures.append(f"the new resolution must be adopted, "
                            f"got {st.width}x{st.height}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: follow_monitor survives its first call on a bring_up-shaped state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
