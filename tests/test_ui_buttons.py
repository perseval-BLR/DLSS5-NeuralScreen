"""Every interactive control on every page fires the right command.

The menu is the only surface the user touches, so a control that silently
does nothing is a broken release. Walks every page (main, settings,
windows), clicks every item, and checks the emitted action against the
expected command table:

  * header icons: help -> github, gear -> settings page, min -> close,
    close (settings) -> back to main;
  * main page: the DLSS 5 toggle -> ("nr",), profile choice -> ("profile",),
    sliders -> ("param", ...) / ("split", ...) / ("nr_res", ...), the
    Actions buttons -> windows page / window_mode / screenshot / record;
  * settings page: language/theme segments -> ("lang",) / ("theme",),
    hotkey rows -> capture, back -> main;
  * windows page: a window row -> ("window",), back -> main;
  * the footer exit -> ("button", "exit").
"""
import sys
from pathlib import Path

import pygame

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
import overlay_ui  # noqa: E402

STATE = {
    "nr": True,
    "profile": "Natural",
    "profiles": ["Natural", "Strong / Cinematic", "Vivid"],
    "params": {"intensity": 1.0, "local_tone": 1.0,
               "local_structure": 1.0, "skin_structure": -1.0},
    "split": 0.0,
    "work_scale": 0.65, "work_scale_cap": 1.0, "nr_small": False,
    "screen_size": "3840x2160",
    "theme": "light", "lang": "en",
    "gpu_text": "RTX 5070 Ti · Blackwell", "gpu_ok": True,
    "window_mode": False,
    "windows": ["1A2B3C: Notepad", "4D5E6F: Chrome - YouTube"],
    "window_current": "1A2B3C: Notepad",
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


def paint(menu):
    """Draw once so the geometry extras (slider tracks, segment cells) exist."""
    surf = pygame.Surface((3840, 2160))
    menu.draw(surf)


def click(menu, item):
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": item.rect.center, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": item.rect.center, "button": 1}))
    return out


def find(menu, kind, key):
    return next((i for i in menu.items
                 if i.kind == kind and i.key == key), None)


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    menu = build()
    menu.layout(3840, 2160)
    paint(menu)

    # 1. Header icons on the main page.
    for key, want in (("help", [("button", "github")]),
                      ("min", [("button", "close")])):
        icon = find(menu, "icon", key)
        if icon is None:
            failures.append(f"no {key} icon on the main page")
            continue
        out = click(menu, icon)
        if out != want:
            failures.append(f"icon {key}: expected {want}, got {out}")
    gear = find(menu, "icon", "gear")
    if gear is None:
        failures.append("no gear icon on the main page")
    else:
        out = click(menu, gear)
        if menu.page != "settings" or out != [("capture", None)]:
            failures.append(f"gear: expected settings page, got {out}")

    # 2. The settings page: the lang drop-down, the theme segment, hotkey
    #    rows, back icon, back button.
    menu.layout(3840, 2160)
    paint(menu)
    lang_choice = find(menu, "choice", "lang")
    if lang_choice is None:
        failures.append("no lang drop-down on the settings page")
    else:
        out = click(menu, lang_choice)
        if out != []:
            failures.append(f"lang: expected open (no action), got {out}")
        menu.layout(3840, 2160)
        paint(menu)
        paint(menu)
        opts = [i for i in menu.options if i.kind == "option"]
        if len(opts) < 2:
            failures.append("the lang list did not open")
        else:
            out = click(menu, opts[1])
            if out != [("lang", "ru")]:
                failures.append(f"lang pick: expected ru, got {out}")
        # The stale option rows are cleared only by the next layout - rebuild
        # before clicking anything below the list.
        menu.layout(3840, 2160)
        paint(menu)
    for key, want in (("theme", [("theme", "dark")]),):
        seg = find(menu, "segmented", key)
        if seg is None:
            failures.append(f"no {key} segment on the settings page")
            continue
        cells = seg.extra.get("cells") or []
        if not cells:
            failures.append(f"the {key} segment has no cells")
            continue
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": cells[1].center, "button": 1}))
        if out != want:
            failures.append(f"segment {key}: expected {want}, got {out}")
    for hk_cmd in ("toggle", "settings", "screenshot_menu", "record",
                   "window_mode", "scale_up", "scale_down", "quit"):
        hk = find(menu, "hotkey", hk_cmd)
        if hk is None:
            failures.append(f"no hotkey row {hk_cmd} on the settings page")
            continue
        out = click(menu, hk)
        if out != [("capture", hk_cmd)]:
            failures.append(f"hotkey row {hk_cmd}: expected capture, got {out}")
    close_icon = find(menu, "icon", "close")
    if close_icon is None:
        failures.append("no close icon on the settings page")
    else:
        out = click(menu, close_icon)
        if menu.page != "main" or out != [("capture", None)]:
            failures.append(f"close icon: expected back to main, got {out}")

    # 3. The main page: toggle, profile, sliders, Actions buttons.
    menu.layout(3840, 2160)
    paint(menu)
    toggle = find(menu, "toggle", "nr")
    if toggle is None:
        failures.append("no DLSS 5 toggle on the main page")
    else:
        out = click(menu, toggle)
        if out != [("nr",)]:
            failures.append(f"toggle: expected [('nr',)], got {out}")
    prof = find(menu, "choice", "profile")
    if prof is None:
        failures.append("no profile choice on the main page")
    else:
        out = click(menu, prof)
        if out != []:
            failures.append(f"profile: expected open (no action), got {out}")
        # The open list: pick the second profile. The option rows are built
        # in layout, but their geometry (strip) is filled by the drawer - so
        # paint twice: the first pass fills the strip, the second builds the
        # rows.
        menu.layout(3840, 2160)
        paint(menu)
        paint(menu)
        opts = [i for i in menu.options if i.kind == "option"]
        if len(opts) < 2:
            failures.append("the profile list did not open")
        else:
            out = click(menu, opts[1])
            if out != [("profile", "Strong / Cinematic")]:
                failures.append(f"profile pick: expected Strong, got {out}")
    for key, want in (("windows", [("capture", None)]),
                      ("fullscreen", [("button", "window_mode")]),
                      ("screenshot", [("button", "screenshot")]),
                      ("record", [("button", "record")])):
        btn = find(menu, "button", key)
        if btn is None:
            failures.append(f"no {key} button in the Actions section")
            continue
        out = click(menu, btn)
        if out != want:
            failures.append(f"button {key}: expected {want}, got {out}")
        if key == "windows":
            if menu.page != "windows":
                failures.append("the windows button should open the windows page")

    # 4. The windows page: a window row and the back button.
    menu.layout(3840, 2160)
    paint(menu)
    row = find(menu, "option", "window")
    if row is None:
        failures.append("no window rows on the windows page")
    else:
        out = click(menu, row)
        if out != [("window", "1A2B3C: Notepad")]:
            failures.append(f"window row: expected pick, got {out}")
    back = find(menu, "action", "back")
    if back is None:
        failures.append("no back button on the windows page")
    else:
        out = click(menu, back)
        if menu.page != "main" or out != [("capture", None)]:
            failures.append(f"back: expected main page, got {out}")

    # 5. The footer exit on the main page.
    menu.layout(3840, 2160)
    paint(menu)
    exit_btn = find(menu, "action", "exit")
    if exit_btn is None:
        failures.append("no exit button in the footer")
    else:
        out = click(menu, exit_btn)
        if out != [("button", "exit")]:
            failures.append(f"exit: expected [('button', 'exit')], got {out}")

    # 6. The sliders emit their commands.
    menu.layout(3840, 2160)
    paint(menu)
    for key, want_prefix in (("intensity", "param"), ("split", "split"),
                             ("nr_res", "nr_res")):
        sl = find(menu, "slider", key)
        if sl is None:
            failures.append(f"no {key} slider on the main page")
            continue
        track = sl.extra.get("track")
        if track is None:
            failures.append(f"the {key} slider has no track")
            continue
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            {"pos": (track.x + track.w // 2, track.centery), "button": 1}))
        if not out or out[0][0] != want_prefix:
            failures.append(f"slider {key}: expected {want_prefix}..., got {out}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: every control on every page fires the right command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
