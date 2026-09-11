"""What the controls LOOK like, checked in pixels.

Every other UI test asks what a control does; these four ask what it shows,
because that is where direction A's first half lives and a behavioural test
would pass on the old look:

* a switch is a pill with a knob, and the knob is on the side that matches
  the state - the square checkbox before it could only be read by the word
  beside it;
* a parameter slider marks where the PROFILE puts that value, so the
  distance from Natural is visible rather than remembered;
* a slider that runs both ways marks zero, its neutral point;
* Quit is not painted in a tone one step from the accent that every number
  on the page uses - the destructive action reads as an action.

The drawing functions are called directly on a small surface: the whole
layout is not the subject here, one control is.
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import fonts  # noqa: E402
import overlay_ui  # noqa: E402
from i18n import STRINGS  # noqa: E402


def _menu():
    pygame.init()
    pygame.display.set_mode((64, 64))
    return overlay_ui.OverlayMenu(
        1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))


def _surface(w=360, h=80, bg=(0, 0, 0)):
    s = pygame.Surface((w, h))
    s.fill(bg)
    return s


def _count(surface, colour, rect=None, tol=12):
    """Pixels within tol of colour, inside rect (default: everywhere)."""
    r = rect or surface.get_rect()
    hits = 0
    for y in range(r.top, r.bottom):
        for x in range(r.left, r.right):
            px = surface.get_at((x, y))
            if all(abs(px[i] - colour[i]) <= tol for i in range(3)):
                hits += 1
    return hits


def main() -> int:
    failures = []
    m = _menu()
    s = STRINGS["en"]
    accent = overlay_ui._rgb(m.c["accent"])
    muted = overlay_ui._rgb(m.c["muted"])
    danger = overlay_ui._rgb(m.c["danger"])

    # 1. The switch: a pill whose knob changes sides with the state.
    rect = pygame.Rect(10, 10, 300, 40)
    for on in (True, False):
        surf = _surface()
        item = overlay_ui.Item("toggle", "nr", rect,
                               value=1.0 if on else 0.0,
                               extra={"label": "DLSS 5"})
        m._draw_toggle(surf, item, s)
        track = pygame.Rect(rect.x, rect.y, 40, rect.h)
        left = pygame.Rect(track.x, track.y, track.w // 2, track.h)
        right = pygame.Rect(track.centerx, track.y, track.w // 2, track.h)
        if on:
            if _count(surf, accent, track) < 100:
                failures.append("the switch is not filled when on")
            # the knob is the bg-coloured disc inside the accent track
            knob = overlay_ui._rgb(m.c["bg"])
            if _count(surf, knob, right) <= _count(surf, knob, left):
                failures.append("the knob is not on the right when on")
        else:
            if _count(surf, accent, track) > 20:
                failures.append("the switch is filled when off")
            if _count(surf, muted, left) <= _count(surf, muted, right):
                failures.append("the knob is not on the left when off")

    # 2. A parameter slider marks the profile's own value, away from the knob.
    surf = _surface(h=90)
    item = overlay_ui.Item("slider", "intensity",
                           pygame.Rect(10, 10, 300, 60),
                           lo=0.0, hi=2.5, value=2.0,
                           extra={"label": "Intensity", "value_text": "2.00",
                                  "mark": 1.0})
    m._draw_slider(surf, item, s)
    track_y = 10 + m._u(overlay_ui.LABEL_H) + m._u(10)
    band = pygame.Rect(10, track_y - m._u(4), 300, m._u(SLIDER_H := 6) + m._u(8))
    mark_x = 10 + int((1.0 - 0.0) / 2.5 * 300)
    near = pygame.Rect(mark_x - 4, band.y, 9, band.h)
    far = pygame.Rect(10, band.y, 40, band.h)
    if _count(surf, muted, near) < 5:
        failures.append("no tick where the profile puts the value")
    if _count(surf, muted, far) > 2:
        failures.append("a tick appeared where the profile does not put it")

    # 3. A slider that runs both ways marks zero without being told.
    surf = _surface(h=90)
    item = overlay_ui.Item("slider", "skin_structure",
                           pygame.Rect(10, 10, 300, 60),
                           lo=-1.0, hi=2.5, value=-1.0,
                           extra={"label": "Skin structure",
                                  "value_text": "-1.00"})
    m._draw_slider(surf, item, s)
    zero_x = 10 + int((0.0 - (-1.0)) / 3.5 * 300)
    near = pygame.Rect(zero_x - 4, band.y, 9, band.h)
    if _count(surf, muted, near) < 5:
        failures.append("the bipolar slider does not mark zero")

    # 4. Quit: the danger tone marks the edge, the label is plain text.
    surf = _surface(w=320, h=70)
    rect = pygame.Rect(10, 10, 300, 50)
    item = overlay_ui.Item("action", "exit", rect,
                           extra={"label": "Quit and unload", "danger": True,
                                  "hotkey": "Ctrl+Alt+Q", "note": "note"})
    m._draw_action(surf, item, s)
    inner = pygame.Rect(rect.x + 6, rect.y + 6, rect.w - 12, rect.h - 12)
    edge = pygame.Rect(rect.x, rect.y, rect.w, 4)
    if _count(surf, danger, inner) > 4:
        failures.append("the Quit label is still painted in the danger tone")
    if _count(surf, danger, edge) < 20:
        failures.append("the Quit button has no danger edge")

    pygame.quit()
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: switch, profile tick, zero tick and the Quit edge all draw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
