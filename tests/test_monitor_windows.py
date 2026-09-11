"""The chosen monitor moves BOTH output windows, with the real worker running.

test_monitor_origin checks the hand-offs one at a time and moves the pygame
layer on its own. This one runs the whole program and looks at where the
windows actually are - the overlay AND the worker's picture window - because
that pair is what a user on a second monitor sees, or does not (issues #28,
#33, #35: the program ran, logged frames, answered hotkeys, and painted on
the primary display).

There is one display on the machine this is developed on, which is why the
bug shipped twice. So the program is started with capture.monitor_origin
lying about where that display is: the code takes the same branch it takes
on a second monitor, only the number differs. Both windows must land on it.

The test process makes itself per-monitor DPI aware before measuring -
without that Windows reports scaled coordinates and a window at (220, 160)
comes back as (176, 128) on a 125% display.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import autocheck  # noqa: E402

ORIGIN = (220, 160)
SHIM = '''import sys
sys.path.insert(0, r"{root}")
import capture
capture.monitor_origin = lambda name: {origin}
import main
raise SystemExit(main.main())
'''


def _dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _windows(title: str) -> list:
    """(title, left, top, w, h) for every visible window with exactly that title."""
    user32 = ctypes.windll.user32
    out: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        # EXACT title: a browser tab or a folder named after the program
        # matches a substring search, and both were caught the first time
        # this ran. The overlay and the worker's window are both titled
        # exactly "NeuralScreen".
        if buf.value == title:
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((buf.value, r.left, r.top,
                        r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    return out


def main() -> int:
    _dpi_aware()
    failures = []

    if autocheck.running_instances():
        print("FAIL: a copy of the program is already running - close it first")
        return 1

    shim = Path(tempfile.mkdtemp(prefix="ns-origin-")) / "run_at_origin.py"
    shim.write_text(SHIM.format(root=str(BASE), origin=repr(ORIGIN)),
                    encoding="utf-8")
    offset = autocheck.log_offset()
    proc = subprocess.Popen([str(BASE / "runtime" / "pythonw.exe"), str(shim)],
                            cwd=str(BASE))
    try:
        if autocheck.wait_for(offset, "NR ON | FPS", 60.0) is None:
            print("FAIL: the pipeline never came up")
            return 1
        time.sleep(2.0)   # the present window is created after the first frame

        wins = _windows("NeuralScreen")
        # The 1x1 taskbar helper is not an output window.
        outputs = [w for w in wins if w[3] > 16 and w[4] > 16]
        for title, x, y, w, h in wins:
            print(f"    {title[:28]:30} at ({x}, {y}) {w}x{h}")
        if len(outputs) < 2:
            failures.append(f"expected the overlay and the worker's window, "
                            f"found {len(outputs)}")
        for title, x, y, _w, _h in outputs:
            if (x, y) != ORIGIN:
                failures.append(f"{title!r} sits at ({x}, {y}), not at {ORIGIN} "
                                f"- it would land on the primary monitor")

        tail = autocheck.log_since(offset)
        if "selected by NS_OUTPUT" not in tail:
            failures.append("the worker did not pick its output by device name")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: both output windows follow the chosen monitor to {ORIGIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
