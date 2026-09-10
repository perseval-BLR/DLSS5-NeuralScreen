"""Monitor identity by DXGI DeviceName.

The capture used to pick monitors by a positional index (EnumDisplayMonitors
order assumed to match dxcam's output_idx) - a cable unplug or a display
reorder silently redirected the capture to a different screen. Now
list_monitors() pairs each monitor with the dxcam output whose devicename
matches, and the config stores the devicename instead of the index.

Checked:
* list_monitors() returns 4-tuples (idx, w, h, devicename) with non-empty
  devicenames matching '\\\\.\\DISPLAY\\d+' (live, on the real desktop);
* the devicename -> output_idx resolution works, and list_monitors() uses
  it (monkeypatched EnumDisplayMonitors + dxcam mapping);
* the config save/load round trip keeps the devicename, and old int
  configs still load.
"""
import ctypes
import json
import re
import sys
import tempfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, capture.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import capture  # noqa: E402
from capture import ScreenCapture, list_monitors, resolve_output_idx  # noqa: E402
from main import _menu_layout_payload, load_config, resolve_params  # noqa: E402

DEVICE_RE = re.compile(r"^\\\\\.\\DISPLAY\d+$")

GOOD = {
    "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
    "warmup": 120, "work_scale": 0.65, "lang": "en",
    "profile": "Strong / Cinematic",
    "intensity": None, "local_tone": None, "local_structure": None,
    "skin_structure": None,
}


class _Menu:
    """The display.menu surface _menu_layout_payload reads."""

    user_scale = 1.0
    user_height = None
    state = {"theme": "dark"}
    offset = [10, 20]


@contextmanager
def _fake_monitors(entries):
    """Monkeypatch EnumDisplayMonitors + GetMonitorInfoW.

    entries: list of (hmon, devicename, w, h). The callback receives the
    rect and GetMonitorInfoW fills szDevice from the entry's devicename.
    """
    real_enum = ctypes.windll.user32.EnumDisplayMonitors
    real_getinfo = ctypes.windll.user32.GetMonitorInfoW

    def fake_enum(_hdc, _rect, proc, _lparam):
        for hmon, _dev, w, h in entries:
            r = wintypes.RECT(0, 0, w, h)
            proc(hmon, None, ctypes.byref(r), 0)
        return True

    def fake_getinfo(hmon, lpmi):
        for _hmon, dev, _w, _h in entries:
            if _hmon == hmon:
                info = ctypes.cast(
                    lpmi, ctypes.POINTER(capture._MONITORINFOEXW)).contents
                info.szDevice = dev
                return True
        return False

    ctypes.windll.user32.EnumDisplayMonitors = fake_enum
    ctypes.windll.user32.GetMonitorInfoW = fake_getinfo
    try:
        yield
    finally:
        ctypes.windll.user32.EnumDisplayMonitors = real_enum
        ctypes.windll.user32.GetMonitorInfoW = real_getinfo


def main() -> int:
    failures = []

    # 1. Live: list_monitors() returns 4-tuples with real devicenames.
    mons = list_monitors()
    print(f"live monitors: {mons}")
    if not mons:
        failures.append("list_monitors() returned no monitors")
    for entry in mons:
        if not isinstance(entry, tuple) or len(entry) != 4:
            failures.append(f"not a 4-tuple: {entry!r}")
            continue
        idx, w, h, dev = entry
        if not isinstance(idx, int):
            failures.append(f"idx is not an int: {entry!r}")
        if not (isinstance(w, int) and w > 0 and isinstance(h, int) and h > 0):
            failures.append(f"bad size: {entry!r}")
        if not isinstance(dev, str) or not dev:
            failures.append(f"empty devicename: {entry!r}")
        elif not DEVICE_RE.match(dev):
            failures.append(f"devicename does not match {DEVICE_RE.pattern}: {dev!r}")
        else:
            # The devicename must resolve back to the same output index.
            if resolve_output_idx(dev) != idx:
                failures.append(
                    f"devicename {dev!r} resolves to {resolve_output_idx(dev)}, "
                    f"list says {idx}")

    # 2. The devicename -> output_idx resolution (monkeypatched dxcam).
    fake_map = {"\\\\.\\DISPLAY1": 0, "\\\\.\\DISPLAY2": 1}
    real_map = capture._dxcam_output_index_by_devicename
    capture._dxcam_output_index_by_devicename = lambda: fake_map
    try:
        if resolve_output_idx("\\\\.\\DISPLAY2") != 1:
            failures.append("resolve_output_idx(DISPLAY2) != 1")
        if resolve_output_idx("\\\\.\\DISPLAY1") != 0:
            failures.append("resolve_output_idx(DISPLAY1) != 0")
        if resolve_output_idx("\\\\.\\DISPLAY9") is not None:
            failures.append("resolve_output_idx(unknown) is not None")
        if ScreenCapture.resolve_monitor("\\\\.\\DISPLAY2") != 1:
            failures.append("ScreenCapture.resolve_monitor(DISPLAY2) != 1")
        if ScreenCapture.resolve_monitor("\\\\.\\DISPLAY9") is not None:
            failures.append("ScreenCapture.resolve_monitor(unknown) is not None")
    finally:
        capture._dxcam_output_index_by_devicename = real_map

    # 3. list_monitors() matches BY devicename, not by position: the fake
    #    EnumDisplayMonitors order is DISPLAY1, DISPLAY2 while the dxcam
    #    mapping swaps the indices - the returned idx must follow the name.
    with _fake_monitors([
        (0x1001, "\\\\.\\DISPLAY1", 1920, 1080),
        (0x1002, "\\\\.\\DISPLAY2", 2560, 1440),
    ]):
        capture._dxcam_output_index_by_devicename = lambda: {
            "\\\\.\\DISPLAY1": 1, "\\\\.\\DISPLAY2": 0}
        try:
            got = list_monitors()
        finally:
            capture._dxcam_output_index_by_devicename = real_map
    want = [(1, 1920, 1080, "\\\\.\\DISPLAY1"),
            (0, 2560, 1440, "\\\\.\\DISPLAY2")]
    if got != want:
        failures.append(f"identity matching failed: {got} != {want}")

    # 4. Fallback: without a dxcam mapping the positional order is kept.
    with _fake_monitors([
        (0x1001, "\\\\.\\DISPLAY1", 1920, 1080),
        (0x1002, "\\\\.\\DISPLAY2", 2560, 1440),
    ]):
        capture._dxcam_output_index_by_devicename = lambda: {}
        try:
            got = list_monitors()
        finally:
            capture._dxcam_output_index_by_devicename = real_map
    want = [(0, 1920, 1080, "\\\\.\\DISPLAY1"),
            (1, 2560, 1440, "\\\\.\\DISPLAY2")]
    if got != want:
        failures.append(f"positional fallback failed: {got} != {want}")

    # 5. The config save payload stores the devicename, and the round trip
    #    through load_config keeps it.
    cfg = dict(GOOD)
    params = resolve_params(cfg)
    payload = _menu_layout_payload(
        cfg, params, 0, "en", 0.65, 0.5, True, False, _Menu())
    saved = payload["monitor"]
    if not isinstance(saved, str) or not DEVICE_RE.match(saved):
        failures.append(f"payload monitor is not a devicename: {saved!r}")
    else:
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                        encoding="utf-8")
        try:
            data = dict(GOOD)
            data.update(payload)
            json.dump(data, f)
            f.close()
            loaded = load_config(Path(f.name))
            if loaded["monitor"] != saved:
                failures.append(f"round trip lost the devicename: "
                                f"{loaded['monitor']!r} != {saved!r}")
        finally:
            Path(f.name).unlink(missing_ok=True)

    # 6. Old configs with a positional int still load unchanged.
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                    encoding="utf-8")
    try:
        json.dump(GOOD, f)
        f.close()
        loaded = load_config(Path(f.name))
        if loaded["monitor"] != 0:
            failures.append(f"old int config changed: {loaded['monitor']!r}")
    finally:
        Path(f.name).unlink(missing_ok=True)

    for fl in failures:
        print("FAIL:", fl)
    if failures:
        return 1
    print("OK: monitors are identified by DXGI devicename, not by position")
    return 0


if __name__ == "__main__":
    sys.exit(main())
