"""FG toggle, discrete multiplier, persisted config and frame-header wiring."""
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import pygame
import commands
import protocol
import settings_io
from test_ui_buttons import build, find, paint, click


def main():
    pygame.init()
    menu = build()
    paint(menu)
    assert find(menu, "slider", "frame_multiplier") is None
    toggle = find(menu, "toggle", "frame_generation")
    assert toggle is not None
    st = SimpleNamespace(cfg={})
    with patch.object(settings_io, "save_menu_layout") as save:
        actions = click(menu, toggle)
        assert actions == [("toggle", "frame_generation")], actions
        commands.apply_menu_action(st, actions[0])
        assert st.cfg["frame_generation"]
        menu.set_state(st.cfg)
        paint(menu)
        slider = find(menu, "slider", "frame_multiplier")
        assert slider is not None
        track = slider.extra["track"]
        for fraction, value in ((1, 4), (0.5, 3), (0, 2)):
            actions = menu._slide(slider, track.x + track.w * fraction)
            assert actions == [("frame_multiplier", value)], actions
            commands.apply_menu_action(st, actions[0])
            assert st.cfg["frame_multiplier"] == value
            worker = SimpleNamespace(stdin=io.BytesIO())
            protocol.send_frame(worker, 0, None, np.zeros((2, 2, 2), np.float16), False, 0,
                                no_color=True, split=0.5, frame_generation=True, frame_multiplier=value)
            header = struct.unpack(protocol.FRAME_FMT, worker.stdin.getvalue()[:struct.calcsize(protocol.FRAME_FMT)])
            flags = header[3]
            assert flags & 0x800 and flags & 0x100
            assert ((flags >> 9) & 3) + 2 == value
            assert flags >> 16 == 32768, "FG flags changed the comparison wipe"
        commands.apply_menu_action(st, ("toggle", "frame_generation"))
        menu.set_state(st.cfg)
        paint(menu)
        assert not st.cfg["frame_generation"]
        assert find(menu, "slider", "frame_multiplier") is None
        assert save.call_count == 5
    menu.set_state({"nr_small": True})
    paint(menu)
    sr_toggle = find(menu, "toggle", "dlss_sr")
    assert sr_toggle is not None
    sr_state = SimpleNamespace(cfg={}, nr_small=True, work_scale=.65)
    with patch.object(settings_io, "save_menu_layout"):
        actions = click(menu, sr_toggle)
        assert actions == [("toggle", "dlss_sr")], actions
        commands.apply_menu_action(sr_state, actions[0])
        assert sr_state.cfg["dlss_sr"]
    worker = SimpleNamespace(stdin=io.BytesIO())
    protocol.send_frame(worker, 9, None, np.zeros((2, 2, 2), np.float16), False, 0,
                        no_color=True, split=.5, prepared=True, dlss_sr=True,
                        frame_generation=True, frame_multiplier=4)
    flags = struct.unpack(protocol.FRAME_FMT, worker.stdin.getvalue()[:24])[3]
    assert flags & 0x7000 == 0x7000 and flags >> 16 == 32768
    assert ((flags >> 9) & 3) == 2
    # SR is independent of Boost, including when Boost is disabled.
    menu.set_state({"nr_small": False, "dlss_sr": True, "dlss_sr_scale": .65})
    paint(menu)
    assert find(menu, "slider", "nr_res") is None
    assert find(menu, "slider", "dlss_sr_scale") is not None
    independent = SimpleNamespace(cfg={"dlss_sr": False, "dlss_sr_scale": .65},
                                  nr_small=False, work_scale=.8)
    with patch.object(settings_io, "save_menu_layout"), patch.object(commands.pipeline, "request_apply") as apply:
        commands.apply_menu_action(independent, ("toggle", "dlss_sr"))
        commands.apply_menu_action(independent, ("dlss_sr_scale", .5))
        assert independent.cfg["dlss_sr"] and independent.cfg["dlss_sr_scale"] == .5
        assert independent.work_scale == .8 and independent.nr_small is False
        apply.assert_not_called()
    menu.set_state({"nr_small": True, "work_scale": .8})
    paint(menu)
    assert find(menu, "slider", "nr_res") and find(menu, "slider", "dlss_sr_scale")
    sr_slider = find(menu, "slider", "dlss_sr_scale")
    actions = menu._slide(sr_slider, sr_slider.extra["track"].left)
    assert actions == [("dlss_sr_scale", .25)] and menu.state["work_scale"] == .8
    menu.set_state({"screen_size": "2560x1440", "work_size": "1920x1080",
                    "nr_small": True, "dlss_sr": True, "dlss_sr_scale": .5})
    paint(menu)
    assert find(menu, "slider", "nr_res").extra["value_text"] == "960x540"
    assert find(menu, "slider", "dlss_sr_scale").extra["value_text"] == "1280x720"
    menu.set_state({"dlss_sr": False})
    paint(menu)
    assert find(menu, "slider", "nr_res").extra["value_text"] == "1920x1080"
    # A failed feature must not leave a checked but inactive SR control.
    failed = SimpleNamespace(cfg={"dlss_sr": True}, lang="en",
        worker_logs=["[sr] first evaluation succeeded", "[sr] Evaluate failed 0xBAD; using Boost composite"],
        display=SimpleNamespace(alert=lambda *args, **kwargs: None))
    with patch.object(settings_io, "save_menu_layout") as save:
        settings_io.refresh_sr(failed)
        assert failed.cfg["dlss_sr"] is False and save.call_count == 1
        settings_io.refresh_sr(failed)
        assert save.call_count == 1
    # The real serializer must retain both settings, not just the command handler.
    cfg = settings_io.load_config(ROOT / "config.json")
    cfg.update(frame_generation=True, frame_multiplier=4, dlss_sr=True, dlss_sr_scale=.5, ui_detection=True)
    params = settings_io.resolve_params(cfg)
    data = dict(cfg)
    data.update(settings_io._menu_layout_payload(cfg, params,
        cfg["monitor"] if isinstance(cfg["monitor"], int) else 0,
        cfg["lang"], cfg["work_scale"], 0.0, True, bool(cfg.get("nr_small")), menu))
    assert data["frame_generation"] and data["frame_multiplier"] == 4
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        restored = settings_io.load_config(path)
        assert restored["ui_detection"]
        assert restored["dlss_sr_scale"] == .5
        assert restored["dlss_sr"]
        assert restored["frame_generation"] and restored["frame_multiplier"] == 4
    pygame.quit()
    print("OK: FG controls, multiplier steps, persistence and wire flags")


if __name__ == "__main__":
    main()
