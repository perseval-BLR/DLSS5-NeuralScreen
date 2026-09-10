"""The wire protocol is one layout described in two languages - check it.

Every command travelling the pipe is a packed C++ struct on one side and a
struct.calcsize format on the other. Nothing used to check that they still
agree: a field added inside the packed region shows up as a runtime desync,
never as a build error.

The C++ side now carries a static_assert per struct, and each assert names the
Python format it must match:

    static_assert(sizeof(VideoWgcCmd) == 32, "VideoWgcCmd != WGC_FMT");

So this test needs no table of its own. It reads those asserts, looks up the
named format in main.py, and demands the same number - which means the two
sides can only drift if someone edits both files to disagree on purpose.

Run:  runtime\\python.exe tests\\test_protocol_sizes.py
"""
import re
import struct
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import main as ns_main  # noqa: E402

CPP = BASE / "native" / "dlss5-feed-host64.cpp"
ASSERT = re.compile(
    r'static_assert\(sizeof\((\w+)\)\s*==\s*(\d+),\s*"[^"]*!=\s*(\w+)"\)')


def main() -> int:
    if not CPP.is_file():
        print(f"SKIP: no {CPP}")
        return 0
    text = CPP.read_text(encoding="utf-8-sig")
    pairs = ASSERT.findall(text)
    if not pairs:
        print("FAIL: the worker carries no protocol static_asserts - the two "
              "sides are unpinned again")
        return 1

    failures = []
    for cpp_name, size, fmt_name in pairs:
        want = int(size)
        fmt = getattr(ns_main, fmt_name, None)
        if fmt is None:
            failures.append(f"{cpp_name}: main.py has no {fmt_name}")
            continue
        got = struct.calcsize(fmt)
        if got != want:
            failures.append(f"{cpp_name}: C++ says {want} bytes, "
                            f"{fmt_name} ({fmt}) packs {got}")
        else:
            print(f"    {cpp_name:20} {fmt_name:16} {got:3} bytes")
    print(f"    {len(pairs)} structs pinned on both sides")

    # Every format main.py sends must be pinned somewhere - a new command with
    # no assert is exactly the gap this test closes.
    pinned = {p[2] for p in pairs}
    declared = {name for name in dir(ns_main)
                if name.endswith("_FMT") and isinstance(getattr(ns_main, name), str)}
    unpinned = sorted(declared - pinned)
    if unpinned:
        failures.append(f"formats with no static_assert on the C++ side: {unpinned}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: both sides agree on every command size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
