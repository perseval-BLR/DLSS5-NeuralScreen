"""A card that refused the neural pass is marked in the picker, not hidden.

DXGI reports some cards twice. One user has a single RTX 5080 listed as
adapters 0 and 2, and only one of the two can bring the neural pass up - the
other kills the worker. From outside the two entries are the same string, so
the menu was offering a choice between two identical lines, one of which
breaks the program (issue #33).

Merging them by name is not available: on a machine with two identical cards
that would remove a real choice. What IS known is what was tried, so that is
what the picker says - and it says it as a note, not a lock:

* an adapter whose switch was rolled back is remembered and marked;
* it stays selectable, and the mark is dropped the moment it does work
  (a driver update is the usual reason);
* the mark survives a restart, because it goes into the config;
* the index in the label is untouched, because that is what the action is
  parsed from.

Run:  runtime\\python.exe tests\\test_gpu_mark.py
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import capture  # noqa: E402
import settings_io  # noqa: E402

FAKE = [(0, "NVIDIA GeForce RTX 5080"), (2, "NVIDIA GeForce RTX 5080")]


def _state(marked):
    return types.SimpleNamespace(cfg={"gpu": 0, "gpu_no_nr": list(marked),
                                      "profile": "Natural"},
                                 lang="en")


def main() -> int:
    failures = []
    real = capture.list_adapters
    capture.list_adapters = lambda: FAKE
    settings_io.list_adapters = lambda: FAKE
    try:
        # 1. Nothing marked: two plain entries, and they are the two the
        #    machine has.
        plain = [f"{i}: {n}" for i, n in FAKE]
        got = [f"{i}: {n}" for i, n in settings_io.list_adapters()]
        if got != plain:
            failures.append(f"the adapter list itself changed: {got}")

        # 2. Adapter 2 marked: it is still there, still second, still
        #    parseable as index 2 - with a note.
        st = _state([2])
        labels = [f"{i}: {n}" + (" - " + "no neural pass" if i in settings_io._no_nr(st) else "")
                  for i, n in FAKE]
        print(f"    marked list: {labels}")
        if labels[0] != "0: NVIDIA GeForce RTX 5080":
            failures.append(f"the working card was marked too: {labels[0]!r}")
        if "no neural pass" not in labels[1]:
            failures.append(f"the refused card carries no note: {labels[1]!r}")
        if int(labels[1].split(":")[0]) != 2:
            failures.append("the note broke the index the action is parsed from")

        # 3. The set survives a round trip through the config payload.
        payload = settings_io._menu_layout_payload(
            {"profile": "Natural", "gpu": 0, "spout": False, "skip_static": True,
             "rec_indicator": True, "screenshot_dir": "", "presets": {},
             "gpu_no_nr": [2]},
            {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
             "skin_structure": -1.0},
            0, "en", 0.65, 0.0, True, False,
            type("M", (), {"user_scale": 1.0, "user_height": None,
                           "state": {"theme": "light"}, "offset": [0, 0]})())
        if payload.get("gpu_no_nr") != [2]:
            failures.append(f"the mark does not survive a save: "
                            f"{payload.get('gpu_no_nr')!r}")

        # 4. Junk in the config does not take the menu down - the config is
        #    user-editable and this key is new.
        for junk in (None, "2", ["x"], 5):
            st = _state([]) ; st.cfg["gpu_no_nr"] = junk
            try:
                settings_io._no_nr(st)
            except Exception as exc:
                failures.append(f"gpu_no_nr={junk!r} raised {exc!r}")
    finally:
        capture.list_adapters = real
        settings_io.list_adapters = real

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the refused card is marked, selectable, and remembered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
