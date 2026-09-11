"""The chosen monitor's position reaches both output windows (issues #28, #33).

The overlay (pygame layer) and the worker's picture window were both created
at (0,0) of the virtual desktop - which is the PRIMARY monitor - while the
capture could run on any output. On a second monitor the result was: the
desktop visibly freezes (capture runs), and the picture lands on the primary
screen, or nowhere the user is looking.

Two hand-offs are checked here, both driven without launching the worker:

* capture.monitor_origin() finds the chosen monitor's corner on the virtual
  desktop (monkeypatched EnumDisplayMonitors: a primary at (0,0) and a
  second monitor at x=1920 and one at a NEGATIVE origin - left/below
  arrangements are real);
* startup._apply_monitor_env() publishes the identity: NS_OUTPUT carries the
  DXGI device name, NS_WINDOW_POS the origin - and BOTH are cleared when the
  name cannot be resolved (the old defaults: output 0, position (0,0));
* Display.set_origin() moves the real window, and _move_to_origin() with
  (0,0) does not move it (the pre-multi-monitor behaviour stays).

Run:  runtime\\python.exe tests\\test_monitor_origin.py
"""
import ctypes
import os
import sys
import types
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, capture.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

# Physical pixels, before pygame loads: SDL freezes DPI awareness at import.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import capture  # noqa: E402
import startup  # noqa: E402


@contextmanager
def _fake_monitors(entries):
    """Monkeypatch EnumDisplayMonitors + GetMonitorInfoW.

    entries: list of (hmon, devicename, x, y, w, h). The callback receives
    the MONITORINFOEXW-style rect and GetMonitorInfoW fills szDevice.
    """
    real_enum = ctypes.windll.user32.EnumDisplayMonitors
    real_getinfo = ctypes.windll.user32.GetMonitorInfoW

    def fake_enum(_hdc, _rect, proc, _lparam):
        for hmon, _dev, x, y, w, h in entries:
            r = wintypes.RECT(x, y, x + w, y + h)
            proc(hmon, None, ctypes.byref(r), 0)
        return True

    def fake_getinfo(hmon, lpmi):
        for _hmon, dev, _x, _y, _w, _h in entries:
            if _hmon == hmon:
                info = ctypes.cast(
                    lpmi, ctypes.POINTER(capture._MONITORINFOEXW)).contents
                info.szDevice = dev
                return True
        return False

    ctypes.windll.user32.EnumDisplayMonitors = fake_enum
    ctypes.windll.user32.GetMonitorInfoW = fake_getinfo
    try:
        yield
    finally:
        ctypes.windll.user32.EnumDisplayMonitors = real_enum
        ctypes.windll.user32.GetMonitorInfoW = real_getinfo


class _Capture:
    def __init__(self, devicename):
        self.devicename = devicename
        self.resolution = (1920, 1080)


def main() -> int:
    failures = []
    before = {k: os.environ.get(k) for k in ("NS_OUTPUT", "NS_WINDOW_POS")}

    # 1. The origin lookup: primary, to the right, and a NEGATIVE one.
    layout = [
        (1, r"\\.\DISPLAY1", 0, 0, 3840, 2160),
        (2, r"\\.\DISPLAY2", 3840, 0, 1920, 1080),
        (3, r"\\.\DISPLAY3", -1920, 2160, 1920, 1080),
    ]
    try:
        with _fake_monitors(layout):
            got = {d: capture.monitor_origin(d)
                   for _h, d, _x, _y, _w, _h in layout}
        want = {r"\\.\DISPLAY1": (0, 0), r"\\.\DISPLAY2": (3840, 0),
                r"\\.\DISPLAY3": (-1920, 2160)}
        for name, want_origin in want.items():
            if got.get(name) != want_origin:
                failures.append(f"monitor_origin({name!r}) = {got.get(name)!r}, "
                                f"want {want_origin}")
        with _fake_monitors(layout):
            if capture.monitor_origin(r"\\.\DISPLAY9") is not None:
                failures.append("an unknown monitor must answer None")
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 2. The hand-off: identity + origin into the environment.
    try:
        with _fake_monitors(layout):
            origin = startup._apply_monitor_env(_Capture(r"\\.\DISPLAY2"))
    finally:
        pass
    if origin != (3840, 0):
        failures.append(f"_apply_monitor_env origin = {origin!r}, want (3840, 0)")
    if os.environ.get("NS_OUTPUT") != r"\\.\DISPLAY2":
        failures.append(f"NS_OUTPUT is {os.environ.get('NS_OUTPUT')!r}")
    if os.environ.get("NS_WINDOW_POS") != "3840,0":
        failures.append(f"NS_WINDOW_POS is {os.environ.get('NS_WINDOW_POS')!r}")

    # An unresolvable name: both are cleared and the caller gets (0, 0).
    with _fake_monitors(layout):
        origin = startup._apply_monitor_env(_Capture(r"\\.\DISPLAY9"))
    if origin != (0, 0):
        failures.append(f"an unknown monitor must fall back to (0,0), got {origin!r}")
    if "NS_OUTPUT" in os.environ or "NS_WINDOW_POS" in os.environ:
        failures.append("an unknown monitor must clear NS_OUTPUT/NS_WINDOW_POS")

    # 3. Display.set_origin moves the real window; (0,0) keeps it put.
    import display as display_mod

    disp = None
    try:
        disp = display_mod.Display(640, 360, click_through=False)
        hwnd = ctypes.windll.user32.FindWindowW(None, "NeuralScreen")
        if not hwnd:
            failures.append("no overlay window found for the origin test")
        else:
            # Show it (set_origin does not; the window starts hidden).
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
            # NOSIZE | NOACTIVATE - but NOT NOMOVE: this one must move.
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 100, 100, 0, 0,
                                              0x0001 | 0x0010)

            def pos():
                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                return (r.left, r.top)

            # (0,0) -> SWP_NOMOVE: the window does not jump to the corner.
            disp.set_origin(0, 0)
            if pos() != (100, 100):
                failures.append(f"origin (0,0) moved the window: {pos()}")
            # A nonzero origin -> the real move.
            disp.set_origin(-1920, 2160)
            if pos() != (-1920, 2160):
                failures.append(f"set_origin did not move the window: {pos()}")
            disp.set_origin(0, 0)
    except Exception as exc:
        failures.append(f"the display half threw: {exc!r}")
    finally:
        try:
            if disp is not None:
                disp.close()
        except Exception:
            pass

    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the chosen monitor's origin reaches the overlay and the worker env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
