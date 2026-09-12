"""Audit: the window-highlight outline must honour the monitor origin.

_draw_window_highlight takes GetWindowRect (virtual-desktop coordinates)
and draws it straight into the pygame layer. The layer's (0,0) is the
monitor origin, so on any monitor that is not the primary (origin e.g.
(3840, 0)) the amber outline is displaced by exactly the origin - drawn
off the visible surface, or on the wrong monitor. Correct only at (0,0),
which is why it has not been reported.

Expected: the drawn rect is the screen rect minus the layer origin.
[audit ui-display]

Run:  runtime\\python.exe tests\\test_highlight_origin.py
"""
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    disp = None
    try:
        import display as display_mod

        disp = display_mod.Display(1920, 1080, click_through=False)
        # Pretend the pipeline runs on a monitor at (3840, 0).
        disp.set_origin(3840, 0)

        # A target window at screen (4000, 100)-(4900, 700).
        real_getrect = ctypes.windll.user32.GetWindowRect
        target = [4000, 100, 4900, 700]

        def fake_getrect(hwnd, prect):
            r = ctypes.cast(prect, ctypes.POINTER(wintypes.RECT)).contents
            r.left, r.top, r.right, r.bottom = target
            return 1

        ctypes.windll.user32.GetWindowRect = fake_getrect

        # Record what rect the highlight would draw, via a recording surface.
        drawn = []
        real_rect = pygame.draw.rect

        def spy_rect(surface, color, rect, *a, **k):
            if tuple(color[:3]) == (0xFF, 0xBF, 0x00):
                drawn.append(tuple(rect))
            return real_rect(surface, color, rect, *a, **k)

        pygame.draw.rect = spy_rect
        try:
            disp.menu.hover_window = 0x1234
            disp._draw_window_highlight()
        finally:
            pygame.draw.rect = real_rect
            ctypes.windll.user32.GetWindowRect = real_getrect

        if not drawn:
            failures.append("the highlight drew nothing (spy missed)")
        else:
            x, y, w, h = drawn[0]
            want = (4000 - 3840, 100)  # screen coords minus the origin
            if (x, y) != want:
                failures.append(
                    f"the outline drew at layer {(x, y)}, want {want} - the "
                    f"origin (3840, 0) was not subtracted from the "
                    f"virtual-desktop rect")
    finally:
        try:
            if disp is not None:
                disp.close()
        except Exception:
            pass
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the window highlight honours the monitor origin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
