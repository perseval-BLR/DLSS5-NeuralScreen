"""Everything the menu is told is something the menu can hold.

`OverlayMenu.set_state` accepts a key only when it already exists in the
menu's own state dict:

    elif k in self.state:
        self.state[k] = v

That is a deliberate guard against typos, and it has one failure mode: add a
control, feed it from `menu_payload`, forget the default - and the value is
dropped in silence. The control then renders from `self.state.get(key)`,
which is None, so it looks permanently off while the action behind it fires
normally. That is exactly what happened to the Spout2 toggle in 1.6.0: the
worker restarted on every click and the box never filled in.

So: every key `menu_payload` produces must exist in a fresh menu's state.
This is a pure dictionary comparison - no window, no worker.
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402


class _Capture:
    devicename = r"\\.\DISPLAY1"
    resolution = (1920, 1080)


def _state():
    """The smallest state menu_payload can be asked about."""
    return types.SimpleNamespace(
        paused=False, work_scale=0.65, nr_small=False, width=1920, height=1080,
        cfg={"profile": "Natural", "spout": True, "rec_indicator": True,
             "screenshot_dir": "", "gpu": 0},
        params={"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
                "skin_structure": -1.0},
        presets={}, lang="en", recorder=None, work_w=1248, work_h=702,
        startup_menu=True, split_pos=0.0, gpu_text="RTX 5080", gpu_ok=True,
        window_hwnd=None, capture=_Capture(), monitor=0, worker_logs=[])


def main() -> int:
    failures = []

    import os
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    pygame.init()
    try:
        from overlay_ui import OverlayMenu
        menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
        payload = settings_io.menu_payload(_state())
        # lang is handled by set_state itself, params has its own branch.
        exempt = {"lang", "params"}
        missing = sorted(k for k in payload if k not in exempt
                         and k not in menu.state)
        if missing:
            failures.append(
                f"menu_payload sends keys the menu cannot hold, so they are "
                f"dropped in silence: {missing}")

        # And the other direction for the one that bit us: a value set through
        # set_state has to be readable back.
        menu.set_state({"spout": True})
        if menu.state.get("spout") is not True:
            failures.append("set_state({'spout': True}) did not stick")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: every menu_payload key reaches the menu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
