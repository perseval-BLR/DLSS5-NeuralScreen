"""The mode-switch veil: blur-in entrance, the assembling mark, fade-out.

The switch used to be a cut: the frozen frame appeared at full strength
the instant a rebuild began and vanished the instant the first frame
arrived. It is a transition now - the veil eases in (a blur-in crossfade
whose alpha ramps over SWITCH_FADE_IN), holds while the mark runs its
assemble-hold-scatter cycle, and fades back out over SWITCH_FADE_OUT.
The veil keeps the layer for the whole fade, so follow_window must not
drag it to the captured window (the reported window-mode glitch: the
veil slid by the top and left edges, the bare desktop showing) and the
layer-shrink helpers must be no-ops until it is down.

Run:  runtime\\python.exe tests\\test_switch_veil.py
"""
import os
import sys
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import display as D  # noqa: E402
import pipeline  # noqa: E402

import numpy as np  # noqa: E402
import pygame  # noqa: E402


def _dist_from_center(tile, w, h):
    return ((tile[0] - w / 2.0) ** 2 + (tile[1] - h / 2.0) ** 2) ** 0.5


def main() -> int:
    failures = []
    W, H = 1920, 1080

    # -- Part A: the mark's geometry --------------------------------
    m = float(min(W, H))
    orbit = m * D.SWITCH_MARK_ORBIT
    tile = m * D.SWITCH_MARK_TILE
    gap = tile * D.SWITCH_MARK_GAP

    # 1. At t=0 the tiles sit at their orbit points - spread out.
    tiles0 = D._assemble_tiles(0.0, W, H)
    if len(tiles0) != 5:
        failures.append(f"expected 5 tiles, got {len(tiles0)}")
    outer0 = [_dist_from_center(t, W, H) for t in tiles0[1:]]
    for d in outer0:
        if abs(d - orbit) > orbit * 0.02:
            failures.append(
                f"at t=0 an outer tile is {d:.0f} px from centre, "
                f"expected the orbit {orbit:.0f} - the mark would pop in "
                f"already assembled")
            break

    # 2. Held: the cluster - centre on the middle, others at `gap`.
    tiles_hold = D._assemble_tiles(1.5, W, H)
    if _dist_from_center(tiles_hold[0], W, H) > 2.0:
        failures.append("the accent tile is not at the centre during hold")
    for t in tiles_hold[1:]:
        d = _dist_from_center(t, W, H)
        if abs(d - gap) > 2.0:
            failures.append(
                f"a held tile sits {d:.0f} px from centre, expected {gap:.0f}")

    # 3. Mid-assemble the outer tiles are INBOUND: strictly closer to the
    #    centre than their starting orbit point. (Not a strict "between
    #    gap and orbit" band: the orbit angles differ from the cluster
    #    directions, so a tile's straight-line path may pass closer to
    #    the centre than its final cluster distance before settling.)
    tiles_mid = D._assemble_tiles(D.SWITCH_MARK_ASSEMBLE * 0.5, W, H)
    ds_mid = [_dist_from_center(t, W, H) for t in tiles_mid[1:]]
    if not all(d < orbit * 0.95 for d in ds_mid):
        failures.append(
            f"mid-assemble distances {[round(d) for d in ds_mid]} are not "
            f"inbound from the orbit {orbit:.0f}")

    # 4. Mid-scatter they move back out: past the cluster, under the orbit.
    t_sc = D.SWITCH_MARK_HOLD_END + (
        D.SWITCH_MARK_SCATTER_END - D.SWITCH_MARK_HOLD_END) * 0.5
    tiles_sc = D._assemble_tiles(t_sc, W, H)
    ds_sc = [_dist_from_center(t, W, H) for t in tiles_sc[1:]]
    if not all(d > gap * 1.05 for d in ds_sc):
        failures.append(
            f"mid-scatter distances {[round(d) for d in ds_sc]} are still "
            f"at the cluster - the mark never scatters")

    # 5. The loop wraps without a jump: the same phase, one period apart.
    a = D._assemble_tiles(0.4, W, H)
    b = D._assemble_tiles(0.4 + D.SWITCH_MARK_PERIOD, W, H)
    for (xa, ya, sa, _), (xb, yb, sb, _) in zip(a, b):
        if abs(xa - xb) > 0.5 or abs(ya - yb) > 0.5 or abs(sa - sb) > 0.5:
            failures.append("the mark jumps on the period wrap")
            break

    # 6. Exactly one accent tile.
    accents = [t[3] for t in tiles_hold]
    if accents.count(True) != 1 or accents.count(False) != 4:
        failures.append(f"accent tiles: {accents}")

    # -- Part B: the Display lifecycle ------------------------------
    pygame.init()
    disp = D.Display(W, H, click_through=False)
    try:
        frame = np.zeros((H, W, 4), dtype=np.uint8)
        frame[..., 3] = 255
        # Count the flips the synchronous entrance draws - the caller
        # blocks right after, so the entrance must be its own frames.
        flips = []
        real_flip = pygame.display.flip
        pygame.display.flip = lambda: flips.append(1)
        try:
            disp.enter_switch_mode(frame, W, H)
        finally:
            pygame.display.flip = real_flip
        if not disp.is_switch_active() or disp._switch_phase != "on":
            failures.append("enter_switch_mode did not settle into the hold")
        if not flips:
            failures.append("the entrance drew no frames - it would read "
                            "as a cut")
        if disp._switch_base is None or disp._switch_dim_soft is None:
            failures.append("enter_switch_mode did not freeze the frame")
        if disp._switch_alpha != float(D.SWITCH_ALPHA):
            failures.append("the veil did not reach full strength")

        # 7. The entrance actually crossfades: the painter draws the
        #    frozen base under a dim that starts transparent. Phase "in"
        #    is where the crossfade lives; the hold shows it at full.
        surf = disp.screen
        disp._switch_base.fill((250, 10, 10))
        disp._switch_dim_soft.fill((10, 250, 10))
        disp._switch_phase = "in"
        disp._draw_veil(0.0, time.monotonic())
        px = surf.get_at((W // 4, H // 4))
        if not (px.r > 200 and px.g < 60):
            failures.append(f"at strength 0 the sharp freeze is not "
                            f"showing: {px}")
        disp._draw_veil(1.0, time.monotonic())
        px = surf.get_at((W // 4, H // 4))
        if not (px.g > 200 and px.r < 60):
            failures.append(f"at strength 1 the dim is not in full: {px}")
        disp._switch_phase = "on"
        disp._draw_veil(0.0, time.monotonic())
        px = surf.get_at((W // 4, H // 4))
        if not (px.g > 200 and px.r < 60):
            failures.append(f"the hold dim is not at full: {px}")

        # 8. The layer is owned by the veil: nothing touches its geometry
        #    until the veil is down (the reported slide).
        #
        #    set_window_layer no longer shrinks the layer at all - in
        #    one-window mode it stays the size of the screen, and only the
        #    window's position is recorded - so there is nothing left for it
        #    to defer. What still has to hold is that it does not resize
        #    anything mid-veil, and that a resize does defer.
        before = disp.screen.get_size()
        disp.set_window_layer(10, 10, 640, 360)
        if disp.screen.get_size() != before:
            failures.append("set_window_layer shrank the layer mid-veil")
        if disp._window_layer != (10, 10, 640, 360):
            failures.append("the window position was not recorded")
        disp.resize(1280, 720)
        if disp._switch_pending is None:
            failures.append("a resize during the veil was not deferred")

        # 9. exit_switch_mode starts the fade-OUT - the layer is not given
        #    back yet (it used to snap back instantly).
        disp.exit_switch_mode()
        if not disp.is_switch_active():
            failures.append("exit_switch_mode dropped the veil instantly - "
                            "the fade-out never runs")
        if disp._switch_phase != "out":
            failures.append("exit_switch_mode did not enter the fade-out")

        # 10. It completes: the veil releases the layer, the deferred
        #     resize lands, and the frozen copies are dropped.
        disp._finish_switch_if_due(time.monotonic() + D.SWITCH_FADE_OUT + 0.01)
        if disp.is_switch_active():
            failures.append("the fade-out never completed")
        if disp._switch_pending is not None:
            failures.append("the deferred resize survived the teardown")
        if disp.screen.get_size() != (1280, 720):
            failures.append(
                f"the deferred resize did not land: "
                f"{disp.screen.get_size()}")
        if disp._switch_base is not None or disp._switch_dim_soft is not None:
            failures.append("the frozen copies survived the teardown")

        # 11. A second enter during the fade-out brings the veil back to
        #     full instead of letting it dissolve under the rebuild.
        disp.enter_switch_mode(frame, W, H)
        disp.exit_switch_mode()
        disp._finish_switch_if_due(time.monotonic() + D.SWITCH_FADE_OUT * 0.5)
        disp.enter_switch_mode(frame, W, H)
        if disp._switch_phase != "on" or disp._switch_alpha != float(D.SWITCH_ALPHA):
            failures.append("the veil did not come back to full when "
                            "re-entered mid-fade")
    finally:
        disp.close()

    # -- Part C: follow_window keeps its hands off the veil ---------
    class _Menu:
        visible = False

    class _VeilDisplay:
        def __init__(self):
            self.menu = _Menu()
            self.moved_to = None
            self.veil = True

        def is_switch_active(self):
            return self.veil

        def is_visible(self):
            return True

        def set_visible(self, on):
            pass

        def move_to(self, x, y):
            self.moved_to = (x, y)

        def raise_topmost(self):
            pass

    rect = (200, 200, 500, 400)
    st = types.SimpleNamespace(
        window_hwnd=0x1234, worker_failed=False,
        width=rect[2], height=rect[3],
        follow_pos=(100, 100), follow_resize=None, follow_size=rect[2:],
        frame_index=1, display=_VeilDisplay())

    real_rect, real_switch, real_time = (
        pipeline.window_frame_rect, pipeline.switch_window, pipeline.time)
    pipeline.window_frame_rect = lambda hwnd: rect
    pipeline.switch_window = lambda s, hwnd: None
    pipeline.time = types.SimpleNamespace(monotonic=lambda: 1000.0)
    try:
        st.display.veil = True
        pipeline.follow_window(st)
        if st.display.moved_to is not None:
            failures.append(
                "follow_window moved the layer while the veil was up - "
                "this is the reported window-mode slide")
        st.display.veil = False
        pipeline.follow_window(st)
        if st.display.moved_to != (rect[0], rect[1]):
            failures.append(
                f"after the veil the layer did not re-sync: "
                f"{st.display.moved_to}")
    finally:
        pipeline.window_frame_rect = real_rect
        pipeline.switch_window = real_switch
        pipeline.time = real_time

    print("=" * 60)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the veil eases in, owns the layer, and eases back out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
