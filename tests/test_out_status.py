"""The worker's NGX result is not a boolean: 0x00000000 means "no frame
this call" and must NOT crash the pipeline.

The regression: any ngx_result != 1 raised RuntimeError, the worker was
restarted three times and NR was turned off. On a laptop on the iGPU the
network skips evaluations (0x00000000) - the exact pattern from issue #11
(kortul, 4060 Laptop). Only the 0xBAD00000 family (NVSDK_NGX_FAILED) is
a real error.

Checked: a skipped frame (0x00000000, no pixels) lands in the queue as
(None) without raising; a real failure (0xBAD00001) raises; ok=0 raises;
a normal frame (result 1) passes through.
"""
import os
import queue
import struct
import sys
import threading
import time

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from main import OUT_FMT, OUT_MAGIC, WorkerReader  # noqa: E402


class FakeWorker:
    """A stand-in for the worker process: a pipe we write OUT headers into."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb")

    def send_out(self, index: int, ok: int, byte_count: int,
                 ngx_result: int, pts: int = 0, payload: bytes = b"") -> None:
        os.write(self._w, struct.pack(OUT_FMT, OUT_MAGIC, index, ok,
                                      byte_count, ngx_result, pts))
        if payload:
            os.write(self._w, payload)

    def close(self) -> None:
        try:
            os.close(self._w)
        except OSError:
            pass


def main() -> int:
    failures = []
    fake = FakeWorker()
    # 1x1: the reader expects byte_count == width*height*4, so a tiny
    # frame keeps the test fast.
    reader = WorkerReader(fake, 1, 1, shm=None)
    try:
        # 1. A normal frame: result 1, pixels inline.
        fake.send_out(1, 1, 4, 1, payload=b"\xAB" * 4)
        got = reader._queue.get(timeout=5.0)
        if got[0] != 1 or not isinstance(got[1], np.ndarray) or \
                got[1].tobytes() != b"\xAB" * 4:
            failures.append(f"normal frame: expected (1, 4 bytes), got {got!r}")

        # 2. A skipped frame: 0x00000000, no pixels - must NOT raise.
        fake.send_out(2, 1, 0, 0x00000000)
        got = reader._queue.get(timeout=5.0)
        if got != (2, None):
            failures.append(f"skipped frame: expected (2, None), got {got!r}")

        # 3. A skipped frame WITH pixels (result 0 but bytes present) must
        #    pass through - the pixels are what matters.
        fake.send_out(3, 1, 4, 0x00000000, payload=b"\xCD" * 4)
        got = reader._queue.get(timeout=5.0)
        if got[0] != 3 or not isinstance(got[1], np.ndarray) or \
                got[1].tobytes() != b"\xCD" * 4:
            failures.append(f"skipped-with-pixels: expected (3, 4 bytes), got {got!r}")

        # 4. ok=0 - the worker itself failed - must surface as an error.
        #    (The reader thread dies on the first error, so each error
        #    case gets its own reader.)
        fake.send_out(4, 0, 0, 1)
        got = reader._queue.get(timeout=5.0)
        if got[0] is not None or not isinstance(got[1], RuntimeError) or \
                "ok=0" not in str(got[1]):
            failures.append(f"ok=0: expected a RuntimeError, got {got!r}")

        # 5. A real NGX failure: 0xBAD00001 - must surface as an error.
        reader2 = WorkerReader(fake, 1, 1, shm=None)
        fake.send_out(5, 1, 0, 0xBAD00001)
        got = reader2._queue.get(timeout=5.0)
        if got[0] is not None or not isinstance(got[1], RuntimeError) or \
                "0xBAD00001" not in str(got[1]):
            failures.append(f"0xBAD00001: expected a RuntimeError, got {got!r}")
        reader2._thread.join(timeout=2.0)
    finally:
        fake.close()
        reader._thread.join(timeout=2.0)

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: 0x00000000 is a skipped frame, only 0xBAD00000 raises")
    return 0


if __name__ == "__main__":
    sys.exit(main())
