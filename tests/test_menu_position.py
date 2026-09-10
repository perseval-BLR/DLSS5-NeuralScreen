"""The menu position is fixed: the saved offset is honoured, never reset.

The old behaviour re-placed the panel into the bottom-right corner on every
menu open in one-window mode (place_bottom_right), and the offset left over
from a small captured window landed the menu half off the desktop after
switching back to fullscreen. Now the panel always opens where the user left
it, and layout() clamps it fully inside the screen (user rule 10.09: fixed
position until the user drags it).

Checked: a saved offset survives layout() unchanged, a stale offset is
clamped to the screen edges (never half off), and the panel is fully
visible on both a 4K and a 1080p screen.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests/ (autocheck)
import overlay_ui  # noqa: E402

STATE = {
    "nr": True,
    "profile": "Natural",
    "profiles": ["Faithful", "Natural", "Strong / Cinematic"],
    "params": {"intensity": 1.0, "local_tone": 1.0,
               "local_structure": 1.0, "skin_structure": -1.0},
    "split": 0.0, "open_on_start": True,
    "gpu_text": "RTX 5070 Ti · Blackwell", "gpu_ok": True,
}


def font_loader(size):
    try:
        return pygame.font.SysFont("consolas", size)
    except Exception:
        return pygame.font.Font(None, size)


def build():
    menu = overlay_ui.OverlayMenu(1.0, font_loader)
    menu.set_state(dict(STATE))
    menu.set_stats({"fps": 55.0, "status": "NR ON", "resolution": "3840x2160",
                    "frames": 100})
    menu.visible = True
    return menu


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    # 1. A saved offset is honoured: the panel lands where the user left it.
    menu = build()
    menu.offset = [300, 200]          # relative to the screen centre
    menu.layout(3840, 2160)
    r = menu.panel_rect
    expect_x = (3840 - r.w) // 2 + 300
    expect_y = (2160 - r.h) // 2 + 200
    if (r.x, r.y) != (expect_x, expect_y):
        failures.append(f"saved offset not honoured: panel at {(r.x, r.y)}, "
                        f"expected {(expect_x, expect_y)}")

    # 2. The panel never leaves the screen: a stale offset (from a smaller
    #    window or a resolution change) is clamped to the edges.
    menu.offset = [10000, 10000]      # way off the bottom-right
    menu.layout(3840, 2160)
    r = menu.panel_rect
    if r.right > 3840 or r.bottom > 2160 or r.x < 0 or r.y < 0:
        failures.append(f"panel escaped the screen: {r} on 3840x2160")
    menu.offset = [-10000, -10000]    # way off the top-left
    menu.layout(3840, 2160)
    r = menu.panel_rect
    if r.right > 3840 or r.bottom > 2160 or r.x < 0 or r.y < 0:
        failures.append(f"panel escaped the screen: {r} on 3840x2160")

    # 3. The same on a 1080p screen (the laptop case from the issues).
    menu.offset = [10000, 10000]
    menu.layout(1920, 1080)
    r = menu.panel_rect
    if r.right > 1920 or r.bottom > 1080 or r.x < 0 or r.y < 0:
        failures.append(f"panel escaped the screen: {r} on 1920x1080")

    # 4. The offset itself is not rewritten by the clamp: the user's saved
    #    position survives a layout pass (only the rendered rect is clamped).
    menu.offset = [10000, 10000]
    menu.layout(1920, 1080)
    if menu.offset != [10000, 10000]:
        failures.append(f"the clamp rewrote the saved offset: {menu.offset}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the menu position is fixed - saved offset honoured, "
          "clamped to the screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
