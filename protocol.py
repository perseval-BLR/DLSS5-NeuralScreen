"""The worker protocol: what the two processes say to each other.

Moved out of main.py unchanged. One subject in one file - the magics, the
struct formats, the senders and the reader thread that turns the worker's
replies into a queue. It needs nothing from main.py, which is why it could
leave: the commands are self-contained by design.

The sizes here are not free-form. Every struct format has a static_assert
behind it in native/dlss5-feed-host64.cpp, and tests/test_protocol_sizes.py
checks the two sides against each other - a field added on one side and not
the other is a build error now, not a runtime desync.
"""
from __future__ import annotations

import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid

import numpy as np

from paths import BASE_DIR  # noqa: F401




# NGX feature 18 goes silent at 3840x2160 (verified in isolation: the worker
# hangs on frame 0 with work=4K, both in legacy and in upscale mode).
# We cap the work resolution at 2560x1440 - that is known to work.
WORK_MAX_W = 2560


WORK_MAX_H = 1440


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


# --- Worker protocol (matches dlss5_converter/core.py) -------------------
# v3 (magic D5V3): a header with full_w/full_h - the worker resizes the frames
# on the GPU itself (NGX Upscaling), Python does not resize on the CPU.
VIDEO_MAGIC = 0x33563544  # 'DV5' v3
CAPTURE_MAGIC = 0x31504143  # CAP1: prepare capture before calculating motion
FRAME_FLAG_PREPARED = 0x1000

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
FRAME_FLAG_SKIP_STATIC = 0x40  # no new frame - let the worker idle instead of re-running NGX

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


def _read_exact(stream, size: int) -> bytes:
    """Read exactly size bytes from the stream (the worker may give fewer)."""
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"the worker stopped after {len(chunks)} of {size} reply bytes")
        chunks.extend(block)
    return bytes(chunks)


SR_SCALE_MAGIC = 0x31435353  # SSC1


def sync_sr_scale(worker, reader, scale: float) -> None:
    percent = min(100, max(25, int(round(scale * 100))))
    if getattr(worker, "_sr_scale_sent", None) == percent:
        return
    worker.stdin.write(struct.pack(FRAME_FMT, SR_SCALE_MAGIC, 0, percent, 0, 0))
    worker.stdin.flush()
    reader.recv(0, timeout=5.0)
    worker._sr_scale_sent = percent


def prepare_capture(worker, reader, index: int, pts: int) -> None:
    """Latch capture and gray together; FRM1 will consume that exact capture."""
    worker.stdin.write(struct.pack(FRAME_FMT, CAPTURE_MAGIC, index, 0, 0, pts))
    worker.stdin.flush()
    reader.recv(index, timeout=5.0)


def send_frame(worker: subprocess.Popen, index: int, rgba: np.ndarray,
               motion: np.ndarray, reset: bool, pts: int,
               shm: "SharedFrameBuffer | None" = None,
               want_pixels: bool = False, motion_small: bool = False,
               no_color: bool = False, bypass: bool = False,
               split: float = 0.0, skip_static: bool = False,
               prepared: bool = False,
               dlss_sr: bool | None = None) -> None:
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
    skip_static: the capture has no new frame (Desktop Duplication timeout,
    an idle window) - the worker answers with an empty result and does NOT
    re-run the network on the stale picture. A screenshot/recording request
    (want_pixels) wins over it.
    """
    flags = (FRAME_FLAG_WANT_PIXELS if want_pixels else 0) | \
            (FRAME_FLAG_MOTION_SMALL if motion_small else 0) | \
            (FRAME_FLAG_NO_COLOR if no_color else 0) | \
            (FRAME_FLAG_BYPASS if bypass else 0) | \
            (FRAME_FLAG_SKIP_STATIC if skip_static else 0)
    if dlss_sr is not None:
        flags |= 0x4000 | (0x2000 if dlss_sr else 0)
    if prepared:
        flags |= FRAME_FLAG_PREPARED
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
                    # The NGX result is not a boolean: 0x00000000 means "no
                    # frame this call" (the network skipped the evaluation -
                    # a laptop on the iGPU, a driver hiccup) and is NOT a
                    # failure. Only the 0xBAD00000 family is a real error
                    # (NVSDK_NGX_FAILED masks the top nibble). Treating
                    # 0x00000000 as a crash restarted the worker three
                    # times and then turned NR off (issue #11, kortul).
                    if (ngx_result & 0xFFF00000) == 0xBAD00000:
                        raise RuntimeError(
                            f"NGX evaluation failed on frame {out_index}: 0x{ngx_result:08X}")
                    if byte_count == 0:
                        # No pixels through the pipe: WNDO mode (the worker
                        # showed the frame in its own window) or a skipped
                        # frame (0x00000000). Either way there is nothing
                        # to show - the pipeline waits for the next one.
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
            # The worker is gone. Every other wait_* re-raises this; here it
            # used to be skipped as if it were a stale frame, so a dead
            # worker cost the whole budget and then reported a timeout -
            # "did not acknowledge within 2s" instead of "the worker
            # stopped", and the caller fell back to a full restart two
            # seconds later than it had to (audit F7).
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("the worker stopped")
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
