"""HDR capture warnings distinguish SDR fallback from native FP16 and startup."""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402
from i18n import STRINGS  # noqa: E402

MARKER = "[dda] output colour space 12 - HDR IS ON for the captured display"
SDR = "[dda] output colour space 0"
CAP_SDR = "[hdr] capture=SDR; neural processing=SDR proxy; export=SDR"
CAP_HDR = "[hdr] capture=FP16 scRGB; neural processing=SDR proxy; export=SDR"


def _state(logs, lang="en"):
    said = []
    return types.SimpleNamespace(
        hdr_alerted=False, lang=lang, worker_logs=list(logs),
        display=types.SimpleNamespace(alert=lambda t, duration=0: said.append(t)),
    ), said


def main() -> int:
    failures = []

    # 1. It speaks on the marker - and only once.
    st, said = _state(["[host] adapter 0", MARKER, CAP_SDR])
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
    st, said = _state([MARKER, CAP_SDR] + [f"[present] frame {i}" for i in range(250)])
    settings_io.warn_hdr(st)
    if not said:
        failures.append("the notice was lost behind later diagnostics - the "
                        "worker prints it once, when the capture opens")

    # Native HDR, discovery before the first frame, and a new SDR output
    # must not inherit a warning from earlier capture diagnostics.
    for logs in ([MARKER], [MARKER, CAP_HDR],
                 [MARKER, CAP_SDR, SDR, CAP_SDR],
                 [MARKER, CAP_SDR, MARKER],
                 [MARKER, CAP_HDR, "[hdr] presentation=FP16 scRGB"]):
        st, said = _state(logs)
        settings_io.warn_hdr(st)
        if said or st.hdr_alerted:
            failures.append(f"false HDR warning: {logs}")
    st, said = _state([MARKER])
    settings_io.warn_hdr(st)
    st.worker_logs.append(CAP_SDR)
    settings_io.warn_hdr(st)
    if len(said) != 1:
        failures.append("SDR fallback after startup was not reported")

    # 5. Every language has the string (test_i18n pins the key set; this
    #    pins that THIS key is the one the code asks for).
    for lang, table in STRINGS.items():
        if not table.get("hdr_on"):
            failures.append(f"no hdr_on string for {lang!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: HDR SDR fallback reported once; FP16, startup and SDR stay quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
