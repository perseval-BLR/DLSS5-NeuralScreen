"""Static frames are skipped, and the network idles instead of re-running.

An idle desktop (Desktop Duplication timeout) or an unredrawn window (empty
WGC pool) used to be a FULL load: the worker kept the previous texture and
ran the network on it again, frame after frame - 66 FPS of pure waste on a
still screen (issue #31 territory).

The skip rides in the frame header as FRAME_FLAG_SKIP_STATIC, so it is a
toggle, not a rebuild. A frame whose OUTPUT would change is never skipped:
NR ON/OFF, the wipe position, a fresh feature (RNSZ), a re-opened window -
and WANT_PIXELS wins, because a screenshot or a recording wants the picture
even when it did not change.

Driven through the real worker, with one window we own as the capture
source: the window redraws only when THIS test flips it, so "no new frame"
is deterministic, not desktop weather.

Checked:
* protocol: the flag packs into the frame header, and only when asked;
* a static source with the flag on: the answer is an empty result (the
  network did not run) and the worker says so once in the log;
* the source redraws: the next frame is processed and the log counts the
  skipped stretch;
* the flag off on the same static source: the old behavior, a full frame;
* want_pixels on a static source with the flag on: the frame comes back.

Run:  runtime\\python.exe tests\\test_skip_static.py
"""
import ctypes
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

# Physical pixels, before pygame loads: SDL freezes the process DPI awareness
# at import, and a window measured in logical units would not match what the
# capture produces.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from main import (FRAME_FLAG_NO_COLOR, FRAME_FLAG_SKIP_STATIC,  # noqa: E402
                  FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC, HEADER_FMT,
                  OUT_FMT, OUT_MAGIC, PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 960, 540
WARMUP = 8

# WGCW: capture one window. Mirrors VideoWgcCmd / VideoWgcAck in the worker.
WGC_MAGIC = 0x57434757      # 'WGCW'
WGC_ACK_MAGIC = 0x4B414757  # 'WGAK'
WGC_FMT = "<4IqQ"           # magic, width, height, flags, pts, hwnd
WGC_ACK_FMT = "<4Iq"        # magic, ok, width, height, pts

TARGET = (31, 97, 211)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def send_wgc(worker, hwnd: int) -> tuple:
    worker.stdin.write(struct.pack(WGC_FMT, WGC_MAGIC, W, H, 0, 0, int(hwnd)))
    worker.stdin.flush()
    ack = read_exact(worker.stdout, struct.calcsize(WGC_ACK_FMT))
    magic, ok, aw, ah, _pts = struct.unpack(WGC_ACK_FMT, ack)
    if magic != WGC_ACK_MAGIC:
        raise AssertionError(f"foreign reply to WGCW: 0x{magic:08X}")
    return ok, aw, ah


def send_capture_frame(worker, index: int, motion: np.ndarray,
                       skip: bool, want: bool = False) -> None:
    """A capture-mode frame: motion only, flags as asked."""
    flags = FRAME_FLAG_NO_COLOR
    if skip:
        flags |= FRAME_FLAG_SKIP_STATIC
    if want:
        flags |= FRAME_FLAG_WANT_PIXELS
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0, flags, index))
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def recv_result(worker):
    """('full', pixels) | ('empty', None) - the worker's answer to one frame."""
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC:
        raise AssertionError(f"foreign reply 0x{magic:08X}")
    if not ok:
        raise AssertionError(f"the worker answered ok=0, ngx=0x{ngx:08X}")
    if nbytes == 0:
        return "empty", None
    data = read_exact(worker.stdout, nbytes)
    return "full", np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy()


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen skip-static test target")
    hwnd = pygame.display.get_wm_info()["window"]

    tick = [0]

    def repaint() -> None:
        tick[0] += 1
        wob = tick[0] % 7
        screen.fill((TARGET[0] + wob, TARGET[1], TARGET[2] - wob))
        pygame.display.flip()
        pygame.event.pump()

    for _ in range(10):
        repaint()
        time.sleep(0.01)

    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(params.get("style", 0)),
                         int(params.get("auto_mask", 0)),
                         int(params.get("ui_correction", 0)),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    motion = np.zeros((H, W, 2), dtype=np.float16)
    try:
        worker.stdin.write(header)
        worker.stdin.flush()

        ok, aw, ah = send_wgc(worker, hwnd)
        if not ok or (aw, ah) != (W, H):
            print(f"FAIL: the worker refused the window ({ok}, {aw}x{ah})")
            return 1

        # Seed: keep repainting until a real frame comes back (the pool may
        # be empty right after OpenWgc).
        seeded = False
        for i in range(40):
            repaint()
            send_capture_frame(worker, i, motion, skip=False)
            state, _ = recv_result(worker)
            if state == "full":
                seeded = True
                break
            time.sleep(0.01)
        if not seeded:
            print("FAIL: not a single captured frame came back out of the pipeline")
            return 1

        # 1. Static source, skip ON: the answers are empties, the network
        #    does not run. The window is NOT repainted from here on.
        idx = 100
        answers = []
        for _ in range(16):
            send_capture_frame(worker, idx, motion, skip=True)
            state, _ = recv_result(worker)
            answers.append(state)
            idx += 1
            time.sleep(0.03)
        tail = answers[-8:]
        if any(s == "full" for s in tail):
            failures.append(f"a static source produced processed frames: {answers}")
        if "empty" not in answers:
            failures.append("not one frame was skipped on a static source")

        # 2. The source redraws: the frame is processed again.
        repaint()
        time.sleep(0.15)
        send_capture_frame(worker, idx, motion, skip=True)
        state, _ = recv_result(worker)
        idx += 1
        if state != "full":
            failures.append("after a redraw the frame must be processed again")

        # 3. Same static source, flag OFF: the old behavior - a full frame.
        send_capture_frame(worker, idx, motion, skip=False)
        state, _ = recv_result(worker)
        idx += 1
        if state != "full":
            failures.append("with the flag off the frame must be processed (old behavior)")

        # 4. Static source, skip ON, but WANT_PIXELS: the picture comes back
        #    anyway - a screenshot or a recording must not lose its frame.
        send_capture_frame(worker, idx, motion, skip=True, want=True)
        state, frame = recv_result(worker)
        idx += 1
        if state != "full" or frame is None:
            failures.append("want_pixels must win over the skip")

        send_wgc(worker, 0)
    finally:
        try:
            worker.stdin.close()
        except Exception:
            pass
        worker.wait(timeout=10)
        pygame.quit()
        err = worker.stderr.read().decode("utf-8", "replace")
        skip_lines = [l for l in err.splitlines() if "[skip]" in l]
        if skip_lines:
            print("worker log:", " | ".join(skip_lines[:4]))
        if not any("no new frame" in l for l in skip_lines):
            failures.append("the worker never logged the idle state ([skip] no new frame)")
        if not any("screen changed" in l for l in skip_lines):
            failures.append("the worker never logged the resume ([skip] the screen changed)")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: static frames are skipped, transitions and want_pixels are not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
