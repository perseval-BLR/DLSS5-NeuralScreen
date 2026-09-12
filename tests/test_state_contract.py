"""Audit F2-adjacent: startup's state contract vs what the loop reads.

_Pipeline.__slots__ makes a read-before-init an AttributeError, and
follow_monitor (via st.mon_resize) is exactly that case: bring_up
initialises follow_pos/follow_resize but not mon_resize; the slot only
appears when a poll ran with the size unchanged. follow_size is set
lazily by follow_window before use, which is fine.

Expected: every slot the main loop reads without a guard has been set
by bring_up. This test builds the state the way bring_up does - by
reading bring_up's source for st.X = assignments - and asserts the
critical ones exist. [audit F2]

Run:  runtime\\python.exe tests\\test_state_contract.py
"""
import re
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

    src = (BASE / "startup.py").read_text(encoding="utf-8")
    # The st.X = / st.X: Type = assignments bring_up performs.
    m = re.search(r"def bring_up\(st\).*?(?=\ndef |\Z)", src, re.S)
    if not m:
        failures.append("could not find bring_up in startup.py")
        init_sets = set()
    else:
        init_sets = set(re.findall(
            r"st\.([a-z_]+)(?:\s*:\s*[^=\n]+)?\s*=", m.group(0)))

    # Names the main loop reads from st without a hasattr/None-guard that
    # only exist after a negotiation. mon_resize is the audited gap:
    # read by follow_monitor, never initialised.
    must_exist_at_boot = {
        "mon_resize",      # the audited gap - follow_monitor reads it
        "follow_pos", "follow_resize",
        "pending_shot", "recorder",
        "dda_mode", "present_mode", "gray_active", "out_shm",
    }
    missing = sorted(must_exist_at_boot - init_sets)
    if missing:
        failures.append(
            f"bring_up never initialises: {missing} - the first read of "
            f"these is an AttributeError with __slots__ (the program exits "
            f"through main()'s top-level handler)")

    # And prove the failure mode is real: a slot-based object.
    class _P:
        __slots__ = ("only",)
    try:
        _ = _P().mon_resize
        failures.append("sanity: slots did not raise (test is broken)")
    except AttributeError:
        pass

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: bring_up initialises the slots the loop reads unguarded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
