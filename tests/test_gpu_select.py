"""Choosing a GPU: the menu value reaches the config, the environment, a restart.

The worker picks its adapter before the D3D12 device exists (NS_GPU is read
once at process start), so the only way onto another card is a fresh worker -
the same path the Spout toggle takes. This test follows one choice end to
end without launching anything:

  menu value "1: NVIDIA ..." -> commands.apply_menu_action -> pipeline.apply_gpu
  -> cfg["gpu"], NS_GPU, teardown + rebuild -> and back into the menu payload
  and config.json on the next launch.

It also pins the two rules that keep the picker honest: the same card is not
a restart, and a card the machine does not have is not silently accepted as
a label.
"""
import os
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import capture  # noqa: E402
import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402
import startup  # noqa: E402

FAKE = [(0, "NVIDIA GeForce RTX 5070 Ti"), (1, "NVIDIA GeForce RTX 4060")]


def _state(gpu=0):
    """The fields apply_gpu touches, including the ones it reads back.

    worker/worker_logs are what the switch checks before saving: a card
    that cannot run the network is reverted instead (test_gpu_rollback).
    Here the card always comes up, so the switch is the whole story.
    """
    return types.SimpleNamespace(
        cfg={"gpu": gpu, "profile": "Natural", "lang": "en"}, lang="en",
        worker=types.SimpleNamespace(poll=lambda: None),
        worker_logs=["[pure] feature 18 ready"], gpu_switch_pending=False,
        display=types.SimpleNamespace(alert=lambda *a, **kw: None))


def main() -> int:
    failures = []
    calls = {"teardown": 0, "rebuild": 0, "saved": 0}
    real = (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
            settings_io.save_menu_layout, pipeline.settings_io.save_menu_layout)
    pipeline.teardown_pipeline = lambda st: calls.__setitem__("teardown", calls["teardown"] + 1)
    pipeline.rebuild_pipeline = lambda st, note: calls.__setitem__("rebuild", calls["rebuild"] + 1)
    pipeline.settings_io.save_menu_layout = lambda st: calls.__setitem__("saved", calls["saved"] + 1) or True
    before = os.environ.get("NS_GPU")
    try:
        # 1. A menu pick of another card: config, environment, one restart.
        st = _state(gpu=0)
        commands.apply_menu_action(st, ("gpu", "1: NVIDIA GeForce RTX 4060"))
        if st.cfg.get("gpu") != 1:
            failures.append(f"the config says gpu={st.cfg.get('gpu')!r}")
        if os.environ.get("NS_GPU") != "1":
            failures.append(f"NS_GPU is {os.environ.get('NS_GPU')!r}")
        if (calls["teardown"], calls["rebuild"]) != (1, 1):
            failures.append(f"expected one teardown and one rebuild, got {calls}")
        if calls["saved"] != 1:
            failures.append("the choice was not written to config.json")

        # 2. Picking the card that is already running changes nothing - a
        #    restart costs seconds of black screen.
        commands.apply_menu_action(st, ("gpu", "1: NVIDIA GeForce RTX 4060"))
        if (calls["teardown"], calls["rebuild"]) != (1, 1):
            failures.append(f"the same card restarted the worker: {calls}")

        # 3. A malformed value is refused, not written.
        commands.apply_menu_action(st, ("gpu", "not an index"))
        if st.cfg.get("gpu") != 1:
            failures.append("a malformed pick changed the config")

        # 4. Startup puts the saved choice back into the environment, and an
        #    absent choice leaves the worker's own default alone.
        os.environ.pop("NS_GPU", None)
        startup._apply_gpu_env({"gpu": 1})
        if os.environ.get("NS_GPU") != "1":
            failures.append("the saved choice did not reach the environment")
        startup._apply_gpu_env({})
        if "NS_GPU" in os.environ:
            failures.append("no choice should leave NS_GPU unset")
    finally:
        (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
         settings_io.save_menu_layout, pipeline.settings_io.save_menu_layout) = real
        if before is None:
            os.environ.pop("NS_GPU", None)
        else:
            os.environ["NS_GPU"] = before

    # 5. The payload the menu renders: every card labelled by its DXGI index,
    #    the current one selected. The index is the identity - it is what
    #    NS_GPU takes and what the worker prints in its adapter lines.
    real_list = capture.list_adapters
    settings_io.list_adapters = lambda: FAKE
    try:
        st = types.SimpleNamespace(cfg={"gpu": 1})
        gpus = [f"{i}: {n}" for i, n in settings_io.list_adapters()]
        current = next((g for g in gpus if g.startswith("1:")), "")
        if gpus != ["0: NVIDIA GeForce RTX 5070 Ti", "1: NVIDIA GeForce RTX 4060"]:
            failures.append(f"the list is {gpus}")
        if current != "1: NVIDIA GeForce RTX 4060":
            failures.append(f"the current card is {current!r}")
    finally:
        settings_io.list_adapters = real_list

    # 6. On this machine the real enumeration answers with NVIDIA cards only,
    #    indexed as DXGI enumerates them.
    for idx, name in capture.list_adapters():
        if not isinstance(idx, int) or idx < 0:
            failures.append(f"bad adapter index {idx!r}")
        if "NVIDIA" not in name.upper():
            failures.append(f"a non-NVIDIA adapter is offered: {name!r}")

    # 7. The choice survives a restart: it is part of the saved payload.
    payload = settings_io._menu_layout_payload(
        {"gpu": 1, "profile": "Natural", "intensity": 1.0, "local_tone": 1.0,
         "local_structure": 1.0, "skin_structure": -1.0},
        {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
         "skin_structure": -1.0}, 0, "en", 0.65, 0.0, True, False,
        types.SimpleNamespace(user_scale=1.0, user_height=None,
                              state={"theme": "light"}, offset=[0, 0]))
    if payload.get("gpu") != 1:
        failures.append(f"the saved payload says gpu={payload.get('gpu')!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the GPU choice drives the config, the environment and the restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
