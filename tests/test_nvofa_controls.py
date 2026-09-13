"""Backend selection, restart, fallback and scene tracking without NVIDIA hardware."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import numpy as np
import pygame
import commands
import pipeline
import settings_io
from guides import TemporalGuideGenerator
from motion_backend import MotionBackendStatus, normalize_backend
from test_ui_buttons import build, paint, find
from test_config_atomic import _payload, GOOD
import json
import tempfile


def main():
    assert all(normalize_backend(v) == "cpu" for v in [None, {}, [], 1, "gpu", "CPU"])
    assert normalize_backend("nvofa") == "nvofa"
    assert _payload()["motion_backend"] == "cpu"
    assert _payload(dict(GOOD, motion_backend="nvofa"))["motion_backend"] == "nvofa"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for value in (None, "nvofa", "obsolete"):
            path.write_text(json.dumps(dict(GOOD, motion_backend=value)), encoding="utf-8")
            assert settings_io.load_config(path)["motion_backend"] == normalize_backend(value)
    state = MotionBackendStatus()
    worker, replacement = object(), object()
    assert not state.update(worker, [])
    assert state.update(worker, ["[nvofa] active: driver"])
    assert state.update(worker, ["old logs evicted"])
    assert not state.update(worker, ["[nvofa] active: driver", "[nvofa] unavailable: execute"])
    assert state.failed
    assert not state.update(worker, []) and state.failed
    assert not state.update(replacement, []) and not state.failed

    guide = TemporalGuideGenerator(320, 180, emit_small=True)
    rng = np.random.default_rng(17)
    gray = rng.integers(60, 170, (180, 320), dtype=np.uint8)
    guide.process(gray=gray)
    original = guide.dis
    guide.dis = Mock(wraps=original)
    shifted = np.roll(gray, 3, axis=1)
    assert not guide.process(gray=shifted, compute_motion=False).motion.any()
    guide.dis.calc.assert_not_called()
    # Fallback resumes against the last captured image, not a stale CPU pair.
    assert np.array_equal(guide.previous_gray, shifted)
    guide.process(gray=np.roll(shifted, 2, axis=1))
    guide.dis.calc.assert_called_once()
    assert guide.process(gray=np.full_like(gray, 255), compute_motion=False).reset

    st = SimpleNamespace(cfg={}, lang="en")
    with patch.dict(os.environ), patch.object(settings_io, "save_menu_layout") as save, \
         patch.object(pipeline, "teardown_pipeline") as down, \
         patch.object(pipeline, "rebuild_pipeline") as up:
        commands.apply_menu_action(st, ("motion_backend", "nvofa"))
        assert st.cfg["motion_backend"] == os.environ["NS_MOTION_BACKEND"] == "nvofa"
        save.assert_called_once_with(st); down.assert_called_once_with(st)
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "nvofa")
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "cpu")
        assert os.environ["NS_MOTION_BACKEND"] == "cpu" and up.call_count == 2

    pygame.init()
    try:
        menu = build(); menu.page = "settings"; paint(menu)
        item = find(menu, "choice", "motion_backend")
        assert item and item.payload == ["cpu", "nvofa"]
        assert menu._pick("motion_backend", "nvofa") == [("motion_backend", "nvofa")]
    finally:
        pygame.quit()
    print("PASS: NVOFA selection/restart, fallback/restart tracking, CPU resume and scene cuts")


if __name__ == "__main__":
    main()
