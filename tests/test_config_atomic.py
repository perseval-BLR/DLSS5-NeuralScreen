"""The atomic config write: temp file + fsync + os.replace, and the payload.

Pure unit test - no worker, no window, no program launch. It feeds
_atomic_write_json() and _menu_layout_payload() from main.py and checks
the contract:

* the helper writes a valid JSON file and replaces the target;
* a crash mid-write (os.replace or os.fsync raising) leaves the ORIGINAL
  config intact and no temp file behind;
* the save sites persist profile, params (intensity/local_tone/
  local_structure/skin_structure) and monitor - the menu changes them in
  memory only, so without this save they would be lost on the next launch.
"""
import builtins
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from main import (  # noqa: E402
    _atomic_write_json, _menu_layout_payload, load_config, resolve_params,
)

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


def _payload(cfg=None, params=None, monitor=1):
    cfg = dict(GOOD) if cfg is None else cfg
    params = resolve_params(cfg) if params is None else params
    return _menu_layout_payload(
        cfg, params, monitor, "en", 0.65, 0.5, True, False, _Menu())


def main() -> int:
    failures = []

    # 1. The helper writes a valid JSON file and replaces the target.
    d = tempfile.mkdtemp(prefix="ns-atomic-")
    try:
        target = Path(d) / "config.json"
        target.write_text('{"old": true}\n', encoding="utf-8")
        data = {"a": 1, "b": [1, 2], "c": {"d": "e"}}
        _atomic_write_json(target, data)
        if not target.is_file():
            failures.append("the target was not written")
        else:
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(f"the written file is not valid JSON: {exc}")
                loaded = None
            if loaded != data:
                failures.append(f"written data differs: {loaded} vs {data}")
            if not target.read_text(encoding="utf-8").endswith("\n"):
                failures.append("the written file does not end with a newline")
        leftover = list(Path(d).glob("*.tmp"))
        if leftover:
            failures.append(f"temp files left behind: {leftover}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    # 2. A crash at the replace step (os.replace raises) leaves the ORIGINAL
    #    config intact and no temp file behind.
    d = tempfile.mkdtemp(prefix="ns-atomic-")
    try:
        target = Path(d) / "config.json"
        original = '{"monitor": 0, "profile": "Natural"}\n'
        target.write_text(original, encoding="utf-8")
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated crash at the replace step")

        os.replace = boom
        try:
            try:
                _atomic_write_json(target, {"monitor": 1})
                failures.append("the replace crash did not propagate")
            except OSError:
                pass
        finally:
            os.replace = real_replace
        if target.read_text(encoding="utf-8") != original:
            failures.append("the original config changed after a replace crash")
        leftover = list(Path(d).glob("*.tmp"))
        if leftover:
            failures.append(f"temp files left after a replace crash: {leftover}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    # 3. A crash mid-write (os.fsync raises) leaves the ORIGINAL config
    #    intact and no temp file behind.
    d = tempfile.mkdtemp(prefix="ns-atomic-")
    try:
        target = Path(d) / "config.json"
        original = '{"monitor": 0, "profile": "Natural"}\n'
        target.write_text(original, encoding="utf-8")
        real_fsync = os.fsync

        def boom_fsync(fd):
            raise OSError("simulated crash mid-write (fsync)")

        os.fsync = boom_fsync
        try:
            try:
                _atomic_write_json(target, {"monitor": 1})
                failures.append("the fsync crash did not propagate")
            except OSError:
                pass
        finally:
            os.fsync = real_fsync
        if target.read_text(encoding="utf-8") != original:
            failures.append("the original config changed after a mid-write crash")
        leftover = list(Path(d).glob("*.tmp"))
        if leftover:
            failures.append(f"temp files left after a mid-write crash: {leftover}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    # 4. The save payload persists profile, params and monitor. The monitor
    #    is saved as the DXGI devicename when it can be resolved (multi-
    #    monitor machines), falling back to the positional int otherwise
    #    (single monitor: devicename_for_output_idx(2) is None).
    cfg = dict(GOOD, profile="Extreme / Overdrive")
    params = resolve_params(cfg)
    params["intensity"] = 2.1
    params["local_tone"] = 1.9
    payload = _payload(cfg, params, monitor=2)
    if payload["profile"] != "Extreme / Overdrive":
        failures.append(f"profile not persisted: {payload['profile']!r}")
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        if payload[key] != params[key]:
            failures.append(f"{key} not persisted: {payload[key]} vs {params[key]}")
    if payload["monitor"] != 2 and not (
            isinstance(payload["monitor"], str)
            and payload["monitor"].startswith("\\\\.\\DISPLAY")):
        failures.append(f"monitor not persisted: {payload['monitor']!r}")

    # 5. Round trip: the payload merged into a config survives load_config
    #    and resolve_params - the settings the user changed in the menu come
    #    back on the next launch.
    d = tempfile.mkdtemp(prefix="ns-atomic-")
    try:
        target = Path(d) / "config.json"
        base = dict(GOOD)
        base.update(_payload(cfg, params, monitor=2))
        _atomic_write_json(target, base)
        loaded = load_config(target)
        if loaded["profile"] != "Extreme / Overdrive":
            failures.append(f"round trip lost the profile: {loaded['profile']!r}")
        if loaded["monitor"] != 2 and not (
                isinstance(loaded["monitor"], str)
                and loaded["monitor"].startswith("\\\\.\\DISPLAY")):
            failures.append(f"round trip lost the monitor: {loaded['monitor']!r}")
        resolved = resolve_params(loaded)
        for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
            if resolved[key] != params[key]:
                failures.append(f"round trip lost {key}: {resolved[key]} vs {params[key]}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    # 6. Both save sites go through the atomic writer, and the menu-layout
    #    save goes through the payload builder.
    #
    #    This used to grep main.py for a literal call, because "the closures
    #    are not importable without launching the program". They are plain
    #    functions in settings_io now, so the check calls them and watches
    #    what they do - which is what it wanted to know all along.
    import types

    import settings_io

    target = Path(tempfile.mkdtemp()) / "config.json"
    target.write_text(json.dumps(GOOD), encoding="utf-8")
    seen = {"atomic": 0, "payload": 0}
    real_atomic = settings_io._atomic_write_json
    real_payload = settings_io._menu_layout_payload

    def spy_atomic(path, data):
        seen["atomic"] += 1
        return real_atomic(path, data)

    def spy_payload(*a, **kw):
        seen["payload"] += 1
        return real_payload(*a, **kw)

    st = types.SimpleNamespace(
        cfg_path=target, cfg=dict(GOOD), params=resolve_params(GOOD),
        monitor=1, lang="en", work_scale=0.65, split_pos=0.5,
        startup_menu=True, nr_small=False,
        display=types.SimpleNamespace(menu=_Menu()))
    settings_io._atomic_write_json = spy_atomic
    settings_io._menu_layout_payload = spy_payload
    try:
        if not settings_io.save_menu_layout(st):
            failures.append("save_menu_layout reported failure")
        if not settings_io.save_hotkeys(st, {"toggle": "Num1"}):
            failures.append("save_hotkeys reported failure")
    finally:
        settings_io._atomic_write_json = real_atomic
        settings_io._menu_layout_payload = real_payload

    if seen["atomic"] != 2:
        failures.append(f"expected 2 atomic save sites, saw {seen['atomic']}")
    if seen["payload"] != 1:
        failures.append("save_menu_layout does not use the payload builder")
    written = json.loads(target.read_text(encoding="utf-8"))
    if written.get("hotkeys") != {"toggle": "Num1"}:
        failures.append(f"the hotkeys did not reach the file: "
                        f"{written.get('hotkeys')!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the config write is atomic and persists profile/params/monitor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
