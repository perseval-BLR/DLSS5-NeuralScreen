"""A parameter change does not rebuild the neural feature.

Every slider step, every profile change goes out as RNSZ, and until now
RNSZ always did the same thing: drain the GPU, release the feature and
every texture, create them again. Measured in a real session, 113-148 ms
of frozen picture per step - and the release/create pair is the one that
leaked 420 MB a time until 1.7.1 fixed which library does the releasing
(#48). A log line from that session says it plainly:

    [video] RNSZ: work 2496x1402 -> 2496x1402

The same size. Nothing needed building. The four sliders, the profile,
the style, the mask and the UI correction are Set on h.params before
EVERY Evaluate out of g_video_options; the NGX feature depends only on
the sizes and the upscale mode - CreateFeature ignores its flags
argument and the preset hint is an environment variable read once per
process.

So the worker now answers a same-size RNSZ by taking the options and
acking, with the feature untouched. This pins both halves: that it does
not rebuild, and that the new parameters really reach the network - a
change that arrived and did nothing would look exactly the same from
the outside.

Run:  runtime\\python.exe tests\\test_param_apply.py
"""
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES, RACK_FMT,
                  RESIZE_ACK_MAGIC, RESIZE_FMT, RESIZE_MAGIC, VIDEO_MAGIC,
                  WORKER_EXE)

W, H = 1280, 720
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    frame[..., 0] = base
    frame[..., 1] = (base // 2 + rng.integers(0, 48, size=(H, W), dtype=np.uint8))
    frame[..., 2] = (255 - base).astype(np.uint8)
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout (got {len(buf)} of {n})")
        buf += chunk
    return buf


def send_frame(worker, index: int, frame: np.ndarray, motion: np.ndarray):
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0,
                                   FRAME_FLAG_WANT_PIXELS, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC or not ok or nbytes == 0:
        raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} bytes={nbytes}")
    return np.frombuffer(read_exact(worker.stdout, nbytes),
                         dtype=np.uint8).reshape(H, W, 4).copy()


def send_params(worker, params, w=W, h=H):
    """RNSZ carrying the parameters - the same message the menu sends."""
    worker.stdin.write(struct.pack(
        RESIZE_FMT, RESIZE_MAGIC, w, h, WARMUP, 0, 0, 0,
        int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]), float(params["intensity"]),
        float(params["local_tone"]), float(params["local_structure"]),
        float(params["skin_structure"]), 0, 0))
    worker.stdin.flush()
    started = time.perf_counter()
    ack = read_exact(worker.stdout, struct.calcsize(RACK_FMT))
    took = (time.perf_counter() - started) * 1000.0
    magic, ok, ngx, _r, _pts = struct.unpack(RACK_FMT, ack)
    if magic != RESIZE_ACK_MAGIC:
        raise RuntimeError(f"foreign reply to RNSZ: 0x{magic:08X}")
    return ok, took


def main() -> int:
    failures = []
    faithful = dict(PROFILES["Faithful"])
    extreme = dict(PROFILES["Extreme / Overdrive"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(faithful["style"]), int(faithful["auto_mask"]),
                         int(faithful["ui_correction"]),
                         float(faithful["intensity"]),
                         float(faithful["local_tone"]),
                         float(faithful["local_structure"]),
                         float(faithful["skin_structure"]), 0, 0)

    log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    log.close()
    frame = make_frame(1)
    motion = np.zeros((H, W, 2), dtype=np.float16)
    with open(log.name, "wb") as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"],
                                cwd=str(WORKER_EXE.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            soft = send_frame(proc, 0, frame, motion)

            # 1. Same size, different parameters: acked, and fast.
            ok, took = send_params(proc, extreme)
            print(f"    parameter change: ok={ok}, {took:.0f} ms")
            if not ok:
                failures.append("the worker refused a parameter-only change")

            strong = send_frame(proc, 1, frame, motion)

            # 2. The parameters really reached the network. Faithful is
            #    intensity 0.70, Extreme is 2.50 - the same input frame
            #    cannot come back the same.
            delta = float(np.abs(strong.astype(np.int16) -
                                 soft.astype(np.int16)).mean())
            print(f"    Faithful vs Extreme on one frame: mean |diff| {delta:.2f}")
            if delta < 0.5:
                failures.append(f"the new parameters changed nothing "
                                f"(mean |diff| {delta:.2f}) - the change was "
                                f"acked and dropped")

            # 3. A real resize still rebuilds - the fast path must not have
            #    swallowed the case it was carved out of.
            ok, _ = send_params(proc, extreme, w=960, h=540)
            if not ok:
                failures.append("a real resize was refused")
            proc.stdin.close()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    Path(log.name).unlink(missing_ok=True)
    params_only = text.count("RNSZ: parameters only")
    rebuilt = text.count("RNSZ applied: feature ready")
    print(f"    log: {params_only} parameter-only, {rebuilt} rebuilt")
    if params_only != 1:
        failures.append(f"expected one parameter-only RNSZ in the log, "
                        f"found {params_only}")
    if rebuilt != 1:
        failures.append(f"expected exactly one real rebuild (the resize), "
                        f"found {rebuilt}")
    if "released" in text and params_only and rebuilt != 1:
        failures.append("the feature was released on a parameter change")

    for f in failures:
        print("FAIL:", f)
    if failures:
        print(text[-1500:])
        return 1
    print("OK: parameters apply without rebuilding the feature, and they land")
    return 0


if __name__ == "__main__":
    sys.exit(main())
