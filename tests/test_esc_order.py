"""Esc closes what is on top: the drop-down first, the panel after it.

The menu carried two Esc branches that contradicted each other. The earlier
one emitted ("button", "close") and shut the whole panel; the later one -
inside the open_choice branch, with a comment promising "Esc closes the
list" - could never run, because the earlier one matched first. So Esc shut
the menu out from under anyone stepping down the 12-language list with the
arrow keys, which is the very navigation that branch was added for.

The audit recorded this as a DECISION rather than a defect, and the decision
taken (12.09) is the one every toolkit on this desktop makes and the one the
dead comment already promised: one Esc closes the list, a second closes the
panel.

[audit ui-display]

Run:  runtime\\python.exe tests\\test_esc_order.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    try:
        from overlay_ui import OverlayMenu

        menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
        menu.set_state({"gpus": ["0: A", "1: B"], "gpu": "0: A"})
        menu.page = "settings"
        menu.visible = True
        menu.layout(3840, 2160)
        surf = pygame.Surface((3840, 2160))
        menu.draw(surf)

        row = next((i for i in menu.items
                    if i.kind == "choice" and i.key == "gpu"), None)
        if row is None:
            print("FAIL: no gpu row to open")
            return 1

        menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": row.rect.center, "button": 1}))
        if not menu.open_choice:
            failures.append("the drop-down did not open on click")

        # 1. The first Esc belongs to the list, and to nothing else.
        out = menu.handle_event(pygame.event.Event(
            pygame.KEYDOWN, {"key": pygame.K_ESCAPE, "unicode": ""}))
        print(f"    Esc with the list open: emitted {out}, "
              f"open_choice={menu.open_choice!r}")
        if menu.open_choice is not None:
            failures.append("the first Esc left the drop-down open")
        if ("button", "close") in out:
            failures.append("the first Esc closed the panel as well as the "
                            "list - it must close only what is on top")

        # 2. The second one closes the panel.
        out = menu.handle_event(pygame.event.Event(
            pygame.KEYDOWN, {"key": pygame.K_ESCAPE, "unicode": ""}))
        print(f"    Esc with no list open: emitted {out}")
        if out != [("button", "close")]:
            failures.append(f"the second Esc emitted {out}, not a panel close")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: Esc closes the drop-down first and the panel second")
    return 0


if __name__ == "__main__":
    sys.exit(main())
