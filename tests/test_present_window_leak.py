"""Closing the worker's picture window must actually destroy it.

The audit read ClosePresent and predicted a leak: WM_QUIT went to the
present thread FIRST, ending its message loop before the WM_CLOSE behind
it could be dispatched, and the DestroyWindow that followed ran on the
worker thread - which fails, silently, because a thread cannot destroy
another thread's window. The handle was nulled anyway, so nothing was
left that could ever take the window down. One per mode switch.

MEASURED: that does not happen. Windows destroys the windows a thread
owns when the thread exits, and the thread does exit - so the old code
leaked nothing. This test was run against the pre-fix worker on purpose
and passed there too. It is a guard on the property, not a proof of the
fix, and it is kept because the property is worth guarding: the HUD is
kept above the picture by finding that window by class and title,

    FindWindowW(L"NeuralScreenPresent", L"NeuralScreen")

which returns whichever one Windows lists first. A leak here would quietly
aim the z-order raise at a window that shows nothing.

What the fix does change is the stuck case: if the thread does not exit
within the timeout, the old code forgot the handle and moved on. Now the
window is destroyed by its own thread, and a thread that will not go says
so in the log - which this test also checks for.

Drives the real worker: open the picture window, close it, six times, and
count the windows of that class belonging to the worker process. One while
it is open, none after it is closed, and no more than one ever.

Run:  runtime\\python.exe tests\\test_present_window_leak.py
"""
import ctypes
import os
import struct
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from main import (FRAME_FMT, FRAME_MAGIC, HEADER_FMT, OUT_FMT,  # noqa: E402
                  PROFILES, VIDEO_MAGIC, WINDOW_ACK_FMT, WINDOW_ACK_MAGIC,
                  WINDOW_FMT, WINDOW_MAGIC, WORKER_EXE)

W, H = 640, 360
CYCLES = 6
user32 = ctypes.windll.user32


def present_windows(pid: int) -> int:
    """How many NeuralScreenPresent windows this process owns."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "NeuralScreenPresent":
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return len(found)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError("the worker closed stdout")
        buf += chunk
    return buf


def send_window(proc, w: int, h: int, flags: int) -> int:
    proc.stdin.write(struct.pack(WINDOW_FMT, WINDOW_MAGIC, w, h, flags, 0))
    proc.stdin.flush()
    magic, ok, _a, _b, _pts = struct.unpack(
        WINDOW_ACK_FMT, read_exact(proc.stdout, struct.calcsize(WINDOW_ACK_FMT)))
    if magic != WINDOW_ACK_MAGIC:
        raise RuntimeError(f"foreign reply to WNDO: 0x{magic:08X}")
    return ok


def main() -> int:
    failures = []
    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 2] = 200
    frame[..., 3] = 255
    motion = np.zeros((H, W, 2), dtype=np.float16)

    log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    log.close()
    counts = []
    with open(log.name, "wb") as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"],
                                cwd=str(WORKER_EXE.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            for cycle in range(CYCLES):
                if not send_window(proc, W, H, 0):
                    failures.append(f"cycle {cycle}: the worker refused WNDO")
                    break
                # One frame, so the window is not just created but used.
                proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, cycle,
                                             1 if cycle == 0 else 0, 0, cycle))
                proc.stdin.write(frame.tobytes())
                proc.stdin.write(motion.tobytes())
                proc.stdin.flush()
                read_exact(proc.stdout, struct.calcsize(OUT_FMT))
                open_count = present_windows(proc.pid)
                send_window(proc, 0, 0, 0)      # WNDO with no size: close it
                time.sleep(0.25)                # the thread has to finish
                closed_count = present_windows(proc.pid)
                counts.append((open_count, closed_count))
            proc.stdin.close()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    Path(log.name).unlink(missing_ok=True)

    print(f"    windows (open, after close) per cycle: {counts}")
    for i, (opened, closed) in enumerate(counts):
        if opened != 1:
            failures.append(f"cycle {i}: {opened} picture windows while open, "
                            f"expected exactly 1")
        if closed != 0:
            failures.append(f"cycle {i}: {closed} picture windows left after "
                            f"the close - a hidden topmost window survived, "
                            f"and FindWindowW can hand it to the HUD raise")
    if "the window thread did not exit on WM_CLOSE" in text:
        failures.append("the present thread had to be killed with WM_QUIT - "
                        "the window did not take its own close")
    if not counts:
        failures.append("no cycle completed - nothing was measured")

    for f in failures:
        print("FAIL:", f)
    if failures:
        print(text[-800:])
        return 1
    print(f"OK: the picture window is destroyed, {CYCLES} open/close cycles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
