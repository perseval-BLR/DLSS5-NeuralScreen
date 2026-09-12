"""Audit (C++ report): _hard_failure must keep a sticky 0xBAD00001 verdict.

After MAX_CONSECUTIVE_RESTARTS the auto-revive decision asks
main._hard_failure(st.worker_logs), which scans only logs[-40:]. The
worker prints "NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH"
once, on frame 0 of a worker that then keeps running - so any 40 further
diagnostics (the always-logged [present]/[cap]/[video]/[skip] set added
by d8d063d) push the verdict out of the window. The app then treats a
permanently broken card as transient and keeps retrying it - the exact
restart-storm the classifier exists to prevent.

refresh_gpu_ok already caches its verdict (st.gpu_ok); _hard_failure
does not.

Expected: a 0xBAD00001 line survives >40 subsequent diagnostics.
[audit cpp-worker]

Run:  runtime\\python.exe tests\\test_hard_failure_window.py
"""
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

from main import _hard_failure  # noqa: E402


def main() -> int:
    failures = []

    verdict_line = "[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH"

    # 1. Fresh: the honest case still works.
    if not _hard_failure(["[host] adapter 0", verdict_line]):
        failures.append("_hard_failure missed a verdict at the tail")

    # 2. The audited case: 50 diagnostics after the verdict - all of them
    #    lines the worker legally emits while running (present/resize/etc).
    noise = [f"[present] window {i}" for i in range(50)]
    aged = ["[host] adapter 0", verdict_line] + noise
    if not _hard_failure(aged):
        failures.append(
            "the 0xBAD00001 verdict is LOST after 50 later diagnostics - "
            "the tail scan has no memory; on a card that cannot run the "
            "pass the app will keep auto-reviving instead of stopping")

    # 3. Transient stays transient.
    if _hard_failure(["[present] window 1", "[video] delivered frame 90 (live)"]):
        failures.append("a transient tail must not classify as hard")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the hard verdict survives 50 later diagnostics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
