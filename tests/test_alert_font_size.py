"""Audit: set_lang must keep the alert font's init sizing.

__init__ builds the alert font at max(10, round(ALERT_FONT_SIZE *
ui_scale)); set_lang rebuilds it at ALERT_FONT_SIZE flat - on a 4K
screen (ui_scale 1.2) a language switch makes every alert visibly
smaller and it stays that way. The CJK intent (rebuild for glyph
coverage) works; the size regresses.

Expected: after set_lang the alert font keeps the __init__ height.
[audit ui-display]

Run:  runtime\\python.exe tests\\test_alert_font_size.py
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
    disp = None
    try:
        import display as display_mod

        # 2160 px tall -> ui_scale 1.2 on this project's formula.
        disp = display_mod.Display(3840, 2160, click_through=False)
        if disp.ui_scale <= 1.0:
            print(f"SKIP: ui_scale is {disp.ui_scale} - needs a tall screen")
            return 0
        init_h = disp._alert_font.get_height()
        if disp._lang == "en":
            disp.set_lang("ru")
        else:
            disp.set_lang("en")
        after_h = disp._alert_font.get_height()
        if after_h != init_h:
            failures.append(
                f"set_lang rebuilt the alert font at {after_h} px; __init__ "
                f"built {init_h} px (ui_scale {disp.ui_scale}) - the size "
                f"regresses on a language switch")
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
    print("OK: the alert font keeps its size across a language switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
