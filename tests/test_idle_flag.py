"""Audit (C++ report): the menu 'idle' flag must survive intervening lines.

The worker logs "[skip] no new frame" ONCE per idle stretch
(g_skip_static_logged), but other always-logged lines keep coming
during it: FollowCapturedWindow's "[wgc] the window is minimised - the
overlay is hidden" / "[wgc] the window is back - the overlay is shown",
"[present]" lifecycle lines, and so on. settings_io.menu_payload reads
only st.worker_logs[-3:], so the single idle line scrolls out within
seconds and the menu flips to "not idling" while the network is
genuinely idling - the confusion the flag was added to remove.

Expected: idle stays True until the "[skip] the screen changed" line.
[audit cpp-worker]

Run:  runtime\\python.exe tests\\test_idle_flag.py
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

import settings_io  # noqa: E402


def _state(logs):
    return types.SimpleNamespace(
        paused=False, work_scale=0.65, nr_small=False, width=1920, height=1080,
        cfg={"profile": "Natural", "spout": False, "rec_indicator": True,
             "screenshot_dir": "", "gpu": 0, "skip_static": True},
        params={"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
                "skin_structure": -1.0},
        presets={}, lang="en", recorder=None, work_w=1248, work_h=702,
        startup_menu=True, split_pos=0.0, gpu_text="RTX 5070 Ti", gpu_ok=True,
        window_hwnd=None, monitor=0, worker_logs=logs,
        capture=types.SimpleNamespace(devicename=r"\\.\DISPLAY1",
                                      resolution=(1920, 1080)))


def main() -> int:
    failures = []

    idle_line = "[skip] no new frame - the network is idle until the screen changes"
    resume = "[skip] the screen changed - 15 frames skipped, the network resumes"

    # 1. Idle line at the very tail: idle.
    p1 = settings_io.menu_payload(_state(["[present] ok", idle_line]))
    if not p1.get("idle"):
        failures.append("idle flag is False with the idle line at the tail")

    # 2. The audited case: four legal diagnostics land after it.
    tail = [idle_line,
            "[present] window revealed on the first Present",
            "[wgc] the window is minimised - the overlay is hidden",
            "[present] overlay lifecycle",
            "[wgc] the window is back - the overlay is shown"]
    p2 = settings_io.menu_payload(_state(tail))
    if not p2.get("idle"):
        failures.append(
            "the idle flag DROPPED although the network is still idling - "
            "the single [skip] line scrolled out of the [-3:] window")

    # 3. After the resume line: not idle.
    p3 = settings_io.menu_payload(_state((tail + [resume])[-5:]))
    if p3.get("idle"):
        failures.append("idle flag stayed True after the resume line")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the idle flag survives intervening diagnostics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
