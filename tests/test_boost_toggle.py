"""Boost: the switch carries the mode, the slider carries only the resolution.

The network is capped at 2560x1440, and in the shipping mode it runs at the
FULL frame size regardless of the work scale - the output frames come back
bit-identical at every position of the resolution slider (measured on four
real 4K frames, 12.09). Boost is what makes the work scale real: the network
runs at the work size and its edit is composited back onto the native 1:1
frame. On a 5070 Ti at 4K that is 16.0 ms against 5.0-7.3 ms, 45.7 -> 72.6 fps.

What this pins, without launching anything:

* the switch asks for the reduced mode and does not touch the work scale -
  a scale reset on every toggle would move the slider under the user's hand
  and lose the resolution they chose;
* turning it back off leaves the scale alone too, so the slider comes back
  where it was;
* a scale above the cap is clamped rather than passed through, whichever way
  it arrives;
* the Num4/Num6 hotkeys walk ONE ladder - every step below the cap is a work
  resolution with Boost on, the step above it is the full frame with Boost
  off. Without that last part the keys would change the number in the alert
  and nothing in the picture whenever Boost happened to be off.
"""
import queue
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import commands  # noqa: E402
import pipeline  # noqa: E402

CAP = 0.65          # a 4K screen: 2560x1440 is reached at 0.65


def _state(small: bool, scale: float):
    return types.SimpleNamespace(
        cfg={"profile": "Natural", "lang": "en"}, lang="en",
        params={"intensity": 1.0}, nr_small=small, work_scale=scale,
        width=3840, height=2160, running=True,
        tray_commands=queue.Queue(),
        display=types.SimpleNamespace(alert=lambda *a, **kw: None))


def main() -> int:
    failures = []
    seen = []
    real_apply = pipeline.request_apply
    commands.pipeline.request_apply = lambda st, scale, profile, params, **kw: \
        seen.append({"scale": round(scale, 2), "small": kw.get("new_small")})
    try:
        # 1. Switching Boost on: the reduced mode is asked for and the work
        #    scale is left exactly as the user set it.
        st = _state(small=False, scale=0.50)
        commands.apply_menu_action(st, ("toggle", "boost"))
        if seen != [{"scale": 0.50, "small": True}]:
            failures.append(f"switching on sent {seen}, expected the reduced "
                            f"mode at the unchanged scale 0.50")
        print(f"    on:  {seen[-1] if seen else None}")

        # 2. And off again: still the same scale, so the slider comes back
        #    where it was left.
        seen.clear()
        st = _state(small=True, scale=0.50)
        commands.apply_menu_action(st, ("toggle", "boost"))
        if seen != [{"scale": 0.50, "small": False}]:
            failures.append(f"switching off sent {seen}, expected the full "
                            f"frame at the unchanged scale 0.50")
        print(f"    off: {seen[-1] if seen else None}")

        # 3. A stored scale above the cap (an old config, or the top step of
        #    the slider as it used to be) must not travel into the feature.
        seen.clear()
        st = _state(small=False, scale=1.0)
        commands.apply_menu_action(st, ("toggle", "boost"))
        if not seen or seen[-1]["scale"] > CAP + 1e-6:
            failures.append(f"a scale above the cap went through: {seen}")

        # 4. The slider itself: always the reduced mode, always clamped.
        seen.clear()
        st = _state(small=True, scale=0.50)
        commands.apply_menu_action(st, ("nr_res", 0.95))
        if not seen or seen[-1] != {"scale": CAP, "small": True}:
            failures.append(f"the slider sent {seen}, expected the cap with "
                            f"the reduced mode on")

        # 5. The hotkeys walk one ladder, off included.
        seen.clear()
        st = _state(small=True, scale=CAP)      # at the top of the reduced range
        st.tray_commands.put("scale_up")
        commands.drain_commands(st)
        if not seen or seen[-1]["small"] is not False:
            failures.append(f"stepping up from the cap sent {seen}, expected "
                            f"the full frame")
        print(f"    hotkey up from the cap: {seen[-1] if seen else None}")

        seen.clear()
        st = _state(small=False, scale=CAP)     # Boost off: one step down
        st.tray_commands.put("scale_down")
        commands.drain_commands(st)
        if not seen or seen[-1]["small"] is not True:
            failures.append(f"stepping down with Boost off sent {seen}, "
                            f"expected the reduced mode")
        elif seen[-1]["scale"] > CAP + 1e-6:
            failures.append(f"stepping down landed above the cap: {seen}")
        print(f"    hotkey down from full: {seen[-1] if seen else None}")
    finally:
        commands.pipeline.request_apply = real_apply

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the switch carries the mode, the scale survives it, the "
          "hotkeys keep one ladder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
