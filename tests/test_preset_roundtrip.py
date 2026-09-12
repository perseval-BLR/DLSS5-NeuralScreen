"""Audit F1: Save preset must reach config.json.

commands.py "save_preset" builds the preset, sets st.cfg["presets"] and
calls settings_io.save_menu_layout - which merges _menu_layout_payload
into the file. That payload has no "presets" key, so the preset never
lands on disk and dies on the next launch. The user still sees "Preset
saved" and the code's own failure branch (which promises to tell the
user "it will not survive a restart") can never fire, because the write
itself "succeeds".

Expected: after apply_menu_action(("button", "save_preset")) the re-read
file carries the preset, and Delete removes it from the file. [audit F1]

Run:  runtime\\python.exe tests\\test_preset_roundtrip.py
"""
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))

import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402


def _menu():
    return types.SimpleNamespace(user_scale=1.0, user_height=None,
                                 state={"theme": "light"}, offset=[0, 0],
                                 set_state=lambda d: None)


def _st(cfg_path: Path):
    return types.SimpleNamespace(
        cfg={"profile": "Natural"}, cfg_path=cfg_path,
        # A REAL params snapshot, not four sliders: the running program
        # carries the NGX plumbing (profile/preset/style/auto_mask/
        # ui_correction) in st.params too, and load_presets drops any
        # entry that does not have it. With a four-key fixture the
        # reload check fails for a reason the program never has.
        params=dict(settings_io.PROFILES["Natural"]),
        presets={}, monitor=0, lang="en", work_scale=0.65, split_pos=0.0,
        startup_menu=True, nr_small=False,
        display=types.SimpleNamespace(menu=_menu(),
                                      alert=lambda *a, **k: None),
    )


def main() -> int:
    failures = []
    d = Path(tempfile.mkdtemp(prefix="ns-preset-"))
    real_apply = pipeline.request_apply
    pipeline.request_apply = lambda *a, **k: None  # delete_preset's tail call
    # commands.py holds its own reference to pipeline - patch both.
    real_cmd_apply = commands.pipeline.request_apply
    commands.pipeline.request_apply = lambda *a, **k: None
    try:
        cfg_path = d / "config.json"
        cfg_path.write_text(json.dumps({
            "monitor": 0, "width": 1920, "height": 1080, "fullscreen": True,
            "warmup": 30, "profile": "Natural", "work_scale": 0.65,
            "intensity": None, "local_tone": None,
            "local_structure": None, "skin_structure": None}), encoding="utf-8")
        st = _st(cfg_path)

        # 1. Save: the preset must reach the file, not just memory.
        commands.apply_menu_action(st, ("button", "save_preset"))
        if not st.presets:
            failures.append("save_preset created nothing in memory")
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        if "presets" not in on_disk:
            failures.append(
                "F1: the preset did not reach config.json - the payload "
                "carries no 'presets' key, so it dies on the next launch")
        else:
            name = next(iter(st.presets))
            if on_disk["presets"].get(name) != st.presets[name]:
                failures.append("F1: the on-disk preset differs from memory")

        # 2. It survives a reload (the real next-launch path).
        if st.presets:
            name = next(iter(st.presets))
            reloaded = settings_io.load_presets(on_disk)
            if name not in reloaded:
                failures.append("F1: load_presets does not see the saved preset")

        # 3. Delete: removed from the file too.
        if st.presets:
            name = next(iter(st.presets))
            st.cfg["profile"] = name
            commands.apply_menu_action(st, ("button", "delete_preset"))
            on_disk2 = json.loads(cfg_path.read_text(encoding="utf-8"))
            if (on_disk2.get("presets") or {}).get(name):
                failures.append("F1: delete did not remove the preset from disk")
    finally:
        pipeline.request_apply = real_apply
        commands.pipeline.request_apply = real_cmd_apply
        shutil.rmtree(d, ignore_errors=True)

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: presets save to and delete from config.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
