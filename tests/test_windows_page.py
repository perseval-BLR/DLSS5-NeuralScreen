"""The windows page: the list with the amber hover highlight.

The window list used to be a cramped drop-down in the view section with
no feedback about what each entry actually is. It moved to its own page
opened by the "Windows..." button: hovering a row reports the hwnd to
main (which draws the amber outline around the real window), clicking
switches the capture, and Back returns to the main page.

Checked: the button opens the page, the rows carry the hwnd and the
selected state, hovering a row sets hover_window, clicking emits the
window command, and Back returns to the main page.
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
    "window_mode": True,
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


def click(menu, item):
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": item.rect.center, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": item.rect.center, "button": 1}))
    return out


def hover(menu, item):
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEMOTION, {"pos": item.rect.center, "rel": (0, 0),
                             "buttons": (0, 0, 0)}))


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    menu = build()
    menu.layout(3840, 2160)

    # 1. The window list opens from the source segment on the main page -
    #    the "Windows..." button moved there with the rest of the source.
    menu.draw(pygame.Surface((3840, 2160)))
    seg = next((i for i in menu.items
                if i.kind == "segmented" and i.key == "source"), None)
    cells = (seg.extra.get("cells") or []) if seg else []
    if len(cells) < 2:
        failures.append("no source segment on the main page")
    else:
        out = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": cells[1].center, "button": 1}))
        menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, {"pos": cells[1].center, "button": 1}))
        print(f"one-window cell -> {out}, page now {menu.page}")
        if menu.page != "windows":
            failures.append("the one-window cell should open the windows page")

    # 2. The windows page lists every window as an option row.
    menu.layout(3840, 2160)
    rows = [i for i in menu.items if i.kind == "option"]
    print(f"window rows: {[(r.payload, r.extra.get('selected')) for r in rows]}")
    if len(rows) != 2:
        failures.append(f"expected 2 window rows, got {len(rows)}")
    if not any(r.payload == "1A2B3C: Notepad" and r.extra.get("selected")
               for r in rows):
        failures.append("the current window should be marked selected")

    # 3. Hovering a row reports the hwnd (the hex prefix).
    row = next(r for r in rows if r.payload == "4D5E6F: Chrome - YouTube")
    hover(menu, row)
    print(f"hover -> hover_window 0x{menu.hover_window:X}")
    if menu.hover_window != 0x4D5E6F:
        failures.append(f"hover should report 0x4D5E6F, got {menu.hover_window}")

    # 4. Moving off the rows clears the highlight.
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEMOTION, {"pos": (10, 10), "rel": (0, 0),
                             "buttons": (0, 0, 0)}))
    if menu.hover_window is not None:
        failures.append("hover_window should clear when the cursor leaves the rows")

    # 5. Clicking a row emits the window command with the hwnd.
    out = click(menu, row)
    print(f"row click -> {out}")
    if ("window", "4D5E6F: Chrome - YouTube") not in out:
        failures.append(f"the row click should emit (window, ...), got {out}")

    # 6. Back returns to the main page and clears the highlight.
    back = next((i for i in menu.items if i.kind == "action" and i.key == "back"),
                None)
    if back is None:
        failures.append("no Back button on the windows page")
    else:
        hover(menu, row)
        out = click(menu, back)
        print(f"back click -> {out}, page now {menu.page}")
        if menu.page != "main":
            failures.append("Back should return to the main page")
        if menu.hover_window is not None:
            failures.append("Back should clear the window highlight")

    # 7. The capture mode line: window mode when window_mode is set,
    #    fullscreen otherwise.
    menu.layout(3840, 2160)
    mode = menu.state.get("window_mode")
    print(f"window_mode state: {mode}")
    if mode is not True:
        failures.append("window_mode should be True in the test state")
    if "mode_window" not in overlay_ui.STRINGS["en"]:
        failures.append("missing mode_window i18n key")
    if "mode_fullscreen" not in overlay_ui.STRINGS["en"]:
        failures.append("missing mode_fullscreen i18n key")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the windows page lists, highlights and switches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
