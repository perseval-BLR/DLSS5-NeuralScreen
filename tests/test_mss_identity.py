"""Audit: the mss fallback must not fabricate a monitor identity.

ScreenCapture._open_mss sets self.devicename = f"\\\\\\\\.\\\\DISPLAY{monitor_idx+1}"
- a positional GUESS. DXGI output order and DISPLAYn numbering are not
guaranteed to agree (the module's own docstring says so), so on an
Optimus machine the guessed name can point at ANOTHER monitor: the
overlay origin, NS_OUTPUT and the saved config identity all follow the
guess while the capture itself is on the right display.

Expected: the reported devicename is the one the opened monitor really
has, or an explicit "unknown" (which _apply_monitor_env handles).
[audit ui-display]

Run:  runtime\\python.exe tests\\test_mss_identity.py
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
    src = (BASE / "capture.py").read_text(encoding="utf-8")

    # The synthesis line, as it exists today.
    if 'f"\\\\\\\\.\\\\DISPLAY{monitor_idx + 1}"' not in src:
        # try the raw form
        if "DISPLAY{monitor_idx + 1}" not in src:
            print("NOTE: the synthesis line is gone - finding likely fixed")
        else:
            failures.append(
                "the mss fallback still synthesises a devicename from the "
                "POSITIONAL index; DXGI order != DISPLAYn numbering can make "
                "monitor_origin/NS_OUTPUT/config identity point at another "
                "display while the capture is on the right one")

    # Is there any real-identity lookup in _open_mss?
    import re
    m = re.search(r"def _open_mss\(self.*?(?=\n    def |\Z)", src, re.S)
    if m:
        body = m.group(0)
        has_lookup = ("EnumDisplayMonitors" in body
                      or "resolve_output_idx" in body
                      or "unknown" in body.lower())
        if not has_lookup and "DISPLAY{" in body:
            failures.append(
                "the mss devicename is still a positional guess with no "
                "identity lookup and no 'unknown' escape hatch")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the mss fallback does not fabricate a monitor identity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
