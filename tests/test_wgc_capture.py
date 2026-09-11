"""The worker captures ONE WINDOW (WGCW) and the pipeline delivers its pixels.

Step 1 of the single-window mode. Until now the worker's only capture source
was Desktop Duplication of the whole screen, which forced the overlay to hide
from screen capture (otherwise the pipeline would capture its own output) -
and that hiding is what stops OBS from seeing the overlay and stops the NVIDIA
App from recording at all.

This test drives the worker directly, with no menu and no main.py loop: it
raises a borderless window of a known colour, hands the worker its HWND, and
demands that the frames coming back out of the pipeline be that window's
content. The capture size in the acknowledgement has to match the window too -
Windows Graphics Capture works in physical pixels, and a mismatch there means
the client would size its textures wrong on a scaled display.

Not covered here: that the capture ignores whatever is drawn on top of the
window. It was measured in the spike (_work/probe_wgc.cpp: a fullscreen
overlay over the target contributes 0% of the captured pixels) and gets its
regression when the overlay itself moves onto the window - there is no second
window to cover the target with at this stage.

Run:  runtime\\python.exe test_wgc_capture.py
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

from main import (FRAME_FLAG_NO_COLOR, FRAME_FLAG_WANT_PIXELS,  # noqa: E402
                  FRAME_FMT, FRAME_MAGIC, HEADER_FMT, OUT_FMT, OUT_MAGIC,
                  PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 960, 540
WARMUP = 8

# WGCW: capture one window. Mirrors VideoWgcCmd / VideoWgcAck in the worker.
WGC_MAGIC = 0x57434757      # 'WGCW'
WGC_ACK_MAGIC = 0x4B414757  # 'WGAK'
WGC_FMT = "<4IqQ"           # magic, width, height, flags, pts, hwnd
WGC_ACK_FMT = "<4Iq"        # magic, ok, width, height, pts

# The window's own colour. Neural rendering enhances what it is given, so a
# flat colour comes back as very nearly the same colour - but not bit for bit,
# hence a tolerance rather than an equality.
TARGET = (31, 97, 211)
TOL = 30


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def send_wgc(worker, hwnd: int) -> tuple:
    """Ask for window capture (hwnd=0 turns it off) and read the ack."""
    worker.stdin.write(struct.pack(WGC_FMT, WGC_MAGIC, W, H, 0, 0, int(hwnd)))
    worker.stdin.flush()
    ack = read_exact(worker.stdout, struct.calcsize(WGC_ACK_FMT))
    magic, ok, aw, ah, _pts = struct.unpack(WGC_ACK_FMT, ack)
    if magic != WGC_ACK_MAGIC:
        raise AssertionError(f"foreign reply to WGCW: 0x{magic:08X}")
    return ok, aw, ah


def send_capture_frame(worker, index: int, motion: np.ndarray) -> None:
    """A capture-mode frame: motion only, and give the pixels back."""
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0,
                                   FRAME_FLAG_NO_COLOR | FRAME_FLAG_WANT_PIXELS,
                                   index))
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def recv_result(worker):
    """The result pixels, or None when the worker had no captured frame yet."""
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC:
        raise AssertionError(f"foreign reply 0x{magic:08X}")
    if not ok or nbytes == 0:
        return None                      # nothing captured yet - try again
    data = read_exact(worker.stdout, nbytes)
    return np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy()


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1

    import pygame
    pygame.init()
    # Borderless: with a title bar the capture item covers the whole window
    # frame and would not match the client area we size everything from.
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen WGC test target")
    hwnd = pygame.display.get_wm_info()["window"]

    def repaint(tick: int) -> None:
        # A window that never redraws produces exactly one capture frame and
        # then silence, so the colour wobbles by a few levels - well inside
        # the tolerance, but enough for the compositor to send a new frame.
        wob = tick % 7
        screen.fill((TARGET[0] + wob, TARGET[1], TARGET[2] - wob))
        pygame.display.flip()
        pygame.event.pump()

    for i in range(20):
        repaint(i)
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
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        motion = np.zeros((H, W, 2), dtype=np.float16)

        ok, aw, ah = send_wgc(worker, hwnd)
        print(f"WGCW: ok={ok}, capture size {aw}x{ah} (window {W}x{H})")
        if not ok:
            print("FAIL: the worker refused to capture the window")
            return 1
        if (aw, ah) != (W, H):
            failures.append(f"the capture is {aw}x{ah}, the window is {W}x{H}")

        # 60 attempts, not 40: a window that has not repainted yet returns no
        # frame at all, and one suite run out of several failed on exactly
        # that.
        #
        # Every frame that comes back is measured, and the verdict is the
        # MEDIAN rather than the last one. The window repaints between
        # sends, so a frame can be caught mid-flip and come back half the
        # old colour - that is this test's own timing, not the worker's, and
        # judging the run by whichever frame happened to arrive last made it
        # flake at 94.4% against a 95% threshold. A median still fails hard
        # if the capture is actually wrong: broken frames are all of them,
        # not one.
        shares = []
        pixels = None
        for i in range(60):
            repaint(i)
            send_capture_frame(worker, i, motion)
            got = recv_result(worker)
            if got is not None:
                pixels = got
                dist = np.abs(got[..., :3].astype(np.int16) -
                              np.array(TARGET, dtype=np.int16)).max(axis=2)
                shares.append(float((dist <= TOL).mean()))
            time.sleep(0.01)
        if pixels is None:
            print("FAIL: not a single frame came back out of the pipeline")
            return 1

        mean = pixels[..., :3].reshape(-1, 3).mean(axis=0)
        share = float(np.median(shares))
        print(f"result mean RGB ({mean[0]:.0f}, {mean[1]:.0f}, {mean[2]:.0f}), "
              f"{len(shares)} frames within {TOL} of the window colour: "
              f"median {share * 100:.1f}%, worst {min(shares) * 100:.1f}%, "
              f"best {max(shares) * 100:.1f}%")
        if share < 0.95:
            failures.append(f"the frames are not the window's content "
                            f"(median only {share * 100:.1f}% of pixels match it)")

        off_ok, _w, _h = send_wgc(worker, 0)
        print(f"WGCW off: ok={off_ok}")
        if not off_ok:
            failures.append("the worker did not acknowledge switching the capture off")
    finally:
        try:
            worker.stdin.close()
        except Exception:
            pass
        worker.wait(timeout=10)
        pygame.quit()
        err = worker.stderr.read().decode("utf-8", "replace")
        tail = [l for l in err.splitlines() if "[wgc]" in l or "[cap]" in l]
        if tail:
            print("worker log:", " | ".join(tail[-4:]))

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the worker captures the window and the pipeline returns its pixels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
