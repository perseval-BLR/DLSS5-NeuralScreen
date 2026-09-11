"""The three silent spots from issue #29 speak up.

A user picked an RTX 2060 Super (Turing: the network cannot be created on it,
and it drives no display). The switch "worked": the feature never came up,
the worker fell into SAFE PASSTHROUGH, the capture quietly fell back to the
display card - and nothing on the screen or in the log said any of it.

Checked here:
* the worker diagnostics that answer "which card runs the network, and did
  it come up" reach the shared log WITHOUT NS_PHASE=1 - and the per-frame
  heartbeat still does not;
* the NR verdict fires ONE alert when the feature is refused, and the alert
  can speak again after a fresh worker clears the verdict;
* a split pipeline (chosen card drives no display, capture stayed behind)
  is surfaced after a deliberate GPU switch - and stays quiet on machines
  where that is the launch state (a hybrid laptop).

Pure logic plus the real OverlayMenu: no worker is launched.
"""
import os
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import channels  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402


class _Display:
    def __init__(self):
        self.alerts = []

    def alert(self, text, duration=2.5):
        self.alerts.append(text)


def _state(**kw):
    st = types.SimpleNamespace(
        lang="en", display=_Display(),
        worker_logs=[], gpu_ok=None, gpu_alerted=False,
        gpu_switch_pending=False,
        cfg={"gpu": 0}, worker=None, reader=None,
        width=1920, height=1080, dda_mode=False, dda_attempted=False,
        gray_active=False, window_hwnd=None,
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def main() -> int:
    failures = []

    # --- 1. The log filter: diagnostics always, the heartbeat never. -------
    os.environ.pop("NS_PHASE", None)
    always = [
        "[host] adapter 2 selected by NS_GPU",
        "[pure] direct feature 18 ready: 1664x936 preset=0 result=0x00000001",
        "[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH",
        "[video] DDA1 OK (2560x1440)",
        "[skip] no new frame - the network is idle until the screen changes",
        "[cap] shared texture 2560x1440 ready",
        "[dda] DuplicateOutput failed 0x887A0004",
    ]
    for line in always:
        if not pipeline._log_wanted(line):
            failures.append(f"a diagnostic line is filtered out: {line!r}")
    quiet = [
        "[video] delivered frame 90 (live)",     # per-30-frame heartbeat
        "[phase] NR | acq 0.0/0.1 | dda 1.9/8.0",
        "[pw] dark=0.05 lit=0.35 exposure=1.00",
    ]
    for line in quiet:
        if pipeline._log_wanted(line):
            failures.append(f"a heartbeat line needs NS_PHASE=1: {line!r}")
    os.environ["NS_PHASE"] = "1"
    try:
        for line in quiet[1:]:
            if not pipeline._log_wanted(line):
                failures.append(f"NS_PHASE=1 hides the profiler: {line!r}")
        if pipeline._log_wanted(quiet[0]):
            failures.append("the frame heartbeat must stay quiet even under NS_PHASE")
    finally:
        os.environ.pop("NS_PHASE", None)

    # --- 2. The NR verdict: red dot now, one alert, speak again after. ----
    st = _state(worker_logs=[
        "[pure] direct feature 18 create failed 0xBAD00001 (NSDK)",
        "[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH",
    ])
    settings_io.refresh_gpu_ok(st)
    if st.gpu_ok is not False:
        failures.append(f"the refusal verdict is {st.gpu_ok!r}, want False")
    if len(st.display.alerts) != 1:
        failures.append(f"the refusal must fire exactly one alert, "
                        f"got {st.display.alerts}")
    settings_io.refresh_gpu_ok(st)  # the verdict is decided - no second alert
    if len(st.display.alerts) != 1:
        failures.append(f"the alert repeated: {st.display.alerts}")
    # A fresh worker clears the verdict AND lets the alert speak again.
    st.gpu_ok = None
    st.gpu_alerted = False
    settings_io.refresh_gpu_ok(st)
    if len(st.display.alerts) != 2:
        failures.append("after a worker reset the alert must speak again")

    # A healthy worker stays silent.
    st2 = _state(worker_logs=[
        "[pure] direct feature 18 ready: 1664x936 preset=0 result=0x00000001"])
    settings_io.refresh_gpu_ok(st2)
    if st2.gpu_ok is not True or st2.display.alerts:
        failures.append(f"a healthy worker: gpu_ok={st2.gpu_ok!r}, "
                        f"alerts={st2.display.alerts}")

    # --- 3. The split pipeline: alert only after a deliberate switch. ------
    # enable_dda fails -> split alert, because a switch is pending.
    calls = {"sent": 0}

    class _Reader:
        def wait_dack(self, timeout):
            raise RuntimeError("the worker could not enable screen capture")

    st3 = _state(gpu_switch_pending=True)
    st3.reader = _Reader()
    st3.worker = types.SimpleNamespace(stdin=types.SimpleNamespace(
        write=lambda b: calls.__setitem__("sent", calls["sent"] + 1),
        flush=lambda: None))
    real_send_dda = channels.send_dda
    real_sync = channels.sync_gray
    channels.send_dda = lambda w, a, b, c=0: None
    channels.sync_gray = lambda s: None
    try:
        channels.enable_dda(st3)
        if st3.dda_mode:
            failures.append("dda_mode must stay False after the refusal")
        if len(st3.display.alerts) != 1 or "display" not in st3.display.alerts[0].lower():
            failures.append(f"the split must be surfaced once, got {st3.display.alerts}")
        if st3.gpu_switch_pending:
            failures.append("the pending flag must clear after the alert")

        # Same failure WITHOUT a pending switch (an Optimus laptop from
        # launch): the fallback is normal there - no alert.
        st4 = _state(gpu_switch_pending=False)
        st4.reader = _Reader()
        st4.worker = st3.worker
        channels.enable_dda(st4)
        if st4.display.alerts:
            failures.append(f"no alert without a deliberate switch: {st4.display.alerts}")

        # A successful enable clears the pending flag silently.
        class _OkReader:
            def wait_dack(self, timeout):
                return None

        st5 = _state(gpu_switch_pending=True)
        st5.reader = _OkReader()
        st5.worker = st3.worker
        channels.enable_dda(st5)
        if not st5.dda_mode:
            failures.append("a successful DDA must set dda_mode")
        if st5.gpu_switch_pending or st5.display.alerts:
            failures.append(f"success must be silent: pending={st5.gpu_switch_pending}, "
                            f"alerts={st5.display.alerts}")
    finally:
        channels.send_dda = real_send_dda
        channels.sync_gray = real_sync

    # --- 4. The menu: the GPU row carries the hint, the hint is not a hit. -
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    pygame.init()
    try:
        from overlay_ui import OverlayMenu

        menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
        menu.set_state({
            "gpus": ["0: RTX 5070 Ti", "1: RTX 2060 Super"],
            "gpu": "1: RTX 2060 Super",
        })
        menu.page = "settings"
        menu.visible = True
        surf = pygame.Surface((3840, 2160))
        menu.layout(3840, 2160)
        menu.draw(surf)
        row = next((i for i in menu.items
                    if i.kind == "choice" and i.key == "gpu"), None)
        if row is None:
            failures.append("no GPU row with two cards on the settings page")
        else:
            if not row.extra.get("hint"):
                failures.append("the GPU row has no hint")
            strip = row.extra.get("strip")
            pos = (row.rect.centerx, row.rect.bottom - 2)
            hit = menu.hit(pos)
            if strip is not None and pos[1] > strip.bottom and hit is row:
                failures.append("a click on the GPU hint must not open the list")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the silent spots speak - log, alert, split warning, GPU hint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
