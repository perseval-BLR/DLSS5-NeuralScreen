"""The menu survives a window-mode switch: open at start, Num5, still visible.

The regression: with the menu open at launch (open_menu_on_start) the saved
offset was computed for the full desktop and landed the panel OUTSIDE a small
captured window - the menu came back clipped off the right edge on the first
window-mode activation. The fix: the saved offset is honoured as-is and
layout() clamps the panel fully inside the screen (user rule 10.09: fixed
position until the user drags it) - no more bottom-right re-placement.

How it is checked: in window mode the overlay is visible to an outside
capture (WDA is off), so a Desktop Duplication frame shows the menu. The
test forces a centred offset; the panel must be visible in the centre and
NOT at the right edge (the old corner jump is gone).

The test forces a light theme, the menu-open config and a centred offset,
and restores the user's config afterwards.
"""
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import autocheck  # noqa: E402

# Physical pixels, before pygame loads: SDL freezes the process DPI awareness
# at import, and a window measured in logical units would not match what the
# capture produces (960x540 logical = 1200x675 physical at 125%).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

W, H = 960, 540
VK_NUMPAD2 = 0x62
VK_NUMPAD5 = 0x65
KEYEVENTF_KEYUP = 0x0002
CFG = BASE / "config.json"
CFG_BACKUP = BASE / "_work" / "config-window-menu-test.json"

# The light theme panel background.
PANEL_RGB = (240, 238, 230)


def numlock_on() -> bool:
    return bool(ctypes.windll.user32.GetKeyState(0x90) & 1)


def toggle_numlock() -> None:
    u = ctypes.windll.user32
    u.keybd_event(0x90, 0, 0, 0)
    u.keybd_event(0x90, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def grab_region(cam, rect):
    frame = None
    deadline = time.monotonic() + 3.0
    while frame is None and time.monotonic() < deadline:
        frame = cam.grab()
        if frame is None:
            time.sleep(0.05)
    if frame is None or rect is None:
        return frame
    x, y, w, h = rect
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1].copy()


def main() -> int:
    busy = autocheck.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1

    # Force the menu-open + light theme config; restore the user's after.
    user_cfg = json.loads(CFG.read_text(encoding="utf-8"))
    CFG_BACKUP.write_text(json.dumps(user_cfg, indent=2), encoding="utf-8")
    test_cfg = dict(user_cfg)
    test_cfg["open_menu_on_start"] = True
    test_cfg["theme"] = "light"
    test_cfg["split"] = 0.0
    test_cfg["nr_small"] = True
    test_cfg["work_scale"] = 0.65
    # A centred offset: the panel must stay where the config says, not jump
    # to the bottom-right corner (the old place_bottom_right behaviour).
    test_cfg["menu_offset"] = [0, 0]
    CFG.write_text(json.dumps(test_cfg, indent=2), encoding="utf-8")

    restore_numlock = False
    if not numlock_on():
        toggle_numlock()
        restore_numlock = True

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen window-menu test target")
    hwnd = pygame.display.get_wm_info()["window"]
    user32 = ctypes.windll.user32

    tick = [0]

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tick[0] += 1
            wob = tick[0] % 7
            screen.fill((31 + wob, 97, 211 - wob))
            pygame.display.flip()
            pygame.event.pump()
            time.sleep(0.02)

    def focus_target() -> bool:
        for attempt in range(6):
            user32.SetForegroundWindow(hwnd)
            pump(0.3)
            if user32.GetForegroundWindow() == hwnd:
                return True
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            user32.SetCursorPos(cx, cy)
            pump(0.2)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            pump(0.4)
            if user32.GetForegroundWindow() == hwnd:
                return True
        return False

    failures = []
    offset = autocheck.launch()
    try:
        if not autocheck.wait_for(offset, "NR ON | FPS", 30.0):
            print("FAIL: NeuralScreen did not start processing")
            return 1
        # The menu is open at start (the config says so). The overlay holds
        # the focus, so the target window is focused for real first.
        if not focus_target():
            print("FAIL: could not bring the target window to the front")
            return 1
        pump(1.0)

        # Num5: into window mode. The menu is STILL OPEN - this is the
        # regression scenario (the first activation clipped it).
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.SetCursorPos((rect.left + rect.right) // 2,
                            (rect.top + rect.bottom) // 2)
        pump(0.3)
        autocheck.send_key(VK_NUMPAD5)
        if autocheck.wait_for(offset, "window capture inside the worker (WGCW)", 30.0) is None:
            print("FAIL: the hotkey did not put the program into window mode")
            return 1
        # The switch overlay (blur + spinner) is up while the new worker
        # warms up; it comes down on the first processed frame. Waiting for
        # "NR ON | FPS" is NOT enough: that line is printed every frame and
        # the PRE-SWITCH one is already in the log, so the wait returns
        # instantly and the test captures the screen under the veil (0.0%
        # panel in the suite). The exact signal is the overlay coming down.
        if autocheck.wait_for(offset, "switch overlay OFF", 30.0) is None:
            print("FAIL: the switch overlay never came down after the "
                  "window-mode switch (the worker may be restarting)")
            return 1
        pump(0.5)

        # The menu must be fully visible and stay where the config put it:
        # in window mode the overlay is visible to an outside capture, and
        # the panel must NOT jump to the right edge (the old bottom-right
        # re-placement is gone - user rule 10.09: fixed position).
        import dxcam
        cam = dxcam.create(output_idx=0, output_color="RGB")
        try:
            frame = grab_region(cam, None)
            if frame is None:
                failures.append("no capture frame to inspect the menu")
            else:
                fh, fw = frame.shape[:2]
                # The right-edge strip must NOT be panel-coloured: the menu
                # is centred now, not cornered. Match the exact light-theme
                # panel colour (240,238,230) with a small tolerance - a
                # plain "brightness > 200" test also matches light wallpapers
                # and fails spuriously on a light desktop.
                def panel_mask(blk):
                    d = (np.abs(blk[..., 0].astype(int) - 240) < 15) & \
                        (np.abs(blk[..., 1].astype(int) - 238) < 15) & \
                        (np.abs(blk[..., 2].astype(int) - 230) < 15)
                    return d
                strip = frame[max(0, fh - 700):fh, max(0, fw - 100):fw]
                light = panel_mask(strip)
                share = float(light.mean())
                print(f"right-edge strip: {share * 100:.1f}% panel-coloured "
                      f"({fw}x{fh} screen)")
                if share > 0.30:
                    failures.append(
                        f"the menu jumped to the right edge after the window-mode "
                        f"switch ({share * 100:.1f}% panel in the strip) - the "
                        f"fixed position was not honoured")
                # And the panel must actually be there: a light blob in the
                # centre band (the centred offset). The capture may shift the
                # exact panel colour slightly, so the centre check uses a
                # brightness threshold - the overlay background behind the
                # panel is dark, so a bright blob in the centre is the panel.
                band = frame[fh // 2 - 200:fh // 2 + 200, fw // 2 - 300:fw // 2 + 300]
                light_b = (band[..., 0] > 200) & (band[..., 1] > 200) & (band[..., 2] > 200)
                bshare = float(light_b.mean())
                print(f"centre band: {bshare * 100:.1f}% panel-coloured")
                if bshare < 0.01:
                    failures.append("no panel visible in the centre band at all")
        finally:
            del cam
    finally:
        autocheck.quit_app()
        pygame.quit()
        if restore_numlock:
            toggle_numlock()
        # Restore the user's config.
        CFG.write_text(json.dumps(user_cfg, indent=2), encoding="utf-8")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the menu stays fully visible across the window-mode switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
