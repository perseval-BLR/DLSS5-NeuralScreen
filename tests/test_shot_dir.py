"""The screenshot folder: a configured dir is where Save As opens.

Issue #20: the user wanted a fixed output folder for screenshots. The
folder picker in the settings remembers the folder; the Save As dialog
then opens in it (the dialog itself always appears - the folder is the
starting point, not a replacement).

Checked: the payload carries the configured folder; the settings button
shows the folder in its caption; the dialog's initial dir comes from
the config.
"""
import importlib.util
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

spec = importlib.util.spec_from_file_location("ns_main", str(BASE / "main.py"))
ns_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []

    # 1. The payload carries the configured folder.
    class _Menu:
        user_scale = 1.0
        user_height = None
        state = {"theme": "dark"}
        offset = [10, 20]

    cfg = {"profile": "Natural", "screenshot_dir": r"C:\Shots"}
    params = {"intensity": 1.0, "local_tone": 1.0,
              "local_structure": 1.0, "skin_structure": -1.0}
    payload = ns_main._menu_layout_payload(
        cfg, params, 0, "en", 0.65, 0.5, True, False, _Menu())
    if payload.get("screenshot_dir") != r"C:\Shots":
        failures.append(f"the folder did not persist: {payload.get('screenshot_dir')!r}")

    # 2. The settings button caption shows the configured folder.
    import pygame
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    try:
        import display as display_mod
        disp = display_mod.Display(640, 360)
        disp.menu.set_state({"screenshot_dir": r"C:\Shots"})
        disp.menu.page = "settings"
        disp.menu.layout(640, 360)
        btn = next((it for it in disp.menu.items
                    if it.kind == "button" and it.key == "shot_dir"), None)
        if btn is None:
            failures.append("no shot_dir button on the settings page")
        else:
            label = btn.extra.get("label", "")
            if "C:\\Shots" not in label:
                failures.append(f"the button does not show the folder: {label!r}")
    finally:
        pygame.quit()

    # 3. The dialog's initial dir comes from the config: the source of
    #    _open_save_dialog must pass the configured folder to the dialog.
    src = (BASE / "main.py").read_text(encoding="utf-8")
    if "_ask_save_path(hwnd, default_name, initial_dir)" not in src:
        failures.append("the dialog does not receive the initial dir")
    if "screenshot_dir" not in src.split("def _open_save_dialog")[1][:800]:
        failures.append("_open_save_dialog does not read screenshot_dir")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the screenshot folder is where Save As opens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
