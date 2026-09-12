"""Rebuilding the NGX feature must not leak video memory (issue #48).

"When I slide intensity or other sliders, the VRAM creeps up. If I just
slide it back and forth, it will fill all the way up until the program (and
anything using VRAM) crashes."

Every slider step calls request_apply, which sends RNSZ, and the worker
answers it by releasing the feature and creating a new one. The release went
to the NGX **core** while the feature had been created by
nvngx_dlssnr.dll - a different implementation, which knows nothing about
that handle - so nothing was freed. Measured before the fix: **+12.6 GB over
30 rebuilds**, about 420 MB each, filling a 16 GB card in half a minute.

The worker reports its own usage through QueryVideoMemoryInfo after every
feature create, so this needs no external tool and a user's log shows the
same number.

Two shapes, because a slider and the resolution slider take different paths
through the same code: the same work size (only parameters changed) and
alternating sizes.

Run:  runtime\\python.exe tests\\test_feature_leak.py
"""
import os
import re
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
from protocol import send_resize  # noqa: E402

W, H = 1280, 720
CYCLES = 12
MEM_RE = re.compile(r"video memory after feature create: (\d+) MB")
# A generous ceiling: the leak was ~420 MB per rebuild, so even one leaked
# feature is far above this. Anything under it is allocator noise.
ALLOWED_MB = 150


def run(alternating: bool):
    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
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
            time.sleep(2.5)
            for i in range(CYCLES):
                p = dict(params)
                p["intensity"] = 0.7 + (i % 8) * 0.15   # what a slider changes
                w, h = (W, H) if not alternating else ((W, H) if i % 2 else (960, 540))
                send_resize(proc, p, w, h, 4, 0, 0, False)
                time.sleep(0.3)
                if proc.poll() is not None:
                    break
            time.sleep(0.8)
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
    return [int(m) for m in MEM_RE.findall(text)]


def main() -> int:
    failures = []
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1

    for name, alt in (("same size (a parameter slider)", False),
                      ("alternating sizes (the resolution slider)", True)):
        series = run(alt)
        if len(series) < 4:
            failures.append(f"{name}: only {len(series)} feature creates were "
                            f"reported - the worker did not rebuild, so "
                            f"nothing was measured")
            continue
        grew = series[-1] - series[0]
        print(f"    {name}: {series[0]} -> {series[-1]} MB "
              f"({grew:+d} over {len(series)} creates)")
        if grew > ALLOWED_MB:
            failures.append(
                f"{name}: video memory grew {grew} MB over {len(series)} "
                f"feature creates - the released feature is not being freed "
                f"(issue #48 was +420 MB per rebuild)")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: rebuilding the feature returns the memory it took")
    return 0


if __name__ == "__main__":
    sys.exit(main())
