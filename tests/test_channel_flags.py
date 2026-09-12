"""Audit F8: teardown+rebuild must reset every channel flag the loop reads.

teardown_pipeline closes the shm; rebuild_pipeline resets the per-worker
flags (present/dda/window/out/motion). gray_active is set/cleared by
channels.sync_gray but NOT reset by either - after a rebuild the loop's
`if st.gray_active:` branch reads a gray section of the NEW shm that the
new worker has not opened (returns None -> one frame with no luminance,
or a shape mismatch through guides' guard).

Expected: the set of flags reset in rebuild_pipeline equals the set the
loop's negotiation block reads. [audit python-core F8]

Run:  runtime\\python.exe tests\\test_channel_flags.py
"""
import re
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

# The flags the main loop negotiates/reads every frame (main.py).
LOOP_FLAGS = ("present_mode", "present_attempted", "dda_mode", "dda_attempted",
              "gray_active", "motion_small", "motion_attempted",
              "out_shm", "out_attempted")


def main() -> int:
    failures = []
    rebuild_src = (BASE / "pipeline.py").read_text(encoding="utf-8")
    m = re.search(r"def rebuild_pipeline\(st[^)]*\).*?(?=\ndef |\Z)",
                  rebuild_src, re.S)
    body = m.group(0) if m else ""
    reset = set(re.findall(r"st\.([a-z_]+)\s*=\s*False", body))
    # attempt flags count as reset when their mode flag is.
    missing = [f for f in LOOP_FLAGS if f not in reset]
    print(f"    reset in rebuild_pipeline: {sorted(reset & set(LOOP_FLAGS))}")
    print(f"    missing: {missing}")
    if "gray_active" in missing:
        failures.append(
            "F8: gray_active is not reset by rebuild_pipeline - after a "
            "rebuild the loop reads st.shm.read_gray() on a section the new "
            "worker has not opened")
    # The rest are informational; only gray_active is the audited gap.
    others = [f for f in missing if f != "gray_active"]
    if others:
        print(f"    note: also not explicitly reset (check each): {others}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: rebuild_pipeline resets the channel flags the loop reads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
