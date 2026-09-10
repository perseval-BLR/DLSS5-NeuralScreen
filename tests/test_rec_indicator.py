"""The recording indicator outside the menu: a red dot + timer.

The menu shows the REC cell, but with the menu closed there was no sign
that a recording is running (user 5080 request). The indicator lives in
the top-right corner of the HUD layer. It is hidden from the recorded
frame itself: draw_capture_overlay bakes only the menu.

Checked: the indicator draws only while recording and only when the
config flag is on; the toggle persists through the menu-layout payload;
the indicator is NOT baked into a captured frame.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pygame  # noqa: E402
import display as display_mod  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    try:
        disp = display_mod.Display(640, 360)
        disp.set_hud({"recording": False, "rec_seconds": 0.0,
                      "rec_indicator": True})
        disp.draw_overlay(min_interval=0.0)
        # The top-right corner: nothing red while not recording. The
        # background is the chroma key (255,0,255) - the blue channel
        # separates it from the red indicator.
        px = disp.screen.get_at((disp.width - 20, 20))
        if px.r > 200 and px.g < 100 and px.b < 100:
            failures.append("the indicator shows while not recording")

        # Recording on: the dot (or the REC text) must be visible.
        disp.set_hud({"recording": True, "rec_seconds": 65.0,
                      "rec_indicator": True})
        disp.draw_overlay(min_interval=0.0)
        found = False
        for y in range(0, 60):
            for x in range(disp.width - 200, disp.width):
                p = disp.screen.get_at((x, y))
                if p.r > 200 and p.g < 100 and p.b < 100:
                    found = True
                    break
            if found:
                break
        if not found:
            failures.append("no red indicator while recording")

        # The flag off: nothing red even while recording.
        disp.set_hud({"recording": True, "rec_seconds": 65.0,
                      "rec_indicator": False})
        disp.draw_overlay(min_interval=0.0)
        found = False
        for y in range(0, 60):
            for x in range(disp.width - 200, disp.width):
                p = disp.screen.get_at((x, y))
                if p.r > 200 and p.g < 100 and p.b < 100:
                    found = True
                    break
            if found:
                break
        if found:
            failures.append("the indicator shows with the flag off")

        # The captured frame must NOT contain the indicator: the bake
        # path draws only the menu.
        disp.set_hud({"recording": True, "rec_seconds": 65.0,
                      "rec_indicator": True})
        disp.draw_overlay(min_interval=0.0)
        cap = pygame.Surface((640, 360))
        cap.fill((10, 20, 30))
        disp.draw_capture_overlay(cap)
        found = False
        for y in range(0, 60):
            for x in range(disp.width - 200, disp.width):
                p = cap.get_at((x, y))
                if p.r > 200 and p.g < 100 and p.b < 100:
                    found = True
                    break
            if found:
                break
        if found:
            failures.append("the indicator leaked into the captured frame")
    finally:
        pygame.quit()

    # The toggle persists through the menu-layout payload.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "ns_main", str(Path(BASE) / "main.py"))
    ns_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ns_main)

    class _Menu:
        user_scale = 1.0
        user_height = None
        state = {"theme": "dark"}
        offset = [10, 20]

    cfg = {"profile": "Natural", "rec_indicator": False}
    params = {"intensity": 1.0, "local_tone": 1.0,
              "local_structure": 1.0, "skin_structure": -1.0}
    payload = ns_main._menu_layout_payload(
        cfg, params, 0, "en", 0.65, 0.5, True, False, _Menu())
    if payload.get("rec_indicator") is not False:
        failures.append(f"the toggle did not persist: {payload.get('rec_indicator')!r}")
    cfg2 = {"profile": "Natural"}
    payload2 = ns_main._menu_layout_payload(
        cfg2, params, 0, "en", 0.65, 0.5, True, False, _Menu())
    if payload2.get("rec_indicator") is not True:
        failures.append(f"the default is not on: {payload2.get('rec_indicator')!r}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the recording indicator draws only while recording and never leaks into the file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
