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


def primary_is_hdr():
    """Is HDR on for the display this test would capture?

    The same question hdr_display.h asks the same way (QueryDisplayConfig ->
    GET_ADVANCED_COLOR_INFO), asked here independently: if Windows says HDR
    and the worker's log does not say "capture=FP16 scRGB", the two
    disagree and the assertions below catch it.

    Returns True, False, or None when Windows would not answer at all.
    """
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # PRIMARY
        info = ctypes.create_string_buffer(40 + 32 * 2)
        ctypes.memmove(info, struct.pack("<I", len(info)), 4)
        if not user32.GetMonitorInfoW(monitor, info):
            return None
        primary = ctypes.wstring_at(ctypes.addressof(info) + 40, 32).split("\0")[0]

        paths = wintypes.UINT(0)
        modes = wintypes.UINT(0)
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(paths),  # ACTIVE
                                              ctypes.byref(modes)):
            return None
        path_buf = ctypes.create_string_buffer(paths.value * 72)
        mode_buf = ctypes.create_string_buffer(modes.value * 64)
        if user32.QueryDisplayConfig(2, ctypes.byref(paths), path_buf,
                                     ctypes.byref(modes), mode_buf, None):
            return None
        for index in range(paths.value):
            path = path_buf[index * 72:(index + 1) * 72]
            src_adapter, src_id = path[0:8], struct.unpack_from("<I", path, 8)[0]
            tgt_adapter, tgt_id = path[20:28], struct.unpack_from("<I", path, 28)[0]
            # GET_SOURCE_NAME (1): which \\.\DISPLAYn this path drives.
            name = ctypes.create_string_buffer(84)
            ctypes.memmove(name, struct.pack("<II", 1, 84) + src_adapter
                           + struct.pack("<I", src_id), 20)
            if user32.DisplayConfigGetDeviceInfo(name):
                continue
            if ctypes.wstring_at(ctypes.addressof(name) + 20, 32).split("\0")[0] != primary:
                continue
            # GET_ADVANCED_COLOR_INFO (9): bit 1 is advancedColorEnabled,
            # bit 2 wideColorEnforced - wide gamut without HDR is not HDR.
            color = ctypes.create_string_buffer(32)
            ctypes.memmove(color, struct.pack("<II", 9, 32) + tgt_adapter
                           + struct.pack("<I", tgt_id), 20)
            if user32.DisplayConfigGetDeviceInfo(color):
                return None
            bits = struct.unpack_from("<I", color, 20)[0]
            return bool(bits & 2) and not bool(bits & 4)
    except Exception as exc:
        print(f"(HDR probe failed: {exc!r})")
    return None


if __name__ == "__main__":
    if "--run" in sys.argv or "--desktop" in sys.argv:
        run("--desktop" in sys.argv)
    else:
        # Run by the suite with no argument: take the test when the machine
        # can actually answer it. A skip that always skips is a green tick
        # for nothing, and this is the only test that exercises the HDR
        # path end to end on real hardware.
        hdr = primary_is_hdr()
        if hdr:
            print("the primary display is in HDR - running the WGC variant")
            run(False)
        else:
            print("SKIP: the primary display is "
                  + ("not in HDR" if hdr is False else "not readable")
                  + " (Win+Alt+B turns it on); --desktop runs the DDA variant")
