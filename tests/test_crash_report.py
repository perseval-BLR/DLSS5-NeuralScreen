"""A crash in the main loop must land in the log with its traceback.

A user on an RTX 3060 sent a log whose whole account of the failure was one
line:

    [main] ERROR: <built-in function get> returned a result with an exception set

That is a SystemError - some C call had already left an error indicator set
and the next builtin tripped over it - so the line names the messenger and
not the cause. With no traceback there was nothing to work from, and the
reporter ended up guessing at a fix and sending it as a patch (issue #41,
PR #42). A whole round trip lost to a missing traceback.

So: the loop's handler prints the traceback, and this pins it. Checked by
importing main and driving the handler's shape rather than by crashing the
real program - the point is the report, not the crash.

Run:  runtime\\python.exe tests\\test_crash_report.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def main() -> int:
    failures = []
    src = (BASE / "main.py").read_text(encoding="utf-8")

    # The loop's own handler - the one that ran in that log.
    m = re.search(r"except Exception as exc:\n(.*?)\n        return 1", src, re.S)
    if m is None:
        failures.append("the main loop's exception handler is gone - "
                        "re-check where a crash is reported now")
    else:
        body = m.group(1)
        if "format_exc" not in body and "print_exc" not in body:
            failures.append("the main loop reports a crash without its "
                            "traceback - a user's log then names the "
                            "messenger and not the cause (issue #41)")
        if "[main] ERROR" not in body:
            failures.append("the crash line lost its [main] ERROR marker - "
                            "that is what people grep for in the log")

    # And the top-level one, for a failure before the loop even starts.
    tail = src[src.rfind('if __name__ == "__main__":'):]
    if "print_exc" not in tail and "format_exc" not in tail:
        failures.append("a failure before the loop starts is reported "
                        "without a traceback")

    # SystemError is the class that actually arrived. It is an Exception, so
    # `except Exception` catches it - if that ever stops being true the
    # handler would miss exactly the case it was written for.
    if not issubclass(SystemError, Exception):
        failures.append("SystemError is no longer an Exception - the handler "
                        "would not catch the case from issue #41")

    print(f"    loop handler logs a traceback: "
          f"{bool(m and ('format_exc' in m.group(1) or 'print_exc' in m.group(1)))}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a crash reaches the log with its traceback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
