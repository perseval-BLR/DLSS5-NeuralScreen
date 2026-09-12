"""Regression: state payload must reach the checkbox; Russian must survive encoding."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pygame
import commands
import pipeline
import settings_io
from i18n import STRINGS
from test_ui_buttons import build, paint, find, click

def main():
    pygame.init()
    try:
        menu = build()
        menu.set_state({"lang": "ru"})
        st = SimpleNamespace(cfg={"profile": "Natural"}, lang="ru")
        with patch.dict(os.environ), patch.object(settings_io, "save_menu_layout"), \
             patch.object(pipeline, "teardown_pipeline"), patch.object(pipeline, "rebuild_pipeline"):
            for before in [False, True, False]:
                st.cfg["gpu_motion"] = before
                menu.set_state(st.cfg)
                paint(menu)
                item = find(menu, "toggle", "gpu_motion")
                assert item is not None
                assert bool(item.value) == before
                assert item.extra["label"] == "Расчёт движения на GPU (эксперимент)"
                assert "?" not in item.extra["hint"]
                action, = click(menu, item)
                commands.apply_menu_action(st, action)
                assert st.cfg["gpu_motion"] is not before
                assert os.environ["NS_GPU_FLOW_EXPERIMENT"] == str(int(not before))
                menu.set_state(st.cfg)
                paint(menu)
                assert bool(find(menu, "toggle", "gpu_motion").value) is not before
                payload = settings_io._menu_layout_payload(
                    st.cfg, {"intensity": 1., "local_tone": 1., "local_structure": 1., "skin_structure": -1.},
                    0, "ru", 1., 0., True, False, menu)
                assert payload["gpu_motion"] is not before
        print("PASS: checkbox on/off, restart environment, persisted payload and Russian text")
    finally:
        pygame.quit()

if __name__ == "__main__":
    main()
