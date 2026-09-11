"""The Spout2 toggle: config flag -> NS_SPOUT -> the worker restart path.

The bridge lives inside the worker process and is switched only by the
environment variable it reads at startup (SpoutBridgeInit). This test
pins the contract:

* the config flag "spout" becomes NS_SPOUT=1 before the worker starts
  (and "0" when absent - the bridge is off by default);
* the toggle flips the flag, rewrites the environment and asks the
  pipeline for a restart (spied on - no real worker is launched);
* the choice survives a save/load round trip through config.json;
* the menu payload carries the flag to the UI.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

spec = importlib.util.spec_from_file_location("ns_main", str(BASE / "main.py"))
ns_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []

    # 1. The config flag sets NS_SPOUT=1; absent means "0" (off by default).
    os.environ.pop("NS_SPOUT", None)
    ns_main._apply_spout_env({"spout": True})
    if os.environ.get("NS_SPOUT") != "1":
        failures.append(f"spout=True did not set NS_SPOUT=1 "
                        f"(got {os.environ.get('NS_SPOUT')!r})")
    ns_main._apply_spout_env({})
    if os.environ.get("NS_SPOUT") != "0":
        failures.append(f"a config without the flag must set NS_SPOUT=0 "
                        f"(got {os.environ.get('NS_SPOUT')!r})")

    # 2. The toggle path: commands.apply_menu_action flips the flag, sets
    #    the environment and calls pipeline.apply_spout with the new value.
    import commands
    import pipeline

    calls = []
    real_apply = pipeline.apply_spout
    pipeline.apply_spout = lambda st, enabled: calls.append(bool(enabled))
    try:
        st = types.SimpleNamespace(cfg={"spout": False})
        commands.apply_menu_action(st, ("toggle", "spout"))
        if calls != [True]:
            failures.append(f"toggle from off must request on, got {calls}")
        st2 = types.SimpleNamespace(cfg={"spout": True})
        commands.apply_menu_action(st2, ("toggle", "spout"))
        if calls != [True, False]:
            failures.append(f"toggle from on must request off, got {calls}")
    finally:
        pipeline.apply_spout = real_apply

    # 3. apply_spout writes the flag, the environment and survives the
    #    rebuild being stubbed (the real one restarts the worker).
    import settings_io

    rebuilds = []
    real_rebuild = pipeline.rebuild_pipeline
    real_teardown = pipeline.teardown_pipeline
    pipeline.rebuild_pipeline = lambda st, note: rebuilds.append(note)
    pipeline.teardown_pipeline = lambda st: None
    saved = []
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: saved.append(dict(st.cfg)) or True
    try:
        st3 = types.SimpleNamespace(
            cfg={"spout": False}, lang="en",
            output_rgba=None, capture=types.SimpleNamespace(resolution=(1920, 1080)))
        pipeline.apply_spout(st3, True)
        if st3.cfg.get("spout") is not True:
            failures.append("apply_spout did not set cfg['spout']")
        if os.environ.get("NS_SPOUT") != "1":
            failures.append("apply_spout did not set NS_SPOUT=1")
        if not rebuilds:
            failures.append("apply_spout did not rebuild the pipeline")
        if not saved or saved[-1].get("spout") is not True:
            failures.append("apply_spout did not persist the choice")
        os.environ.pop("NS_SPOUT", None)
        pipeline.apply_spout(st3, False)
        if st3.cfg.get("spout") is not False:
            failures.append("apply_spout did not clear cfg['spout']")
        if os.environ.get("NS_SPOUT") != "0":
            failures.append("apply_spout did not set NS_SPOUT=0")
    finally:
        pipeline.rebuild_pipeline = real_rebuild
        pipeline.teardown_pipeline = real_teardown
        settings_io.save_menu_layout = real_save

    # 4. The payload persists the flag (config.json round trip).
    class _Menu:
        user_scale = 1.0
        user_height = None
        state = {"theme": "dark"}
        offset = [10, 20]

    GOOD = {
        "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
        "warmup": 120, "work_scale": 0.65, "lang": "en",
        "profile": "Natural",
        "intensity": None, "local_tone": None, "local_structure": None,
        "skin_structure": None,
    }
    payload = ns_main._menu_layout_payload(
        dict(GOOD, spout=True), ns_main.resolve_params(GOOD), 0, "en",
        0.65, 0.5, True, False, _Menu())
    if payload.get("spout") is not True:
        failures.append(f"the payload lost the spout flag: {payload.get('spout')!r}")
    payload2 = ns_main._menu_layout_payload(
        dict(GOOD), ns_main.resolve_params(GOOD), 0, "en",
        0.65, 0.5, True, False, _Menu())
    if payload2.get("spout") is not False:
        failures.append(f"the default must be off: {payload2.get('spout')!r}")

    # 5. Round trip through the atomic writer: what was saved comes back.
    d = tempfile.mkdtemp(prefix="ns-spout-")
    try:
        target = Path(d) / "config.json"
        base = dict(GOOD)
        base.update(payload)
        ns_main._atomic_write_json(target, base)
        loaded = ns_main.load_config(target)
        if loaded.get("spout") is not True:
            failures.append(f"round trip lost the flag: {loaded.get('spout')!r}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print(" -", f)
        return 1
    print("OK: the Spout2 toggle drives the config, the environment and the restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
