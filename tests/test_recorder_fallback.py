"""NVENC codec fallback: AV1 -> HEVC -> H.264, decided at open time.

The recorder must work on RTX 30 cards, which have no AV1 NVENC encoder.
add_stream() alone is not a probe (PyAV opens the encoder lazily, at the
first mux), so the probe opens each candidate for real on a throwaway
null-muxer container. These tests fake that probe:

  * av1 fails at add_stream -> hevc is chosen and frames still encode;
  * av1 opens but fails at codec_context.open() (the real RTX 30 shape) ->
    hevc is chosen;
  * every codec fails -> the constructor raises instead of writing a file
    that dies mid-recording.

Plus the two review findings that came with the fallback:
  * needs_frame() gates WANT_PIXELS: one frame per stream slot, and no
    demand once the encoder has failed;
  * close() cannot deadlock on a stuck encoder: a full queue and a thread
    that never consumes it must not block the caller.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
from recorder import VideoRecorder  # noqa: E402

W, H = 640, 360
FPS = 30.0
FRAMES = 10


def make_frame(i: int) -> np.ndarray:
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = (i * 25) % 256
    frame[..., 1] = 60
    frame[..., 2] = 120
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


class FakeCtx:
    """codec_context stand-in: open() fails for the codecs we fake as broken."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    def open(self) -> None:
        if self.fail:
            raise av.FFmpegError("no NVENC capable devices found")


class FakeStream:
    """Stream stand-in: accepts the probe's configuration, then open()."""

    def __init__(self, fail: bool = False):
        self.codec_context = FakeCtx(fail)


class FakeProbe:
    """Null-muxer container stand-in for the probe."""

    def __init__(self, fail_at_add: set, fail_at_open: set):
        self.fail_at_add = fail_at_add
        self.fail_at_open = fail_at_open

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_stream(self, name, rate=None, **kwargs):
        if name in self.fail_at_add:
            raise av.FFmpegError(f"no encoder named {name}")
        return FakeStream(fail=name in self.fail_at_open)


def patch_probe(fail_at_add=(), fail_at_open=()):
    """Route the probe's null-muxer opens to a FakeProbe; real files stay real."""
    real_open = av.open

    def fake_open(file, mode=None, format=None, **kwargs):
        if format == "null":
            return FakeProbe(set(fail_at_add), set(fail_at_open))
        return real_open(file, mode=mode, format=format, **kwargs)

    av.open = fake_open
    return real_open


def record_and_check(out: Path, failures: list, label: str) -> None:
    """Write FRAMES frames, close, and verify the file reads back as HEVC."""
    rec = VideoRecorder(str(out), W, H, fps=FPS, audio=False)
    try:
        for i in range(FRAMES):
            rec.write(make_frame(i))
            time.sleep(1.0 / FPS)
    finally:
        rec.close()
    if rec.codec != "hevc_nvenc":
        failures.append(f"{label}: chosen codec is {rec.codec}, not hevc_nvenc")
    if rec.written == 0:
        failures.append(f"{label}: no frames were written")
    if not out.is_file() or out.stat().st_size == 0:
        failures.append(f"{label}: the file was not created")
        return
    with av.open(str(out)) as container:
        stream = container.streams.video[0]
        decoded = [f for f in container.decode(stream)]
    if not decoded:
        failures.append(f"{label}: the file holds no decodable frames")
    if "hevc" not in stream.codec_context.name:
        failures.append(f"{label}: the file's codec is "
                        f"{stream.codec_context.name}, not hevc")
    out.unlink(missing_ok=True)


def test_fallback_on_add_stream_failure(out: Path, failures: list) -> None:
    # av1 fails at add_stream (the simplest shape of "no AV1 on this card").
    real = patch_probe(fail_at_add=("av1_nvenc",))
    try:
        record_and_check(out, failures, "add_stream failure")
    finally:
        av.open = real


def test_fallback_on_open_failure(out: Path, failures: list) -> None:
    # The real RTX 30 shape: add_stream succeeds, the encoder open fails.
    real = patch_probe(fail_at_open=("av1_nvenc",))
    try:
        record_and_check(out, failures, "open failure")
    finally:
        av.open = real


def test_all_codecs_fail_raises(out: Path, failures: list) -> None:
    real = patch_probe(fail_at_add=("av1_nvenc", "hevc_nvenc", "h264_nvenc"))
    try:
        try:
            VideoRecorder(str(out), W, H, fps=FPS, audio=False)
            failures.append("all codecs failed but the constructor did not raise")
        except RuntimeError as exc:
            if "no NVENC encoder available" not in str(exc):
                failures.append(f"unexpected error: {exc}")
    finally:
        av.open = real
    out.unlink(missing_ok=True)


def test_needs_frame_gates_want_pixels(out: Path, failures: list) -> None:
    rec = VideoRecorder(str(out), W, H, fps=FPS, audio=False)
    try:
        if rec.needs_frame():
            failures.append("needs_frame() True at t=0 (slot 0 is dropped by write)")
        time.sleep(1.0 / FPS + 0.01)
        if not rec.needs_frame():
            failures.append("needs_frame() False when the first slot is open")
        rec.write(make_frame(0))
        if rec.needs_frame():
            failures.append("needs_frame() True right after a frame was taken")
        time.sleep(1.0 / FPS + 0.01)
        if not rec.needs_frame():
            failures.append("needs_frame() False when the next slot is open")
        rec._encode_error = RuntimeError("boom")
        if rec.needs_frame():
            failures.append("needs_frame() True after an encoder failure")
    finally:
        rec.close()
    out.unlink(missing_ok=True)


def test_close_does_not_deadlock_on_stuck_encoder(out: Path,
                                                 failures: list) -> None:
    rec = VideoRecorder(str(out), W, H, fps=FPS, audio=False)
    gate = threading.Event()

    def stuck() -> None:
        gate.wait()          # never released: an NVENC stall that never ends

    rec._thread = threading.Thread(target=stuck, name="stuck-encode", daemon=True)
    rec._thread.start()
    for i in range(rec.QUEUE_DEPTH):
        rec._queue.put((i + 1, make_frame(i)))   # fill the queue
    rec._thread.join = lambda timeout=None: None  # the 30 s join elapses
    t0 = time.perf_counter()
    rec.close()
    dt = time.perf_counter() - t0
    if dt > 2.0:
        failures.append(f"close() blocked for {dt:.1f} s on a stuck encoder")
    if rec._thread is not None:
        failures.append("close() left the thread reference behind")
    out.unlink(missing_ok=True)


def main() -> int:
    failures: list = []
    out = Path(tempfile.gettempdir()) / "ns-test-fallback.mp4"
    out.unlink(missing_ok=True)
    test_fallback_on_add_stream_failure(out, failures)
    test_fallback_on_open_failure(out, failures)
    test_all_codecs_fail_raises(out, failures)
    test_needs_frame_gates_want_pixels(out, failures)
    test_close_does_not_deadlock_on_stuck_encoder(out, failures)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: AV1->HEVC fallback, needs_frame() gating, close() without deadlock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
