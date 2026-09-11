"""Opt-in Windows integration smoke test; briefly opens a colored test window.

Requires the bundled NVIDIA runtime and an RTX GPU. On an HDR primary display
checks FP16 WGC/DDA capture, scRGB presentation, bypass, neural processing, wipe,
SDR pixel export, and switching from HDR capture back to SDR pipe input.
Run with --run for WGC, or --desktop for full-screen desktop capture/present.
"""
import ctypes
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
import numpy as np
import pygame
import protocol as wire


def exact(pipe, size):
    data = bytearray()
    while len(data) < size:
        part = pipe.read(size - len(data))
        if not part:
            raise RuntimeError("worker exited during HDR test")
        data.extend(part)
    return data


def run(desktop=False):
    pygame.init()
    width, height = (ctypes.windll.user32.GetSystemMetrics(0),
                     ctypes.windll.user32.GetSystemMetrics(1)) if desktop else (640, 360)
    if not desktop:
        screen = pygame.display.set_mode((width, height), pygame.NOFRAME)
        screen.fill((30, 100, 210))
        pygame.display.flip()
        hwnd = pygame.display.get_wm_info()["window"]
    work_w, work_h = (1280, 720) if desktop else (width, height)
    env = dict(os.environ, NS_HDR="1", NS_NR_SMALL="1", NS_PW_ADAPTIVE="0")
    worker = subprocess.Popen([str(ROOT / "native/nvngx.dll"), "--live"],
                              cwd=ROOT / "native", env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    logs = []
    drain = threading.Thread(target=lambda: logs.extend(iter(worker.stderr.readline, b"")), daemon=True)
    drain.start()
    watchdog = threading.Timer(90, worker.kill)
    watchdog.start()
    results = 0
    try:
        worker.stdin.write(struct.pack(wire.HEADER_FMT, wire.VIDEO_MAGIC,
            work_w, work_h, 1, 0, 0, 0, 1, 0, 0, 1., 1., 1., -1., width, height))
        worker.stdin.flush()
        if desktop:
            wire.send_dda(worker, width, height)
            ack = struct.unpack(wire.DDA_ACK_FMT, exact(worker.stdout, struct.calcsize(wire.DDA_ACK_FMT)))
            assert ack[0] == wire.DDA_ACK_MAGIC and ack[1] == 1, ack
        else:
            wire.send_wgc(worker, hwnd)
            ack = struct.unpack(wire.WGC_ACK_FMT, exact(worker.stdout, struct.calcsize(wire.WGC_ACK_FMT)))
            assert ack[:4] == (wire.WGC_ACK_MAGIC, 1, width, height), ack
        wire.send_window(worker, width, height)
        ack = struct.unpack(wire.WINDOW_ACK_FMT, exact(worker.stdout, struct.calcsize(wire.WINDOW_ACK_FMT)))
        assert ack[0] == wire.WINDOW_ACK_MAGIC and ack[1] == 1, ack
        motion = np.zeros((work_h, work_w, 2), dtype=np.float16).tobytes()
        for index in range(12):
            pygame.event.pump()
            if not desktop:
                screen.fill((30 + index, 100, 210))
                pygame.display.flip()
            flags = wire.FRAME_FLAG_NO_COLOR | wire.FRAME_FLAG_WANT_PIXELS
            if index % 3 == 0:
                flags |= wire.FRAME_FLAG_BYPASS
            elif index % 3 == 2:
                flags |= wire.FRAME_FLAG_SPLIT | (32768 << 16)
            worker.stdin.write(struct.pack(wire.FRAME_FMT, wire.FRAME_MAGIC, index, 1, flags, index))
            worker.stdin.write(motion)
            worker.stdin.flush()
            ack = struct.unpack(wire.OUT_FMT, exact(worker.stdout, struct.calcsize(wire.OUT_FMT)))
            assert ack[0] == wire.OUT_MAGIC and ack[2] == 1, ack
            if ack[3]:
                assert ack[3] == width * height * 4, ack
                pixels = np.frombuffer(exact(worker.stdout, ack[3]), np.uint8)
                assert pixels.reshape(-1, 4)[:, :3].max() > 0, "empty SDR export"
                results += 1
            time.sleep(.03)
        assert results >= 6, results
        # Disable native capture: the existing SDR pipe/present must still work.
        wire.send_wgc(worker, 0)
        ack = struct.unpack(wire.WGC_ACK_FMT, exact(worker.stdout, struct.calcsize(wire.WGC_ACK_FMT)))
        assert ack[1] == 1, ack
        frame = np.full((height, width, 4), 128, dtype=np.uint8)
        frame[:, :, 3] = 255
        flags = wire.FRAME_FLAG_BYPASS | wire.FRAME_FLAG_WANT_PIXELS
        worker.stdin.write(struct.pack(wire.FRAME_FMT, wire.FRAME_MAGIC, 12, 1, flags, 12))
        worker.stdin.write(frame.tobytes())
        worker.stdin.write(motion)
        worker.stdin.flush()
        ack = struct.unpack(wire.OUT_FMT, exact(worker.stdout, struct.calcsize(wire.OUT_FMT)))
        assert ack[2] == 1 and ack[3] == frame.nbytes, ack
        assert exact(worker.stdout, ack[3]) == frame.tobytes(), "HDR->SDR pipe regression"
    finally:
        worker.stdin.close()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        watchdog.cancel()
        drain.join(timeout=3)
        pygame.quit()
        log = b"".join(logs).decode("utf-8", "replace")
        print(log)
    assert worker.returncode == 0, worker.returncode
    assert "capture=FP16 scRGB" in log, "HDR capture not exercised: run on an HDR primary display"
    assert "presentation=FP16 scRGB" in log, "HDR presentation not exercised"
    assert "presentation=8-bit SDR" in log, "HDR->SDR transition not exercised"
    print(f"PASS: {'DDA' if desktop else 'WGC'} HDR capture/present, NR, bypass, wipe, SDR export and HDR->SDR transition ({results} frames)")


if __name__ == "__main__":
    if "--run" in sys.argv or "--desktop" in sys.argv:
        run("--desktop" in sys.argv)
    else:
        print("SKIP: opt-in HDR/GPU test; use --run (WGC) or --desktop (DDA)")
