"""Audit: a hint under a choice row must never be a hit target.

d8d063d shrank the layout-time strip to CTRL_H (the control only), but
_draw_choice still recomputes the strip as rect.h - label_h (the WHOLE
row, hint included) and writes it back into item.extra["strip"]. Since
draw() overwrites it on every frame, after the first frame the hit
guard's strip.bottom equals the row bottom and a click on the hint text
opens the dropdown - the invariant the code comment promises ("a hint
... is not a hit target").

The existing test_silent_spots step 4 re-reads the strip AFTER draw(),
so it compares the overwritten strip against itself and cannot see it.

Expected: after draw() the strip is still the control height, and a
click in the hint band is not the row. [audit ui-display]

Run:  runtime\\python.exe tests\\test_choice_hint_hit.py
"""
import os
import sys
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
    try:
        from overlay_ui import CTRL_H, OverlayMenu

        menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
        menu.set_state({"gpus": ["0: RTX 5070 Ti", "1: RTX 2060 Super"],
                        "gpu": "1: RTX 2060 Super"})
        menu.page = "settings"
        menu.visible = True
        surf = pygame.Surface((3840, 2160))

        # 1. Layout-time strip: the control height, not the row.
        menu.layout(3840, 2160)
        row = next((i for i in menu.items
                    if i.kind == "choice" and i.key == "gpu"), None)
        if row is None:
            failures.append("no GPU choice row on the settings page")
        else:
            strip = row.extra.get("strip")
            if strip is None:
                failures.append("the row has no strip after layout")
            else:
                if strip.h != menu._u(CTRL_H):
                    failures.append(
                        f"the layout strip is {strip.h} px, want CTRL_H "
                        f"({menu._u(CTRL_H)} px)")

        # 2. The drawer must not grow it back over the hint band.
        menu.draw(surf)
        row2 = next((i for i in menu.items
                     if i.kind == "choice" and i.key == "gpu"), None)
        if row2 is not None:
            strip2 = row2.extra.get("strip")
            if strip2 is not None and strip2.h != menu._u(CTRL_H):
                failures.append(
                    f"after draw() the strip is {strip2.h} px (the row is "
                    f"{row2.rect.h} px) - the drawer overwrote it with the "
                    f"full row, so the hint band is a hit target again")

            # 3. The behaviour the user sees: a click in the hint band.
            if strip2 is not None:
                band_y = min(row2.rect.bottom - 2, strip2.bottom + 6)
                hit = menu.hit((row2.rect.centerx, band_y))
                if hit is row2:
                    failures.append(
                        f"a click at y={band_y} (the hint band) hits the "
                        f"choice row - it would open the dropdown")
                # ...and the control itself still IS a hit target.
                hit_ctrl = menu.hit((row2.rect.centerx, strip2.centery))
                if hit_ctrl is not row2:
                    failures.append("the control band must stay clickable")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the choice hint band is not a hit target, before and after draw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
