"""Check what an external screen capture sees of the overlay.

The overlay window carries SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
because the input is Desktop Duplication of the whole screen: without the flag
the pipeline would capture its own output. The flag is therefore load-bearing
in two opposite ways, and both are worth a test:

  * while it is set, no outside capture may see the overlay - otherwise the
    self-capture loop is back;
  * clearing it must make the overlay visible - that is what the single-window
    (WGC) mode is built on, and measured on a real recorder the flag turned out
    to be the whole story: with it the NVIDIA App refuses to record at all.

Method: raise the real overlay (the same display.Display the program uses),
paint a rectangle in a colour nothing else on a desktop has, and grab the
screen through dxcam - Desktop Duplication, the path OBS display capture uses.
Nothing is written to disk: the frame is reduced to numbers in memory, because
the desktop behind the marker is none of this test's business.

The marker is identified by what the CAPTURE returns for it rather than by
the bytes we painted. On an HDR desktop the compositor takes SDR content
through scRGB and back, and duplication returns a different colour for the
same rectangle: (7,231,149) came back as (21,255,235). The capturable run
therefore supplies the reference colour and the hidden run is searched for
it - which also holds on an SDR desktop, where the two agree.

Run:  runtime\\python.exe test_capture_visibility.py
"""
import ctypes
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import display as D  # noqa: E402

WDA_NONE = 0x0

# Not in the brand palette, not the chroma key, and nothing on a real desktop
# looks like it - so a match cannot come from the wallpaper.
MARKER = (7, 231, 149)
MARKER_W, MARKER_H = 480, 240
# The layer is 235/255 opaque, so the captured marker is the marker blended
# with 8% of whatever is behind it. 40 per channel covers that and is still
# far away from any other colour.
TOL = 40
# The screen must be given a moment: the compositor and Desktop Duplication
# both run behind us, and grab() answers None until something changes.
FRAMES = 40
GRAB_AT = 20


def measure(capturable: bool, cam) -> dict:
    """Raise the overlay in one of the two states and look for the marker."""
    import pygame

    user32 = ctypes.windll.user32
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    disp = D.Display(w, h, click_through=True)
    # The overlay is created HIDDEN on purpose (no blank flash during the
    # NGX warm-up) and shown only after the first real frame - reveal() is
    # that moment. The visibility test needs the window physically up in
    # BOTH states; only the WDA flag differs between them.
    disp.reveal()
    if capturable:
        if not user32.SetWindowDisplayAffinity(disp.get_hwnd(), WDA_NONE):
            disp.close()
            return {"error": "SetWindowDisplayAffinity(NONE) failed"}
    disp.set_hud_only(True, force=True)
    disp.menu.visible = False

    x0, y0 = (w - MARKER_W) // 2, (h - MARKER_H) // 2
    rect = pygame.Rect(x0, y0, MARKER_W, MARKER_H)
    frame = None
    for i in range(FRAMES):
        disp.draw_overlay(0.0)
        pygame.draw.rect(disp.screen, MARKER, rect)
        pygame.display.flip()
        if i == GRAB_AT:
            deadline = time.monotonic() + 3.0
            while frame is None and time.monotonic() < deadline:
                frame = cam.grab()
                if frame is None:
                    time.sleep(0.05)
        time.sleep(0.02)
    disp.close()

    if frame is None:
        return {"error": "Desktop Duplication returned no frame"}
    fh, fw = frame.shape[:2]
    if (fw, fh) != (w, h):
        return {"error": f"the capture is {fw}x{fh}, the screen is {w}x{h}"}
    patch = frame[y0:y0 + MARKER_H, x0:x0 + MARKER_W].astype(np.int16)
    return {"patch": patch,
            "median": np.median(patch.reshape(-1, 3), axis=0).astype(np.int16),
            "mean": tuple(int(v) for v in patch.reshape(-1, 3).mean(axis=0))}


def main() -> int:
    try:
        import dxcam
    except Exception as exc:
        print(f"SKIP: dxcam is unavailable ({exc!r})")
        return 0

    cam = dxcam.create(output_idx=0, output_color="RGB")
    failures = []
    results = {}
    try:
        for capturable in (False, True):
            name = "capturable" if capturable else "hidden"
            res = measure(capturable, cam)
            results[name] = res
            if "error" in res:
                failures.append(f"{name}: {res['error']}")
                print(f"{name}: ERROR {res['error']}")
                continue
            print(f"{name}: mean {res['mean']}")
            time.sleep(0.5)
    finally:
        del cam

    # The marker is identified by what the CAPTURE returns for it, not by
    # the bytes we painted. On an HDR desktop the compositor takes SDR
    # content through scRGB and back, and Desktop Duplication then returns
    # a different colour for the same rectangle - (7,231,149) came back as
    # (26,255,235), which no fixed tolerance around the painted value can
    # cover. So the capturable run supplies the reference (a solid
    # rectangle of ONE colour is the signal), the hidden run is searched
    # for that colour, and the two are required to be far apart - without
    # which "a uniform patch" would also describe an empty desktop.
    hid = results.get("hidden", {})
    shown = results.get("capturable", {})
    if "patch" in hid and "patch" in shown:
        observed, desktop = shown["median"], hid["median"]
        separation = int(np.abs(observed - desktop).max())

        def nearer_marker(patch):
            """Share of pixels closer to the marker than to the desktop.

            A share, not a tolerance. The overlay layer is 235/255 opaque,
            so every captured marker pixel carries 8% of whatever is behind
            it - and on an HDR desktop that blend happens in linear light,
            where 8% of a bright window moves a channel by a hundred rather
            than by twenty. Which of the two colours a pixel is closer to
            survives that; a fixed radius around the marker does not.
            """
            to_marker = np.abs(patch - observed).max(axis=2)
            to_desktop = np.abs(patch - desktop).max(axis=2)
            return float((to_marker < to_desktop).mean())

        visible = nearer_marker(shown["patch"])
        # The leak is asked the strict way round: a pixel counts only if it
        # is the captured marker colour itself, within TOL. "Closer to the
        # marker than to the desktop" would also catch a bright patch of
        # somebody's wallpaper, and this is the assertion that must never
        # cry wolf - it is the one guarding the self-capture loop.
        leak = float((np.abs(hid["patch"] - observed).max(axis=2) <= TOL).mean())
        painted = int(np.abs(observed - np.array(MARKER, dtype=np.int16)).max())
        print(f"captured marker {tuple(int(v) for v in observed)} "
              f"(painted {MARKER}, {painted} off), desktop behind it "
              f"{tuple(int(v) for v in desktop)}")
        print(f"visible {visible * 100:.1f}% · separation {separation} · "
              f"leak {leak * 100:.1f}%")
        if separation <= TOL:
            failures.append(f"the capturable overlay is the same colour as the "
                            f"desktop behind it (separation {separation}) - "
                            f"nothing was drawn, or nothing was captured")
        elif visible < 0.95:
            failures.append(f"clearing WDA_EXCLUDEFROMCAPTURE did not make the "
                            f"overlay visible ({visible * 100:.1f}% of the "
                            f"rectangle)")
        if leak > 0.01:
            failures.append(f"the hidden overlay leaks into an outside capture "
                            f"({leak * 100:.1f}% of the marker) - the "
                            f"self-capture loop is back")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: hidden while the flag is set, fully visible without it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
