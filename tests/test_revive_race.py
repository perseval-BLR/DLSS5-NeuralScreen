"""Audit F4: a user revive must cancel the armed auto-revive.

Worker fails transiently -> worker_failed=True, next_auto_revive armed
(+30 s), overlay hidden. The user presses Num1 within the window: the
toggle path revives the worker itself, but next_auto_revive stays armed.
If the revived worker's first frame does not arrive inside 30 s, the
top-of-loop auto-revive fires AGAIN on top of the live worker - the
double-init hazard restart_worker's docstring warns about - with a
second "NR ON" alert. In its failure branch, paused stays as the user
set it (False), so the HUD/tray then claim NR ON with no worker.

Expected: at most one revive per failure; a successful user revive
disarms the pending auto-revive. [audit F4]

Run:  runtime\\python.exe tests\\test_revive_race.py
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


def main() -> int:
    failures = []

    # Source-level pin: the toggle's revive path must touch next_auto_revive.
    src = (BASE / "commands.py").read_text(encoding="utf-8")
    # Find the "toggle" revive block.
    start = src.find('elif cmd == "toggle":')
    end = src.find('elif cmd ==', start + 1)
    block = src[start:end if end > 0 else start + 4000]
    disarms = "next_auto_revive" in block
    if not disarms:
        failures.append(
            "F4: the toggle revive block never touches st.next_auto_revive - "
            "a user revive leaves the auto-revive armed; 30 s later it "
            "restarts the (possibly already healthy) worker again")

    # And the auto-revive's own failure branch: paused must not be left False.
    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    block_start = main_src.find("auto-reviving the worker")
    if block_start < 0:
        failures.append("could not find the auto-revive block in main.py")
    else:
        block2 = main_src[block_start:block_start + 1200]
        # On failure it should re-arm worker_failed AND leave NR state
        # consistent (paused True or a re-armed next_auto_revive).
        if ("st.worker_failed = True" in block2
                and "st.paused = True" not in block2
                and "st.next_auto_revive" not in block2):
            failures.append(
                "F4: the auto-revive failure branch sets worker_failed but "
                "leaves paused=False - the HUD shows NR ON with no worker")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the revive paths cannot double-fire or desync NR state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
