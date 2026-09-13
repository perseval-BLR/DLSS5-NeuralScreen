"""An alert belongs at the top centre of the SCREEN, wherever the overlay is.

It was centred on the overlay, which is the whole monitor only in
full-screen mode. In one-window mode the overlay is the size of the
captured window and sits on it, so an alert about the graphics card or the
monitor turned up in the middle of somebody's browser (user, 13.09).

And the gap from the top was a share of the height - 4.5%. That is 65 px on
a 1440-tall screen and 13 px on a window 300 tall, which is not the same
margin by any reading of the word.

So: placed against the monitor, a fixed scaled margin below its top edge,
and clamped into the overlay - because an alert drawn outside the overlay
is an alert nobody sees. A window in the bottom corner of the screen still
gets one, as high and as central as that window allows.

Geometry only: _alert_rect is asked directly, with the monitor and the
overlay's own position stubbed. No window, no worker.

Run:  runtime\\python.exe tests\\test_alert_position.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

SCREEN = (0, 0, 2560, 1440)


def main() -> int:
    import pygame
    pygame.init()
    import display as D

    failures = []
    disp = object.__new__(D.Display)      # no window, no pygame display
    disp.ui_scale = 1.0
    W, H = 420, 80                        # a plausible alert box

    def place(overlay_w, overlay_h, own_xy, screen=SCREEN):
        disp.width, disp.height = overlay_w, overlay_h
        disp._screen_rect = lambda: screen
        disp._own_rect = lambda: (own_xy[0], own_xy[1], overlay_w, overlay_h)
        return D.Display._alert_rect(disp, W, H)

    margin = int(round(D.ALERT_TOP_MARGIN * disp.ui_scale))

    # 1. Full screen: the overlay IS the monitor.
    r = place(2560, 1440, (0, 0))
    print(f"    full screen:      {r}")
    if r.centerx != 1280:
        failures.append(f"not centred on the screen: centre {r.centerx}, want 1280")
    if r.y != margin:
        failures.append(f"the top gap is {r.y}, want {margin}")

    # 2. One window, somewhere in the middle. Horizontally the alert still
    #    aims at the centre of the SCREEN. Vertically it cannot reach the
    #    top of the screen - the window starts 100 px below it - so it sits
    #    at the window's own top edge, which is as high as it can go.
    r = place(1600, 900, (400, 100))
    print(f"    window at 400,100: {r} (screen centre is at x={1280 - 400} here)")
    if r.centerx != 1280 - 400:
        failures.append(f"the alert followed the window instead of the screen: "
                        f"centre {r.centerx}, want {1280 - 400}")
    if r.y != 8:
        failures.append(f"the top is {r.y}, want 8 - as high as this window "
                        f"reaches")

    # 2b. A window that DOES touch the top of the screen gets the real
    #     margin, not the clamp.
    r = place(1200, 800, (600, 0))
    print(f"    window at 600,0:   {r}")
    if r.y != margin:
        failures.append(f"a window at the top of the screen got {r.y}, "
                        f"want the margin {margin}")
    if r.centerx != 1280 - 600:
        failures.append(f"centre {r.centerx}, want {1280 - 600}")

    # 3. A window in the bottom-right corner: the screen's top centre is far
    #    outside it, so the alert clamps to this window's own top left area
    #    and stays fully inside.
    r = place(600, 400, (1900, 1000))
    print(f"    window bottom-right: {r}")
    if not (0 <= r.x and r.right <= 600 and 0 <= r.y and r.bottom <= 400):
        failures.append(f"the alert left the overlay: {r} in 600x400")

    # 4. A window smaller than the alert: it still has to be drawn, not
    #    pushed to a negative corner.
    r = place(300, 60, (100, 100))
    print(f"    window smaller than the alert: {r}")
    if r.x < 0 or r.y < 0:
        failures.append(f"a too-small overlay put the alert off its own edge: {r}")

    # 5. A second monitor, whose corner is not (0, 0).
    r = place(1920, 1080, (2560, 0), screen=(2560, 0, 1920, 1080))
    print(f"    second monitor:   {r}")
    if r.centerx != 960:
        failures.append(f"on a second monitor the alert is at {r.centerx}, "
                        f"want 960 - the origin was not subtracted")

    # 6. Nothing answers (no window yet): fall back to the overlay's own
    #    centre rather than crash or draw off-screen.
    disp.width, disp.height = 2560, 1440
    disp._screen_rect = lambda: None
    disp._own_rect = lambda: None
    r = D.Display._alert_rect(disp, W, H)
    print(f"    no geometry:      {r}")
    if r.centerx != 1280 or r.y != margin:
        failures.append(f"the fallback is wrong: {r}")

    # 7. The layer itself. In one-window mode it stays the size of the
    #    SCREEN - it is not shrunk onto the captured window and not expanded
    #    again for every alert. The shrinking cost a set_mode each way (the
    #    SDL window is recreated and every attribute re-applied), and with
    #    alerts needing the screen too it became several per second: "a lot
    #    of blinking of both the alert and the menu when picking windows".
    import types
    calls = []
    d2 = object.__new__(D.Display)
    d2.ui_scale = 1.0
    d2.width, d2.height = 2560, 1440
    d2._alerts = []
    d2._window_layer = None
    d2._frame_size = None
    d2._switch_active = False
    d2.menu = types.SimpleNamespace(visible=False, state={"theme": "light"})
    d2._alert_font = pygame.font.Font(None, 20)
    d2.screen = pygame.Surface((2560, 1440))
    d2._screen_rect = lambda: SCREEN
    d2._own_rect = lambda: (0, 0, 2560, 1440)
    d2.set_fullscreen_layer = lambda w, h: calls.append(("expand", w, h))

    D.Display.alert(d2, "GPU", duration=0.0001)
    import time as _t
    _t.sleep(0.01)
    D.Display._draw_alerts(d2)
    if calls:
        failures.append(f"an alert resized the layer - that is the blinking: "
                        f"{calls}")

    # 8. Going onto a window records where it is and how big the frame is,
    #    and leaves the layer alone.
    D.Display.set_window_layer(d2, 400, 100, 1600, 900)
    print(f"    after set_window_layer: layer {d2.width}x{d2.height}, "
          f"window {d2._window_layer}, resizes {calls}")
    if (d2.width, d2.height) != (2560, 1440):
        failures.append(f"the layer left the screen: {d2.width}x{d2.height}")
    if d2._window_layer != (400, 100, 1600, 900):
        failures.append(f"the window position was not recorded: {d2._window_layer}")
    if calls:
        failures.append(f"going onto a window resized the layer: {calls}")

    # 9. And an alert then lands at the top centre of the SCREEN, because
    #    that is what the layer is.
    r = D.Display._alert_rect(d2, W, H)
    print(f"    alert in window mode: {r}")
    if r.centerx != 1280 or r.y != margin:
        failures.append(f"the alert is not at the screen's top centre: {r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: alerts sit at the top centre of the screen, in either mode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
