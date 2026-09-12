"""Audit F12: the GPU-refusal alert must fire without opening the menu.

d8d063d made the NR verdict fire a gpu_nr_fail alert from
settings_io.refresh_gpu_ok - but refresh_gpu_ok is called only by
menu_payload, which runs only while the menu is visible (or at the
startup-menu moment). With the menu closed (open_menu_on_start off, or
after "collapse"), the verdict arrives silently - exactly the issue #29
situation the alert was added for ("nothing on the screen said so").

Expected: a main-loop tick evaluates the verdict, or the call chain
reaches refresh_gpu_ok from a place that runs with the menu closed.
[audit python-core F12]

Run:  runtime\\python.exe tests\\test_gpu_alert_path.py
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


def main() -> int:
    failures = []

    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    settings_src = (BASE / "settings_io.py").read_text(encoding="utf-8")

    # Where refresh_gpu_ok is defined and what calls it.
    if "def refresh_gpu_ok" not in settings_src:
        failures.append("refresh_gpu_ok is gone - re-check the finding")
    callers = [name for name in ("main.py",) if "refresh_gpu_ok" in
               (BASE / name).read_text(encoding="utf-8")]
    # menu_payload is the only in-code caller?
    in_menu_payload = bool(re.search(
        r"def menu_payload\(st[^)]*\).*?refresh_gpu_ok\(st\)",
        settings_src, re.S))
    print(f"    refresh_gpu_ok called from menu_payload: {in_menu_payload}")
    print(f"    refresh_gpu_ok referenced directly in main.py: "
          f"{bool(callers)}")

    # menu_payload is only built while the menu is visible / at startup.
    guarded = "if st.display.menu.visible" in main_src
    print(f"    menu_payload guarded by menu.visible in the loop: {guarded}")

    if in_menu_payload and guarded and not callers:
        failures.append(
            "F12: the verdict (and its alert) is only evaluated inside "
            "menu_payload, which the loop builds only while the menu is "
            "visible - with the menu closed a refused feature stays silent, "
            "the exact issue #29 gap the alert was added to close")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the GPU verdict reaches the alert with the menu closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
