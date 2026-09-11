"""Win32 window helpers: geometry, the window under the cursor, the list.

Moved out of main.py unchanged. Everything here is pure Windows plumbing -
it takes a window handle and asks the system about it - which is why it was
the first thing that did not belong in a 2200-line main().

The rules these encode are hard-won and the comments carry them:
  * a window's visible bounds are the DWM frame bounds, not GetWindowRect;
  * the desktop (Progman/WorkerW) is not a window you can capture;
  * a window that does not show in the taskbar (owned, tool, DWM-cloaked)
    is a background helper, not something a user means to pick.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


DWMWA_EXTENDED_FRAME_BOUNDS = 9


def window_frame_rect(hwnd: int):
    """The window's visible bounds as the compositor sees them: (x, y, w, h).

    Not GetWindowRect: that one includes the invisible resize border - several
    pixels of nothing on each side - so an overlay placed by it sits visibly
    off the window. The DWM answer is also the rectangle Windows Graphics
    Capture hands over, which is what the picture has to line up with.
    Returns None when the window is gone.
    """
    r = _RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(int(hwnd)), ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(hwnd)),
                                                  ctypes.byref(r)):
            return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def foreign_foreground() -> int:
    """The focused window, unless it is one of ours. 0 when there is none.

    "Ours" matters because the hotkey may be pressed while the menu has the
    focus, and capturing our own overlay is exactly the loop this mode exists
    to avoid.
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return 0
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return 0
    if _is_desktop_window(hwnd):
        return 0
    return int(hwnd)


def _is_desktop_window(hwnd: int) -> bool:
    """Progman / WorkerW - the desktop itself, not a window to capture.

    Clicking the desktop before Num5 made the capture target the wallpaper
    (Progman): the overlay then showed the desktop with every real window
    transparent above it. The desktop is not a window - exclude it (user
    report: "only the desktop is shown, the windows are transparent").
    """
    user32 = ctypes.windll.user32
    cls = ctypes.create_unicode_buffer(64)
    if not user32.GetClassNameW(hwnd, cls, 64):
        return False
    return cls.value in ("Progman", "WorkerW")


def window_under_cursor() -> int:
    """The topmost real window under the mouse, 0 when none/ours/desktop.

    The Num5 alternative to the focused window: point at the window you want
    and press. Works on the desktop too - the focused window there is
    Progman, which is not capturable.
    """
    user32 = ctypes.windll.user32
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return 0
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return 0
    # WindowFromPoint can return a child (a button inside Chrome); walk up
    # to the top-level owner.
    top = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    if not top:
        top = hwnd
    if not user32.IsWindowVisible(top) or user32.IsIconic(top):
        return 0
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(top, ctypes.byref(pid))
    if pid.value == os.getpid():
        return 0
    if _is_desktop_window(top):
        return 0
    return int(top)


def _is_taskbar_window(hwnd: int) -> bool:
    """A window that shows in the taskbar: top-level, not owned, not a tool
    window.

    Background processes keep hidden helper windows (owned or tool
    windows) that EnumWindows still reports - they are not real desktop
    apps and must not appear in the window list (user report: "сторонние
    процессы попадают в список").
    """
    user32 = ctypes.windll.user32
    if user32.GetWindow(hwnd, 4):  # GW_OWNER
        return False
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x00000080:  # WS_EX_TOOLWINDOW
        return False
    cloaked = ctypes.c_int(0)
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
        return False  # DWM_CLOAKED: hidden from the taskbar (TextInputHost,
        # the Settings helper windows, ...)
    return True


def list_capturable_windows() -> list[tuple[int, str]]:
    """Visible top-level windows that can be captured: (hwnd, title).

    The window list for the menu. Excludes our own windows, the desktop
    (Progman/WorkerW), windows without a title and windows that do not
    show in the taskbar (owned/tool windows of background processes).
    """
    user32 = ctypes.windll.user32
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        if _is_desktop_window(hwnd) or not _is_taskbar_window(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title:
            out.append((int(hwnd), title))
        return True

    user32.EnumWindows(cb, 0)
    return out
