"""A card the network cannot use must not become the saved card.

Issue #33: a machine listed the same RTX 5080 twice - DXGI does that - and
the second entry could not initialise NGX. The worker died three times in a
row, NR went off, the overlay hid, and the menu went with it. The choice was
already in config.json, so the next launch came up on the same dead card
with no way back except editing the file by hand.

So the switch proves itself before it is saved: the worker's own verdict
("feature 18 ready" against "create failed" / "NGX unavailable" / a dead
process) decides, and a card that fails hands the pipeline back to the one
that worked. Silence is not failure - a slow card keeps the switch.
"""
import os
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pipeline  # noqa: E402


class _Worker:
    """A worker process that is alive, or exited with a code."""

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def _state(gpu=0, logs=None, worker=None):
    alerts = []
    return types.SimpleNamespace(
        cfg={"gpu": gpu}, lang="en", worker_logs=list(logs or []),
        worker=worker if worker is not None else _Worker(),
        gpu_switch_pending=False, alerts=alerts,
        display=types.SimpleNamespace(alert=alerts.append))


def _drive(st, index, logs_after_rebuild):
    """apply_gpu with the rebuild stubbed out; the stub feeds the verdict."""
    saved = (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
             pipeline.settings_io.save_menu_layout)
    rebuilds = []
    writes = []

    def rebuild(state, note):
        rebuilds.append(int(state.cfg.get("gpu", -1)))
        # The first rebuild is the new card: give it the verdict under test.
        # Anything after that is the revert, and that one always comes up.
        state.worker_logs = list(
            logs_after_rebuild if len(rebuilds) == 1 else ["[pure] feature 18 ready"])

    pipeline.teardown_pipeline = lambda state: None
    pipeline.rebuild_pipeline = rebuild
    pipeline.settings_io.save_menu_layout = lambda state: writes.append(
        int(state.cfg.get("gpu", -1))) or True
    try:
        pipeline.apply_gpu(st, index)
    finally:
        (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
         pipeline.settings_io.save_menu_layout) = saved
    return rebuilds, writes


def main() -> int:
    failures = []
    before = os.environ.get("NS_GPU")
    try:
        # 1. The card works: it is kept, saved once, and nothing is alerted.
        st = _state(gpu=0)
        rebuilds, writes = _drive(st, 1, ["[pure] feature 18 ready"])
        if st.cfg["gpu"] != 1 or os.environ.get("NS_GPU") != "1":
            failures.append(f"a working card was not kept: {st.cfg['gpu']}")
        if writes != [1]:
            failures.append(f"expected one save of card 1, got {writes}")
        if st.alerts:
            failures.append(f"a working switch alerted: {st.alerts}")

        # 2. The network refuses on the new card: the old one comes back, the
        #    config is NOT written, and the user is told.
        st = _state(gpu=0)
        rebuilds, writes = _drive(
            st, 2, ["[pure] direct feature 18 create failed 0xBAD00001"])
        if st.cfg["gpu"] != 0 or os.environ.get("NS_GPU") != "0":
            failures.append(f"a dead card stuck: cfg={st.cfg['gpu']}, "
                            f"env={os.environ.get('NS_GPU')}")
        if writes:
            failures.append(f"a dead card was written to the config: {writes}")
        if rebuilds != [2, 0]:
            failures.append(f"expected a rebuild on 2 then back on 0: {rebuilds}")
        if not st.alerts:
            failures.append("the revert said nothing to the user")

        # 3. NGX never initialised at all - same outcome.
        st = _state(gpu=0)
        _drive(st, 3, ["[host] NVSDK_NGX_D3D12_Init -> 0xBAD00001",
                       "[host] NGX unavailable"])
        if st.cfg["gpu"] != 0:
            failures.append("an adapter without NGX stuck")

        # 4. The worker process died: the verdict needs no log line.
        st = _state(gpu=0, worker=_Worker(code=1))
        _drive(st, 4, [])
        if st.cfg["gpu"] != 0:
            failures.append("a dead worker did not revert the card")

        # 5. Silence is not failure: a card that says nothing in time keeps
        #    the switch, because a slow start is not a broken card.
        st = _state(gpu=0)
        rebuilds, writes = _drive(st, 5, [])
        if st.cfg["gpu"] != 5 or writes != [5]:
            failures.append(f"a silent-but-alive card was reverted: "
                            f"cfg={st.cfg['gpu']}, writes={writes}")

        # 6. Picking the card already in use does nothing at all.
        st = _state(gpu=1)
        rebuilds, writes = _drive(st, 1, ["[pure] feature 18 ready"])
        if rebuilds or writes:
            failures.append(f"re-picking the same card rebuilt: {rebuilds}")
    finally:
        if before is None:
            os.environ.pop("NS_GPU", None)
        else:
            os.environ["NS_GPU"] = before

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a card that cannot run the network reverts and is never saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
