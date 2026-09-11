"""One window's size, measured two ways, must not start a rebuild loop.

In window mode two numbers describe the same window and they are not the
same number on every Windows:

  * what the WORKER's capture returns (the WGCW ack) - this becomes
    st.width/height, the size the pipeline is built for;
  * what WE measure with DWMWA_EXTENDED_FRAME_BOUNDS - this is what
    follow_window looks at every frame to notice a resize.

On Windows 11 they agree. On Windows 10 WGC hands back the GetWindowRect
size, which includes the invisible resize border: a user's log (issue #30)
shows capture 1354x853 against frame 1340x846 - 14 pixels of border across
and 7 down. follow_window compared the two directly, never found them equal,
and rebuilt the pipeline every half second: the screen blinked once every
two seconds until window mode was switched off.

So the comparison is against the frame size measured when the pipeline was
built. This test drives follow_window with the numbers from that log.
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pipeline  # noqa: E402

CAPTURE = (1354, 853)   # what WGCW answered on Windows 10
FRAME = (1340, 846)     # what DWM reports for the same window


class _Menu:
    visible = False


class _Display:
    """Only what follow_window touches."""

    def __init__(self):
        self.menu = _Menu()
        self.moved_to = None

    def is_visible(self):
        return True

    def set_visible(self, on):
        pass

    def move_to(self, x, y):
        self.moved_to = (x, y)

    def raise_topmost(self):
        pass


def _state(frame_size):
    st = types.SimpleNamespace(
        window_hwnd=0x1707D8, worker_failed=False,
        width=CAPTURE[0], height=CAPTURE[1],
        follow_pos=(100, 100), follow_resize=None, follow_size=frame_size,
        frame_index=1, display=_Display())
    return st


def _drive(st, rect, seconds, rebuilds):
    """Run follow_window over a stretch of time with the window at rect."""
    real_rect, real_switch, real_time = (
        pipeline.window_frame_rect, pipeline.switch_window, pipeline.time)
    pipeline.window_frame_rect = lambda hwnd: rect

    def fake_switch(s, hwnd):
        # The real switch_window rebuilds and records the frame size it
        # rebuilt for - without that the loop under test would fire again
        # after every settle, which is the bug itself.
        rebuilds.append(hwnd)
        s.follow_size = rect[2:]

    pipeline.switch_window = fake_switch
    clock = {"t": 1000.0}
    pipeline.time = types.SimpleNamespace(monotonic=lambda: clock["t"])
    try:
        for _ in range(int(seconds / 0.1)):
            clock["t"] += 0.1
            st.frame_index += 1
            pipeline.follow_window(st)
    finally:
        pipeline.window_frame_rect = real_rect
        pipeline.switch_window = real_switch
        pipeline.time = real_time


def main() -> int:
    failures = []

    # 1. The Windows 10 case: capture and frame disagree by the invisible
    #    border, the window never moves. Three seconds - six times the
    #    half-second settle - must produce no rebuild at all.
    st = _state(FRAME)
    rebuilds = []
    _drive(st, (100, 100) + FRAME, 3.0, rebuilds)
    if rebuilds:
        failures.append(f"a still window rebuilt {len(rebuilds)} times "
                        f"(capture {CAPTURE}, frame {FRAME})")

    # 2. A real resize still rebuilds - once, after the size holds still.
    st = _state(FRAME)
    rebuilds = []
    _drive(st, (100, 100, 1200, 700), 3.0, rebuilds)
    if len(rebuilds) != 1:
        failures.append(f"a real resize produced {len(rebuilds)} rebuilds, "
                        f"expected 1")

    # 3. A resize that does not hold still is not acted on: the size changes
    #    on every frame (someone dragging the handle), so the settle timer
    #    keeps restarting.
    st = _state(FRAME)
    rebuilds = []
    real_rect, real_switch = pipeline.window_frame_rect, pipeline.switch_window
    pipeline.switch_window = lambda s, hwnd: rebuilds.append(hwnd)
    try:
        for i in range(30):
            pipeline.window_frame_rect = (
                lambda hwnd, i=i: (100, 100, 1200 + i * 4, 700 + i * 3))
            st.frame_index += 1
            pipeline.follow_window(st)
    finally:
        pipeline.window_frame_rect = real_rect
        pipeline.switch_window = real_switch
    if rebuilds:
        failures.append(f"a drag in progress rebuilt {len(rebuilds)} times")

    # 4. With no remembered size (a pipeline built before the window could be
    #    measured) the first frame adopts what it sees instead of rebuilding.
    st = _state(None)
    rebuilds = []
    _drive(st, (100, 100) + FRAME, 3.0, rebuilds)
    if st.follow_size != FRAME:
        failures.append(f"the frame size was not adopted: {st.follow_size}")
    if rebuilds:
        failures.append("adopting the size should not rebuild")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the capture size and the frame size disagree without a rebuild "
          "loop, and a real resize still rebuilds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
