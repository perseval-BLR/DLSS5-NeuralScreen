"""read_out() reuses its destination buffers and never overwrites one in use.

A 4K frame is 33 MB and read_out() used to allocate a fresh one per frame:
measured with four frames held alive (the recorder's queue depth), 12.0 ms
against 2.6 ms into a pre-allocated buffer - 2.8 GB/s against 12.7 GB/s,
the difference being page faults on freshly mapped memory rather than the
memcpy. It runs on the reader thread inside the recv the main loop is
blocked on, so it was ~9 ms of every recorded frame.

The ring that replaces it is only safe because a slot is never handed out
while somebody still holds it: a returned frame travels BY REFERENCE into
the encoder queue and lives until the encoder has written it. So the
guarantee this test exists for is the third one below - hold every frame,
as a stalled encoder would, and every single one must keep its own pixels.

Checked:
* the pixels that come back are the pixels in the section;
* nothing held -> the buffers are reused, the ring stays bounded;
* everything held -> every frame is a different buffer and keeps its own
  content (no slot is rewritten under its owner);
* with every slot busy read_out still answers correctly (fresh fallback);
* close_out() drops the ring, and a reopen at a new size is shaped for it;
* the seqlock still rejects a frame the worker is mid-write on.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import numpy as np  # noqa: E402
from protocol import OUT_RING_SLOTS, SharedFrameBuffer  # noqa: E402

W, H = 32, 16


def make_shm() -> SharedFrameBuffer:
    """A tiny buffer - the ring's behaviour has nothing to do with the size."""
    return SharedFrameBuffer(64, 64, max_work_w=64, max_work_h=64)


def publish(shm: SharedFrameBuffer, value: int, seq: int = 2) -> None:
    """Put a frame into the section the way the worker does: pixels, then an
    even sequence number to say the write is finished."""
    shm._out_buf[:] = value
    shm._out_mm[0:8] = int(seq).to_bytes(8, "little")


def main() -> int:
    failures = []

    # 1. The pixels that come back are the pixels in the section.
    shm = make_shm()
    shm.open_out(W, H)
    publish(shm, 61)
    frame = shm.read_out()
    if frame is None:
        failures.append("read_out returned None for a complete frame")
    elif not (frame == 61).all():
        failures.append("read_out returned the wrong pixels")
    elif frame.shape != (H, W, 4):
        failures.append(f"read_out returned shape {frame.shape}, want {(H, W, 4)}")

    # 2. Nothing held: the buffers are reused and the ring stays bounded.
    seen = set()
    for i in range(40):
        publish(shm, i % 251)
        got = shm.read_out()
        seen.add(id(got))
        del got
    if len(shm._out_ring) > OUT_RING_SLOTS:
        failures.append(f"the ring grew to {len(shm._out_ring)} slots, "
                        f"cap is {OUT_RING_SLOTS}")
    if len(seen) > OUT_RING_SLOTS + 1:
        failures.append(f"{len(seen)} distinct buffers over 40 reads - "
                        f"they are not being reused")

    # 3. THE guarantee: everything held keeps its own content.
    shm2 = make_shm()
    shm2.open_out(W, H)
    held = []
    for i in range(OUT_RING_SLOTS + 3):
        publish(shm2, i + 1)
        got = shm2.read_out()
        if got is None:
            failures.append(f"read_out returned None on held frame {i}")
            break
        held.append((i + 1, got))
    ids = {id(buf) for _, buf in held}
    if len(ids) != len(held):
        failures.append(f"{len(held)} held frames share only {len(ids)} "
                        f"buffers - a slot was handed out while still in use")
    for want, buf in held:
        if not (buf == want).all():
            failures.append(f"a held frame was overwritten: wanted {want}, "
                            f"found {np.unique(buf)[:4]}")
            break

    # 4. Every slot busy: the answer is still correct (a fresh array).
    publish(shm2, 199)
    extra = shm2.read_out()
    if extra is None or not (extra == 199).all():
        failures.append("read_out failed once every ring slot was in use")

    # 5. close_out drops the ring; a reopen is shaped for the new size.
    shm2.close_out()
    if shm2._out_ring:
        failures.append("close_out left the old ring in place")
    shm2.open_out(W * 2, H)
    publish(shm2, 7)
    reopened = shm2.read_out()
    if reopened is None or reopened.shape != (H, W * 2, 4):
        failures.append("a reopened section did not produce the new shape")

    # 6. The seqlock still rejects a frame the worker is mid-write on.
    shm3 = make_shm()
    shm3.open_out(W, H)
    publish(shm3, 5, seq=3)  # odd = the worker is writing
    if shm3.read_out() is not None:
        failures.append("read_out accepted a frame with an odd sequence")

    for s in (shm, shm2, shm3):
        s.close_out()
        s.close()

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the out ring is reused, bounded, and never overwrites a frame "
          "still in use")
    return 0


if __name__ == "__main__":
    sys.exit(main())
