"""The monitor picker says that the picture is not a window you drag.

Two reporters arrived at the same question by different routes: one tried to
drag the picture onto his second screen, the other tried Win+Shift+Left
(#35), and a third said the program "cannot be moved back to the main
monitor, yet its position is fixed" (#33). Nothing happens, because the
output is not a window - it is a layer covering the whole chosen monitor,
and the picker in Settings is the control they were looking for.

So the picker carries the sentence. Two rules that are easy to break later:

* it is only there with more than one monitor - on a single display it is
  noise about a choice that does not exist;
* it is a caption, not a hit target. A hint under a choice row makes the row
  taller, and a click on the explanation must not open the drop-down (which
  test_choice_hint_hit pins for the control in general).

Run:  runtime\\python.exe tests\\test_monitor_hint.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402


def _menu(monitors):
    import overlay_ui
    menu = overlay_ui.OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
    menu.set_state({"monitors": monitors, "monitor": "0", "lang": "en"})
    menu.page = "settings"
    menu.visible = True
    menu.layout(3840, 2160)
    menu.draw(pygame.Surface((3840, 2160)))
    return next((i for i in menu.items
                 if i.kind == "choice" and i.key == "monitor"), None)


def main() -> int:
    failures = []
    pygame.init()
    try:
        from i18n import STRINGS

        two = _menu(["0: 3840x2160 (\\\\.\\DISPLAY1)", "1: 1920x1080 (\\\\.\\DISPLAY2)"])
        if two is None:
            failures.append("no monitor picker with two monitors")
        else:
            hint = str(two.extra.get("hint") or "")
            print(f"    two monitors -> hint {hint[:48]!r}")
            if not hint.strip():
                failures.append("no hint on the monitor picker with two "
                                "monitors - the question people actually "
                                "asked goes unanswered")
            # A hint makes the row taller than the field; the field is what
            # takes clicks.
            strip = two.extra.get("strip")
            if strip is not None and strip.h >= two.rect.h:
                failures.append("the choice field covers the whole row, hint "
                                "included - a click on the explanation would "
                                "open the list")

        one = _menu(["0: 3840x2160 (\\\\.\\DISPLAY1)"])
        if one is None:
            failures.append("no monitor picker with one monitor")
        elif str(one.extra.get("hint") or "").strip():
            failures.append("the hint is shown with a single monitor, where "
                            "it explains a choice that does not exist")

        for lang, table in STRINGS.items():
            if not table.get("monitor_hint"):
                failures.append(f"no monitor_hint string for {lang!r}")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the picker explains itself on two monitors and stays quiet on one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
