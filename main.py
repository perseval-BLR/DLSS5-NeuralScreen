"""DLSS 5 Desktop NR - the integration skeleton of the prototype.

The loop: desktop capture (capture.ScreenCapture) -> motion guides
(guides.TemporalGuideGenerator) -> the NGX worker (native/nvngx.dll in
--live mode) -> fullscreen output (display.Display).

Controls (global hotkeys, RegisterHotKey + a polling fallback - see
hotkeys.py). Num Lock must be on: the numpad sends Insert/End/arrows
without it:
    Num1          - NR on/off
    Num2          - the settings menu
    Num3          - screenshot
    Num0          - recording
    Num4 / Num6   - processing resolution down / up
    Num5          - process one window instead of the whole screen
    Ctrl+Alt+Q    - quit (the same as "Exit" in the tray)

Every key can be reassigned in the settings menu (config.json
"hotkeys").

Run:
    python main.py [--config config.json]
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# --- Log to a file instead of the console --------------------------------
# The release is launched through pythonw.exe (no console window): stdout and
# stderr are None there and any print would fail. We redirect them into
# NeuralScreen.log next to main.py - every print keeps working and the user
# reads the log as a file rather than a window. Startup errors (a missing DLL
# and the like) are additionally shown in a message box (see the bottom of
# this file).
LOG_PATH = Path(__file__).resolve().parent / "NeuralScreen.log"

# The version shown in the menu header. Kept in sync with native/launcher.rc
# (FileVersion/ProductVersion) and build_release_zip.py at release time.
APP_VERSION = "1.5.2"

# The channel label: the header shows the version, the channel lives in the
# settings page (user rule 2026-09-08).
CHANNEL_LABEL = "@perseval_BLR"


def _init_logging() -> None:
    """Redirect stdout/stderr into NeuralScreen.log (utf-8)."""
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception:
        pass  # it did not work - the prints just vanish, we do not crash

# DPI awareness BEFORE any import (cv2, capture, display, tray): if some
# module sets awareness first (dxcam, for instance, calls
# SetProcessDpiAwareness(2) when creating an Output), a second call returns
# ERROR_ACCESS_DENIED and the pygame window ends up scaled (125% ->
# 3072x1728). PER_MONITOR_AWARE_V2 = -4. Errors are ignored: display.py
# repeats the call.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Embedded Python (python313._pth) does not add cwd to sys.path - we add the
# script folder by hand so the local modules work (capture, display, guides).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
import pygame  # HUD overlay on the recorded frame (image.frombuffer)

from capture import ScreenCapture, list_monitors
from display import Display
from guides import TemporalGuideGenerator
from hotkeys import (HotkeyController, build_bindings,
                     describe as describe_hotkeys, numlock_needed, numlock_on,
                     parse_binding)
from recorder import VideoRecorder
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from i18n import STRINGS as UI_STRINGS

# The project page: README, hotkeys, requirements. Opened from the menu.
REPO_URL = "https://github.com/perseval-BLR/DLSS5-NeuralScreen"
CHANNEL_URL = "https://www.youtube.com/@perseval_BLR/videos"
from tray import TrayController
from taskbar import TaskbarWindow

# --- Worker protocol (matches dlss5_converter/core.py) -------------------
# v3 (magic D5V3): a header with full_w/full_h - the worker resizes the frames
# on the GPU itself (NGX Upscaling), Python does not resize on the CPU.
VIDEO_MAGIC = 0x33563544  # 'DV5' v3
FRAME_MAGIC = 0x314D5246  # 'FMR1'
OUT_MAGIC = 0x3154554F    # 'OUT1'

HEADER_FMT = "<10I4f2I"   # magic, w, h, warmup, frame_count, profile, preset,
                          # style, auto_mask, ui_correction, intensity,
                          # local_tone, local_structure, skin_structure,
                          # full_w, full_h
FRAME_FMT = "<4Iq"        # magic, index, reset, reserved, pts
OUT_FMT = "<5Iq"          # magic, index, ok, bytes, ngx_result, pts

# SHMI: the frame travels through shared memory and only the FRM1 header with
# the FRAME_FLAG_SHM flag goes down the pipe. The worker loads the pixels into
# a texture straight from the mapping - two 33 MB copies disappear (the write
# into the pipe and the read out of it).
SHM_MAGIC = 0x494D4853      # 'SHMI'
SHM_ACK_MAGIC = 0x4B434153  # 'SACK'
SHM_FMT = "<4Iq64s"         # magic, color_bytes, motion_bytes, flags, pts, name (88 bytes)
SHM_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts (24 bytes)
FRAME_FLAG_SHM = 0x1         # a bit in the reserved field of the frame header
FRAME_FLAG_WANT_PIXELS = 0x2  # return the pixels even in window mode (for a screenshot)
FRAME_FLAG_MOTION_SMALL = 0x4  # motion field at flow resolution, upscaled by the worker
FRAME_FLAG_SPLIT = 0x20        # before/after wipe; position in the high 16 bits of reserved

# MOTS: the motion field arrives at the optical-flow resolution (~320x180) and
# the worker upscales it to the work resolution on the GPU. The CPU is spared
# a resize and the conversion of 6 million values - ~8 ms per frame measured.
MOTION_MAGIC = 0x53544F4D      # 'MOTS'
MOTION_ACK_MAGIC = 0x4B43414D  # 'MACK'
MOTION_FMT = "<4Iq"            # magic, width, height, flags, pts (24 bytes)
MOTION_ACK_FMT = "<4Iq"


# WNDO: the worker shows the result itself, in its own window above the
# screen. While that window is up OUT1 arrives with bytes=0 - no pixels come
# back to Python at all, and the worker's readback, the reverse pipe and the
# pygame blit all disappear.
WINDOW_MAGIC = 0x4F444E57      # 'WNDO'
WINDOW_ACK_MAGIC = 0x4B434157  # 'WACK'
WINDOW_FMT = "<4Iq"            # magic, width, height, flags, pts (24 bytes)
WINDOW_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts
WINDOW_FLAG_CAPTURABLE = 0x1   # debug: do NOT hide the window from screen capture
WINDOW_FLAG_DISABLE = 0x2      # close the window, go back to sending pixels

# RNSZ: change the work resolution on the fly (without restarting the worker
# process). The worker recreates the NGX feature at the new sizes and answers
# RACK.
RESIZE_MAGIC = 0x5A534E52  # 'RNSZ'
RESIZE_ACK_MAGIC = 0x4B434152  # 'RACK'
RESIZE_FMT = "<10I4f2I"   # the same layout as HEADER_FMT (magic instead of VIDEO_MAGIC)
# The slot the header keeps frame_count in carries flags in a resize.
RESIZE_FLAG_NR_SMALL = 0x1   # run the network at the work size, scale the result back
RACK_FMT = "<4Iq"         # magic, ok, ngx_result, reserved, pts (24 bytes)

# DDA1: the worker captures the screen itself (Desktop Duplication) - the
# colour goes straight into a GPU texture and Python no longer ships 33 MB per
# frame. FRM1 frames go out with FRAME_FLAG_NO_COLOR: motion only, no colour.
DDA_MAGIC = 0x31414444  # 'DDA1'
WGC_MAGIC = 0x57434757      # 'WGCW' - capture ONE window instead of the desktop
WGC_ACK_MAGIC = 0x4B414757  # 'WGAK' - its acknowledgement, with the real capture size
WGC_FMT = "<4IqQ"           # magic, width, height, flags, pts, hwnd
WGC_ACK_FMT = "<4Iq"        # magic, ok, width, height, pts
DDA_ACK_MAGIC = 0x4B434144  # 'DACK'
DDA_FMT = "<4Iq"        # magic, width, height, flags, pts (24 bytes)
DDA_ACK_FMT = "<4Iq"    # magic, ok, reserved0, reserved1, pts
FRAME_FLAG_NO_COLOR = 0x8  # in DDA mode: we send no colour (the worker takes it)
FRAME_FLAG_BYPASS = 0x10  # NR OFF: skip NGX, show the raw capture

# GRAY: the worker writes luminance (a downsample of the screen, ~320x180)
# into a reverse mapping for Python - for the guides' optical flow. In DDA
# mode this replaces the dxcam grab: the gray frame comes straight off the GPU.
# OUTS: the reverse channel for PIXELS. A 4K recorded frame weighs 33 MB, and
# through the pipe that is ~7 ms per frame (measured: recv 17.4 -> 31.6 ms
# when recording is switched on). Through shared memory those bytes never
# travel down the pipe.
OUTS_MAGIC = 0x5354554F      # 'OUTS'
OUTS_ACK_MAGIC = 0x324B414F  # 'OAK2'
OUTS_FMT = "<4Iq64s"         # like GRAY_FMT: magic, w, h, flags, pts, name
OUTS_ACK_FMT = "<4Iq"
# VideoResultHeader.bytes: the pixels are in the OUTS section, not in the pipe.
OUT_BYTES_IN_SHM = 0xFFFFFFFF

GRAY_MAGIC = 0x59415247  # 'GRAY'
GRAY_ACK_MAGIC = 0x4B434147  # 'GAK'
GRAY_FMT = "<4Iq64s"    # magic, width, height, flags, pts, name (88 bytes)
GRAY_ACK_FMT = "<4Iq"   # magic, ok, reserved0, reserved1, pts

# --- DLSS 5 NR profiles (field order as in the converter) -----------------
PROFILES = {
    "Faithful": dict(profile=0, preset=0, style=0, auto_mask=0, ui_correction=0,
                     intensity=0.70, local_tone=0.75, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(profile=1, preset=0, style=1, auto_mask=0, ui_correction=0,
                    intensity=1.00, local_tone=1.00, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                               intensity=1.65, local_tone=1.40, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                                intensity=2.50, local_tone=2.00, local_structure=2.00, skin_structure=1.5),
}

BASE_DIR = Path(__file__).resolve().parent
NATIVE_DIR = BASE_DIR / "native"
# IMPORTANT: NGX Core returns FAIL_PlatformError from Init_Ext for ANY process
# name other than nvngx.dll (verified experimentally). The file name is part of
# the NGX contract.
WORKER_EXE = NATIVE_DIR / "nvngx.dll"

FPS_LOG_INTERVAL = 2.0  # seconds, FPS log to the console
PERF_LOG_INTERVAL = 5.0  # seconds, log of the mean pipeline stage timings
PERF_KEYS = ("grab", "resize_full", "guides", "send", "recv", "show")

# The global hotkeys live in hotkeys.py (RegisterHotKey). The layout and the
# reasons behind the combinations are in that module's docstring.
WORK_SCALE_STEP = 0.05
WORK_SCALE_MIN = 0.1
WORK_SCALE_MAX = 1.0
# NGX feature 18 goes silent at 3840x2160 (verified in isolation: the worker
# hangs on frame 0 with work=4K, both in legacy and in upscale mode).
# We cap the work resolution at 2560x1440 - that is known to work.
WORK_MAX_W = 2560
WORK_MAX_H = 1440

DEFAULT_LANG = "en"


def _work_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """The NGX work resolution: scale of full, but no larger than
    WORK_MAX_W/H (NGX goes silent at 4K - the limit verified in isolation).

    Two rules that come from being bitten:

    At 1:1 the answer is the frame itself, with no rounding. Rounding to the
    nearest even number turned a 539-pixel-high window (900x500 plus its title
    bar) into a 540-high work size - larger than the frame - and the worker
    died on the header. An odd size at 1:1 stays on the legacy path, which is
    known to work (test_odd_frame_size).

    And a downscale rounds DOWN, never up: the work resolution must never
    exceed the frame it came from.
    """
    if scale >= 1.0:
        w, h = int(width), int(height)
    else:
        w = max(64, int(width * scale) // 2 * 2)
        h = max(64, int(height * scale) // 2 * 2)
    if w > WORK_MAX_W or h > WORK_MAX_H:
        k = min(WORK_MAX_W / w, WORK_MAX_H / h)
        w = max(64, int(w * k) // 2 * 2)
        h = max(64, int(h * k) // 2 * 2)
    return min(w, int(width)), min(h, int(height))


class SharedFrameBuffer:
    """Shared memory for the worker's input frame (the SHMI command).

    The layout is fixed and does NOT depend on work_scale:
        [0 .. color_capacity)                - RGBA8 full-res
        [color_capacity .. +motion_capacity) - motion float16 work-res
    The motion offset is constant, so a resolution change (RNSZ) needs no
    renegotiation of SHMI - only the used length changes.

    INVARIANT: there is one slot. Frame N+1 must not be placed until the
    worker has returned the result for frame N, otherwise we overwrite the
    pixels under its hands. The main loop is strictly paired (send -> recv),
    so the invariant holds. Add pipelining and a second slot will be needed.
    """

    def __init__(self, full_w: int, full_h: int,
                 max_work_w: int = WORK_MAX_W, max_work_h: int = WORK_MAX_H):
        self.color_capacity = full_w * full_h * 4
        self.motion_capacity = max_work_w * max_work_h * 4
        self.size = self.color_capacity + self.motion_capacity
        # The section name: ASCII, unique per process - the worker opens it
        # through OpenFileMappingA in the same Windows session.
        self.name = f"NeuralScreen_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._mm = mmap.mmap(-1, self.size, tagname=self.name)
        self._buf = np.ndarray((self.size,), dtype=np.uint8, buffer=self._mm)
        self.negotiated = False  # set by start_worker after SACK

        # --- Reverse channel: gray (luminance) for guides in DDA mode ---
        # The worker writes a downsample of the screen here (320x180 = the
        # flow size) and Python reads it instead of the dxcam grab for
        # DISOpticalFlow.
        self.gray_w, self.gray_h = 0, 0
        self.gray_bytes = 0
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm: mmap.mmap | None = None
        self._gray_buf: np.ndarray | None = None  # (gray_bytes,) uint8

        # --- Reverse channel: the result pixels (recording/screenshot) ---
        self.out_w, self.out_h = 0, 0
        self.out_bytes = 0
        self.out_name = ""
        self._out_mm: mmap.mmap | None = None
        self._out_buf: np.ndarray | None = None  # (h, w, 4) uint8

    def open_gray(self, w: int, h: int) -> None:
        """Open a gray section of w*h bytes (create it if there was none).

        On a size change the section name CHANGES: the worker holds the old
        handle and CreateFileMapping with the same name would return the old
        section - a larger mmap would fail and the channel would die quietly
        (audit H2). send_gray() passes the fresh name to the worker after
        open_gray().
        """
        if self._gray_mm is not None and self.gray_w == w and self.gray_h == h:
            return
        self.close_gray()
        self.gray_w, self.gray_h = w, h
        self.gray_bytes = w * h
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm = mmap.mmap(-1, self.gray_bytes, tagname=self.gray_name)
        self._gray_buf = np.ndarray((self.gray_bytes,), dtype=np.uint8, buffer=self._gray_mm)

    def open_out(self, w: int, h: int) -> None:
        """Open the section for the returned pixels (RGBA8 w*h).

        The name changes on every open - just like gray: the worker holds the
        old handle and CreateFileMapping with the same name would return the
        old section, at its old size. The first 8 bytes are a seqlock written
        by the worker (odd while writing, even when done).
        """
        if self._out_mm is not None and self.out_w == w and self.out_h == h:
            return
        self.close_out()
        self.out_w, self.out_h = w, h
        self.out_bytes = w * h * 4 + 8  # + seqlock
        self.out_name = f"NeuralScreenOut_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._out_mm = mmap.mmap(-1, self.out_bytes, tagname=self.out_name)
        self._out_buf = np.ndarray((h, w, 4), dtype=np.uint8, buffer=self._out_mm, offset=8)

    def read_out(self) -> np.ndarray | None:
        """A copy of the frame from the section, guarded by the seqlock.

        The copy is mandatory: there is one slot, the worker overwrites it
        with the next frame, and the frame outlives that - it goes into the
        encoder queue. The seqlock (first 8 bytes) detects a torn frame: if
        the worker is mid-write (odd) or the sequence changed while we
        copied, we retry a few times and then fall back to None (the caller
        skips the frame).
        """
        if self._out_buf is None:
            return None
        for _ in range(4):
            seq1 = int.from_bytes(self._out_mm[0:8], "little")
            if seq1 & 1:
                continue  # worker is writing - not ready yet
            buf = self._out_buf.copy()
            seq2 = int.from_bytes(self._out_mm[0:8], "little")
            if seq1 == seq2:
                return buf
        return None  # torn after retries - caller skips the frame

    def close_out(self) -> None:
        if self._out_buf is not None:
            self._out_buf = None
        if self._out_mm is not None:
            try:
                self._out_mm.close()
            except Exception:
                pass
            self._out_mm = None
        self.out_bytes = 0
        self.out_w = self.out_h = 0

    def read_gray(self) -> np.ndarray | None:
        """Return a copy of the gray frame (320x180 uint8), or None if it is
        not open.

        The worker writes with memcpy and no shared barrier - a tear is
        theoretically possible. At 320x180 that is microseconds; one torn
        optical-flow frame is not critical (guides survive it and the next
        frame fixes it). An accepted risk - a seqlock would be overengineering.
        """
        if self._gray_buf is None:
            return None
        return self._gray_buf.copy()

    def close_gray(self) -> None:
        if self._gray_buf is not None:
            self._gray_buf = None
        if self._gray_mm is not None:
            try:
                self._gray_mm.close()
            except Exception:
                pass
            self._gray_mm = None

    def put(self, rgba: np.ndarray, motion: np.ndarray) -> None:
        """Put the frame and motion into the mapping (one memcpy each)."""
        color = rgba.reshape(-1)
        if color.nbytes > self.color_capacity:
            raise ValueError(f"a frame of {color.nbytes} B does not fit into "
                             f"{self.color_capacity} B of shared memory")
        mv = motion.reshape(-1).view(np.uint8)
        if mv.nbytes > self.motion_capacity:
            raise ValueError(f"motion of {mv.nbytes} B does not fit into "
                             f"{self.motion_capacity} B of shared memory")
        np.copyto(self._buf[:color.nbytes], color)
        off = self.color_capacity
        np.copyto(self._buf[off:off + mv.nbytes], mv)

    def close(self) -> None:
        self.negotiated = False
        self.close_gray()
        self._buf = None  # numpy holds the buffer: without the reset mmap.close() raises BufferError
        try:
            self._mm.close()
        except Exception as exc:
            print(f"[main] could not close the shared memory: {exc}", file=sys.stderr)


def load_config(path: Path) -> dict:
    """Load and validate config.json."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    required = {"monitor", "width", "height", "fullscreen", "warmup", "profile",
                "intensity", "local_tone", "local_structure", "skin_structure"}
    missing = required - set(cfg)
    if missing:
        raise ValueError(f"config.json: missing fields: {sorted(missing)}")
    if cfg["profile"] not in PROFILES:
        raise ValueError(f"config.json: unknown profile {cfg['profile']!r}; "
                         f"available: {sorted(PROFILES)}")
    for key in ("width", "height", "warmup"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"config.json: field {key} must be a positive integer")
    # work_scale: 0.25..1.0 - the NGX processing resolution relative to the output
    scale = float(cfg.get("work_scale", 1.0))
    cfg["work_scale"] = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, scale))
    # lang: the language of the HUD/alerts/menu (en/ru, DEFAULT_LANG by default)
    lang = str(cfg.get("lang", DEFAULT_LANG))
    if lang not in UI_STRINGS:
        lang = DEFAULT_LANG
    cfg["lang"] = lang
    return cfg


def resolve_params(cfg: dict) -> dict:
    """Profile + custom NR parameters from the config (null = use the profile)."""
    params = dict(PROFILES[cfg["profile"]])
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        value = cfg.get(key)
        if value is not None:
            params[key] = float(value)
    return params


def _read_exact(stream, size: int) -> bytes:
    """Read exactly size bytes from the stream (the worker may give fewer)."""
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"the worker stopped after {len(chunks)} of {size} reply bytes")
        chunks.extend(block)
    return bytes(chunks)


def _drain_stderr(worker, logs: list[str], stop: threading.Event) -> None:
    """Background drain of the worker's stderr (otherwise the buffer fills up
    and the worker hangs).

    One thread per worker; it finishes on EOF (the process died) or on the
    stop event (shutdown_worker). After a restart the old thread reads from
    the CLOSED stderr of the old worker: readline() returns b"" (EOF) and the
    thread exits - it does not hang and does not read the new worker's stderr.
    """
    try:
        for raw in iter(worker.stderr.readline, b""):
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace").rstrip()
            logs.append(line)
            # The list grows without bound (NS_PHASE=1 adds a line per frame)
            # - every consumer reads only the tail, so we keep 2000.
            if len(logs) > 2000:
                del logs[: len(logs) - 2000]
            # The worker log goes into the shared log, but only when the
            # profiler is on (NS_PHASE=1): otherwise it just sits in the
            # buffer and is seen only when something crashed. Besides the
            # phase measurements we let [pure]/[host] through: they carry the
            # NGX result code and the chosen model preset, and without them
            # there is no telling what was actually created. [present] is
            # let through too: the overlay window lifecycle (created, hidden,
            # revealed, resize) is part of the startup/shutdown diagnostics.
            if "[present]" in line or "[spout]" in line or "[arch]" in line or (
                    os.environ.get("NS_PHASE") == "1" and (
                        "[phase]" in line or "[pure]" in line or "[host]" in line)):
                print(line)
    except Exception:
        pass


def start_worker(params: dict, width: int, height: int, warmup: int,
                 full_w: int = 0, full_h: int = 0,
                 shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str]]:
    """Start the NGX worker in --live mode and send the header.

    width/height is the work resolution (the NGX feature), full_w/full_h is
    the size of the input frames coming from Python (the worker resizes them
    on the GPU through NGX Upscaling; full_w=0 -> the old 1:1 mode).

    Returns (worker, logs, reader, stop): reader is the permanent stdout
    reader thread (see WorkerReader), stop is the event used to finish
    _drain_stderr on shutdown.
    """
    if not WORKER_EXE.is_file():
        raise FileNotFoundError(
            f"worker not found: {WORKER_EXE}\n"
            "Copy nvngx.dll (the built worker) and nvngx_dlssnr.dll into native/."
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    worker = subprocess.Popen(
        [str(WORKER_EXE), "--live"],
        cwd=str(NATIVE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    logs: list[str] = []
    stop = threading.Event()
    threading.Thread(target=_drain_stderr, args=(worker, logs, stop), daemon=True).start()
    # In upscale mode (full_w>0) the worker returns full-res frames - the
    # reader must expect the full sizes, otherwise byte_count will not match.
    out_w = full_w if full_w else width
    out_h = full_h if full_h else height
    reader = WorkerReader(worker, out_w, out_h, shm)

    header = struct.pack(
        HEADER_FMT,
        VIDEO_MAGIC, width, height, int(warmup), 0,  # frame_count=0 -> an endless loop
        params["profile"], params["preset"], params["style"],
        params["auto_mask"], params["ui_correction"],
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    )
    worker.stdin.write(header)
    worker.stdin.flush()
    if shm is not None:
        _negotiate_shm(worker, reader, shm)
    return worker, logs, reader, stop


def _negotiate_shm(worker: subprocess.Popen, reader: "WorkerReader",
                   shm: SharedFrameBuffer, timeout: float = 10.0) -> None:
    """Hand the shared memory name to the worker (SHMI) and wait for SACK.

    A refusal is not fatal: if the worker could not open the mapping we stay
    on sending the frame down the pipe - that path is still there and works.
    """
    shm.negotiated = False
    try:
        worker.stdin.write(struct.pack(
            SHM_FMT, SHM_MAGIC, shm.color_capacity, shm.motion_capacity, 0, 0,
            shm.name.encode("ascii")))
        worker.stdin.flush()
        reader.wait_sack(timeout)
        shm.negotiated = True
        print(f"[main] shared memory agreed: {shm.size / 1e6:.1f} MB, "
              f"the frame does not go through the pipe")
    except Exception as exc:
        print(f"[main] shared memory unavailable ({exc}) - frames through the pipe",
              file=sys.stderr)


def send_frame(worker: subprocess.Popen, index: int, rgba: np.ndarray,
               motion: np.ndarray, reset: bool, pts: int,
               shm: "SharedFrameBuffer | None" = None,
               want_pixels: bool = False, motion_small: bool = False,
               no_color: bool = False, bypass: bool = False,
               split: float = 0.0) -> None:
    """Send a frame to the worker.

    With shared memory agreed, only the 24-byte header with the
    FRAME_FLAG_SHM flag goes down the pipe and the pixels are placed into the
    mapping. Otherwise it is the old path: header + RGBA8 + motion float16
    sent inline through the pipe.

    no_color (DDA mode): the worker takes the colour itself from Desktop
    Duplication - only motion goes down the pipe, rgba is ignored.
    bypass (NR OFF): the worker skips NGX and shows the raw capture - the
    overlay (window, HUD) stays alive while the effect is off.
    split (0..1): the share of the frame on the left the worker leaves
    unprocessed - the before/after wipe. 0 means off.
    """
    flags = (FRAME_FLAG_WANT_PIXELS if want_pixels else 0) | \
            (FRAME_FLAG_MOTION_SMALL if motion_small else 0) | \
            (FRAME_FLAG_NO_COLOR if no_color else 0) | \
            (FRAME_FLAG_BYPASS if bypass else 0)
    if split > 0.0:
        # The wipe position rides in the high 16 bits of the same flags field:
        # there is no dedicated field in the header, and widening it for a
        # single number would mean changing the protocol on both sides.
        frac = min(0xFFFF, max(0, int(round(min(1.0, split) * 0xFFFF))))
        flags |= FRAME_FLAG_SPLIT | (frac << 16)
    if no_color:
        # DDA mode: motion only, no colour (SHM is not used for colour)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
        worker.stdin.write(motion.tobytes())
        worker.stdin.flush()
        return
    if shm is not None and shm.negotiated:
        shm.put(rgba, motion)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset),
                                       FRAME_FLAG_SHM | flags, pts))
        worker.stdin.flush()
        return
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
    worker.stdin.write(rgba.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def send_resize(worker: subprocess.Popen, params: dict, width: int, height: int,
                warmup: int, full_w: int = 0, full_h: int = 0,
                nr_small: bool = False) -> None:
    """Send RNSZ - change the work resolution/parameters on the fly.

    The worker recreates the NGX feature at the new sizes (ReleaseFeature ->
    CreateFeature inside the same process) and answers RACK. A process restart
    is not needed - a restart was exactly what caused the hangs and crashes
    (exit 127).
    """
    worker.stdin.write(struct.pack(
        RESIZE_FMT,
        RESIZE_MAGIC, width, height, int(warmup),
        RESIZE_FLAG_NR_SMALL if nr_small else 0,
        params["profile"], params["preset"], params["style"],
        params["auto_mask"], params["ui_correction"],
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    ))
    worker.stdin.flush()


def send_motion_size(worker: subprocess.Popen, width: int, height: int,
                     flags: int = 0, pts: int = 0) -> None:
    """MOTS: at what resolution the motion field will arrive.

    0x0 turns it off: the field goes back to the work resolution.
    """
    worker.stdin.write(struct.pack(MOTION_FMT, MOTION_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_window(worker: subprocess.Popen, width: int, height: int,
                flags: int = 0, pts: int = 0) -> None:
    """WNDO: ask the worker to raise its own output window (or close it).

    width=height=0 or the WINDOW_FLAG_DISABLE flag closes the window and goes
    back to sending pixels through the pipe.
    """
    worker.stdin.write(struct.pack(WINDOW_FMT, WINDOW_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_dda(worker: subprocess.Popen, width: int, height: int,
             flags: int = 0, pts: int = 0) -> None:
    """DDA1: ask the worker to capture the screen itself (Desktop Duplication).

    width=height=0 turns the capture off and goes back to sending the frame
    from Python. While it is active FRM1 frames carry FRAME_FLAG_NO_COLOR
    (motion only).
    """
    worker.stdin.write(struct.pack(DDA_FMT, DDA_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_wgc(worker: subprocess.Popen, hwnd: int, width: int = 0,
             height: int = 0, pts: int = 0) -> None:
    """WGCW: ask the worker to capture ONE window instead of the desktop.

    Windows Graphics Capture of a single window is unaffected by whatever is
    drawn on top of it, so there is no self-capture loop - which is the whole
    reason for this mode: the overlay no longer has to hide from screen
    capture, and an outside recorder can see it. hwnd = 0 turns it off.
    """
    worker.stdin.write(struct.pack(WGC_FMT, WGC_MAGIC, int(width), int(height),
                                   0, int(pts), int(hwnd)))
    worker.stdin.flush()


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


DWMWA_EXTENDED_FRAME_BOUNDS = 9


def window_frame_rect(hwnd: int):
    """The window's visible bounds as the compositor sees them: (x, y, w, h).

    Not GetWindowRect: that one includes the invisible resize border - several
    pixels of nothing on each side - so an overlay placed by it sits visibly
    off the window. The DWM answer is also the rectangle Windows Graphics
    Capture hands over, which is what the picture has to line up with.
    Returns None when the window is gone.
    """
    r = _RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(int(hwnd)), ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(hwnd)),
                                                  ctypes.byref(r)):
            return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def foreign_foreground() -> int:
    """The focused window, unless it is one of ours. 0 when there is none.

    "Ours" matters because the hotkey may be pressed while the menu has the
    focus, and capturing our own overlay is exactly the loop this mode exists
    to avoid.
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return 0
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return 0
    if _is_desktop_window(hwnd):
        return 0
    return int(hwnd)


def _is_desktop_window(hwnd: int) -> bool:
    """Progman / WorkerW - the desktop itself, not a window to capture.

    Clicking the desktop before Num5 made the capture target the wallpaper
    (Progman): the overlay then showed the desktop with every real window
    transparent above it. The desktop is not a window - exclude it (user
    report: "only the desktop is shown, the windows are transparent").
    """
    user32 = ctypes.windll.user32
    cls = ctypes.create_unicode_buffer(64)
    if not user32.GetClassNameW(hwnd, cls, 64):
        return False
    return cls.value in ("Progman", "WorkerW")


def window_under_cursor() -> int:
    """The topmost real window under the mouse, 0 when none/ours/desktop.

    The Num5 alternative to the focused window: point at the window you want
    and press. Works on the desktop too - the focused window there is
    Progman, which is not capturable.
    """
    user32 = ctypes.windll.user32
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return 0
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return 0
    # WindowFromPoint can return a child (a button inside Chrome); walk up
    # to the top-level owner.
    top = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    if not top:
        top = hwnd
    if not user32.IsWindowVisible(top) or user32.IsIconic(top):
        return 0
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(top, ctypes.byref(pid))
    if pid.value == os.getpid():
        return 0
    if _is_desktop_window(top):
        return 0
    return int(top)


def _is_taskbar_window(hwnd: int) -> bool:
    """A window that shows in the taskbar: top-level, not owned, not a tool
    window.

    Background processes keep hidden helper windows (owned or tool
    windows) that EnumWindows still reports - they are not real desktop
    apps and must not appear in the window list (user report: "сторонние
    процессы попадают в список").
    """
    user32 = ctypes.windll.user32
    if user32.GetWindow(hwnd, 4):  # GW_OWNER
        return False
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x00000080:  # WS_EX_TOOLWINDOW
        return False
    cloaked = ctypes.c_int(0)
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
        return False  # DWM_CLOAKED: hidden from the taskbar (TextInputHost,
        # the Settings helper windows, ...)
    return True


def list_capturable_windows() -> list[tuple[int, str]]:
    """Visible top-level windows that can be captured: (hwnd, title).

    The window list for the menu. Excludes our own windows, the desktop
    (Progman/WorkerW), windows without a title and windows that do not
    show in the taskbar (owned/tool windows of background processes).
    """
    user32 = ctypes.windll.user32
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        if _is_desktop_window(hwnd) or not _is_taskbar_window(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title:
            out.append((int(hwnd), title))
        return True

    user32.EnumWindows(cb, 0)
    return out


def send_gray(worker: subprocess.Popen, width: int, height: int,
              name: str, flags: int = 0, pts: int = 0) -> None:
    """GRAY: give the worker the name of the reverse mapping for luminance.

    In DDA mode the worker writes a downsample of the screen there (width x
    height, usually 320x180 = the flow field size) and Python reads it for
    guides. width=height=0 turns the reverse channel off.
    """
    if len(name) >= 64:
        raise ValueError("the gray section name is longer than 63 characters")
    worker.stdin.write(struct.pack(GRAY_FMT, GRAY_MAGIC, int(width), int(height),
                                   int(flags), int(pts), name.encode("ascii")))
    worker.stdin.flush()


def send_out(worker: subprocess.Popen, width: int, height: int,
             name: str, flags: int = 0, pts: int = 0) -> None:
    """OUTS: give the worker the name of the section for the result pixels.

    width=height=0 turns the channel off and the pixels travel inline through
    the pipe again.
    """
    if len(name) >= 64:
        raise ValueError("the out section name is longer than 63 characters")
    worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, int(width), int(height),
                                   int(flags), int(pts), name.encode("ascii")))
    worker.stdin.flush()


class WorkerReader:
    """The permanent reader thread for the worker's stdout (one per worker).

    Created in start_worker, it lives as long as the worker does and dies on
    EOF: shutdown_worker terminates the process -> the pipe closes -> read()
    returns b"" -> _read_exact raises EOFError -> a sentinel goes into the
    queue.

    A replacement for the old recv_frame (a thread per EVERY frame): on a
    timeout the reader thread does NOT hang on read() - it keeps reading the
    following frames, main simply did not get its answer in time. On a restart
    the old reader dies on the EOF of the old stdout and physically cannot
    read the data of the new worker (different pipes) - there is no read race.
    """

    def __init__(self, worker: subprocess.Popen, width: int, height: int,
                 shm: "SharedFrameBuffer | None" = None):
        self._worker = worker
        self._width = width
        self._height = height
        # The pixels arrive through it once the OUTS channel is agreed.
        self._shm = shm
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="worker-reader")
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                magic_raw = _read_exact(self._worker.stdout, 4)
                magic = struct.unpack("<I", magic_raw)[0]
                if magic == MOTION_ACK_MAGIC:
                    # MACK: acknowledgement of MOTS
                    rest = _read_exact(self._worker.stdout, struct.calcsize(MOTION_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(MOTION_ACK_FMT, magic_raw + rest)
                    self._queue.put(("mack", ok))
                elif magic == WINDOW_ACK_MAGIC:
                    # WACK: acknowledgement of WNDO - the window is up or closed
                    rest = _read_exact(self._worker.stdout, struct.calcsize(WINDOW_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(WINDOW_ACK_FMT, magic_raw + rest)
                    self._queue.put(("wack", ok))
                elif magic == SHM_ACK_MAGIC:
                    # SACK: acknowledgement of SHMI - the worker opened the mapping
                    rest = _read_exact(self._worker.stdout, struct.calcsize(SHM_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(SHM_ACK_FMT, magic_raw + rest)
                    self._queue.put(("sack", ok))
                elif magic == RESIZE_ACK_MAGIC:
                    # RACK (24 bytes): acknowledgement of RNSZ - we put it in
                    # the queue, main takes it via wait_rack()
                    rest = _read_exact(self._worker.stdout, struct.calcsize(RACK_FMT) - 4)
                    _magic, ok, ngx_result, _reserved, _pts = struct.unpack(RACK_FMT, magic_raw + rest)
                    self._queue.put(("rack", (ok, ngx_result)))
                elif magic == DDA_ACK_MAGIC:
                    # DACK (24 bytes): acknowledgement of DDA1 - capture moved to the worker
                    rest = _read_exact(self._worker.stdout, struct.calcsize(DDA_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(DDA_ACK_FMT, magic_raw + rest)
                    self._queue.put(("dack", ok))
                elif magic == WGC_ACK_MAGIC:
                    # WGAK (24 bytes): acknowledgement of WGCW. It carries the
                    # size the window capture really produces - physical
                    # pixels, which is what the pipeline has to be built for.
                    rest = _read_exact(self._worker.stdout, struct.calcsize(WGC_ACK_FMT) - 4)
                    _magic, ok, aw, ah, _pts = struct.unpack(WGC_ACK_FMT, magic_raw + rest)
                    self._queue.put(("wgak", (ok, aw, ah)))
                elif magic == OUTS_ACK_MAGIC:
                    rest = _read_exact(self._worker.stdout,
                                       struct.calcsize(OUTS_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(
                        OUTS_ACK_FMT, magic_raw + rest)
                    self._queue.put(("outs", ok))
                elif magic == GRAY_ACK_MAGIC:
                    # GAK: acknowledgement of GRAY - the reverse luminance channel is open
                    rest = _read_exact(self._worker.stdout, struct.calcsize(GRAY_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(GRAY_ACK_FMT, magic_raw + rest)
                    self._queue.put(("gak", ok))
                elif magic == OUT_MAGIC:
                    rest = _read_exact(self._worker.stdout, struct.calcsize(OUT_FMT) - 4)
                    _magic, out_index, ok, byte_count, ngx_result, _pts = struct.unpack(OUT_FMT, magic_raw + rest)
                    if not ok:
                        raise RuntimeError(f"worker answered with an error for frame {out_index}: ok={ok}")
                    if ngx_result != 1:
                        raise RuntimeError(
                            f"NGX evaluation failed on frame {out_index}: 0x{ngx_result:08X}")
                    if byte_count == 0:
                        # WNDO mode: the worker showed the frame in its own
                        # window, no pixels go through the pipe
                        self._queue.put((out_index, None))
                        continue
                    if byte_count == OUT_BYTES_IN_SHM:
                        # The pixels are in the OUTS section. The copy is made
                        # here, in the reader thread: main is waiting for the
                        # frame anyway, and this way the copy does not pile
                        # onto its thread along with everything else.
                        if self._shm is None or self._shm._out_buf is None:
                            raise RuntimeError(
                                "the worker said the pixels are in shared "
                                "memory, but the section is not open")
                        frame = self._shm.read_out()
                        if frame is None:
                            # A torn frame (seqlock retries exhausted): skip
                            # it, but keep the protocol paired - main treats
                            # None as "frame not ready" and moves on.
                            self._queue.put((out_index, None))
                            continue
                        self._queue.put((out_index, frame))
                        continue
                    if byte_count != self._width * self._height * 4:
                        raise RuntimeError(
                            f"worker returned {byte_count} bytes instead of {self._width * self._height * 4}")
                    data = _read_exact(self._worker.stdout, byte_count)
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(self._height, self._width, 4)
                    self._queue.put((out_index, frame))
                else:
                    raise RuntimeError(f"invalid magic in the worker reply: 0x{magic:08X}")
        except Exception as exc:
            # EOF (the worker exited or was killed) or a protocol error - sentinel
            self._queue.put((None, exc))

    def set_output_size(self, width: int, height: int) -> None:
        """Change the expected size of the output frames (right after RNSZ)."""
        self._width = width
        self._height = height

    def wait_mack(self, timeout: float) -> None:
        """Wait for MACK - the acknowledgement of the motion field size (MOTS)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge MOTS within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "mack":
                if not payload:
                    raise RuntimeError("the worker could not enable GPU motion upscaling")
                return

    def wait_wack(self, timeout: float) -> None:
        """Wait for WACK - the acknowledgement of the WNDO command."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge WNDO within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "wack":
                if not payload:
                    raise RuntimeError("the worker could not raise the output window")
                return

    def wait_dack(self, timeout: float) -> None:
        """Wait for DACK - the acknowledgement of DDA1 (capture in the worker)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge DDA1 within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "dack":
                if not payload:
                    raise RuntimeError("the worker could not enable screen capture")
                return

    def wait_wgak(self, timeout: float) -> tuple:
        """Wait for WGAK - the acknowledgement of WGCW; returns the capture size."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge WGCW within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "wgak":
                ok, aw, ah = payload
                if not ok:
                    raise RuntimeError("the worker could not capture that window")
                return aw, ah

    def wait_gak(self, timeout: float) -> None:
        """Wait for GAK - the acknowledgement that the reverse gray channel is open."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge GRAY within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "gak":
                if not payload:
                    raise RuntimeError("the worker could not open the gray channel")
                return

    def wait_oak(self, timeout: float) -> None:
        """Wait for OAK2 - the acknowledgement of the shared-memory pixel channel."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge OUTS within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "outs":
                if not payload:
                    raise RuntimeError("the worker could not open the pixel channel")
                return

    def wait_sack(self, timeout: float) -> None:
        """Wait for SACK - the shared memory acknowledgement (SHMI)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the worker did not acknowledge SHMI within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
            if got == "sack":
                if not payload:
                    raise RuntimeError("the worker could not open the shared memory")
                return

    def wait_rack(self, timeout: float) -> None:
        """Wait for RACK - the acknowledgement of a resolution change (RNSZ).

        Frames that arrived before RACK (after a recv timeout) are skipped.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"the worker did not acknowledge the resolution change within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got == "rack":
                ok, ngx_result = payload
                if not ok:
                    raise RuntimeError(f"RNSZ rejected by the worker: ngx=0x{ngx_result:08X}")
                return
            # (index, frame) - a frame from before RACK - skip it

    def recv(self, index: int, timeout: float):
        """Wait for frame index; timeout > 0 guards against an NGX hang.

        Returns an np.ndarray with the pixels, or None if the worker showed
        the frame in its own window (WNDO mode) and sent no pixels.

        Replies with a foreign index (frames main no longer waits for after a
        timeout) are dropped - the protocol cannot desynchronise.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"the worker has been silent for {timeout:.0f}s on frame {index} - NGX did not answer after the restart")
            try:
                got_index, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue  # the loop raises TimeoutError itself once the deadline passes
            if got_index is None:
                if isinstance(payload, Exception):
                    raise payload
                raise EOFError("the worker stopped")
            if got_index == index:
                return payload
            # A reply for a frame main no longer waits for (after a timeout) - skip


def check_worker(worker: subprocess.Popen, logs: list[str]) -> None:
    """If the worker died - print the last stderr lines and raise."""
    code = worker.poll()
    if code is not None:
        tail = "\n".join(logs[-40:]) or "(stderr empty)"
        raise RuntimeError(
            f"the NGX worker exited with code {code}.\n"
            f"last stderr lines:\n{tail}"
        )


def shutdown_worker(worker: subprocess.Popen, stop: threading.Event | None = None) -> None:
    """Graceful shutdown: close stdin (EOF -> the worker exits with code 0), wait 10 s.

    stop is the finish event for _drain_stderr (from start_worker): it is set
    immediately so the drain thread does not hang on readline() of a closed
    stderr (on Windows closing a pipe from another thread does not wake
    readline - the thread exits only on EOF after the process dies, or on
    stop).
    """
    if worker.poll() is not None:
        if stop is not None:
            stop.set()
        return
    if stop is not None:
        stop.set()
    try:
        if worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
    except OSError:
        pass
    try:
        code = worker.wait(timeout=10)
        print(f"[main] worker exited cleanly (code {code})")
    except subprocess.TimeoutExpired:
        print("[main] worker did not exit within 10 s - forcing termination")
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()


def restart_worker(worker: subprocess.Popen, params: dict, width: int, height: int,
                   warmup: int, full_w: int = 0, full_h: int = 0,
                   stop: threading.Event | None = None,
                   shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str], WorkerReader, threading.Event]:
    """Restart the worker at a new resolution (a work_scale change).

    The worker creates the NGX feature from the header sizes and reads exactly
    w*h*4 bytes per frame - the resolution cannot be changed on the fly, only
    by a restart. Warmup on a restart is smaller (30) so the screen does not
    freeze.

    A 2 s pause between shutdown and start: the old worker holds the NGX GPU
    resources (nvngx_dlssnr.dll, 165 MB + a D3D12 device) - initialising a new
    process concurrently on the same GPU hangs or kills it (observed: exit 127
    and a hung recv after apply_settings).

    The old reader/drain die on EOF of the old worker's closed pipes
    (shutdown_worker terminates the process) - there is no read race with the
    new worker: the pipes are different and the old thread physically cannot
    read the stdout of the new process.
    """
    shutdown_worker(worker, stop)
    time.sleep(2.0)
    return start_worker(params, width, height, warmup, full_w, full_h, shm)


def hotkey_labels(bindings: dict) -> dict:
    """Bindings -> {command: "Num1"} for the captions on the menu buttons."""
    return {cmd: name for _mods, _vk, cmd, name in bindings.values()}


def _autostart_enabled() -> bool:
    """Is autostart currently on? (HKCU Run, the NeuralScreen value)."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "NeuralScreen")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _set_autostart(enabled: bool) -> bool:
    """Enable/disable autostart with Windows (HKCU Run).

    We launch NeuralScreen.vbs through wscript - a hidden launcher with no
    console. Returns True on success.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            vbs = str(BASE_DIR / "NeuralScreen.vbs")
            winreg.SetValueEx(key, "NeuralScreen", 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        else:
            try:
                winreg.DeleteValue(key, "NeuralScreen")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"[main] autostart not configured: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="DLSS 5 Desktop NR prototype")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json",
                        help="path to config.json (defaults to next to main.py)")
    args = parser.parse_args()
    _init_logging()  # pythonw: stdout/stderr -> NeuralScreen.log

    # One instance only: two copies fight over the screen capture (the
    # second one gets a dead DDA and the first one loses frames). The
    # mutex is the standard Windows single-instance mechanism - it lives
    # in the kernel and dies with the process, so a crashed copy does not
    # block the next launch.
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "NeuralScreen_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[main] another NeuralScreen is already running - this copy exits", file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    params = resolve_params(cfg)
    width, height = int(cfg["width"]), int(cfg["height"])
    monitor = int(cfg["monitor"])
    warmup = int(cfg["warmup"])
    work_scale = float(cfg["work_scale"])
    # The worker reads NS_NR_SMALL once, at startup: with it on, Neural
    # Rendering runs at the work resolution and the result is scaled back up
    # instead of the network chewing the whole screen. Off by default - it is
    # faster but softer, and an update must not change how the picture looks
    # without being asked. Toggling it later restarts the worker, which is why
    # it lives in the environment rather than in the frame protocol.
    nr_small = bool(cfg.get("nr_small", False))
    os.environ["NS_NR_SMALL"] = "1" if nr_small else "0"
    lang = str(cfg["lang"])

    # The output resolution comes FROM THE REAL MONITOR, not from a stale
    # config.json (the monitor may have been switched to 1440p while the
    # config still remembers 4K - the overlay, the recording and the worker
    # window would start drifting away from the screen).
    capture = ScreenCapture(monitor_idx=monitor)
    mon_w, mon_h = capture.resolution
    if mon_w > 0 and mon_h > 0 and (mon_w, mon_h) != (width, height):
        print(f"[main] monitor {monitor} is {mon_w}x{mon_h} (config: {width}x{height}), "
              f"taking the real resolution")
        width, height = mon_w, mon_h

    print(f"[main] NeuralScreen - profile {cfg['profile']!r}, "
          f"resolution {width}x{height}, monitor {monitor}")
    print(f"[main] NGX parameters: {params}")
    print(f"[main] work_scale {work_scale:.2f} (NGX resolution "
          f"{int(width * work_scale)}x{int(height * work_scale)})")

    worker: subprocess.Popen | None = None
    reader: WorkerReader | None = None
    worker_stop: threading.Event | None = None
    shm: SharedFrameBuffer | None = None
    display: Display | None = None
    tray: TrayController | None = None
    hotkeys: HotkeyController | None = None
    recorder: VideoRecorder | None = None
    try:
        # The worker and guides run at the work resolution (the NGX feature is
        # created from the header sizes; guides' assert requires them to match)
        work_w, work_h = _work_size(width, height, work_scale)
        # The v3 protocol (full_w/full_h) ONLY when work != full: at work==full
        # (scale 1.0) the worker crashes or hangs in upscale mode (verified in
        # isolation) - we use legacy full_w=0, as in D5V2.
        full_w = width if (work_w != width or work_h != height) else 0
        full_h = height if (work_w != width or work_h != height) else 0
        # Shared memory for the input frame: its size does not depend on
        # work_scale (see SharedFrameBuffer), so it is created once per process.
        shm = SharedFrameBuffer(width, height)
        # Which card this is and whether NR works on it. The model comes from
        # nvapi, but the support verdict comes from the worker rather than the
        # architecture: only it knows whether feature 18 was created.
        gpu_info = gpu_probe()
        gpu_text = gpu_describe(gpu_info)
        gpu_ok: bool | None = None
        print(f"[main] GPU: {gpu_text or 'unknown'} "
              f"(group 0x{gpu_info['arch_group']:X}, officially supported: "
              f"{'yes' if gpu_info['official'] else 'no'})")
        # The stock warm-up is 120 discarded evaluations. On a fast Blackwell
        # card that is a second or two; on Turing/Ampere/Ada it can take far
        # longer than the frame watchdog, which then kills the worker on
        # frame 0 and starts a restart storm (seen on RTX 2070 at ~1 FPS and
        # on RTX 3060 Ti at ~18 FPS). Unsupported/pre-Blackwell cards get a
        # short warm-up; the actual effect is still evaluated normally
        # afterwards.
        effective_warmup = warmup
        if not gpu_info["official"] and warmup > 4:
            effective_warmup = 4
            print(f"[main] pre-Blackwell GPU: warmup {warmup} -> "
                  f"{effective_warmup} to avoid a false frame-0 watchdog "
                  f"timeout")
        worker, worker_logs, reader, worker_stop = start_worker(
            params, work_w, work_h, effective_warmup, full_w, full_h, shm)
        print(f"[main] worker started (pid {worker.pid}), header sent "
              f"({work_w}x{work_h})")

        print(f"[main] capturing monitor {monitor}: {capture.resolution}")

        display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
        display.set_lang(lang)
        # The program draws over the desktop and gives no sign of itself -
        # without this it is unclear after launch whether it is running.
        startup_menu = bool(cfg.get("open_menu_on_start", True))
        # The before/after wipe: the share of the frame the worker leaves raw.
        split_pos = min(1.0, max(0.0, float(cfg.get("split", 0.0))))
        startup_pending = True
        # The menu size, position and theme - exactly as the user left them.
        display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
        saved_theme = cfg.get("theme")
        if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
            display.menu.set_state({"theme": saved_theme})
        saved_offset = cfg.get("menu_offset")
        if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
            display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
        saved_height = cfg.get("menu_height")
        if isinstance(saved_height, (int, float)) and saved_height > 0:
            display.menu.user_height = int(saved_height)
        print(f"[main] output window {display.width}x{display.height}")

        # Tray icon: commands go into a queue, the main loop reads them
        tray_commands: queue.Queue = queue.Queue()
        # Answers from the "Save as" dialog. The dialog is modal and lives in
        # its own thread (see _open_save_dialog); the path arrives here.
        shot_paths: queue.Queue = queue.Queue()
        shot_dialog_open = False
        tray = TrayController(tray_commands, labels={
            "settings": UI_STRINGS[lang].get("settings_title", "Settings"),
            "quit": UI_STRINGS[lang].get("exit", "Exit"),
        })
        tray._set_state(nr=True, scale=work_scale)
        tray.start()
        print("[main] tray icon started")

        # Taskbar button: the overlay and the worker window are tool
        # windows, so the program lived only in the tray. A 1x1 APPWINDOW
        # window gives the program a real taskbar button; clicking it sends
        # the same "settings" command as a left click on the tray (user
        # rule 2026-09-09: the program must always show in the taskbar).
        taskbar = TaskbarWindow(tray_commands, "NeuralScreen")
        taskbar.start()
        print("[main] taskbar window started")

        # Global hotkeys: RegisterHotKey rather than polling the key state.
        # The system gives the keypress to us alone and does not pass it to the
        # active application - Num1 inside a game toggles NR and the game never
        # sees the key (the polling fallback does not swallow it, but the numpad
        # is free in games). The commands go into the same queue the tray uses. The
        # user's bindings come from config.json ("hotkeys": {"toggle": "Num1", ...}).
        hotkey_overrides = cfg.get("hotkeys")
        if not isinstance(hotkey_overrides, dict):
            hotkey_overrides = {}
        hotkey_bindings = build_bindings(hotkey_overrides)
        hotkeys = HotkeyController(tray_commands, hotkey_bindings)
        hotkeys.start()
        if hotkeys.registered:
            print(f"[main] hotkeys registered: {', '.join(hotkeys.registered)} "
                  f"({describe_hotkeys(hotkey_bindings)})")
        if hotkeys.failed:
            print(f"[main] hotkeys taken by another program: {', '.join(hotkeys.failed)}",
                  file=sys.stderr)
        # The numpad sends different key codes with Num Lock off, so those
        # bindings do not misbehave - they are simply absent. Say so, or it
        # looks like the program ignores the keyboard.
        numpad = numlock_needed(hotkey_bindings)
        if numpad and not numlock_on():
            print(f"[main] Num Lock is off: the numpad hotkeys "
                  f"({', '.join(numpad)}) will not fire until it is on",
                  file=sys.stderr)
            display.alert(UI_STRINGS[lang]["numlock_off"], duration=6.0)
        # The captions on the menu buttons come from the same bindings that were
        # registered. Strictly after build_bindings: before that they do not exist.
        display.menu.set_hotkeys(hotkey_labels(hotkey_bindings))

        # The settings live in the overlay menu (Num2). There is no separate
        # window any more: it was a second interface over the same fields, it
        # stole focus from the game and dragged the whole of tcl/tk into the
        # runtime.

        guides = TemporalGuideGenerator(work_w, work_h)

        # A reused buffer: every frame allocated ~100 MB (a 4K grab plus the
        # resizes plus flow), the GC could not keep up -> OOM around frame 1900.
        # The buffer is reused through cv2.resize(dst=...). work/out buffers are
        # not needed: in v3 the full->work->full resize is done by the worker on
        # the GPU (NGX Upscaling).
        buf_full = np.empty((height, width, 4), dtype=np.uint8)

        paused = False
        # The worker died and exhausted the restart budget: the pipeline is
        # stopped (no send/recv, no more restarts) and the overlay is hidden
        # so the desktop is not covered by a black window (issue #3: black
        # screen on a GPU where feature 18 cannot be created). Cleared when
        # the user turns NR back on.
        worker_failed = False
        frame_index = 0
        pts = 0
        guide = None  # initialised before the loop: Num1 before the first NR frame must not raise NameError
        output_rgba = None  # the last NR frame (for a screenshot); None until the first one
        # WNDO mode: the worker shows the frame, no pixels come back to Python.
        want_present = bool(cfg.get("worker_present", True))
        want_motion_small = bool(cfg.get("motion_on_gpu", True))
        want_dda = bool(cfg.get("capture_in_worker", True))  # DDA: the worker takes the colour
        # The result pixels come back through shared memory, not the pipe.
        want_out_shm = bool(cfg.get("pixels_in_shm", True))
        # System audio ("what you hear") as a second track in the recording.
        # A config flag rather than a menu item: it is a decision made once,
        # not something to reach for while the overlay is up.
        record_audio = bool(cfg.get("record_audio", True))
        out_shm = False
        out_attempted = False
        motion_small = False  # the worker upscales the motion field itself
        motion_attempted = False  # already tried for the current worker
        present_mode = False      # the worker window is up right now
        present_attempted = False  # already tried for the current worker (do not spam)
        dda_mode = False          # the worker captures the screen itself
        dda_attempted = False     # already tried for the current worker (do not spam)
        window_hwnd = None        # WGCW target; None = the whole desktop (DDA1)
        last_foreground = 0       # the last focused window that was not ours
        follow_pos = None         # where the overlay currently sits (window mode)
        follow_resize = None      # a pending size change, waiting to settle
        mon_w, mon_h = width, height  # the full monitor size (for the menu layer)
        gray_active = False       # guides take luminance from the worker's gray channel
        pending_shot: Path | None = None  # a screenshot waiting for a frame with pixels
        recorder: VideoRecorder | None = None  # recording (Num0), MP4 AV1 NVENC
        work_frame = None  # the current work frame; None -> grab at the top of the loop
        fps_window: list[float] = []
        last_log = time.monotonic()
        last_fps = 0.0
        # Stage timings: mean ms over PERF_LOG_INTERVAL (the [perf] log)
        perf: dict[str, list[float]] = {k: [] for k in PERF_KEYS}
        last_perf_log = time.monotonic()

        def _ask_save_path(parent_hwnd: int, default_name: str) -> Path | None:
            """The native "Save as" dialog (GetSaveFileNameW).

            Returns the chosen path, or None on cancel. The JPEG filter is the
            default; the extension is appended when the user leaves it out.
            """
            try:
                import ctypes
                from ctypes import wintypes

                class OPENFILENAME(ctypes.Structure):
                    _fields_ = [
                        ("lStructSize", wintypes.DWORD),
                        ("hwndOwner", wintypes.HWND),
                        ("hInstance", wintypes.HINSTANCE),
                        ("lpstrFilter", wintypes.LPCWSTR),
                        ("lpstrCustomFilter", wintypes.LPWSTR),
                        ("nMaxCustFilter", wintypes.DWORD),
                        ("nFilterIndex", wintypes.DWORD),
                        ("lpstrFile", wintypes.LPWSTR),
                        ("nMaxFile", wintypes.DWORD),
                        ("lpstrFileTitle", wintypes.LPWSTR),
                        ("nMaxFileTitle", wintypes.DWORD),
                        ("lpstrInitialDir", wintypes.LPCWSTR),
                        ("lpstrTitle", wintypes.LPCWSTR),
                        ("Flags", wintypes.DWORD),
                        ("nFileOffset", wintypes.WORD),
                        ("nFileExtension", wintypes.WORD),
                        ("lpstrDefExt", wintypes.LPCWSTR),
                        ("lCustData", wintypes.LPARAM),
                        ("lpfnHook", wintypes.LPVOID),
                        ("lpTemplateName", wintypes.LPCWSTR),
                        ("pvReserved", wintypes.LPVOID),
                        ("dwReserved", wintypes.DWORD),
                        ("FlagsEx", wintypes.DWORD),
                    ]

                buf = ctypes.create_unicode_buffer(1024)
                buf.value = default_name
                ofn = OPENFILENAME()
                ofn.lStructSize = ctypes.sizeof(OPENFILENAME)
                ofn.hwndOwner = parent_hwnd or None
                ofn.lpstrFilter = "JPEG image (*.jpg)\0*.jpg\0PNG image (*.png)\0*.png\0All files (*.*)\0*.*\0"
                ofn.lpstrFile = buf
                ofn.nMaxFile = 1024
                ofn.lpstrDefExt = "jpg"
                ofn.Flags = 0x00000002 | 0x00000008  # OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                ok = ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn))
                if not ok:
                    return None
                path = Path(buf.value.strip())
                if not path.suffix:
                    path = path.with_suffix(".jpg")
                return path
            except Exception as exc:
                print(f"[main] save dialog unavailable ({exc}) - "
                      f"screenshot goes to screenshots/", file=sys.stderr)
                shot_dir = BASE_DIR / "screenshots"
                shot_dir.mkdir(exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
                return shot_dir / f"neuralscreen-{stamp}.jpg"

        def _save_screenshot(path: Path, rgba) -> None:
            """Save the frame as a maximum-quality JPEG.

            An open menu ends up in the screenshot: our layer is excluded from
            capture, so we draw it onto the frame ourselves.
            """
            try:
                surf = pygame.image.frombuffer(
                    rgba, (rgba.shape[1], rgba.shape[0]), "RGBX")
                display.draw_capture_overlay(surf)
            except Exception as exc:
                print(f"[main] menu was not baked into the screenshot: {exc}", file=sys.stderr)
            try:
                import cv2 as _cv2
                path.parent.mkdir(parents=True, exist_ok=True)
                ok = _cv2.imwrite(str(path),
                                  _cv2.cvtColor(rgba, _cv2.COLOR_RGBA2BGRA),
                                  [_cv2.IMWRITE_JPEG_QUALITY, 100])
                if ok:
                    print(f"[main] screenshot: {path}")
                    display.alert(f"Screenshot: {path.name}")
                else:
                    print(f"[main] failed to write the screenshot: {path}", file=sys.stderr)
            except Exception as exc:
                print(f"[main] screenshot failed: {exc}", file=sys.stderr)

        def _perf(key: str, t0: float) -> None:
            """Record the stage duration (ms) into the timings dictionary."""
            perf[key].append((time.perf_counter() - t0) * 1000.0)
        running = True
        # Protection against rapid changes (arrow key repeat, a jerked slider):
        # the intermediate values are coalesced and only the last one is applied.
        # 0.5 s rather than 2 s: the change goes through RNSZ inside the live
        # worker process, not through a restart with an NGX init/shutdown plus
        # sleep(2) - the expensive path is only a fallback now.
        RESTART_COOLDOWN = 0.5  # seconds
        RESTART_WARMUP = 10     # warmup after a resolution change (do not freeze the screen)
        RACK_TIMEOUT = 20.0     # seconds to wait for RACK after RNSZ
        last_restart = 0.0
        pending_apply: tuple | None = None  # the deferred (scale, profile, params)
        # Auto-recovery limit: if the worker dies N times in a row we turn NR
        # off (pause) and raise an alert instead of spinning through restarts.
        MAX_CONSECUTIVE_RESTARTS = 3
        consecutive_restarts = 0
        guide_fails = 0

        def _recreate_capture() -> None:
            """Recreate the capture (a fresh DDA session) after a failure or mode change."""
            nonlocal capture
            try:
                capture.close()
            except Exception:
                pass
            capture = ScreenCapture(monitor_idx=monitor)

        def _safe_grab() -> np.ndarray | None:
            """grab() that recreates the capture on failure.

            Launching a game in fullscreen invalidates Desktop Duplication
            (DXGI_ERROR_ACCESS_LOST / a mode change) - dxcam may raise instead
            of returning None. We recreate the DDA session and return None (the
            loop skips the iteration).
            """
            nonlocal capture
            try:
                return capture.grab()
            except Exception as exc:
                print(f"[main] capture failed ({exc}) - recreating the DDA session")
                try:
                    _recreate_capture()
                except Exception as exc2:
                    print(f"[main] recreating the capture failed: {exc2}",
                          file=sys.stderr)
                return None

        def _do_restart(new_scale: float, new_profile: str, new_params: dict,
                        full: bool = False, new_small: bool | None = None) -> None:
            """Change work_scale/profile/parameters WITHOUT recreating pygame or the capture.

            The main path is RNSZ: the worker recreates the NGX feature inside
            the same process and answers RACK (~0.3 s instead of ~3 s for a
            restart). If RNSZ did not go through - a full restart of the worker
            process.

            CRITICAL: guides is recreated at the NEW work resolution and
            assigned to the outer variable (nonlocal guides). The assignment
            used to be local - the outer guides stayed at the old size and main
            sent motion of the old size, while the worker reads exactly
            new_w*new_h*4 bytes:
              * scaling up   -> the worker waits for the missing bytes and goes
                silent, main hangs in reader.recv(60 s), the window stops
                pumping messages -> Application Hang (Event Id 1002) -> exit 127;
              * scaling down -> the extra bytes desynchronise the stream, the
                worker sees a foreign magic and exits -> BrokenPipe -> a restart
                loop.
            Reproduced in isolation: _work/test_stale_motion_repro.py (case A -
            TimeoutError, B - BrokenPipeError, C - the control, OK).
            pygame/D3D11 had nothing to do with the crashes.
            """
            nonlocal work_scale, work_w, work_h, params, frame_index, pts, work_frame
            nonlocal nr_small
            nonlocal worker, worker_logs, reader, worker_stop, last_restart
            nonlocal guides  # without this main sends motion of the old size
            work_scale = new_scale
            cfg["profile"] = new_profile
            params = new_params
            if new_small is not None and new_small != nr_small:
                nr_small = new_small
                cfg["nr_small"] = nr_small
                # The environment is what a freshly started worker reads; the
                # live one is told through the resize below.
                os.environ["NS_NR_SMALL"] = "1" if nr_small else "0"
                _save_menu_layout()
            new_w, new_h = _work_size(width, height, work_scale)
            new_full_w = width if (new_w != width or new_h != height) else 0
            new_full_h = height if (new_w != width or new_h != height) else 0
            print(f"[main] applying: profile {new_profile!r}, "
                  f"work_scale {work_scale:.2f} ({new_w}x{new_h}), params {params}")
            display.alert(UI_STRINGS[lang]["settings_applied"])

            applied = False
            if worker.poll() is None and not full:
                try:
                    t_rnsz = time.perf_counter()
                    send_resize(worker, params, new_w, new_h, RESTART_WARMUP,
                                new_full_w, new_full_h, nr_small)
                    reader.wait_rack(timeout=RACK_TIMEOUT)
                    reader.set_output_size(new_full_w or new_w, new_full_h or new_h)
                    applied = True
                    print(f"[main] RNSZ applied: {new_w}x{new_h} in "
                          f"{(time.perf_counter() - t_rnsz) * 1000:.0f} ms")
                except Exception as exc:
                    print(f"[main] RNSZ did not go through ({exc}) - full worker restart",
                          file=sys.stderr)
            if not applied:
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, new_w, new_h, RESTART_WARMUP,
                    new_full_w, new_full_h, worker_stop, shm)
                _forget_present()
                # The new worker knows nothing about DDA/gray: reset the flags
                # so the main loop sends DDA1/GRAY again. Otherwise the frames
                # go out with NO_COLOR to a worker that is not capturing - a
                # desync and a restart loop.
                _forget_dda()
                _forget_out()

            # The order matters: work_w/work_h and guides change TOGETHER,
            # otherwise the motion size drifts away from what the worker
            # expects (see the docstring).
            work_w, work_h = new_w, new_h
            guides = TemporalGuideGenerator(work_w, work_h, emit_small=motion_small)
            _sync_motion_size()  # the flow resolution may have changed
            _sync_gray()         # the gray channel lives in the worker, size = guides flow
            frame_index = 0
            pts = 0
            work_frame = None  # the indices are reset - a fresh grab is needed
            tray._set_state(scale=work_scale)
            last_restart = time.monotonic()

        def _switch_monitor(new_monitor: int) -> None:
            """Switch the capture/output monitor - a full pipeline restart.

            The resolution, the capture, the window, the worker and the shm
            are all tied to the monitor - it cannot be switched on the fly.
            Recording stops (the frame size changes). The menu is recreated
            with its theme/language/layout preserved.
            """
            # Everything downstream of the size - the worker, the shm, the
            # overlay, the flags - is rebuilt by _rebuild_pipeline, which owns
            # those names; this function only picks the monitor and the size.
            nonlocal monitor, width, height, work_w, work_h, capture
            if new_monitor == monitor:
                return
            print(f"[main] monitor change: {monitor} -> {new_monitor}")
            _teardown_pipeline()
            try:
                capture.close()
            except Exception:
                pass
            # The new monitor: its real resolution.
            monitor = new_monitor
            cfg["monitor"] = monitor
            capture = ScreenCapture(monitor_idx=monitor)
            width, height = capture.resolution
            work_w, work_h = _work_size(width, height, work_scale)
            _rebuild_pipeline(f"Monitor {monitor}: {width}x{height}")

        def _teardown_pipeline() -> None:
            """Stop everything that is sized to the current width/height.

            Shared by the monitor switch and the window switch: the worker,
            the shared memory and a running recording are all built for one
            frame size and cannot survive a change of it.
            """
            nonlocal recorder, pending_shot, worker, worker_stop
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as exc:
                    print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
                recorder = None
            pending_shot = None
            shutdown_worker(worker, worker_stop)
            try:
                shm.close()
            except Exception:
                pass

        def _rebuild_pipeline(note: str) -> None:
            """Build the worker, the shm and the overlay for the current size.

            The second half of what used to be _switch_monitor: it reads the
            nonlocal width/height/work_w/work_h and rebuilds everything that
            depends on them, resetting the per-worker flags so the main loop
            negotiates DDA1/WGCW, GRAY, OUTS and the window again.
            """
            nonlocal shm, worker, worker_logs, reader, worker_stop
            nonlocal display, guides, buf_full
            nonlocal frame_index, pts, work_frame, output_rgba
            nonlocal present_mode, present_attempted, dda_mode, dda_attempted
            nonlocal gray_active, motion_small, motion_attempted, gpu_ok
            nonlocal out_shm, out_attempted
            # Freeze the last picture with a spinner before the old worker
            # dies: the rebuild takes ~1 s (new worker, NGX warm-up) and the
            # bare desktop would flash underneath (user: mode-switch flashes).
            # The overlay spans the whole monitor even when the next mode is
            # one window - no bare desktop at the edges of the spinner.
            display.enter_switch_mode(output_rgba, *capture.resolution)
            menu_was_open = display.menu.visible
            full_w = width if (work_w != width or work_h != height) else 0
            full_h = height if (work_w != width or work_h != height) else 0
            shm = SharedFrameBuffer(width, height)
            worker, worker_logs, reader, worker_stop = start_worker(
                params, work_w, work_h, warmup, full_w, full_h, shm)
            # The window and the menu are rebuilt, keeping the user settings.
            # A soft resize instead of close()+recreate: the old code went
            # through pygame.quit() and built a fresh window - the screen went
            # black for a moment on every Num5 (user: screen flashes on mode
            # switches). The worker and the shm MUST be torn down and rebuilt
            # (new size), the SDL window does not have to be.
            recreated = False
            try:
                display.resize(width, height)
                recreated = False
            except Exception as exc:
                print(f"[main] soft resize failed ({exc}) - recreating the window")
                # The menu is recreated with the window: snapshot its live
                # state (position, scale, height) into cfg so the restore
                # below picks up where the user left it, not the stale
                # values from the last menu close (user rule 10.09: fixed
                # position until the user drags it).
                cfg["menu_offset"] = [int(display.menu.offset[0]),
                                      int(display.menu.offset[1])]
                cfg["menu_scale"] = round(display.menu.user_scale, 2)
                cfg["menu_height"] = (None if display.menu.user_height is None
                                      else int(display.menu.user_height))
                try:
                    display.close()
                except Exception:
                    pass
                display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
                recreated = True
            # In one-window mode the overlay stops hiding from screen capture:
            # the input is that window, not the desktop, so there is no
            # self-capture loop to break - and an outside recorder can see the
            # result. The worker does the same for its picture window.
            display.set_excluded_from_capture(window_hwnd is None)
            display.set_lang(lang)
            display.menu.set_hotkeys(hotkey_labels(hotkey_bindings))
            saved_theme = cfg.get("theme")
            if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
                display.menu.set_state({"theme": saved_theme})
            display.menu.set_state({"lang": lang})
            # The position/scale/height restore applies ONLY to a recreated
            # menu (the window was rebuilt). On a soft resize the menu is
            # alive and keeps exactly what the user set - re-applying the
            # cfg values here would snap it back to the last saved state on
            # every mode switch (user: menu returns to the launch position
            # and scale after picking a window).
            if recreated:
                display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
                saved_offset = cfg.get("menu_offset")
                if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
                    display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
                saved_height = cfg.get("menu_height")
                if isinstance(saved_height, (int, float)) and saved_height > 0:
                    display.menu.user_height = int(saved_height)
            if menu_was_open:
                display.menu.set_state(_menu_payload())
                display.menu.visible = True
                display.set_menu_opaque(True)
                display.set_menu_input(True)
                # The saved offset is honoured as-is: the panel stays where
                # the user left it, clamped to the screen by layout() (user
                # rule 10.09: fixed position until the user drags it).
                if window_hwnd is not None:
                    display.set_fullscreen_layer(mon_w, mon_h)
            # guides and the buffers follow the new resolution.
            guides = TemporalGuideGenerator(work_w, work_h, emit_small=motion_small)
            buf_full = np.empty((height, width, 4), dtype=np.uint8)
            # Pipeline flags - the new worker knows nothing.
            present_mode = False
            present_attempted = False
            dda_mode = False
            dda_attempted = False
            gray_active = False
            motion_small = False
            motion_attempted = False
            out_shm = False
            out_attempted = False
            gpu_ok = None  # a new worker means a new verdict on feature 18
            frame_index = 0
            pts = 0
            work_frame = None
            # The last NR frame belongs to the previous monitor and size.
            # Without the reset a screenshot right after the switch would
            # save it.
            output_rgba = None
            _save_menu_layout()
            print(f"[main] pipeline rebuilt: {width}x{height}, "
                  f"work {work_w}x{work_h} - {note}")
            # The mode-change alert must survive a rebuild: the pipeline
            # teardown clears the alert list, and in a game the user has no
            # time to read a 2.5 s toast. 6 s is long enough to read while
            # the game keeps running (user: "the Num5 alert disappears too
            # fast").
            display.alert(note, duration=6.0)

        def _switch_window(hwnd: int) -> None:
            """Point the capture at one window (hwnd) or back at the desktop (0).

            The window's capture size is not something to guess: GetWindowRect
            includes the invisible resize borders and the DWM frame, while the
            capture produces the compositor's own surface. So the running
            worker is asked first (WGCW answers with the real size), and the
            pipeline is rebuilt for exactly that.
            """
            nonlocal window_hwnd, width, height, work_w, work_h
            nonlocal follow_pos, follow_resize
            if hwnd and not want_dda:
                display.alert(UI_STRINGS[lang]["win_fail"])
                print("[main] window mode needs capture in the worker "
                      "(capture_in_worker is off)", file=sys.stderr)
                return
            # The switch overlay goes up BEFORE the probe: the probe can take
            # ~1 s (WGCW round trip with the running worker) and the old
            # pipeline is already dead by then - without the overlay the
            # desktop sits bare (user: black gap on one-window mode switch).
            # enter_switch_mode is idempotent and covers the rebuild too.
            display.enter_switch_mode(output_rgba, *capture.resolution)
            if hwnd:
                try:
                    aw, ah = _probe_window_capture(hwnd)
                except Exception as exc:
                    # The probe left the worker inside a WGCW session that
                    # may be half-open: put the source back on the desktop
                    # before bailing out (audit #4, F2).
                    try:
                        send_dda(worker, width, height)
                    except Exception:
                        pass
                    display.alert(UI_STRINGS[lang]["win_fail"])
                    print(f"[main] the worker cannot capture that window: {exc}",
                          file=sys.stderr)
                    display.exit_switch_mode()  # the overlay was raised before the probe
                    return
                if aw < 64 or ah < 64:
                    # Below the work-resolution floor there is nothing to
                    # process - and a work size larger than the frame is how
                    # the worker gets killed. The probe above already switched
                    # the worker's source to WGCW as a side effect: put it
                    # back on the desktop, otherwise the frozen tiny window
                    # becomes the picture until the next rebuild (audit #4,
                    # F2).
                    send_dda(worker, width, height)
                    print(f"[main] the window is {aw}x{ah} - too small to process",
                          file=sys.stderr)
                    display.alert(UI_STRINGS[lang]["win_fail"])
                    display.exit_switch_mode()  # the overlay was raised before the probe
                    return
                _teardown_pipeline()
                window_hwnd = int(hwnd)
                width, height = int(aw), int(ah)
                note = UI_STRINGS[lang]["win_mode_on"]
            else:
                _teardown_pipeline()
                window_hwnd = None
                width, height = capture.resolution
                note = UI_STRINGS[lang]["win_mode_off"]
            work_w, work_h = _work_size(width, height, work_scale)
            follow_pos = None        # a fresh overlay starts at (0,0)
            follow_resize = None
            _rebuild_pipeline(note)

        def _follow_window() -> None:
            """Keep the HUD layer on the window being processed.

            Position every frame - it is one DWM call and a SetWindowPos only
            when the window actually moved. A SIZE change is a different
            animal: the worker, the shared memory and every texture are built
            for one frame size, so it means rebuilding the pipeline - and doing
            that on every pixel while someone drags a resize handle would be
            unusable. The new size has to hold still for half a second first.
            """
            nonlocal follow_pos, follow_resize
            if window_hwnd is None:
                return
            # The worker is dead: the overlay must stay hidden (issue #3) -
            # nothing would fill it, and showing it covers the desktop with
            # a black window.
            if worker_failed:
                return
            rect = window_frame_rect(window_hwnd)
            if rect is None:
                return
            x, y, w, h = rect
            if ctypes.windll.user32.IsIconic(ctypes.c_void_p(window_hwnd)):
                # Minimised: the capture goes silent (the worker hides its own
                # window for the same reason), so the HUD goes with it rather
                # than floating over whatever is underneath.
                if display.is_visible():
                    display.set_visible(False)
                    follow_pos = None
                return
            if not display.is_visible():
                display.set_visible(True)
            moved = (x, y) != follow_pos
            # While the menu is open the user may be dragging it by its title
            # bar - following the captured window would yank the HUD (and the
            # menu with it) back onto the window every frame, which is the
            # "does not grab, stutters, flickers" report. The position is
            # re-synced on the first frame after the menu closes.
            if moved and not display.menu.visible:
                display.move_to(x, y)
                follow_pos = (x, y)
            # Both windows are topmost, and within that group the one raised
            # last is on top. The worker re-asserts its picture window every
            # time the target moves, so the HUD has to keep coming back up -
            # otherwise the menu ends up UNDER the picture, invisible both to
            # the user and to a recorder. Measured: without this the menu
            # changed 0% of what an outside capture saw.
            if moved or frame_index % 30 == 0:
                display.raise_topmost()
            if (w, h) != (width, height):
                now = time.monotonic()
                if follow_resize is None or follow_resize[0] != (w, h):
                    follow_resize = ((w, h), now)
                elif now - follow_resize[1] > 0.5:
                    follow_resize = None
                    print(f"[main] the window is now {w}x{h} - rebuilding the pipeline")
                    _switch_window(window_hwnd)
            else:
                follow_resize = None

        def _probe_window_capture(hwnd: int) -> tuple:
            """Ask the CURRENT worker for the capture size of a window.

            It switches that worker's source as a side effect, which is
            harmless: the caller tears it down immediately afterwards.
            """
            send_wgc(worker, hwnd)
            return reader.wait_wgak(timeout=15.0)

        def _enable_out_shm() -> None:
            """OUTS: agree that the result pixels will go through a section.

            Called after every worker start: the command lives inside its
            process and a new one knows nothing about it. A refusal is not
            fatal - the pixels travel down the pipe as before.
            """
            nonlocal out_shm, out_attempted
            out_attempted = True
            if not want_out_shm:
                return
            try:
                shm.open_out(width, height)
                send_out(worker, width, height, shm.out_name)
                reader.wait_oak(timeout=15.0)
                out_shm = True
                print(f"[main] result pixels through shared memory "
                      f"({width}x{height}, {shm.out_bytes / 1024 / 1024:.0f} MB)")
            except Exception as exc:
                out_shm = False
                print(f"[main] shared memory for pixels unavailable ({exc}) - "
                      f"they go through the pipe", file=sys.stderr)

        def _sync_motion_size() -> None:
            """MOTS: agree the motion field resolution with the worker.

            Called after guides is created and after every worker start: the
            command lives inside the worker process and a new one knows
            nothing about it. A refusal is not fatal - we do the upscale on
            the CPU, as before.
            """
            nonlocal motion_small, motion_attempted
            motion_attempted = True
            if not want_motion_small:
                return
            guides.emit_small = True
            try:
                send_motion_size(worker, guides.motion_width, guides.motion_height)
                reader.wait_mack(timeout=15.0)
                motion_small = True
                print(f"[main] motion field {guides.motion_width}x{guides.motion_height} - "
                      f"upscaled by the worker on the GPU")
            except Exception as exc:
                guides.emit_small = False
                motion_small = False
                print(f"[main] GPU motion upscale unavailable ({exc}) - doing it on the CPU",
                      file=sys.stderr)

        def _enable_present() -> None:
            """Ask the worker to present the frame itself (WNDO).

            A refusal is not fatal: we stay on returning pixels to Python and
            drawing them in pygame - that path has not gone anywhere.
            """
            nonlocal present_mode, present_attempted
            present_attempted = True
            try:
                send_window(worker, width, height, 0)
                reader.wait_wack(timeout=15.0)
                present_mode = True
                display.set_hud_only(True)
                display.raise_topmost()  # the HUD must be ABOVE the worker's window
                print("[main] presenting in the worker window: no frame comes back to Python")
            except Exception as exc:
                present_mode = False
                display.set_hud_only(False)
                print(f"[main] worker window unavailable ({exc}) - output through pygame",
                      file=sys.stderr)

        def _disable_present() -> None:
            """Close the worker window and go back to drawing in pygame."""
            nonlocal present_mode, present_attempted
            if not present_mode:
                return
            try:
                send_window(worker, 0, 0, WINDOW_FLAG_DISABLE)
                reader.wait_wack(timeout=10.0)
            except Exception as exc:
                print(f"[main] could not close the worker window: {exc}", file=sys.stderr)
            present_mode = False
            present_attempted = False  # after a pause the window can be raised again
            display.set_hud_only(False)

        def _forget_present() -> None:
            """The worker restarted - its window and settings died with the process."""
            nonlocal present_mode, present_attempted, motion_small, motion_attempted
            present_mode = False
            present_attempted = False
            motion_small = False
            motion_attempted = False
            display.set_hud_only(False)

        def _sync_gray() -> None:
            """GRAY: renegotiate the reverse luminance channel for guides.

            The worker writes exactly the flow size of guides into the
            mapping. The channel changes together with guides (flow may
            change after RNSZ), so a resync is needed in _enable_dda and
            after apply. A refusal is not fatal - guides stay on dxcam.
            """
            nonlocal gray_active
            if not dda_mode:
                return
            try:
                gw, gh = guides.flow_width, guides.flow_height
                shm.open_gray(gw, gh)
                send_gray(worker, gw, gh, shm.gray_name)
                reader.wait_gak(timeout=15.0)
                gray_active = True
                print(f"[main] gray channel {gw}x{gh}: guides take luminance from the worker")
            except Exception as exc:
                gray_active = False
                print(f"[main] gray channel unavailable ({exc}) - guides through dxcam",
                      file=sys.stderr)

        def _enable_dda() -> None:
            """Ask the worker to capture the screen itself (DDA1).

            While it is active FRM1 frames carry FRAME_FLAG_NO_COLOR - no
            colour goes down the pipe, the worker takes it from Desktop
            Duplication straight on the GPU. Together with DDA we activate
            the reverse gray channel: the worker writes luminance there (the
            flow field size), guides read it and no longer depend on dxcam.
            A refusal is not fatal: we stay on sending frames from Python.
            """
            nonlocal dda_mode, dda_attempted, capture
            dda_attempted = True
            try:
                # In DDA mode guides still need the frame (motion), so dxcam
                # keeps running - we simply stop sending colour to the worker.
                send_dda(worker, width, height, 0)
                reader.wait_dack(timeout=15.0)
                dda_mode = True
                _sync_gray()
                print("[main] screen capture inside the worker (DDA1): no colour through the pipe")
            except Exception as exc:
                dda_mode = False
                print(f"[main] capture inside the worker unavailable ({exc}) - frames through Python",
                      file=sys.stderr)

        def _enable_wgc() -> None:
            """Ask the worker to capture the target WINDOW (WGCW).

            The same deal as DDA1 - the colour stops going down the pipe and
            the reverse gray channel feeds the guides - except the source is
            one window, which is why the overlay will not have to hide from
            screen capture. If the window has gone (closed, minimised) we drop
            back to the whole screen rather than freezing on the last frame.
            """
            nonlocal dda_mode, dda_attempted, window_hwnd
            dda_attempted = True
            if window_hwnd is None:
                return
            if not ctypes.windll.user32.IsWindow(window_hwnd):
                print("[main] the captured window is gone - back to full screen",
                      file=sys.stderr)
                _switch_window(0)
                return
            try:
                send_wgc(worker, window_hwnd)
                aw, ah = reader.wait_wgak(timeout=15.0)
                dda_mode = True
                _sync_gray()
                print(f"[main] window capture inside the worker (WGCW): "
                      f"{aw}x{ah}, no colour through the pipe")
            except Exception as exc:
                dda_mode = False
                print(f"[main] window capture unavailable ({exc}) - back to full screen",
                      file=sys.stderr)
                display.alert(UI_STRINGS[lang]["win_fail"])
                _switch_window(0)

        def _disable_dda() -> None:
            """Turn off capture in the worker and send the frame from Python again."""
            nonlocal dda_mode
            if not dda_mode:
                return
            try:
                send_dda(worker, 0, 0, 0)
                reader.wait_dack(timeout=10.0)
            except Exception as exc:
                print(f"[main] could not turn off capture in the worker: {exc}", file=sys.stderr)
            dda_mode = False

        def _forget_dda() -> None:
            """The worker restarted - its DDA capture died with the process."""
            nonlocal dda_mode, dda_attempted, gray_active
            dda_mode = False
            dda_attempted = False
            gray_active = False

        def _forget_out() -> None:
            """The worker restarted - it knows nothing about the OUTS section."""
            nonlocal out_shm, out_attempted
            out_shm = False
            out_attempted = False

        def _save_menu_layout() -> None:
            """Remember the panel size and position in config.json.

            We write on menu close and on exit rather than on every mouse
            move: dragging would otherwise hammer the file dozens of times
            per second.
            """
            try:
                data = json.loads(args.config.read_text(encoding="utf-8"))
                data["menu_scale"] = round(display.menu.user_scale, 2)
                # Height: None means "fit the content", and that is what we write.
                data["menu_height"] = (None if display.menu.user_height is None
                                       else int(display.menu.user_height))
                data["open_menu_on_start"] = startup_menu
                data["split"] = round(split_pos, 2)
                data["nr_small"] = bool(nr_small)
                data["work_scale"] = round(work_scale, 2)
                data["theme"] = display.menu.state.get("theme", "light")
                data["lang"] = lang
                data["menu_offset"] = [int(display.menu.offset[0]),
                                       int(display.menu.offset[1])]
                args.config.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            except Exception as exc:
                print(f"[main] could not save the menu layout: {exc}", file=sys.stderr)

        def _refresh_gpu_ok() -> None:
            """Whether NR works - from the worker's answer, not the architecture.

            Only the worker knows for sure: it calls CreateFeature and gets
            the NGX code back. The architecture only tells us what NVIDIA
            promises. Once decided, the answer is not revisited - worker
            restarts add lines but the verdict does not change.
            """
            nonlocal gpu_ok
            if gpu_ok is not None:
                return
            for line in reversed(worker_logs[-80:]):
                if "feature 18 ready" in line:
                    gpu_ok = True
                    return
                # The real refusal line from the worker is "[pure] direct
                # feature 18 create failed"; "Unsupported GPU architecture"
                # lives inside nvngx_dlssnr.dll and never reaches its stderr.
                # SAFE PASSTHROUGH (the worker stays alive and shows the raw
                # frame) is the same verdict: no feature, no NR.
                if "feature 18 create failed" in line or "NR feature unavailable" in line:
                    gpu_ok = False
                    return

        def _open_save_dialog() -> None:
            """Show "Save as" without stalling the pipeline.

            GetSaveFileNameW is modal: in the main loop it would freeze the
            overlay on the last frame, and with a recording running the pause
            over the dialog would land in the MP4 as a still (PTS comes from
            the clock). So the dialog lives in its own thread and the path
            comes back through a queue. A second dialog is not opened - one
            window is already up.
            """
            nonlocal shot_dialog_open
            if shot_dialog_open:
                return
            shot_dialog_open = True
            hwnd = display.get_hwnd()
            default_name = f"neuralscreen-{time.strftime('%Y%m%d-%H%M%S')}.jpg"

            def _run() -> None:
                try:
                    shot_paths.put(_ask_save_path(hwnd, default_name))
                except Exception as exc:
                    print(f"[main] the save dialog crashed: {exc}", file=sys.stderr)
                    shot_paths.put(None)

            threading.Thread(target=_run, name="save-dialog", daemon=True).start()

        def _drain_save_dialog() -> None:
            """Take the path from the dialog if the user has already answered."""
            nonlocal shot_dialog_open, pending_shot
            try:
                while True:
                    shot_path = shot_paths.get_nowait()
                    shot_dialog_open = False
                    if shot_path is None:
                        print("[main] screenshot cancelled by the user")
                        continue
                    if present_mode:
                        pending_shot = shot_path
                        print(f"[main] screenshot from the next frame: {shot_path}")
                    elif output_rgba is not None:
                        _save_screenshot(shot_path, output_rgba)
                    else:
                        display.alert("No frame yet")
            except queue.Empty:
                pass

        def _save_hotkeys(mapping: dict) -> None:
            """Write the assignments into config.json.

            Separate from _save_menu_layout: that one runs on menu close,
            while the user expects a key to be saved right away.
            """
            try:
                data = json.loads(args.config.read_text(encoding="utf-8"))
                data["hotkeys"] = dict(mapping)
                args.config.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            except Exception as exc:
                print(f"[main] could not save the hotkeys: {exc}",
                      file=sys.stderr)

        def _work_scale_cap() -> float:
            """The scale above which the work size just hits the NGX cap.

            Rounded down to the slider's own step so the value is reachable:
            a cap the slider cannot land on exactly would leave the top of the
            range doing nothing, which is the whole thing being fixed here.
            """
            raw = min(1.0, WORK_MAX_W / max(1, width), WORK_MAX_H / max(1, height))
            return max(0.35, int(raw / 0.05) * 0.05)

        def _menu_payload() -> dict:
            """The current state for the menu - a single source of truth."""
            _refresh_gpu_ok()
            wins = list_capturable_windows()
            return {
                "nr": not paused,
                "work_scale": work_scale,
                # Where the work size hits the 2560x1440 cap. Everything above
                # it lands on the same resolution, so the slider puts "the whole
                # screen" there instead of a dead stretch.
                "work_scale_cap": _work_scale_cap(),
                "work_scale_min": WORK_SCALE_MIN,
                "nr_small": nr_small,
                "screen_size": f"{width}x{height}",
                "profile": cfg["profile"],
                "profiles": list(PROFILES),
                "params": {k: params[k] for k in
                           ("intensity", "local_tone",
                            "local_structure", "skin_structure")},
                "lang": lang,
                "recording": recorder is not None,
                "work_size": f"{work_w}x{work_h}",
                "rec_seconds": (recorder.duration_ms / 1000.0) if recorder else 0.0,
                "open_on_start": startup_menu,
                "autostart": _autostart_enabled(),
                "split": split_pos,
                "gpu_text": gpu_text,
                "gpu_ok": gpu_ok,
                "window_mode": window_hwnd is not None,
                "monitor": str(monitor),
                "monitors": [f"{i}: {w}x{h}" for i, w, h in list_monitors()],
                "windows": [f"{h:X}: {t}" for h, t in wins],
                "window_current": next(
                    (f"{h:X}: {t}" for h, t in wins if h == window_hwnd), ""),
                "version": APP_VERSION,
                "channel": CHANNEL_LABEL,
            }

        def _apply_menu_action(action: tuple) -> None:
            """A menu action -> a real setting.

            The menu changes nothing on its own: it reports what the user
            wants and the decision is taken here, where params and cfg live.
            """
            nonlocal lang, running, startup_menu, split_pos, hotkey_bindings
            nonlocal nr_small
            kind = action[0]
            if kind == "nr":
                tray_commands.put("toggle")
            elif kind == "nr_res":
                # One control, one meaning: how much resolution the network
                # sees. Above the cap there is nothing left to reduce, so that
                # end of the slider is "the whole screen" - which is the same
                # thing as the reduced mode being off.
                want = float(action[1])
                cap = _work_scale_cap()
                if want > cap + 1e-6:
                    request_apply(1.0, cfg["profile"], params, new_small=False)
                else:
                    request_apply(want, cfg["profile"], params, new_small=True)
            elif kind == "split":
                # No need to recreate the worker: the wipe position rides in
                # every frame's header.
                split_pos = min(1.0, max(0.0, float(action[1])))
            elif kind == "toggle" and action[1] == "open_on_start":
                startup_menu = not startup_menu
                _save_menu_layout()
                print(f"[main] menu at startup: {'yes' if startup_menu else 'no'}")
            elif kind == "toggle" and action[1] == "autostart":
                # Autostart with Windows (HKCU Run). The state lives in the
                # registry, not in the config - read it and invert.
                new_state = not _autostart_enabled()
                if _set_autostart(new_state):
                    print(f"[main] autostart with Windows: {'on' if new_state else 'off'}")
                    display.alert(UI_STRINGS[lang].get(
                        "autostart_on" if new_state else "autostart_off",
                        "Autostart ON" if new_state else "Autostart OFF"))
                else:
                    display.alert(UI_STRINGS[lang].get("autostart_err", "Autostart failed"))
            elif kind == "param":
                new_params = dict(params)
                new_params[action[1]] = float(action[2])
                request_apply(work_scale, cfg["profile"], new_params)
            elif kind == "profile":
                request_apply(work_scale, action[1], dict(PROFILES[action[1]]))
            elif kind == "lang":
                if action[1] in UI_STRINGS and action[1] != lang:
                    lang = action[1]
                    display.set_lang(lang)
                    display.menu.set_state({"lang": lang})
                    print(f"[main] interface language -> {lang}")
            elif kind == "capture":
                # While the menu waits for a keypress the global hotkeys must
                # be suspended: otherwise Num2 toggles the menu instead of
                # landing in the field.
                if action[1]:
                    hotkeys.suspend()
                else:
                    hotkeys.resume()
            elif kind == "hotkey":
                cmd, text = action[1], action[2]
                parsed = parse_binding(text)
                if parsed is None:
                    print(f"[main] could not parse the combination {text!r}", file=sys.stderr)
                    display.alert(UI_STRINGS[lang]["hotkey_bad"])
                else:
                    over = cfg.get("hotkeys")
                    over = dict(over) if isinstance(over, dict) else {}
                    over[cmd] = text
                    cfg["hotkeys"] = over
                    hotkey_bindings = build_bindings(over)
                    hotkeys.rebind(hotkey_bindings)
                    display.menu.set_hotkeys(hotkey_labels(hotkey_bindings))
                    _save_hotkeys(over)
                    print(f"[main] {cmd} -> {text}")
                    display.alert(UI_STRINGS[lang]["settings_applied"])
            elif kind == "theme":
                # The menu has already applied the theme to itself
                # (overlay_ui); here we only remember it for config.json -
                # _save_menu_layout() runs on menu close and on exit.
                print(f"[main] menu theme -> {action[1]}")
            elif kind == "monitor":
                # The value arrives as "N: WxH" - take the index before the colon.
                try:
                    new_monitor = int(str(action[1]).split(":")[0])
                except (ValueError, IndexError):
                    print(f"[main] invalid monitor: {action[1]!r}", file=sys.stderr)
                    return
                if new_monitor != monitor:
                    _switch_monitor(new_monitor)
            elif kind == "window":
                # The window list in the menu: the value is "hwnd: title".
                try:
                    target = int(str(action[1]).split(":")[0], 16)
                except (ValueError, IndexError):
                    print(f"[main] invalid window: {action[1]!r}", file=sys.stderr)
                    return
                if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(target)):
                    print(f"[main] the window 0x{target:X} is gone", file=sys.stderr)
                    display.alert(UI_STRINGS[lang]["win_fail"])
                    return
                # Bring the chosen window to the front: the capture follows
                # it, and a window buried under others would show through
                # the overlay as a half-covered picture (user: the chosen
                # window must come to the foreground, no overlaps).
                user32 = ctypes.windll.user32
                user32.BringWindowToTop(ctypes.c_void_p(target))
                user32.SetForegroundWindow(ctypes.c_void_p(target))
                print(f"[main] window mode on from the menu - target hwnd "
                      f"0x{target:X}")
                _switch_window(target)
            elif kind == "button":
                name = action[1]
                if name == "close":
                    display.menu.visible = False
                    display.set_menu_opaque(False)
                    display.set_menu_input(False)
                    _save_menu_layout()
                elif name == "exit":
                    print(f"[main] exit: button in the overlay menu "
                          f"(frames processed {frame_index})")
                    running = False
                elif name == "record":
                    tray_commands.put("record")
                elif name == "screenshot":
                    tray_commands.put("screenshot_menu")
                elif name == "window_mode":
                    # The fullscreen button in the footer: the same action
                    # as the Num5 hotkey - in window mode it returns to the
                    # whole screen, in fullscreen mode it is a no-op with an
                    # alert (the user asked for a visible "already active").
                    if window_hwnd is not None:
                        print("[main] window mode off - back to the whole screen")
                        _switch_window(0)
                    else:
                        display.alert(UI_STRINGS[lang]["fs_active"])
                elif name == "github":
                    # The hotkeys, profiles and requirements are described
                    # only in the README - there was no way to learn about
                    # them from the program itself.
                    try:
                        import webbrowser
                        webbrowser.open(REPO_URL)
                        display.alert(UI_STRINGS[lang]["github_opened"])
                    except Exception as exc:
                        print(f"[main] could not open {REPO_URL}: {exc}",
                              file=sys.stderr)
                elif name == "channel":
                    # The channel label in the settings page opens the
                    # channel (user rule 2026-09-08).
                    try:
                        import webbrowser
                        webbrowser.open(CHANNEL_URL)
                        display.alert(UI_STRINGS[lang]["github_opened"])
                    except Exception as exc:
                        print(f"[main] could not open {CHANNEL_URL}: {exc}",
                              file=sys.stderr)

        def request_apply(new_scale: float, new_profile: str, new_params: dict,
                          new_small: bool | None = None) -> None:
            """Apply the settings with coalescing over RESTART_COOLDOWN.

            The single entry point for the settings window, the tray and the
            hotkeys: the cooldown check used to live only on the settings
            path, while the tray and the arrows called _do_restart directly -
            key repeat on an arrow produced a flood of RNSZ.
            """
            nonlocal pending_apply
            if time.monotonic() - last_restart < RESTART_COOLDOWN:
                pending_apply = (new_scale, new_profile, new_params, new_small)
                print(f"[main] apply deferred (cooldown {RESTART_COOLDOWN:.1f} s), "
                      f"the last value will be applied")
            else:
                _do_restart(new_scale, new_profile, new_params, new_small=new_small)

        def _drain_commands() -> bool:
            """Handle the tray/hotkey commands; False when the program must quit.

            Called from the main loop AND from inside the recv wait: at 4K a
            heavy scene can take ~1 s per NGX frame, and the hotkeys must
            stay responsive while main waits for the worker (user: "NR toggle
            does not always fire in Cyberpunk").
            """
            nonlocal running, paused, worker_failed, recorder, window_hwnd, work_frame, last_foreground, work_scale, params, lang, width, height, mon_w, mon_h, frame_index, pending_apply, last_restart, split_pos, startup_menu, nr_small, work_h, work_w, monitor, dda_mode, dda_attempted, present_mode, present_attempted, out_shm, out_attempted, motion_small, motion_attempted, gray_active, follow_pos, follow_resize, pts, output_rgba, pending_shot, consecutive_restarts, guide_fails, buf_full, guides, shm, worker, worker_logs, reader, worker_stop, display, tray, hotkeys, cfg
            try:
                while True:
                    cmd = tray_commands.get_nowait()
                    if cmd == "quit":
                        print(f"[main] exit: tray or the quit hotkey "
                              f"(frames processed {frame_index})")
                        running = False
                    elif cmd == "settings":
                        # Num2 and a left click on the tray open the overlay
                        # menu - the only place the settings live.
                        display.menu.set_state(_menu_payload())
                        opened = display.menu.toggle()
                        display.set_menu_opaque(opened)
                        display.set_menu_input(opened)
                        if opened:
                            # In one-window mode the HUD layer is the size of
                            # the captured window - a menu near the edge would
                            # be clipped by it. Expand the layer to the whole
                            # monitor while the menu is open, so the menu is
                            # always fully visible (user: menu lost outside a
                            # small window). The saved offset is honoured -
                            # layout() clamps it to the screen (user rule
                            # 10.09: fixed position until the user drags it).
                            if window_hwnd is not None:
                                display.set_fullscreen_layer(mon_w, mon_h)
                            # The mouse lands on the title bar, so the user
                            # does not have to hunt for the pointer (user
                            # request). The layout must be current for the
                            # title rect to be valid.
                            try:
                                display.menu.layout(
                                    display.screen.get_width(),
                                    display.screen.get_height())
                                cx, cy = display.menu.title_center()
                                ctypes.windll.user32.SetCursorPos(cx, cy)
                            except Exception:
                                pass
                        else:
                            # The menu closed: put the HUD layer back on the
                            # captured window.
                            if window_hwnd is not None:
                                rect = window_frame_rect(window_hwnd)
                                if rect is not None:
                                    display.set_window_layer(*rect)
                            _save_menu_layout()
                        print(f"[main] overlay menu {'opened' if opened else 'closed'}")
                    elif cmd == "toggle":
                        paused = not paused
                        if not paused:
                            work_frame = None  # a fresh grab after the pause
                            if worker_failed:
                                # The worker died and was shut down (issue #3):
                                # revive it - a fresh process may succeed (a
                                # transient GPU conflict, a driver hiccup).
                                worker_failed = False
                                print("[main] reviving the worker after the failure")
                                try:
                                    worker, worker_logs, reader, worker_stop = restart_worker(
                                        worker, params, work_w, work_h, warmup,
                                        width if (work_w != width or work_h != height) else 0,
                                        height if (work_w != width or work_h != height) else 0,
                                        worker_stop, shm)
                                    _forget_present()
                                    _forget_dda()
                                    _forget_out()
                                    _sync_motion_size()
                                    frame_index = 0
                                    pts = 0
                                except Exception as exc:
                                    print(f"[main] worker revive failed ({exc}) - "
                                          f"staying NR OFF", file=sys.stderr)
                                    paused = True
                                    worker_failed = True
                            display.set_visible(True)
                        print(f"[main] NR {'OFF (bypass NGX)' if paused else 'ON'}")
                        display.alert(UI_STRINGS[lang]["nr_off" if paused else "nr_on"])
                        tray._set_state(nr=not paused)
                    elif cmd == "screenshot_menu":
                        _open_save_dialog()
                    elif cmd == "record":
                        # Num0: record the NR frame into an MP4. The frames
                        # are requested from the worker through
                        # FRAME_FLAG_WANT_PIXELS (the screenshot mechanism,
                        # but for every recorded frame).
                        if recorder is None:
                            rec_dir = BASE_DIR / "recordings"
                            rec_dir.mkdir(exist_ok=True)
                            stamp = time.strftime("%Y%m%d-%H%M%S")
                            # Two recordings within one second must not
                            # overwrite each other - we add milliseconds.
                            stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
                            path = str(rec_dir / f"neuralscreen-{stamp}.mp4")
                            try:
                                # 30 fps, not 60: every recorded frame is a
                                # full 33 MB round-trip from the worker
                                # (FRAME_FLAG_WANT_PIXELS -> pipe), and the
                                # measurement showed 60 fps recording costs
                                # ~36% of the FPS (101 -> 65). Halving the
                                # frame rate halves that cost; the picture
                                # quality per frame is identical.
                                recorder = VideoRecorder(path, width, height, fps=30,
                                                         audio=record_audio)
                            except Exception as exc:
                                print(f"[main] recording did not start: {exc}", file=sys.stderr)
                                display.alert(f"REC ERROR: {exc}")
                                recorder = None
                            else:
                                print(f"[main] recording started: {path}")
                                display.alert(UI_STRINGS[lang]["record_on"])
                        else:
                            rec_path = recorder.path
                            try:
                                recorder.close()
                            except Exception as exc:
                                print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
                            secs = recorder.duration_ms / 1000.0
                            print(f"[main] recording finished: {rec_path} "
                                  f"({recorder.written} frames, {secs:.1f}s)")
                            display.alert(UI_STRINGS[lang]["record_off"])
                            recorder = None
                    elif cmd == "window_mode":
                        # The window under the cursor wins: it works on the
                        # desktop too (the focused window there is Progman,
                        # which is not capturable), and it is what the user
                        # is looking at. Fall back to the last focused
                        # foreign window when the cursor is over nothing
                        # capturable (our own overlay, the desktop).
                        if window_hwnd is not None:
                            print("[main] window mode off - back to the whole screen")
                            _switch_window(0)
                        else:
                            target = window_under_cursor() or last_foreground
                            if target:
                                print(f"[main] window mode on - target hwnd "
                                      f"0x{target:X}")
                                _switch_window(target)
                            else:
                                # Nothing but our own windows has had the
                                # focus, so there is nothing to capture but
                                # ourselves.
                                print("[main] window mode: no window to capture "
                                      "(only our own windows have had the focus)",
                                      file=sys.stderr)
                                display.alert(UI_STRINGS[lang]["win_none"])
                    elif cmd in ("scale_up", "scale_down"):
                        delta = WORK_SCALE_STEP if cmd == "scale_up" else -WORK_SCALE_STEP
                        new_scale = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, work_scale + delta))
                        if abs(new_scale - work_scale) > 1e-6:
                            new_w, new_h = _work_size(width, height, new_scale)
                            print(f"[main] work_scale -> {new_scale:.2f} ({new_w}x{new_h})")
                            display.alert(UI_STRINGS[lang]["work_scale_changed"].format(new_scale, new_w, new_h))
                            request_apply(new_scale, cfg["profile"], params)
            except queue.Empty:
                pass
            return running

        while running:
            loop_start = time.perf_counter()
            now = time.monotonic()

            if not _drain_commands():
                break

            # The worker is gone (restart budget exhausted): the pipeline is
            # stopped. Commands still run (Num1 revives it), but no frame is
            # grabbed or sent - the worker is dead and would only be
            # restarted in vain (issue #3: endless restart loop on a GPU
            # where feature 18 cannot be created).
            if worker_failed:
                time.sleep(0.05)
                continue

            # Deferred apply (coalescing): if a restart happened recently, we
            # apply the last value once the pause is over
            if pending_apply is not None and time.monotonic() - last_restart >= RESTART_COOLDOWN:
                p_scale, p_profile, p_params, p_small = pending_apply
                pending_apply = None
                print("[main] applying the deferred settings")
                _do_restart(p_scale, p_profile, p_params, new_small=p_small)

            if not running:
                break

            # NR OFF - bypass: the pipeline keeps spinning (grab -> show the
            # raw frame in the worker's window) but the NGX effect is skipped.
            # The overlay (picture + HUD) stays alive and predictable; we hide
            # everything only on a real exit. A bypass frame is sent like any
            # other (the flag lives in the header) so send/recv stay paired.
            bypass = paused
            # (for readability: send_frame is called with bypass=bypass)

            # The answer from the "Save as" dialog (it runs in its own thread).
            _drain_save_dialog()

            if want_present and not present_mode and not present_attempted:
                _enable_present()
            # Who has the focus, for the window-mode hotkey: by the time it
            # is pressed the menu may be in front, so the last window that was
            # not ours is remembered continuously.
            fg = foreign_foreground()
            if fg:
                last_foreground = fg
            # A game that goes fullscreen raises itself above every topmost
            # window, ours included, and then the menu is drawn but not on
            # screen. While it is open we keep coming back up; a SetWindowPos
            # that changes nothing is cheap, and 30 frames is fast enough that
            # nobody sees the menu disappear.
            if display.menu.visible and frame_index % 30 == 0:
                display.raise_topmost()
            # The same for the HUD even when the menu is closed: a borderless
            # game (Cyberpunk) keeps itself on top and our HUD stays
            # underneath it forever. Re-assert only when the topmost window
            # is NOT ours - in the steady state this is zero SetWindowPos
            # calls, so no DWM flicker (user: flicker + invisible HUD over
            # borderless games).
            if frame_index % 30 == 0:
                try:
                    top = ctypes.windll.user32.GetTopWindow(0)
                    if top and top != display.get_hwnd():
                        display.raise_topmost()
                except Exception:
                    pass
            if window_hwnd is not None:
                if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(window_hwnd)):
                    print("[main] the captured window closed - back to full screen",
                          file=sys.stderr)
                    _switch_window(0)
                    continue
                _follow_window()
            if want_dda and not dda_mode and not dda_attempted:
                if window_hwnd is not None:
                    _enable_wgc()
                else:
                    _enable_dda()
            if want_motion_small and not motion_small and not motion_attempted:
                motion_attempted = True
                _sync_motion_size()
            if want_out_shm and not out_shm and not out_attempted:
                _enable_out_shm()

            # --- Input for the overlay menu --------------------------
            # Events are read only while the menu is open: the rest of the
            # time the window is click-through, there are no events, and an
            # extra get() would eat the queue from pump() inside drawing.
            if display.menu.visible:
                for ev in pygame.event.get():
                    for action in display.menu.handle_event(ev):
                        _apply_menu_action(action)
                if not display.menu.dragging:
                    display.menu.set_state(_menu_payload())

            # --- Grab ahead: while NGX computes frame N we grab N+1 -------
            # work_frame == None happens on the first frame, after a worker
            # restart (a work_scale change) and after grab()==None. Then the
            # grab happens at the start of the iteration, BEFORE send - the
            # synchronisation with the worker is not lost (send/recv are
            # always paired, recv is mandatory after any send).
            # The worker (v3, NGX Upscaling) resizes full->work->full on the
            # GPU itself: Python sends a full-res frame, motion at work-res
            # (guides is built with work_w/work_h and downsamples its own
            # input) and receives full-res back.
            if work_frame is None and not gray_active:
                t0 = time.perf_counter()
                frame = _safe_grab()
                _perf("grab", t0)
                if frame is None:
                    continue  # the frame is not ready yet - skip the iteration
                if frame.shape[1] != width or frame.shape[0] != height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    except cv2.error:
                        # The monitor resolution changed: buf_full was
                        # preallocated for the old size - recreate and retry
                        buf_full = np.empty((height, width, 4), dtype=np.uint8)
                        cv2.resize(frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    _perf("resize_full", t0)
                    frame = buf_full
                else:
                    frame = np.ascontiguousarray(frame, dtype=np.uint8)
                work_frame = frame

            # --- Sending the frame with auto-recovery ---
            # The worker can die or hang (NGX after RNSZ, a GPU conflict) -
            # instead of crashing, main restarts the worker with the current
            # parameters and carries on. This is the last line of defence:
            # the program does not fall over.
            try:
                t0 = time.perf_counter()
                if gray_active:
                    guide = guides.process(gray=shm.read_gray())
                else:
                    guide = guides.process(work_frame)
                _perf("guides", t0)
            except Exception as guide_exc:
                # guides is not critical: ValueError/TypeError/cv2.error (the
                # shape of the gray frame, a division by zero) must not take
                # the process down. We skip the frame - the worker gets the
                # next one. But a persistent error (an incompatible gray
                # channel, a broken shape) would spin main at 100% CPU -
                # after 5 failures in a row we fall back to zero motion: the
                # frames keep flowing and the picture does not freeze.
                print(f"[main] guides.process failed ({guide_exc}) - frame skipped",
                      file=sys.stderr)
                guide_fails += 1
                if guide_fails >= 5:
                    print(f"[main] guides.process is unstable - zero motion "
                          f"(frames keep flowing)", file=sys.stderr)
                    guide_fails = 0
                    guide = guides.zero_guide()
                else:
                    continue
            try:
                check_worker(worker, worker_logs)
                t0 = time.perf_counter()
                send_frame(worker, frame_index, work_frame, guide.motion, guide.reset,
                           pts, shm, want_pixels=(pending_shot is not None or recorder is not None),
                           motion_small=motion_small,
                           no_color=bool(dda_mode),
                           bypass=bypass,
                           split=split_pos)
                _perf("send", t0)
            except (BrokenPipeError, OSError, EOFError, RuntimeError) as exc:
                consecutive_restarts += 1
                if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] the worker died {consecutive_restarts} times in a row - NR OFF")
                    paused = True
                    worker_failed = True
                    display.alert(UI_STRINGS[lang]["nr_off"])
                    tray._set_state(nr=False)
                    consecutive_restarts = 0
                    work_frame = None
                    # The worker is gone and will not come back on its own:
                    # stop hammering it, hide the overlay so the desktop is
                    # not covered by a black window (issue #3), and wait for
                    # the user to turn NR back on.
                    try:
                        shutdown_worker(worker, worker_stop)
                    except Exception:
                        pass
                    display.set_visible(False)
                    continue
                print(f"[main] worker lost while sending ({exc}) - restarting "
                      f"({consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                if worker_logs:
                    print("[main] worker stderr (tail):")
                    for line in worker_logs[-15:]:
                        print(f"  {line}")
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, work_w, work_h, 10,
                    width if (work_w != width or work_h != height) else 0,
                    height if (work_w != width or work_h != height) else 0,
                    worker_stop, shm)
                _forget_present()
                _forget_dda()
                _forget_out()
                _sync_motion_size()
                frame_index = 0
                pts = 0
                work_frame = None
                continue

            # Grab the next frame WHILE the worker computes the current one
            # (NGX is ~70-100 ms/frame - the bottleneck). dxcam is thread-safe
            # within one thread - a second thread is unnecessary, we simply
            # move grab() between send and recv. Buffers: send_frame copies
            # the data into the pipe (tobytes) and guides.process keeps no
            # references to its input - buf_full can be reused right away.
            # In DDA mode the worker grabs the frame itself - Python does not.
            next_frame = None
            if not gray_active:
                t0 = time.perf_counter()
                next_frame = _safe_grab()
                _perf("grab", t0)
            if next_frame is not None:
                if next_frame.shape[1] != width or next_frame.shape[0] != height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(next_frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    except cv2.error:
                        buf_full = np.empty((height, width, 4), dtype=np.uint8)
                        cv2.resize(next_frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    _perf("resize_full", t0)
                    next_frame = buf_full
                else:
                    next_frame = np.ascontiguousarray(next_frame, dtype=np.uint8)
            # next_frame == None: the frame is not ready - the start of the
            # next iteration will do the grab (work_frame = None). The
            # synchronisation with the worker is not lost: send has already
            # gone out and the recv below is mandatory.

            t0 = time.perf_counter()
            try:
                output_rgba = None
                recv_reader = reader
                recv_deadline = time.monotonic() + 5.0
                while time.monotonic() < recv_deadline:
                    try:
                        output_rgba = reader.recv(frame_index, timeout=0.05)
                        break
                    except TimeoutError:
                        # A heavy 4K scene can take ~1 s per NGX frame -
                        # keep the hotkeys alive while main waits (user:
                        # "NR toggle does not always fire in Cyberpunk").
                        # The switch overlay's spinner must keep animating
                        # while the new worker warms up.
                        if display.is_switch_active():
                            display.draw_overlay(0.0)
                        if not _drain_commands():
                            running = False
                            break
                        if reader is not recv_reader:
                            break  # a command restarted the worker
                        continue
                else:
                    raise TimeoutError(
                        f"the worker has been silent for 5s on frame {frame_index} - NGX did not answer after the restart")
                if not running:
                    break
                if reader is not recv_reader:
                    continue  # the worker was restarted by a command
            except (TimeoutError, EOFError, RuntimeError, OSError) as exc:
                consecutive_restarts += 1
                if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] worker silent/dying {consecutive_restarts} times in a row - NR OFF")
                    paused = True
                    worker_failed = True
                    display.alert(UI_STRINGS[lang]["nr_off"])
                    tray._set_state(nr=False)
                    consecutive_restarts = 0
                    work_frame = None
                    # No frame will ever arrive - the switch overlay must not
                    # hang over the desktop forever (audit M2: the veil is
                    # removed only on a received frame).
                    display.exit_switch_mode()
                    # Same for the overlay itself: hide it so the desktop is
                    # not covered by a black window (issue #3).
                    try:
                        shutdown_worker(worker, worker_stop)
                    except Exception:
                        pass
                    display.set_visible(False)
                    continue
                print(f"[main] worker silent/dead on frame {frame_index} ({exc}) - restarting "
                      f"({consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, work_w, work_h, 10,
                    width if (work_w != width or work_h != height) else 0,
                    height if (work_w != width or work_h != height) else 0,
                    worker_stop, shm)
                _forget_present()
                _forget_dda()
                _forget_out()
                _sync_motion_size()
                frame_index = 0
                pts = 0
                work_frame = None
                continue
            _perf("recv", t0)
            # A frame arrived - the failure chain is broken. Without the reset
            # the counter accumulated across the whole session and three
            # unrelated failures (even an hour apart) turned NR off.
            consecutive_restarts = 0
            status = "NR OFF" if paused else "NR ON"
            pts += 1

            t0 = time.perf_counter()
            try:
                if recorder is not None and output_rgba is not None:
                    # Our layer is excluded from capture
                    # (WDA_EXCLUDEFROMCAPTURE), so we bake the open menu onto
                    # the frame ourselves. frombuffer references the numpy
                    # buffer (no copy): the blit writes straight into
                    # output_rgba.
                    try:
                        surf = pygame.image.frombuffer(
                            output_rgba, (output_rgba.shape[1], output_rgba.shape[0]), "RGBX")
                        display.draw_capture_overlay(surf)
                    except Exception as menu_exc:
                        print(f"[main] menu was not baked into the recorded frame: {menu_exc}",
                              file=sys.stderr)
                    # The recording gets its own try: an encoder failure must
                    # NOT land in the "output failed" except (that one
                    # recreates the pygame window on every frame - an endless
                    # loop). A recording error stops the recording, not the
                    # window.
                    try:
                        recorder.write(output_rgba)
                    except Exception as rec_exc:
                        print(f"[main] frame write failed ({rec_exc}) - "
                              f"stopping the recording", file=sys.stderr)
                        try:
                            recorder.close()
                        except Exception:
                            pass
                        recorder = None
                if present_mode:
                    # In WNDO mode the worker draws the frame on screen; in
                    # Python the pixels arrive ONLY on want_pixels
                    # (recording/screenshot). There is no need to show them in
                    # pygame: that is a pointless 4K blend (~22 ms) and a
                    # flicker of the frame in the HUD layer above the worker's
                    # window. The HUD is refreshed by draw_overlay() with
                    # throttling (not every frame).
                    display.exit_switch_mode()  # the new worker is presenting
                    display.reveal()  # a real frame exchange happened
                    if pending_shot is not None and output_rgba is not None:
                        _save_screenshot(pending_shot, output_rgba)
                        pending_shot = None
                    display.draw_overlay()
                elif output_rgba is None:
                    # The frame is already on screen - the worker showed it, only the HUD here
                    # (WGCW/DDA without want_pixels: no colour reaches Python).
                    # This is still a live exchange with the rebuilt worker: the
                    # switch overlay must come down or the menu stays hidden
                    # behind the veil forever (user: clipped/blank after Num5).
                    display.exit_switch_mode()
                    # reveal() is THE only way to show the window while
                    # _reveal_pending is set (audit H1): this branch is hit on
                    # every frame when the WNDO window is unavailable and DDA/
                    # WGCW works (fallback config) - without the call the HUD
                    # and the menu stay invisible forever in that setup.
                    display.reveal()
                    display.draw_overlay()
                else:
                    display.exit_switch_mode()  # the next frame replaces the overlay
                    display.reveal()  # a real frame exchange happened
                    display.show(output_rgba)
                    if pending_shot is not None:
                        _save_screenshot(pending_shot, output_rgba)
                        pending_shot = None
            except Exception as exc:
                # A display mode change (entering/leaving a fullscreen game)
                # can kill the pygame/SDL context - recreate the window.
                print(f"[main] output failed ({exc}) - recreating the window")
                # Snapshot the live menu state before the window dies - the
                # restore below must pick up where the user left it, not the
                # stale cfg values (user rule 10.09: fixed position until
                # the user drags it).
                cfg["menu_offset"] = [int(display.menu.offset[0]),
                                      int(display.menu.offset[1])]
                cfg["menu_scale"] = round(display.menu.user_scale, 2)
                cfg["menu_height"] = (None if display.menu.user_height is None
                                      else int(display.menu.user_height))
                try:
                    display.close()
                except Exception:
                    pass
                display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
                display.set_lang(lang)
                # In one-window mode the overlay must stay visible to outside
                # recorders: the NEW window comes up with the WDA flag set
                # (the Display default), so state it explicitly here - the
                # same call _rebuild_pipeline makes. Without this, any
                # display-mode change while in window mode silently drops
                # the overlay from NVIDIA App / OBS capture until the next
                # pipeline rebuild (audit #4, F1).
                display.set_excluded_from_capture(window_hwnd is None)
                # The menu is created together with the window - we give it
                # back its size, position, theme and language, otherwise after
                # a game starts it jumps to the centre, turns light and
                # switches to en.
                display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
                display.menu.set_hotkeys(hotkey_labels(hotkey_bindings))
                saved_theme = cfg.get("theme")
                if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
                    display.menu.set_state({"theme": saved_theme})
                display.menu.set_state({"lang": lang})
                saved = cfg.get("menu_offset")
                if isinstance(saved, (list, tuple)) and len(saved) == 2:
                    display.menu.offset = [int(saved[0]), int(saved[1])]
                if present_mode:
                    # The new window must become a transparent layer over the worker again
                    display.set_hud_only(True)
                    display.raise_topmost()
                display.alert(UI_STRINGS[lang]["nr_on"])
            _perf("show", t0)
            display.set_hud({
                "fps": last_fps,
                "status": status,
                "resolution": f"{width}x{height}",
                "profile": cfg["profile"],
                "params": {k: v for k, v in params.items() if k not in ("profile", "preset", "style", "auto_mask", "ui_correction")},
                "frames": frame_index,
            })

            frame_index += 1
            if startup_pending and frame_index >= 2:
                # Wait for the first displayed frame: an open menu over a
                # window that is not filled yet flashes black.
                startup_pending = False
                if startup_menu:
                    display.menu.set_state(_menu_payload())
                    display.menu.visible = True
                    display.set_menu_opaque(True)
                    display.set_menu_input(True)
                    print("[main] menu opened at startup")
                else:
                    display.alert(UI_STRINGS[lang]["started"], 3.5)
            work_frame = next_frame  # None -> grab at the start of the next iteration
            fps_window.append(time.perf_counter() - loop_start)
            if len(fps_window) > 120:
                fps_window.pop(0)

            if now - last_log >= FPS_LOG_INTERVAL:
                last_fps = len(fps_window) / sum(fps_window) if fps_window else 0.0
                scene = f" | scene {guide.scene_score:.3f}" if guide is not None else ""
                print(f"[main] {status} | FPS {last_fps:5.1f} | frames {frame_index} | "
                      f"work {work_w}x{work_h}{scene}")
                last_log = now

            if now - last_perf_log >= PERF_LOG_INTERVAL:
                parts = []
                for key in PERF_KEYS:
                    samples = perf[key]
                    if samples:
                        parts.append(f"{key} {sum(samples) / len(samples):.1f}ms")
                    samples.clear()
                if parts:
                    print("[perf] " + " | ".join(parts))
                last_perf_log = now

        print("[main] exiting at the user's request")
    except KeyboardInterrupt:
        print("\n[main] interrupted (Ctrl+C)")
    except Exception as exc:
        print(f"[main] ERROR: {exc}", file=sys.stderr)
        if worker is not None and worker.poll() is not None:
            print("[main] the worker crashed; last stderr lines:", file=sys.stderr)
            for line in worker_logs[-40:]:
                print(f"  {line}", file=sys.stderr)
        return 1
    finally:
        # A recording may have been running at exit: without close() the moov
        # atom is not written and the file stays broken (players refuse it).
        if recorder is not None:
            try:
                recorder.close()
            except Exception as exc:
                print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
        if worker is not None:
            shutdown_worker(worker, worker_stop)
        if shm is not None:
            shm.close()
        try:
            _save_menu_layout()
        except Exception:
            pass
        if capture is not None:
            try:
                capture.close()
            except Exception as exc:
                print(f"[main] failed to close the capture: {exc}", file=sys.stderr)
        if display is not None:
            try:
                display.close()
            except Exception as exc:
                print(f"[main] failed to close the window: {exc}", file=sys.stderr)
        try:
            hotkeys.stop()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        try:
            taskbar.stop()
        except Exception:
            pass
        print("[main] resources released")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # pythonw: there is no console - show the reason in a message box and
        # keep the details in NeuralScreen.log.
        import traceback
        traceback.print_exc()
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(
                None,
                f"NeuralScreen failed to start: {exc}\n\nDetails in NeuralScreen.log next to the program.",
                "NeuralScreen", 0x10)  # MB_ICONERROR
        except Exception:
            pass
        sys.exit(1)
