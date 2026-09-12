"""Audit: a successful WGCW must clear gpu_switch_pending.

apply_gpu sets gpu_switch_pending=True so that a capture channel that
fails to come up on the chosen card produces the gpu_split alert. In
one-window mode the first negotiation after the rebuild is enable_wgc:
on SUCCESS it never clears the flag (only enable_dda does). The flag
then stays armed for the rest of the session, and an unrelated DDA
failure much later fires a stale, wrong "the chosen card drives no
display" alert.

Expected: the pending flag is consumed exactly once, by the first
capture-channel negotiation after the switch, whatever its outcome.
[audit python-core F5]

Run:  runtime\\python.exe tests\\test_gpu_pending_wgc.py
"""
import sys
import types
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

import channels  # noqa: E402


def main() -> int:
    failures = []
    alerts = []

    class _Reader:
        def wait_wgak(self, timeout):
            return (1920, 1080)  # success

    st = types.SimpleNamespace(
        window_hwnd=0x1234, dda_attempted=False, dda_mode=False,
        gpu_switch_pending=True, gray_active=False,
        reader=_Reader(),
        display=types.SimpleNamespace(alert=lambda t, **k: alerts.append(t)),
        lang="en", worker=None,
    )

    import ctypes
    real_send = channels.send_wgc
    real_gray = channels.sync_gray
    real_iswindow = ctypes.windll.user32.IsWindow
    channels.send_wgc = lambda *a, **k: None
    channels.sync_gray = lambda s: None
    # The probe window is a fake handle: IsWindow would refuse it before the
    # channel logic under test is reached.
    ctypes.windll.user32.IsWindow = lambda hwnd: True
    try:
        ok = channels.enable_wgc(st)
    finally:
        channels.send_wgc = real_send
        channels.sync_gray = real_gray
        ctypes.windll.user32.IsWindow = real_iswindow

    if not ok:
        failures.append("enable_wgc reported failure on a successful WGAK")
    if st.gpu_switch_pending:
        failures.append(
            "F5: a SUCCESSFUL window capture left gpu_switch_pending armed - "
            "a later unrelated DDA failure will fire a stale gpu_split alert")
    if alerts:
        failures.append(f"a successful switch must stay quiet, got {alerts}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a successful window capture consumes gpu_switch_pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
