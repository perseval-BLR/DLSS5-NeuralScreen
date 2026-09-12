"""An HDR display gets said out loud, once.

The network is trained on SDR. On an HDR desktop its result reads as
"everything is too bright and lowering the sliders does nothing" - which is
a report we have already had (issue #27) and a notice a user asked for
(issue #33). The log carried the fact and nothing else did.

The fact comes from the worker, which asks the OUTPUT it duplicates for its
colour space - so it is about the screen being processed, not about some
monitor in the registry, which is what the startup header reads.

Checked: it speaks on the marker, it speaks ONCE (an alert on every frame
would be worse than silence), it stays quiet without the marker, and the
string exists in the user's language.

Run:  runtime\\python.exe tests\\test_hdr_notice.py
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402
from i18n import STRINGS  # noqa: E402

MARKER = "[dda] output colour space 12 - HDR IS ON for the captured display"
SDR = "[dda] output colour space 0"


def _state(logs, lang="en"):
    said = []
    return types.SimpleNamespace(
        hdr_alerted=False, lang=lang, worker_logs=list(logs),
        display=types.SimpleNamespace(alert=lambda t, duration=0: said.append(t)),
    ), said


def main() -> int:
    failures = []

    # 1. It speaks on the marker - and only once.
    st, said = _state(["[host] adapter 0", MARKER, "[dda] capture active"])
    settings_io.warn_hdr(st)
    settings_io.warn_hdr(st)
    settings_io.warn_hdr(st)
    if len(said) != 1:
        failures.append(f"the notice fired {len(said)} times, not once - it is "
                        f"asked on every thirtieth frame for the whole session")
    elif not said[0].strip():
        failures.append("the notice is empty")
    else:
        print(f"    said: {said[0][:70]}")

    # 2. An SDR display says nothing.
    st, said = _state(["[host] adapter 0", SDR, "[dda] capture active"])
    settings_io.warn_hdr(st)
    if said:
        failures.append(f"an SDR display got the HDR notice: {said}")

    # 3. Nothing at all in the log: nothing said, and nothing raised.
    st, said = _state([])
    settings_io.warn_hdr(st)
    if said:
        failures.append("the notice fired with an empty log")

    # 4. The marker can be older than the tail of a busy log.
    st, said = _state([MARKER] + [f"[present] frame {i}" for i in range(150)])
    settings_io.warn_hdr(st)
    if not said:
        failures.append("the notice was lost behind later diagnostics - the "
                        "worker prints it once, when the capture opens")

    # 5. Every language has the string (test_i18n pins the key set; this
    #    pins that THIS key is the one the code asks for).
    for lang, table in STRINGS.items():
        if not table.get("hdr_on"):
            failures.append(f"no hdr_on string for {lang!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an HDR display is reported once, an SDR one not at all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
