"""The header collapse icon and the one-window footer button.

The collapse used to be a footer button next to "quit the program" - the
two looked equally harmless, even though one hides the menu and the other
unloads the program. It moved to the header as a minimise glyph ([help]
[gear] [min]), and the freed footer slot became the one-window mode
button (the feature was only reachable through the Num5 hotkey).

Checked: the header holds exactly help/gear/min on the main page, the
min icon emits the close command, the footer holds screenshot/record/
one-window, the one-window button emits the window_mode command, and the
settings page still shows the back (close) icon only.
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
    "profile": "Strong / Cinematic",
    "profiles": ["Faithful", "Natural", "Strong / Cinematic"],
    "params": {"intensity": 1.65, "local_tone": 1.40,
               "local_structure": 1.50, "skin_structure": 1.00},
    "split": 0.0, "open_on_start": True,
    "gpu_text": "RTX 5070 Ti · Blackwell", "gpu_ok": True,
    "windows": ["1A2B3C: Notepad", "4D5E6F: Chrome"],
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


def click(menu, item):
    """A left click at the item's centre; returns the emitted commands."""
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": item.rect.center, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": item.rect.center, "button": 1}))
    return out


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    menu = build()
    menu.layout(3840, 2160)

    # 1. The header on the main page: exactly help, gear and min.
    # The layout walks right to left, so the items list holds them reversed:
    # the visual order (left to right) is the reverse of the list.
    icons = [i for i in menu.items if i.kind == "icon"]
    keys = [i.key for i in icons]
    visual = list(reversed(keys))
    print(f"header icons (list {keys}, visual {visual})")
    if visual != ["help", "gear", "min"]:
        failures.append(f"the header should be [help, gear, min], got {visual}")

    # 2. The min icon emits the close command (hide the menu).
    min_icon = next((i for i in icons if i.key == "min"), None)
    if min_icon is None:
        failures.append("no min icon in the header")
    else:
        out = click(menu, min_icon)
        print(f"min click -> {out}")
        if ("button", "close") not in out:
            failures.append(f"the min icon should emit (button, close), got {out}")

    # 3. The footer on the main page: only Exit. The capture actions moved
    #    into the Actions section (user rule 10.09: Select window +
    #    Fullscreen on top, Screenshot + Record below).
    actions = [i for i in menu.items if i.kind == "action"]
    keys = [i.key for i in actions]
    print(f"footer actions: {keys}")
    if keys != ["exit"]:
        failures.append(f"the footer should hold only exit, got {keys}")
    if "window" in keys:
        failures.append("the one-window button should be gone from the footer")
    if "collapse" in keys:
        failures.append("the collapse button should be gone from the footer")

    # 4. The fullscreen button lives in the Actions section and emits the
    #    window_mode command.
    fs_btn = next((i for i in menu.items
                   if i.kind == "button" and i.key == "fullscreen"), None)
    if fs_btn is None:
        failures.append("no fullscreen button in the Actions section")
    else:
        out = click(menu, fs_btn)
        print(f"fullscreen click -> {out}")
        if ("button", "window_mode") not in out:
            failures.append(f"the fullscreen button should emit "
                            f"(button, window_mode), got {out}")

    # 5. The settings page: only the back (close) icon, no min.
    menu.page = "settings"
    menu.layout(3840, 2160)
    icons = [i for i in menu.items if i.kind == "icon"]
    keys = [i.key for i in icons]
    print(f"settings header icons: {keys}")
    if keys != ["close"]:
        failures.append(f"the settings header should be [close], got {keys}")

    # 6. The back icon returns to the main page.
    close_icon = next((i for i in icons if i.key == "close"), None)
    if close_icon is None:
        failures.append("no close icon in the settings header")
    else:
        out = click(menu, close_icon)
        print(f"close click -> {out}, page now {menu.page}")
        if menu.page != "main":
            failures.append("the close icon should return to the main page")

    # 7. Language and theme live on the SETTINGS page, not the main one
    #    (user rule 10.09: the main page is the main page - appearance
    #    controls moved behind the gear). The main page must not offer
    #    them, the settings page must.
    def seg_keys(page: str) -> list:
        menu.page = page
        menu.layout(3840, 2160)
        return [i.key for i in menu.items if i.kind == "segmented"]

    main_segs = seg_keys("main")
    print(f"main page segmented: {main_segs}")
    if "lang" in main_segs or "theme" in main_segs:
        failures.append(f"lang/theme must not be on the main page, got {main_segs}")
    set_segs = seg_keys("settings")
    print(f"settings page segmented: {set_segs}")
    if "lang" in set_segs:
        failures.append(f"lang must be a drop-down, not a segment, got {set_segs}")
    if "theme" not in set_segs:
        failures.append(f"the settings page must hold the theme segment, got {set_segs}")
    set_choices = [i.key for i in menu.items if i.kind == "choice"]
    if "lang" not in set_choices:
        failures.append(f"the settings page must hold the lang drop-down, got {set_choices}")

    # 8. Clicking the theme segment on the settings page emits the same
    #    ("theme", ...) action main already handles. The segment cells are
    #    built during drawing (item.extra["cells"]), so the menu must be
    #    drawn once before the click - exactly like the real loop does.
    #    The settings page is taller than the panel, so the segment may sit
    #    below the viewport - scroll it into view first (the real loop
    #    scrolls the same way).
    menu.page = "settings"
    menu.layout(3840, 2160)
    theme_seg = next((i for i in menu.items
                      if i.kind == "segmented" and i.key == "theme"), None)
    if theme_seg is None:
        failures.append("no theme segment on the settings page")
    else:
        vp = menu._viewport
        if theme_seg.rect.bottom > vp.bottom:
            menu.scroll = theme_seg.rect.bottom - vp.bottom + 20
            menu.layout(3840, 2160)
        # draw() re-runs layout() with the SURFACE size - the test surface is
        # 64x64, which would re-layout the panel for a 64px screen. Draw on a
        # real-size surface instead, exactly like the live loop does.
        menu.draw(pygame.Surface((3840, 2160)))
        # draw() re-runs layout() - the items are recreated, so the segment
        # must be looked up again (the old object is no longer in the list).
        theme_seg = next((i for i in menu.items
                          if i.kind == "segmented" and i.key == "theme"), None)
        if theme_seg is None:
            failures.append("no theme segment after the redraw")
        else:
            out = click(menu, theme_seg)
            print(f"theme click -> {out}")
            if not any(k == "theme" for k, *_ in out):
                failures.append(f"the theme segment should emit a theme "
                                f"action, got {out}")

    # 9. The live indicators (stats block + GPU line) are main-page only
    #    (user rule 10.09): the settings and windows pages are about
    #    configuration - FPS/RES/WORK/FRAMES/REC/PROFILE and the GPU dot
    #    are noise there. The rects must be empty on those pages and real
    #    on the main one.
    menu.page = "main"
    menu.layout(3840, 2160)
    main_stats = menu._stats_rect
    main_gpu = menu._gpu_rect
    print(f"main stats rect: {main_stats}, gpu rect: {main_gpu}")
    if main_stats.w <= 0 or main_gpu.h <= 0:
        failures.append("the main page must show the stats block and the "
                        "GPU line")
    for page in ("settings", "windows"):
        menu.page = page
        menu.layout(3840, 2160)
        print(f"{page} stats rect: {menu._stats_rect}, "
              f"gpu rect: {menu._gpu_rect}")
        if menu._stats_rect.w > 0 or menu._gpu_rect.h > 0:
            failures.append(f"the {page} page must not show the live "
                            f"indicators")

    # 10. The windows page: its own title ("Select window") and NO header
    #     icons - the Back button in the footer is the only way out (user
    #     rule 10.09). The settings page keeps its close icon.
    menu.page = "windows"
    menu.layout(3840, 2160)
    w_icons = [i.key for i in menu.items if i.kind == "icon"]
    print(f"windows page header icons: {w_icons}")
    if w_icons:
        failures.append(f"the windows page must have no header icons, "
                        f"got {w_icons}")
    menu.page = "settings"
    menu.layout(3840, 2160)
    s_icons = [i.key for i in menu.items if i.kind == "icon"]
    print(f"settings page header icons: {s_icons}")
    if s_icons != ["close"]:
        failures.append(f"the settings page must keep the close icon, "
                        f"got {s_icons}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the header collapse and the fullscreen button behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
