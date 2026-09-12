"""Neural Rendering at the work resolution: the picture survives, the menu drives it.

Two halves, because the feature has two halves that fail differently.

The worker half feeds a detailed frame through the reduced-resolution path and
checks the result is a real picture. That check exists for a specific reason:
the first working version produced a completely blank frame while reporting a
52% FPS gain, because both scaling passes shared two descriptors in one command
list and the GPU read them after both had been recorded. Nothing but looking at
the pixels catches that.

The menu half checks the controls actually emit the actions main listens for,
and that the slider stops where the work size hits its cap instead of running
into a dead top end.
"""
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, NATIVE_DIR, OUT_FMT, OUT_MAGIC, PROFILES,
                  RACK_FMT, RESIZE_ACK_MAGIC, RESIZE_FLAG_NR_SMALL, RESIZE_FMT,
                  RESIZE_MAGIC, VIDEO_MAGIC, WORKER_EXE)

FULL_W, FULL_H = 1920, 1080
WORK_W, WORK_H = 1280, 720
WARMUP = 8
FRAMES = 4


def make_frame(w: int, h: int) -> np.ndarray:
    """Detail everywhere: on flat colour a broken scaler looks just like a good one."""
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    f = np.zeros((h, w, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    f[..., 0] = base
    f[..., 1] = (base // 2 + rng.integers(0, 48, (h, w), dtype=np.uint8))
    f[..., 2] = (255 - base).astype(np.uint8)
    f[..., 3] = 255
    return np.ascontiguousarray(f)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def run_worker(small: bool) -> dict:
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        FULL_W, FULL_H)
    frame = make_frame(FULL_W, FULL_H)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)
    body = frame.tobytes() + motion.tobytes()

    env = dict(os.environ)
    env["NS_NR_SMALL"] = "1" if small else "0"
    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                            env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = None
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        for i in range(FRAMES):
            proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                         1 if i == 0 else 0,
                                         FRAME_FLAG_WANT_PIXELS, i))
            proc.stdin.write(body)
            proc.stdin.flush()
            head = read_exact(proc.stdout, struct.calcsize(OUT_FMT))
            magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
            if magic != OUT_MAGIC or not ok:
                raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} ngx=0x{ngx:08X}")
            if nbytes:
                data = read_exact(proc.stdout, nbytes)
                out = np.frombuffer(data, dtype=np.uint8).reshape(FULL_H, FULL_W, 4).copy()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")
    nr_line = next((l for l in err.splitlines() if "[nr]" in l), "")
    return {"out": out, "nr": nr_line.strip(), "log": err, "input": frame}


def detail(img: np.ndarray) -> float:
    """Variance of a Laplacian - zero means a flat frame, which is the bug."""
    g = img[..., :3].astype(np.float32).mean(axis=2)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
           + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def check_worker(failures: list) -> None:
    base = run_worker(small=False)
    if base["out"] is None:
        failures.append("no pixels came back with the mode off")
        return
    small = run_worker(small=True)
    if small["out"] is None:
        failures.append("no pixels came back with the mode on")
        return

    d_in = detail(base["input"])
    d_base = detail(base["out"])
    d_small = detail(small["out"])
    print(f"detail: input {d_in:.0f}, full-res NR {d_base:.0f}, reduced NR {d_small:.0f}")
    print(f"worker: {small['nr'] or '(no [nr] line)'}")

    if small["out"].shape != (FULL_H, FULL_W, 4):
        failures.append(f"reduced mode returned {small['out'].shape}, "
                        f"expected the full size")
    if "[nr] network runs at" not in small["nr"]:
        failures.append("the worker did not report running the network smaller")
    # The blank-frame bug: a flat result, which no FPS number would reveal.
    # The margin is generous on purpose. The test pattern changes by 7 levels
    # per pixel - almost Nyquist - so a 1.5x downscale wipes out far more of it
    # than real desktop content loses (measured at 4K: 4074 -> 1057, a quarter,
    # against a fifteenth here). The bug being guarded against gives exactly
    # 0.0, so there is no need to sit close to the real value.
    if d_small < d_in * 0.02:
        failures.append(f"the reduced-resolution frame is flat ({d_small:.1f} "
                        f"against {d_in:.1f} in the input) - scaling is broken")
    # It must still resemble the input rather than being noise or a shifted copy.
    diff = float(np.abs(small["out"][..., :3].astype(np.int16)
                        - base["input"][..., :3].astype(np.int16)).mean())
    print(f"mean |reduced - input|: {diff:.1f} of 255")
    if diff > 40:
        failures.append(f"the reduced-resolution frame does not resemble the "
                        f"input (mean difference {diff:.1f})")


def check_menu(failures: list) -> None:
    """Every control on screen does something.

    The menu carried a toggle and a slider for the same idea once, and with
    the toggle off the slider still moved while changing the picture by
    exactly nothing - the outputs come back bit-identical at every position
    when the reduced mode is off (measured on four real 4K frames, 12.09).
    The two were merged into one slider for that reason, and split again when
    the mode became Boost: a switch people can find, and the slider only on
    screen while it would do something.

    So: with Boost off there is a switch and no slider at all; with Boost on
    the slider is there, it stops at the cap rather than one step past it,
    and dragging it reports a resolution instead of being mistaken for an NR
    parameter.
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    import overlay_ui

    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        def font_loader(size):
            try:
                return pygame.font.SysFont("consolas", size)
            except Exception:
                return pygame.font.Font(None, size)

        CAP = 0.65
        menu = overlay_ui.OverlayMenu(1.0, font_loader)
        menu.set_state({"nr_small": False, "work_scale": 0.50,
                        "work_scale_cap": CAP, "work_size": "2560x1440",
                        "screen_size": "3840x2160",
                        "profiles": ["Faithful"], "profile": "Faithful",
                        "params": {"intensity": 1.0, "local_tone": 1.0,
                                   "local_structure": 1.0, "skin_structure": 1.0}})
        menu.visible = True
        surf = pygame.Surface((1920, 1080))
        menu.draw(surf)

        items = {i.key: i for i in menu.items}
        if "work_scale" in items:
            failures.append("the old work_scale slider is still in the menu")
        if "boost" not in items:
            failures.append("no Boost switch in the menu")
            return
        if items["boost"].value > 0.5:
            failures.append("the Boost switch reads on while the mode is off")
        # The slider would be inert here: the network runs at the full frame
        # size whatever it says.
        if "nr_res" in items:
            failures.append("the resolution slider is on screen with Boost off, "
                            "where every position of it does the same thing")
        # The switch itself, not the caption under it: a hint makes the row
        # taller and is deliberately not a hit target, so that a stray click
        # on the explanation cannot restart the worker.
        got = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            {"pos": (items["boost"].rect.x + 4, items["boost"].rect.y + 2),
             "button": 1}))
        print(f"clicked Boost: {got}")
        if ("toggle", "boost") not in got:
            failures.append(f"the Boost switch emitted {got}, not a boost toggle")
        menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, {"button": 1}))

        # Boost on: now the resolution is a real choice.
        menu.set_state({"nr_small": True, "work_scale": 0.50,
                        "work_size": "1920x1080"})
        menu.draw(surf)
        items = {i.key: i for i in menu.items}
        if "nr_res" not in items:
            failures.append("no resolution slider with Boost on")
            return

        ws = items["nr_res"]
        print(f"slider {ws.lo:.2f}..{ws.hi:.2f}, at {ws.value:.2f} "
              f"showing {ws.extra.get('value_text')!r}")
        if abs(ws.hi - CAP) > 1e-6:
            failures.append(f"the slider runs to {ws.hi:.2f}; with the switch "
                            f"carrying the mode it should stop at the {CAP:.2f} cap")
        if abs(ws.value - 0.50) > 1e-6:
            failures.append(f"the knob is at {ws.value:.2f}, not at the work "
                            f"scale it was given")
        # The label shows the size the network RUNS at. It used to read
        # "3840x2160 - full" at the top and promise a resolution NGX cannot
        # deliver (user, 12.09).
        shown = str(ws.extra.get("value_text"))
        if "1920x1080" not in shown:
            failures.append(f"the slider shows {shown!r}, not the work size "
                            f"the network runs at")
        if "3840x2160" in shown:
            failures.append(f"the slider still promises the screen size: {shown!r}")

        # Drag to the left end: a resolution action, and not an NR parameter.
        track = ws.extra.get("track")
        got = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": (track.x + 1, track.centery),
                                     "button": 1}))
        kinds = [g[0] for g in got]
        print(f"dragged to the left end: {got}")
        if "nr_res" not in kinds:
            failures.append(f"the slider emitted {kinds}, not a resolution action")
        if "param" in kinds:
            failures.append("the slider was taken for an NR parameter")
        if got and got[0][1] > CAP:
            failures.append(f"the left end gave {got[0][1]:.2f}, above the cap")

        # And back to the right end: the cap, and not a step past it - the
        # step past the cap used to mean "off", and the switch owns that now.
        menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, {"button": 1}))
        menu.draw(surf)
        ws = {i.key: i for i in menu.items}["nr_res"]
        track = ws.extra.get("track")
        got = menu.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, {"pos": (track.right - 1, track.centery),
                                     "button": 1}))
        print(f"dragged to the right end: {got}")
        if not got or got[0][0] != "nr_res":
            failures.append(f"the right end emitted {got}")
        elif abs(got[0][1] - CAP) > 1e-6:
            failures.append(f"the right end gave {got[0][1]:.2f} instead of the "
                            f"{CAP:.2f} cap")
    finally:
        pygame.quit()


def check_live_switch(failures: list) -> None:
    """The mode changes in a running worker, without restarting it.

    This is the point of carrying the flag in the resize. It used to live in an
    environment variable read once at startup, which is why the menu had a
    separate toggle at all - and why that toggle could not simply be folded
    into the slider.
    """
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        FULL_W, FULL_H)
    frame = make_frame(FULL_W, FULL_H)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)
    body = frame.tobytes() + motion.tobytes()
    env = dict(os.environ, NS_NR_SMALL="0")     # start in full-screen mode

    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                            env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pump(index):
        proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                     1 if index == 0 else 0,
                                     FRAME_FLAG_WANT_PIXELS, index))
        proc.stdin.write(body)
        proc.stdin.flush()
        head = read_exact(proc.stdout, struct.calcsize(OUT_FMT))
        magic, _i, ok, nbytes, _n, _p = struct.unpack(OUT_FMT, head)
        if magic != OUT_MAGIC or not ok:
            raise RuntimeError("bad reply")
        if not nbytes:
            return None
        data = read_exact(proc.stdout, nbytes)
        return np.frombuffer(data, dtype=np.uint8).reshape(FULL_H, FULL_W, 4).copy()

    before = after = None
    acked = False
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        for i in range(FRAMES):
            before = pump(i)

        # The same sizes, only the flag changes.
        proc.stdin.write(struct.pack(
            RESIZE_FMT, RESIZE_MAGIC, WORK_W, WORK_H, WARMUP,
            RESIZE_FLAG_NR_SMALL,
            0, 0, int(params["style"]), int(params["auto_mask"]),
            int(params["ui_correction"]),
            float(params["intensity"]), float(params["local_tone"]),
            float(params["local_structure"]), float(params["skin_structure"]),
            FULL_W, FULL_H))
        proc.stdin.flush()
        ack = read_exact(proc.stdout, struct.calcsize(RACK_FMT))
        magic, ok, ngx, _r, _p = struct.unpack(RACK_FMT, ack)
        acked = magic == RESIZE_ACK_MAGIC and bool(ok)
        print(f"resize with the flag: acked={acked} ngx=0x{ngx:08X}")

        for i in range(FRAMES, FRAMES * 2):
            after = pump(i)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")

    if not acked:
        failures.append("the worker did not accept the resize carrying the flag")
    if "[nr] network runs at" not in err:
        failures.append("the worker never switched to running the network smaller")
    restarts = err.count("standalone D3D12 device ready")
    print(f"worker device inits: {restarts} (1 = it was never restarted)")
    if restarts != 1:
        failures.append(f"the worker started {restarts} times - the mode change "
                        f"should not need a restart")
    if before is None or after is None:
        failures.append("no pixels around the switch")
        return
    if np.array_equal(before, after):
        failures.append("the picture is identical before and after the switch - "
                        "the flag did nothing")
    else:
        d = float(np.abs(before[..., :3].astype(np.int16)
                         - after[..., :3].astype(np.int16)).mean())
        print(f"picture changed on the switch: mean |diff| {d:.1f} of 255")


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    failures: list = []
    check_worker(failures)
    check_live_switch(failures)
    check_menu(failures)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: real picture, mode switches in a live worker, one control drives it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
