"""Pixels asked for right after a resize must not come back empty.

The capture branch has an early exit for "no new frame AND nothing captured
yet", which answers an empty result so the network is never handed an empty
colour. g_dda_ready - the flag behind it - is cleared by every RNSZ and by
every capture restart, and set again only by the next captured frame. On a
screen that is not changing, that next frame never comes: duplication
answers WAIT_TIMEOUT and a WGC pool stays empty until the window redraws.

So a frame carrying WANT_PIXELS inside that window got bytes=0. A
screenshot survives it - Python keeps the request and the next frame saves
it - but the recorder feeds only what it is given, so the recording ends up
with a hole exactly where the resize was (audit cpp-worker).

It matters more since window-mode resizes stopped restarting the worker:
every fullscreen toggle in a captured window is now an RNSZ, which is
precisely the moment this hits.

The test is the one the audit asked for: a static target window, a real
size change, then one WANT_PIXELS frame before anything repaints. The
reply has to carry pixels.

Run:  runtime\\python.exe tests\\test_pixels_after_resize.py
"""
import ctypes
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

import pygame  # noqa: E402
import protocol as wire  # noqa: E402
from main import (FRAME_FLAG_NO_COLOR, FRAME_FLAG_WANT_PIXELS,  # noqa: E402
                  FRAME_FMT, FRAME_MAGIC, HEADER_FMT, OUT_FMT, OUT_MAGIC,
                  PROFILES, RACK_FMT, RESIZE_ACK_MAGIC, RESIZE_FMT,
                  RESIZE_MAGIC, VIDEO_MAGIC, WGC_ACK_FMT, WGC_ACK_MAGIC,
                  WORKER_EXE)

W, H = 640, 360
TARGET = (40, 150, 220)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError("the worker closed stdout")
        buf += chunk
    return buf


def send_frame(proc, index, motion, want_pixels):
    flags = FRAME_FLAG_NO_COLOR | (FRAME_FLAG_WANT_PIXELS if want_pixels else 0)
    proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                 1 if index == 0 else 0, flags, index))
    proc.stdin.write(motion.tobytes())
    proc.stdin.flush()
    magic, _i, ok, nbytes, _n, _p = struct.unpack(
        OUT_FMT, read_exact(proc.stdout, struct.calcsize(OUT_FMT)))
    if magic != OUT_MAGIC:
        raise RuntimeError(f"foreign reply: 0x{magic:08X}")
    data = read_exact(proc.stdout, nbytes) if nbytes else b""
    return ok, nbytes, data


def send_resize(proc, w, h, params):
    proc.stdin.write(struct.pack(
        RESIZE_FMT, RESIZE_MAGIC, w, h, 4, 0, 0, 0,
        int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]), float(params["intensity"]),
        float(params["local_tone"]), float(params["local_structure"]),
        float(params["skin_structure"]), 0, 0))
    proc.stdin.flush()
    magic, ok, _n, _r, _p = struct.unpack(
        RACK_FMT, read_exact(proc.stdout, struct.calcsize(RACK_FMT)))
    if magic != RESIZE_ACK_MAGIC:
        raise RuntimeError(f"foreign reply to RNSZ: 0x{magic:08X}")
    return ok


def main() -> int:
    failures = []
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    screen.fill(TARGET)
    pygame.display.flip()
    hwnd = pygame.display.get_wm_info()["window"]
    for _ in range(20):            # let the window settle and paint once
        pygame.event.pump()
        time.sleep(0.02)

    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    log.close()
    with open(log.name, "wb") as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"],
                                cwd=str(WORKER_EXE.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            wire.send_wgc(proc, hwnd)
            ack = struct.unpack(WGC_ACK_FMT,
                                read_exact(proc.stdout, struct.calcsize(WGC_ACK_FMT)))
            if ack[0] != WGC_ACK_MAGIC or not ack[1]:
                print(f"FAIL: the worker refused the window: {ack}")
                return 1
            cap_w, cap_h = ack[2], ack[3]
            motion = np.zeros((H, W, 2), dtype=np.float16)

            # 1. Run until the capture really has a frame.
            got_pixels = 0
            for i in range(30):
                pygame.event.pump()
                ok, nbytes, _d = send_frame(proc, i, motion, want_pixels=True)
                if nbytes:
                    got_pixels += 1
                    if got_pixels >= 2:
                        break
                time.sleep(0.03)
            if got_pixels < 2:
                failures.append("the capture never produced pixels at all - "
                                "nothing was measured")

            # 2. A real size change: this is what clears the flag. Nothing
            #    repaints the target window from here on.
            work_w, work_h = 480, 270
            if not send_resize(proc, work_w, work_h, params):
                failures.append("the worker refused the resize")

            # 3. The very next frame asks for pixels, before any repaint.
            #    The motion field has to be the NEW work size: the worker
            #    reads exactly that many bytes, and sending the old one
            #    desynchronises the stream (which is how this test first
            #    killed the worker instead of measuring it).
            small_motion = np.zeros((work_h, work_w, 2), dtype=np.float16)
            ok, nbytes, data = send_frame(proc, 100, small_motion, want_pixels=True)
            # The resize was asked for at 1:1 (no full size), so the frame
            # the worker hands back is the new WORK size, not the capture.
            expect = work_w * work_h * 4
            print(f"    after the resize: ok={ok}, bytes={nbytes} "
                  f"(a {work_w}x{work_h} frame is {expect}; "
                  f"the window is {cap_w}x{cap_h})")
            if not nbytes:
                failures.append("the frame right after a resize came back "
                                "EMPTY while asking for pixels - that is the "
                                "hole in the recording")
            elif nbytes != expect:
                failures.append(f"pixels came back at the wrong size: "
                                f"{nbytes}, expected {expect}")
            else:
                pixels = np.frombuffer(data, np.uint8).reshape(work_h, work_w, 4)
                if int(pixels[..., :3].max()) == 0:
                    failures.append("the pixels after the resize are black - "
                                    "an empty texture was handed over")
            proc.stdin.close()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            pygame.quit()

    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    Path(log.name).unlink(missing_ok=True)
    for line in text.splitlines():
        if "pixels asked for before" in line:
            print("   ", line.strip())

    for f in failures:
        print("FAIL:", f)
    if failures:
        print(text[-900:])
        return 1
    print("OK: a resize does not put a hole in what the client is given")
    return 0


if __name__ == "__main__":
    sys.exit(main())
