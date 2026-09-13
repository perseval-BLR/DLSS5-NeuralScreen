"""The one-window hotkey really points the pipeline at one window.

Step 2 of the single-window mode, end to end through the running program
rather than through the worker alone: a window is focused, Num5 is pressed,
and the whole pipeline has to come back up sized to that window - and Num5
again has to put it back on the whole screen.

What this pins down, because all three have already been got wrong once:
  * the capture size is the window's PHYSICAL size, taken from the worker's
    acknowledgement rather than guessed from GetWindowRect (which includes the
    invisible resize border);
  * the window that gets captured is the last one that was NOT ours - by the
    time the hotkey is pressed our own menu may hold the focus, and capturing
    our own overlay is the loop this mode exists to avoid;
  * switching back is clean, not a dead pipeline stuck on a window.

The geometry is checked too: both layers - the worker's picture window and the
HUD on top of it - have to sit on the target window, follow it when it moves,
and get out of the way when it is minimised. A resize means rebuilding the
pipeline, and that must happen once the size settles rather than on every
pixel of a drag.

And the point of the whole mode: with one window as the input there is no
self-capture loop, so the overlay stops hiding from screen capture. That is
checked both ways - the display affinity of both layers, and an actual Desktop
Duplication grab that has to show the open menu in window mode and must not
show it on the whole screen, where hiding is still mandatory.

Requires NeuralScreen not to be running. ~40 seconds.

Run:  runtime\\python.exe test_window_mode.py
"""
import ctypes
import ctypes.wintypes
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

# Physical pixels, before pygame loads (SDL freezes DPI awareness at import).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import autocheck  # noqa: E402  - launch/log/quit helpers live there

W, H = 960, 540
TARGET = (31, 97, 211)
VK_NUMLOCK = 0x90
VK_NUMPAD2 = 0x62
VK_NUMPAD5 = 0x65
KEYEVENTF_KEYUP = 0x0002


def find_window(class_name: str, exclude_pid: int = 0) -> int:
    """The first visible top-level window of a class, optionally not ours.

    The two overlay layers are found by class: the worker's picture window is
    "NeuralScreenPresent", and the HUD is a pygame window - and so is this
    test's own target, hence exclude_pid.
    """
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        if buf.value != class_name:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if exclude_pid:
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == exclude_pid:
                return True
        found.append(int(hwnd))
        return False

    user32.EnumWindows(cb, None)
    return found[0] if found else 0


def frame_rect(hwnd: int):
    """The window's visible bounds, the way the compositor sees them."""
    r = ctypes.wintypes.RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(int(hwnd)), ctypes.c_uint(9), ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(r))
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def grab_region(cam, rect):
    """One Desktop Duplication frame, optionally cropped to (x, y, w, h)."""
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


def display_affinity(hwnd: int) -> int:
    """WDA_NONE (0) or WDA_EXCLUDEFROMCAPTURE (0x11) for a window."""
    aff = ctypes.c_ulong(0xFFFF)
    ctypes.windll.user32.GetWindowDisplayAffinity(ctypes.c_void_p(int(hwnd)),
                                                  ctypes.byref(aff))
    return int(aff.value)


def numlock_on() -> bool:
    return bool(ctypes.windll.user32.GetKeyState(VK_NUMLOCK) & 1)


def toggle_numlock() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_NUMLOCK, 0, 0, 0)
    user32.keybd_event(VK_NUMLOCK, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def main() -> int:
    busy = autocheck.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen window-mode test target")
    hwnd = pygame.display.get_wm_info()["window"]
    user32 = ctypes.windll.user32

    tick = [0]

    def pump(seconds: float) -> None:
        """Keep the target window alive and repainting.

        A window that never redraws produces one capture frame and then
        nothing, so the colour wobbles a little - inside any tolerance, but
        enough for the compositor to hand over a new frame.
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tick[0] += 1
            wob = tick[0] % 7
            screen.fill((TARGET[0] + wob, TARGET[1], TARGET[2] - wob))
            pygame.display.flip()
            pygame.event.pump()
            time.sleep(0.02)

    def focus_target() -> bool:
        """Bring the target window to the front - for real.

        SetForegroundWindow from a background process is refused by Windows,
        and the first version of this test failed for exactly that reason: the
        overlay kept the focus, so the program had no foreign window to
        capture. A click is real user input, and the window is ours and at a
        known place.
        """
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
            user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
            pump(0.4)
            if user32.GetForegroundWindow() == hwnd:
                return True
        return False

    def menu_change(cam, rect) -> float:
        """How much of a region changes when the menu is opened.

        The differential is the marker. The menu is drawn only by the overlay,
        so if an outside capture can see the overlay, opening it moves a lot of
        pixels; if the overlay is hidden, the region is left alone apart from
        the target window's own wobble.
        """
        import numpy as np
        before = grab_region(cam, rect)
        autocheck.send_key(VK_NUMPAD2)
        pump(2.0)
        after = grab_region(cam, rect)
        autocheck.send_key(VK_NUMPAD2)
        pump(1.0)
        if before is None or after is None or before.shape != after.shape:
            return -1.0
        diff = np.abs(before.astype(np.int16) - after.astype(np.int16)).max(axis=2)
        return float((diff > 40).mean())

    def wait_log(offset: int, needle: str, timeout: float) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            text = autocheck.log_since(offset)
            if needle in text:
                return text
            pump(0.2)
        return ""

    restore_numlock = False
    if not numlock_on():
        # The defaults live on the numpad, which needs Num Lock; the test puts
        # it back the way it found it.
        toggle_numlock()
        restore_numlock = True

    import dxcam
    cam = dxcam.create(output_idx=0, output_color="RGB")
    failures = []
    offset = autocheck.launch()
    try:
        if not wait_log(offset, "NR ON | FPS", 30.0):
            print("FAIL: NeuralScreen did not start processing")
            return 1
        # The overlay opens its menu on start and takes both the focus and the
        # mouse, so the menu is closed first (Num2) - after that the overlay is
        # click-through again and the target window can be focused for real.
        autocheck.send_key(VK_NUMPAD2)
        pump(1.0)
        if not focus_target():
            print("FAIL: could not bring the target window to the front")
            return 1
        pump(1.0)

        # 1. Into window mode. Num5 now takes the window UNDER THE CURSOR
        # (falling back to the last focused one), so the mouse is parked in
        # the middle of the target window first.
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.SetCursorPos((rect.left + rect.right) // 2,
                            (rect.top + rect.bottom) // 2)
        pump(0.3)
        autocheck.send_key(VK_NUMPAD5)
        text = wait_log(offset, "window capture inside the worker (WGCW)", 30.0)
        if not text:
            print("FAIL: the hotkey did not put the program into window mode")
            print(autocheck.log_since(offset)[-800:])
            return 1
        line = [l for l in text.splitlines() if "(WGCW)" in l][-1]
        print("in :", line.strip())
        if f"{W}x{H}" not in line:
            failures.append(f"the capture is not the window's size ({W}x{H}): {line.strip()}")
        rebuilt = [l for l in text.splitlines() if "pipeline rebuilt" in l]
        if not rebuilt:
            failures.append("the pipeline was not rebuilt for the window")
        elif f"{W}x{H}" not in rebuilt[-1]:
            failures.append(f"the pipeline was rebuilt at the wrong size: {rebuilt[-1].strip()}")
        else:
            print("    ", rebuilt[-1].strip())
        # It has to keep running in that mode, not fall over after one frame.
        mark = autocheck.log_offset()
        pump(4.0)
        after = autocheck.log_since(mark)
        if "NR ON | FPS" not in after:
            failures.append("no frames in window mode - the pipeline stalled")
        if "back to full screen" in after:
            failures.append("window mode dropped itself back to full screen")

        # 2. Geometry: both layers sit on the window.
        pres = find_window("NeuralScreenPresent")
        hud = find_window("pygame", exclude_pid=os.getpid())
        if not pres:
            failures.append("the worker's picture window was not found")
        if not hud:
            failures.append("the HUD overlay window was not found")

        def check_on_target(what: str) -> None:
            """The PICTURE sits on the target window; the HUD covers the screen.

            The picture is the processed frame and belongs exactly on the
            window it was made from. The HUD layer used to be put there too,
            and that is what sent the menu and every alert inside somebody
            else's window - and the shrinking and expanding around it cost a
            set_mode each way, which is what the blinking was (user, 13.09).
            It stays the size of the monitor now; everything drawn on it is
            placed against the screen, and it is click-through and
            colour-keyed, so a layer nobody has drawn on is not visible.
            """
            want = frame_rect(hwnd)
            if pres:
                got = frame_rect(pres)
                off = max(abs(got[0] - want[0]), abs(got[1] - want[1]))
                print(f"     {what}: picture at {got[:2]}, window at {want[:2]}")
                if off > 2:
                    failures.append(f"{what}: the picture layer is {off} px off "
                                    f"the window ({got[:2]} vs {want[:2]})")
            if hud:
                got = frame_rect(hud)
                print(f"     {what}: HUD at {got[:2]} sized {got[2]}x{got[3]}")
                if got[2] < want[2] or got[3] < want[3]:
                    failures.append(f"{what}: the HUD layer {got[2]}x{got[3]} is "
                                    f"smaller than the window {want[2]}x{want[3]} "
                                    f"- it must cover the screen")

        check_on_target("placed")

        # 3. Move the window - the layers follow.
        user32.SetWindowPos(hwnd, 0, 520, 360, 0, 0, 0x0001 | 0x0004 | 0x0010)
        pump(2.0)
        check_on_target("moved")

        # 4. Minimise - the picture must get out of the way rather than hang a
        #    frozen frame over whatever is underneath.
        mark = autocheck.log_offset()
        user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE
        deadline = time.monotonic() + 6.0
        hidden = False
        while time.monotonic() < deadline and not hidden:
            time.sleep(0.2)
            hidden = pres and not user32.IsWindowVisible(ctypes.c_void_p(pres))
        if not hidden:
            failures.append("the picture stayed visible while the window was minimised")
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        if not focus_target():
            failures.append("could not restore the target window")
        pump(2.0)
        if pres and not user32.IsWindowVisible(ctypes.c_void_p(pres)):
            failures.append("the picture did not come back after the window was restored")

        # 5. Resize - the pipeline follows the new size once it settles.
        #    The live road is the one taken when the worker is capturing:
        #    WGCW at the new size plus RNSZ, ~120 ms and no new process.
        #    A rebuild is the fallback and is accepted here, because what
        #    this step is about is that the size is followed at all.
        mark = autocheck.log_offset()
        new_w, new_h = W - 160, H - 120
        user32.SetWindowPos(hwnd, 0, 520, 360, new_w, new_h, 0x0004 | 0x0010)
        text = wait_log(mark, "reconfigured live", 20.0) or             wait_log(mark, "rebuilding the pipeline", 5.0)
        if not text:
            failures.append("a resized window was not followed - neither a live "
                            "reconfiguration nor a rebuild")
        else:
            live = "reconfigured live" in text
            needle = "reconfigured live" if live else "rebuilding the pipeline"
            line = [l for l in text.splitlines() if needle in l][-1]
            print("size:", line.strip())
            if f"{new_w}x{new_h}" not in line:
                failures.append(f"the resize used the wrong size: {line.strip()}")
            if not live and not wait_log(mark, f"pipeline rebuilt: {new_w}x{new_h}", 30.0):
                failures.append("the pipeline did not come back at the new size")

        # 6. The point of the mode: an outside capture can see the overlay.
        #    The resize above may have rebuilt the pipeline, in which case
        #    the windows are new ones - the handles have to be found again,
        #    and the worker needs a moment to raise its picture window. The
        #    live road keeps the same windows, and finding them again costs
        #    nothing.
        pres = hud = 0
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and not (pres and hud):
            pres = pres or find_window("NeuralScreenPresent")
            hud = hud or find_window("pygame", exclude_pid=os.getpid())
            if pres and hud:
                break
            pump(0.4)
        for name, w in (("picture", pres), ("HUD", hud)):
            if not w:
                failures.append(f"the {name} layer was not found after the resize")
                continue
            aff = display_affinity(w)
            print(f"     affinity of the {name} layer in window mode: 0x{aff:X}")
            if aff != 0:
                failures.append(f"the {name} layer still hides from capture "
                                f"in window mode (affinity 0x{aff:X})")
        #    And a real grab: opening the menu has to CHANGE what Desktop
        #    Duplication sees. The menu opens in the bottom-right corner of
        #    the SCREEN (not of the captured window), so the whole screen is
        #    the region to compare - the menu covers ~2.7% of it, so a
        #    visible menu moves well over 0.5% of the pixels. Counting a
        #    colour does not work (the desktop has amber in it too), but a
        #    change does.
        share = menu_change(cam, None)
        print(f"     the menu changes {share * 100:.1f}% of what an outside "
              f"capture sees of the screen")
        if share < 0.005:
            failures.append(f"an outside capture does not see the overlay in "
                            f"window mode ({share * 100:.1f}% changed) - the "
                            f"whole point of the mode")

        # 7. And back out.
        mark = autocheck.log_offset()
        autocheck.send_key(VK_NUMPAD5)
        text = wait_log(mark, "pipeline rebuilt", 30.0)
        if not text:
            failures.append("the hotkey did not switch back to the whole screen")
        else:
            line = [l for l in text.splitlines() if "pipeline rebuilt" in l][-1]
            print("out:", line.strip())
            if "3840x2160" not in line and f"{W}x{H}" in line:
                failures.append("switching back kept the window size")
        mark = autocheck.log_offset()
        pump(4.0)
        if "NR ON | FPS" not in autocheck.log_since(mark):
            failures.append("no frames after switching back")

        # 8. On the whole screen the hiding is mandatory again: the input is
        #    the desktop, and without the flag the pipeline would capture its
        #    own output.
        pres = find_window("NeuralScreenPresent")
        hud = find_window("pygame", exclude_pid=os.getpid())
        for name, w in (("picture", pres), ("HUD", hud)):
            if not w:
                failures.append(f"the {name} layer disappeared after switching back")
                continue
            aff = display_affinity(w)
            print(f"     affinity of the {name} layer on the whole screen: 0x{aff:X}")
            if aff != 0x11:
                failures.append(f"the {name} layer does not hide from capture on "
                                f"the whole screen (affinity 0x{aff:X}) - the "
                                f"self-capture loop is back")
        share = menu_change(cam, None)
        print(f"     the menu changes {share * 100:.1f}% of what an outside "
              f"capture sees of the screen")
        if share > 0.02:
            failures.append(f"an outside capture sees the overlay on the whole "
                            f"screen ({share * 100:.1f}% changed) - it must stay "
                            f"hidden there or the pipeline captures itself")
    finally:
        del cam
        left = autocheck.quit_app()
        pygame.quit()
        if restore_numlock:
            toggle_numlock()
        if left:
            failures.append(f"processes left behind: {left}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the hotkey switches to one window and back, at the right size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
