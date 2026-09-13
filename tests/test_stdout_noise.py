"""Someone else printing must not be able to break the protocol.

The worker's stdout carries the binary protocol, so anything else in that
process that prints lands in the middle of it: NVIDIA's own logging, an
injected overlay, a library's stray printf. Issue #61 is what that looks
like from the outside - an RTX 5090, latest drivers, and:

    [main] shared memory unavailable (invalid magic in the worker reply:
           0x3230325B) - frames through the pipe

0x3230325B is the bytes "[202" - the first four characters of somebody's
timestamped log line. From there the stream is offset by four bytes, so
WNDO, DDA1, MOTS and OUTS each timed out after fifteen seconds while the
worker's own log said OK to every one of them, and the program spent its
life restarting a worker that was answering into a stream nobody could
read. The reporter saw "the overlay is invisible and NR is sporadic".

So the protocol got its own handle: fd 1 is duplicated for our writes and
the real fd 1 is pointed at stderr. NS_STDOUT_NOISE=1 makes the worker
print exactly that kind of line on purpose, right where it did damage.

Run:  runtime\\python.exe tests\\test_stdout_noise.py
"""
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES, VIDEO_MAGIC,
                  WORKER_EXE)

W, H = 640, 360


def read_exact(pipe, n):
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"the worker closed stdout (got {len(buf)} of {n})")
        buf += chunk
    return buf


def run(shared: bool):
    """Four frames through a worker that prints on stdout on purpose.

    shared=True asks the worker NOT to take the pipe - the way it was
    before this fix - so the noise lands in the middle of the stream. That
    half is the negative control: a check that passes with and without the
    thing it checks is not a check.

    Returns (replies, log, error).
    """
    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 1] = 170
    frame[..., 3] = 255
    motion = np.zeros((H, W, 2), dtype=np.float16)
    env = dict(os.environ, NS_STDOUT_NOISE="1")
    if shared:
        env["NS_SHARED_STDOUT"] = "1"
    else:
        env.pop("NS_SHARED_STDOUT", None)
    log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    log.close()
    replies, error = [], None
    with open(log.name, "wb") as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"],
                                cwd=str(WORKER_EXE.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, env=env)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            for i in range(4):
                proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                             1 if i == 0 else 0,
                                             FRAME_FLAG_WANT_PIXELS, i))
                proc.stdin.write(frame.tobytes())
                proc.stdin.write(motion.tobytes())
                proc.stdin.flush()
                head = read_exact(proc.stdout, struct.calcsize(OUT_FMT))
                magic, index, ok, nbytes, _ngx, _pts = struct.unpack(OUT_FMT, head)
                replies.append((magic, index, ok, nbytes))
                if magic != OUT_MAGIC:
                    break          # the stream is offset; reading on is noise
                if nbytes:
                    read_exact(proc.stdout, nbytes)
            proc.stdin.close()
            proc.wait(timeout=15)
        except Exception as exc:
            error = repr(exc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    Path(log.name).unlink(missing_ok=True)
    return replies, text, error


def main() -> int:
    failures = []
    source = (BASE / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8")
    if "OwnTheProtocolPipe" not in source:
        failures.append("the protocol no longer takes a private handle - a "
                        "stray printf can corrupt it again (issue #61)")

    replies, text, error = run(shared=False)
    print(f"    protected:  {[(hex(m), i, ok, n) for m, i, ok, n in replies]}"
          + (f" error={error}" if error else ""))
    if error:
        failures.append(f"the stream broke with the pipe protected: {error}")
    if len(replies) != 4:
        failures.append(f"only {len(replies)} of 4 frames were answered")
    for magic, _i, _ok, _n in replies:
        if magic != OUT_MAGIC:
            failures.append(f"a reply came back with magic {hex(magic)} - the "
                            f"noise reached the protocol")
            break
    if "[NOISE] a stray line on stdout" not in text:
        failures.append("the deliberate noise did not reach stderr - either "
                        "the hook did not fire or it went down the pipe")
    else:
        print("    the stray line landed in the log, where it belongs")

    # The negative control. Without the private handle the same noise has to
    # break the stream - otherwise the first half proves nothing.
    bad, bad_text, bad_error = run(shared=True)
    print(f"    sharing:    {[(hex(m), i, ok, n) for m, i, ok, n in bad]}"
          + (f" error={bad_error}" if bad_error else ""))
    broke = bool(bad_error) or any(m != OUT_MAGIC for m, _i, _ok, _n in bad)         or len(bad) != 4
    if not broke:
        failures.append("sharing stdout with the noise did NOT break the "
                        "stream - this test cannot tell the fix from its "
                        "absence, so it proves nothing")
    else:
        first_bad = next((hex(m) for m, _i, _ok, _n in bad if m != OUT_MAGIC),
                         None)
        print(f"    ... and sharing breaks it"
              + (f" (first foreign magic {first_bad})" if first_bad else
                 f" ({bad_error})"))

    for f in failures:
        print("FAIL:", f)
    if failures:
        print(text[-600:])
        return 1
    print("OK: a stray printf cannot get into the protocol")
    return 0


if __name__ == "__main__":
    sys.exit(main())
