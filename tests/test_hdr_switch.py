"""HDR compatibility is a switch, it is off, and it says so in 12 languages.

The HDR path (PR #36) changes the capture format, the swap chain format and
the colour space of the presentation. Every one of those is a way for the
picture to disappear on hardware nobody here owns, so the mode is
experimental and opt-in: SETTINGS -> CAPTURE, off unless asked for.

The hand-off is the one the Spout bridge already uses, for the same reason -
HdrEnabled() is read once per worker process and everything downstream is
decided then, so the switch is a worker restart and not a protocol message.

What this pins, without launching anything:

* the default is off in both payloads, and the WORKER's own default is off
  too - a worker started by hand, or by a test, keeps the SDR behaviour;
* the flag reaches the worker as NS_HDR, and bring_up sets it before the
  first worker starts;
* the menu action goes through pipeline.apply_hdr: config, environment,
  save, teardown, rebuild - and the HDR notice is armed again, because the
  new worker has not said anything about its capture yet;
* the toggle is on the settings page and NOT on the main page;
* every language has all four strings, and every one of them FITS - a
  hint is clipped, not wrapped, and three of these labels are long.

Run:  runtime\\python.exe tests\\test_hdr_switch.py
"""
import os
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402
import startup  # noqa: E402
from i18n import STRINGS  # noqa: E402

KEYS = ("hdr_mode", "hdr_mode_hint", "hdr_mode_on", "hdr_mode_off")


class _Capture:
    devicename = r"\\.\DISPLAY1"
    resolution = (1920, 1080)


def _state(hdr=False):
    return types.SimpleNamespace(
        paused=False, work_scale=0.65, nr_small=False, width=1920, height=1080,
        cfg={"profile": "Natural", "spout": False, "hdr": hdr,
             "rec_indicator": True, "screenshot_dir": "", "gpu": 0},
        params={"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
                "skin_structure": -1.0},
        presets={}, lang="en", recorder=None, work_w=1248, work_h=702,
        startup_menu=True, split_pos=0.0, gpu_text="RTX 5080", gpu_ok=True,
        window_hwnd=None, capture=_Capture(), monitor=0, worker_logs=[],
        hdr_alerted=True,
        display=types.SimpleNamespace(alert=lambda *a, **kw: None))


def main() -> int:
    failures = []

    # 1. Off by default, on both roads out of the config.
    payload = settings_io.menu_payload(_state())
    if payload.get("hdr") is not False:
        failures.append(f"menu_payload says hdr={payload.get('hdr')!r} "
                        f"for a config that never asked for it")
    layout = settings_io._menu_layout_payload(
        {"profile": "Natural"},
        {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
             "skin_structure": -1.0}, 0, "en", 0.65, 0.0, True, False,
        types.SimpleNamespace(user_scale=1.0, user_height=None, offset=(0, 0),
                              state={"theme": "light"}))
    if layout.get("hdr") is not False:
        failures.append(f"the saved layout says hdr={layout.get('hdr')!r} "
                        f"for an empty config")

    # 2. The worker's own default. HdrEnabled() is what every HDR decision
    #    in the worker hangs off, and it must need a "1" - not merely "not
    #    a 0". test_wgc_capture and the other fixtures run a bare worker.
    src = (BASE / "native" / "hdr_display.h").read_text(encoding="utf-8")
    if 'strcmp(value, "1") == 0' not in src:
        failures.append("the worker's HdrEnabled() no longer defaults to off "
                        "- a worker started without NS_HDR would change format")

    # 3. The environment hand-off, both ways, and from bring_up.
    for flag, want in ((True, "1"), (False, "0"), (None, "0")):
        os.environ["NS_HDR"] = "x"
        startup._apply_hdr_env({} if flag is None else {"hdr": flag})
        if os.environ.get("NS_HDR") != want:
            failures.append(f"hdr={flag!r} put NS_HDR="
                            f"{os.environ.get('NS_HDR')!r}, expected {want!r}")
    if "_apply_hdr_env(st.cfg)" not in (BASE / "startup.py").read_text(encoding="utf-8"):
        failures.append("bring_up never applies the HDR flag - the first "
                        "worker of the session would inherit a stale NS_HDR")

    # 4. The menu action. The whole path is pipeline.apply_hdr: it owns the
    #    config write, the environment, the save and the restart.
    seen = []
    real = pipeline.apply_hdr
    commands.pipeline.apply_hdr = lambda st, enabled: seen.append(enabled)
    try:
        commands.apply_menu_action(_state(hdr=False), ("toggle", "hdr"))
        commands.apply_menu_action(_state(hdr=True), ("toggle", "hdr"))
    finally:
        commands.pipeline.apply_hdr = real
    if seen != [True, False]:
        failures.append(f"the toggle asked for {seen}, expected [True, False]")

    # 5. And what apply_hdr actually does. Everything it touches is stubbed
    #    except the flag itself - this is about the order and the fallout,
    #    not about restarting a worker for real.
    st = _state(hdr=False)
    st.hdr_alerted = True
    steps = []
    saved = (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
             pipeline.settings_io.save_menu_layout)
    pipeline.teardown_pipeline = lambda s: steps.append("teardown")
    pipeline.rebuild_pipeline = lambda s, msg=None: steps.append(f"rebuild:{msg}")
    pipeline.settings_io.save_menu_layout = lambda s: steps.append("save")
    os.environ["NS_HDR"] = "0"
    try:
        pipeline.apply_hdr(st, True)
    finally:
        (pipeline.teardown_pipeline, pipeline.rebuild_pipeline,
         pipeline.settings_io.save_menu_layout) = saved
    if st.cfg.get("hdr") is not True:
        failures.append("apply_hdr did not write the flag into the config")
    if os.environ.get("NS_HDR") != "1":
        failures.append(f"apply_hdr left NS_HDR={os.environ.get('NS_HDR')!r}")
    if [s.split(":")[0] for s in steps] != ["save", "teardown", "rebuild"]:
        failures.append(f"apply_hdr did {steps}, expected save, teardown, rebuild")
    if not any(s.startswith("rebuild:") and STRINGS["en"]["hdr_mode_on"] in str(s)
               for s in steps):
        failures.append(f"the restart said {steps[-1:]}, not the HDR toast")
    if st.hdr_alerted is not False:
        failures.append("the HDR notice stayed armed-down across the restart - "
                        "the new worker's capture would never be reported")
    os.environ["NS_HDR"] = "0"

    # 6. The control itself: on the settings page, not on the main one, and
    #    a click on it asks for the toggle.
    import pygame  # noqa: E402
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts  # noqa: E402
    from overlay_ui import OverlayMenu  # noqa: E402

    lang_failures = []
    for lang in STRINGS:
        table = STRINGS[lang]
        missing = [k for k in KEYS if not table.get(k)]
        if missing:
            lang_failures.append(f"{lang}: missing {missing}")
            continue
        menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False, L=lang:
                           fonts.load(size, mono=mono, bold=bold, lang=L))
        menu.lang = lang
        menu.set_state({"lang": lang, "hdr": False, "spout": False,
                        "rec_indicator": True, "theme": "light"})
        menu.visible = True
        menu.page = "main"
        menu.layout(3840, 2160)
        if any(i.kind == "toggle" and i.key == "hdr" for i in menu.items):
            lang_failures.append(f"{lang}: the HDR switch is on the MAIN page")
        menu.page = "settings"
        menu.layout(3840, 2160)
        row = next((i for i in menu.items
                    if i.kind == "toggle" and i.key == "hdr"), None)
        if row is None:
            lang_failures.append(f"{lang}: no HDR switch on the settings page")
            continue
        if lang == "en":
            pos = (row.rect.x + row.rect.w // 2, row.rect.y + 2)
            out = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONUP, {"pos": pos, "button": 1}))
            if out != [("toggle", "hdr")]:
                failures.append(f"clicking the switch emitted {out}")
        # It fits. _clip truncates a label and every hint line at the row
        # width - silently - and these strings are the longest in the
        # section in half of the twelve languages.
        room = row.rect.w - int(menu._u(20) * 1.8) - menu._u(12)
        if menu._font.size(table["hdr_mode"])[0] > room:
            lang_failures.append(
                f"{lang}: the label is clipped "
                f"({menu._font.size(table['hdr_mode'])[0]} > {room} px)")
        # The hint under it is measured by test_settings_hints, together
        # with every other hint on both pages - one line, and it fits.
    failures.extend(lang_failures)
    print(f"    languages checked: {len(STRINGS)}, "
          f"problems: {len(lang_failures)}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: HDR compatibility is off by default, switchable, and fits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
