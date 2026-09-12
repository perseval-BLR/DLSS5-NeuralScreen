"""Audit F7: wait_rack must surface a dead worker immediately.

Every other wait_* in WorkerReader checks the (None, exc) sentinel and
re-raises it; wait_rack skips it like a stale frame and burns its whole
timeout (RACK_TIMEOUT = 20 s in production) before raising TimeoutError.
A worker that died mid-RNSZ therefore shows as a 20-second frozen menu,
not as a dead worker.

Expected: EOFError arrives almost immediately (well before the timeout).
[audit F7]

Run:  runtime\\python.exe tests\\test_wait_rack_eof.py
"""
import io
import sys
import time
import types
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

import protocol  # noqa: E402


class _FakeWorker:
    """The minimal surface WorkerReader touches: a stdout that is at EOF."""

    def __init__(self):
        self.stdout = io.BytesIO(b"")  # immediate EOF -> sentinel
        self.pid = 0


def main() -> int:
    failures = []
    reader = protocol.WorkerReader(_FakeWorker(), 64, 64, None)
    time.sleep(0.3)  # let the reader thread put the sentinel

    t0 = time.monotonic()
    try:
        reader.wait_rack(timeout=2.0)
        failures.append("wait_rack returned on a dead worker")
    except TimeoutError as exc:
        took = time.monotonic() - t0
        failures.append(
            f"F7: wait_rack raised TimeoutError after {took:.1f}s of a 2.0s "
            f"budget - the EOF sentinel was skipped, not re-raised "
            f"({exc})")
    except EOFError:
        took = time.monotonic() - t0
        if took > 1.0:
            failures.append(f"EOF surfaced too late ({took:.1f}s)")
    except Exception as exc:
        failures.append(f"unexpected exception type {type(exc).__name__}: {exc}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: wait_rack surfaces the EOF sentinel immediately")
    return 0


if __name__ == "__main__":
    sys.exit(main())
