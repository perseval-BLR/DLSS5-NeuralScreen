"""The worker's GPU waits survive a pipeline rebuild with the capture live.

One auto-reset event serves every fence wait in the worker - several fences
among them - and SetEventOnCompletion is not cancelled when a wait returns.
So a wait can be woken by a registration made for an OLDER value. That used
to be reported as a failure, and the next wait inherited the next stale
signal: the pipeline stayed exactly one completion out of step and never
recovered.

In the wild (issue #33) that was 2766 consecutive "[cap] swizzle fence
timeout" lines, each 30 ms apart rather than the 10 s the timeout asks for,
running from the moment the user switched to his second monitor until he
switched back - which tore the pipeline down and built it again. The picture
was simply absent for all of it: "tried NR with no result on either, then
changed back to main, NR worked".

This machine has one display, so the exact state is not reproduced here.
What IS exercised is the path it appeared on - Desktop Duplication running
while the feature is recreated underneath it, several times over - and the
thing nothing was watching for: a fence wait that gives up. Any timeout line
at all fails this test.

Run:  runtime\\python.exe tests\\test_fence_waits.py
"""
import ctypes
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from main import (HEADER_FMT, NATIVE_DIR, PROFILES,  # noqa: E402
                  VIDEO_MAGIC, WORKER_EXE)
from protocol import DDA_FMT, DDA_MAGIC, send_resize  # noqa: E402

WARMUP = 4
CYCLES = 4
# Work sizes to rebuild between - a real resolution change, not a no-op.
SIZES = [(1280, 720), (1920, 1080), (960, 540), (1600, 900)]
TIMEOUT_MARKERS = ("fence timeout", "did not retire allocator")


def main() -> int:
    failures = []
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1

    params = dict(PROFILES["Natural"])
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, SIZES[0][0], SIZES[0][1],
                         WARMUP, 0, 0, 0, int(params["style"]),
                         int(params["auto_mask"]), int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                                env=dict(os.environ), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            time.sleep(2.0)
            # The capture inside the worker: this is the path the timeouts
            # appeared on (the swizzle from the duplicated texture).
            proc.stdin.write(struct.pack(DDA_FMT, DDA_MAGIC, sw, sh, 0, 0))
            proc.stdin.flush()
            time.sleep(1.5)
            for i in range(CYCLES):
                w, h = SIZES[(i + 1) % len(SIZES)]
                send_resize(proc, params, w, h, WARMUP, 0, 0, False)
                time.sleep(1.5)
                if proc.poll() is not None:
                    failures.append(f"the worker exited during rebuild {i + 1}")
                    break
        except Exception as exc:
            failures.append(f"driving the worker failed: {exc!r}")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            err.seek(0)
            text = err.read().decode("utf-8", "replace")

    bad = [l for l in text.splitlines()
           if any(m in l for m in TIMEOUT_MARKERS)]
    rebuilds = text.count("feature 18 ready")
    print(f"    {rebuilds} feature builds, capture live, "
          f"{len(bad)} fence timeouts")
    if rebuilds < 2:
        failures.append(f"only {rebuilds} feature builds - the rebuilds did "
                        f"not happen, so nothing was exercised")
    if bad:
        failures.append(f"{len(bad)} fence wait(s) gave up; first: "
                        f"{bad[0].strip()[:110]}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: no fence wait gave up across the rebuilds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
