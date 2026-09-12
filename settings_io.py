"""What the menu shows, and what the config keeps.

Two directions of one subject. Out: the payload the overlay menu renders -
profiles, presets, sliders, monitors, hotkey captions, the GPU line and
whether neural rendering is really running on it. In: the two things a user
changes through the menu that have to survive a restart - the menu's own
position and size, and the hotkey assignments.

Both go into config.json through the atomic writer rather than over the live
file: a crash mid-write used to truncate the config and lose every setting.
"""
from __future__ import annotations

import json
import os
import sys
import winreg
from pathlib import Path

from paths import BASE_DIR
from capture import devicename_for_output_idx, list_adapters, list_monitors
from i18n import STRINGS as UI_STRINGS
# The work caps are the worker's contract, not a setting: the same two
# numbers size the shared motion buffer in the SHMI handshake.
from protocol import WORK_MAX_H, WORK_MAX_W  # noqa: F401
from winapi import list_capturable_windows


def _work_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """The NGX work resolution: scale of full, but no larger than
    WORK_MAX_W/H (NGX goes silent at 4K - the limit verified in isolation).

    Two rules that come from being bitten:

    At 1:1 the answer is the frame itself, with no rounding. Rounding to the
    nearest even number turned a 539-pixel-high window (900x500 plus its title
    bar) into a 540-high work size - larger than the frame - and the worker
    died on the header. An odd size at 1:1 stays on the legacy path, which is
    known to work (test_odd_frame_size).

    And a downscale rounds DOWN, never up: the work resolution must never
    exceed the frame it came from.
    """
    if scale >= 1.0:
        w, h = int(width), int(height)
    else:
        w = max(64, int(width * scale) // 2 * 2)
        h = max(64, int(height * scale) // 2 * 2)
    if w > WORK_MAX_W or h > WORK_MAX_H:
        k = min(WORK_MAX_W / w, WORK_MAX_H / h)
        w = max(64, int(w * k) // 2 * 2)
        h = max(64, int(h * k) // 2 * 2)
    return min(w, int(width)), min(h, int(height))


def hotkey_labels(bindings: dict) -> dict:
    """Bindings -> {command: "Num1"} for the captions on the menu buttons."""
    return {cmd: name for _mods, _vk, cmd, name in bindings.values()}

from i18n import STRINGS as UI_STRINGS


# The project page: README, hotkeys, requirements. Opened from the menu.
REPO_URL = "https://github.com/perseval-BLR/DLSS5-NeuralScreen"


CHANNEL_URL = "https://www.youtube.com/@perseval_BLR/videos"


PRESET_NAME_PREFIX = "Preset"


# The global hotkeys live in hotkeys.py (RegisterHotKey). The layout and the
# reasons behind the combinations are in that module's docstring.
WORK_SCALE_STEP = 0.05


WORK_SCALE_MAX = 1.0


def _next_preset_name(presets: dict) -> str:
    """The first free "Preset N" name (Preset 1, Preset 2, ...)."""
    n = 1
    while f"{PRESET_NAME_PREFIX} {n}" in presets:
        n += 1
    return f"{PRESET_NAME_PREFIX} {n}"


def _set_autostart(enabled: bool) -> bool:
    """Enable/disable autostart with Windows (HKCU Run).

    We launch NeuralScreen.vbs through wscript - a hidden launcher with no
    console. Returns True on success.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            vbs = str(BASE_DIR / "NeuralScreen.vbs")
            winreg.SetValueEx(key, "NeuralScreen", 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        else:
            try:
                winreg.DeleteValue(key, "NeuralScreen")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"[main] autostart not configured: {exc}", file=sys.stderr)
        return False


# The version shown in the menu header. Kept in sync with native/launcher.rc
# (FileVersion/ProductVersion) and build_release_zip.py at release time.
APP_VERSION = "1.7.0"


# The channel label: the header shows the version, the channel lives in the
# settings page (user rule 2026-09-08).
CHANNEL_LABEL = "@perseval_BLR"


# --- DLSS 5 NR profiles (field order as in the converter) -----------------
PROFILES = {
    "Faithful": dict(profile=0, preset=0, style=0, auto_mask=0, ui_correction=0,
                     intensity=0.70, local_tone=0.75, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(profile=1, preset=0, style=1, auto_mask=0, ui_correction=0,
                    intensity=1.00, local_tone=1.00, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                               intensity=1.65, local_tone=1.40, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                                intensity=2.50, local_tone=2.00, local_structure=2.00, skin_structure=1.5),
}


WORK_SCALE_MIN = 0.1


PARAM_MIN, PARAM_MAX = 0.0, 2.5


# The four sliders a user preset stores. The same keys as PROFILES carries,
# minus the NGX plumbing (profile/preset/style/auto_mask/ui_correction stay
# tied to the built-in profile the preset was saved from).
PRESET_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")


SKIN_MIN = -1.0


DEFAULT_LANG = "en"


def _valid_preset_value(key: str, value) -> bool:
    """A preset value is a finite number inside the slider range.

    The config is user-editable: a hand-typed "intensity": "abc" or 99.0
    must not crash the program - the preset is dropped instead (the
    built-in profiles always survive).
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    lo = SKIN_MIN if key == "skin_structure" else PARAM_MIN
    return lo <= value <= PARAM_MAX


# The NGX plumbing fields a preset carries along with the four sliders.
# They are integers with a small, known range (the same values PROFILES
# uses); anything outside is a broken entry.
_PRESET_INT_KEYS = {
    "profile": (0, 2), "preset": (0, 2), "style": (0, 2),
    "auto_mask": (0, 1), "ui_correction": (0, 1),
}


def load_presets(cfg: dict) -> dict:
    """The user presets from the config, validated.

    A preset is a full params snapshot: the four sliders plus the NGX
    plumbing (profile/preset/style/auto_mask/ui_correction), so applying
    it reproduces the exact look it was saved with. Anything that is not
    exactly that shape is dropped - a broken entry must not take the
    program down, and a broken entry must not be offered in the menu
    either.
    """
    raw = cfg.get("presets")
    if not isinstance(raw, dict):
        return {}
    presets: dict = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(values, dict):
            continue
        clean = {}
        ok = True
        for key in PRESET_KEYS:
            if key not in values or not _valid_preset_value(key, values[key]):
                ok = False
                break
            clean[key] = float(values[key])
        if not ok:
            continue
        for key, (lo, hi) in _PRESET_INT_KEYS.items():
            v = values.get(key)
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                ok = False
                break
            clean[key] = v
        if ok:
            presets[name.strip()] = clean
    return presets


def load_config(path: Path) -> dict:
    """Load and validate config.json."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    required = {"monitor", "width", "height", "fullscreen", "warmup", "profile",
                "intensity", "local_tone", "local_structure", "skin_structure"}
    missing = required - set(cfg)
    if missing:
        raise ValueError(f"config.json: missing fields: {sorted(missing)}")
    if cfg["profile"] not in PROFILES:
        # A user preset name, or a stale reference to a deleted preset.
        # A stale reference must not take the program down - fall back to
        # the default profile (the menu still lists the surviving presets).
        if cfg["profile"] not in load_presets(cfg):
            print(f"[main] config.json: unknown profile {cfg['profile']!r}; "
                  f"falling back to 'Natural'", file=sys.stderr)
            cfg["profile"] = "Natural"
    for key in ("width", "height", "warmup"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"config.json: field {key} must be a positive integer")
    # work_scale: 0.25..1.0 - the NGX processing resolution relative to the output
    scale = float(cfg.get("work_scale", 1.0))
    cfg["work_scale"] = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, scale))
    # lang: the language of the HUD/alerts/menu (en/ru, DEFAULT_LANG by default)
    lang = str(cfg.get("lang", DEFAULT_LANG))
    if lang not in UI_STRINGS:
        lang = DEFAULT_LANG
    cfg["lang"] = lang
    return cfg


def resolve_params(cfg: dict) -> dict:
    """Profile + custom NR parameters from the config (null = use the profile).

    A user preset is a full params snapshot and wins over the built-in
    profile it was saved from; the per-key overrides below still apply on
    top (they are the live slider values).
    """
    if cfg["profile"] in PROFILES:
        params = dict(PROFILES[cfg["profile"]])
    else:
        params = dict(load_presets(cfg).get(cfg["profile"], PROFILES["Natural"]))
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        value = cfg.get(key)
        if value is not None:
            params[key] = float(value)
    return params


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write data to path atomically: a temp file in the same directory,
    flushed and fsynced, then os.replace() over the target.

    A crash mid-write used to truncate config.json in place and the program
    lost the user's settings. The temp file lives next to the target so the
    replace is a rename within one volume - atomic on Windows. On failure the
    temp file is removed and the original is left untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _autostart_enabled() -> bool:
    """Is autostart currently on? (HKCU Run, the NeuralScreen value)."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "NeuralScreen")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _menu_layout_payload(cfg: dict, params: dict, monitor: int, lang: str,
                         work_scale: float, split_pos: float,
                         startup_menu: bool, nr_small: bool, menu) -> dict:
    """The settings _save_menu_layout persists into config.json.

    Everything the user can change in the menu: the panel geometry, the
    processing settings and the NR parameters. profile/params/monitor are
    included because the menu changes them in memory only (cfg/params are
    updated live) - without this save they would be lost on the next launch.

    monitor is saved as the DXGI devicename (e.g. '\\\\.\\DISPLAY1') so the
    saved monitor keeps pointing at the same physical display when the
    arrangement changes; old configs with a positional int still load.
    """
    monitor_name = devicename_for_output_idx(int(monitor))
    return {
        "menu_scale": round(menu.user_scale, 2),
        "menu_height": (None if menu.user_height is None
                        else int(menu.user_height)),
        "open_menu_on_start": startup_menu,
        "split": round(split_pos, 2),
        "nr_small": bool(nr_small),
        "work_scale": round(work_scale, 2),
        "theme": menu.state.get("theme", "light"),
        "lang": lang,
        "menu_offset": [int(menu.offset[0]), int(menu.offset[1])],
        "profile": cfg["profile"],
        "intensity": params["intensity"],
        "local_tone": params["local_tone"],
        "local_structure": params["local_structure"],
        "skin_structure": params["skin_structure"],
        "monitor": monitor_name if monitor_name is not None else int(monitor),
        "rec_indicator": bool(cfg.get("rec_indicator", True)),
        "screenshot_dir": cfg.get("screenshot_dir") or "",
        # The Spout2 bridge choice must survive a restart: the worker
        # reads NS_SPOUT at startup, and main sets it from this flag.
        "spout": bool(cfg.get("spout", False)),
        # Which card runs the network and the capture. An index, as
        # DXGI enumerates adapters - the same number the worker takes
        # in NS_GPU and prints in its "[host] adapter N" lines.
        "gpu": int(cfg.get("gpu", 0)),
        # Skip static frames: no new frame from the capture - the network
        # idles instead of re-running on the same picture. A per-frame flag,
        # so it survives a restart through the config alone.
        "skip_static": bool(cfg.get("skip_static", True)),
        # The user's saved presets. Without this key "Save preset" wrote
        # everything EXCEPT the preset: the menu said "Preset saved", the
        # save really did succeed, and the preset was gone on the next
        # launch - the code's own "it will not survive a restart" branch
        # could never fire, because nothing had failed (audit F1).
        "presets": dict(cfg.get("presets") or {}),
    }




def work_scale_cap(st) -> float:
    """The scale above which the work size just hits the NGX cap.

    Rounded down to the slider's own step so the value is reachable:
    a cap the slider cannot land on exactly would leave the top of the
    range doing nothing, which is the whole thing being fixed here.
    """
    raw = min(1.0, WORK_MAX_W / max(1, st.width), WORK_MAX_H / max(1, st.height))
    return max(0.35, int(raw / 0.05) * 0.05)


#: What the worker says about the neural pass, in its own words. ONE set,
#: read by both places that ask: settings_io.refresh_gpu_ok (which drives
#: the dot in the menu and the alert) and pipeline.gpu_came_up (which
#: decides whether a GPU switch is kept or reverted). They used to carry a
#: token list each, and the lists had already drifted apart - a rename in
#: the worker would have blinded one of them and left the other working,
#: which is the worst shape for a bug like this to take.
NR_VERDICT_OK = ("feature 18 ready",)
NR_VERDICT_FAIL = ("feature 18 create failed",   # [pure], the direct refusal
                   "NR feature unavailable",     # [video], SAFE PASSTHROUGH
                   "NGX unavailable",            # [host], nothing came up
                   "no NVIDIA adapter found")    # [host], nothing to run on


def nr_verdict(lines):
    """True / False / None from the worker's log lines, NEWEST FIRST.

    None means the worker has not said yet - which is not a failure. A card
    that takes its time still works, and treating silence as a refusal
    would be worse than the bug the callers guard against.
    """
    for line in lines:
        if any(token in line for token in NR_VERDICT_OK):
            return True
        if any(token in line for token in NR_VERDICT_FAIL):
            return False
    return None


def refresh_gpu_ok(st) -> None:
    """Whether NR works - from the worker's answer, not the architecture.

    Only the worker knows for sure: it calls CreateFeature and gets
    the NGX code back. The architecture only tells us what NVIDIA
    promises. Once decided, the answer is not revisited - worker
    restarts add lines but the verdict does not change.
    """
    if st.gpu_ok is not None:
        return
    # The refusal lines are named once, in nr_verdict: the real one from the
    # worker is "[pure] direct feature 18 create failed" ("Unsupported GPU
    # architecture" lives inside nvngx_dlssnr.dll and never reaches its
    # stderr), and SAFE PASSTHROUGH is the same verdict - the worker stays
    # alive and shows the raw frame, so: no feature, no NR.
    verdict = nr_verdict(reversed(st.worker_logs[-80:]))
    if verdict is None:
        return
    st.gpu_ok = verdict
    if not verdict:
        # The red dot alone was not enough: in issue #29 the user picked a
        # card that cannot run the pass and nothing on screen said so. One
        # alert per verdict - a fresh worker clears gpu_ok and the alert can
        # speak again.
        if not st.gpu_alerted:
            st.gpu_alerted = True
            st.display.alert(UI_STRINGS[st.lang].get(
                "gpu_nr_fail",
                "This GPU cannot run the neural pass - the picture stays unprocessed"))


def warn_hdr(st) -> None:
    """Say once that the captured display is in HDR.

    The worker asks the OUTPUT it duplicates for its colour space, so this
    is about the screen being processed rather than about some monitor in
    the registry. The network is trained on SDR and an HDR desktop comes
    out looking blown out with sliders that appear to do nothing - a report
    we have had (issue #27) and a notice a user asked for (issue #33).
    """
    if st.hdr_alerted:
        return
    for line in reversed(st.worker_logs[-200:]):
        if "HDR IS ON for the captured display" in line:
            st.hdr_alerted = True
            st.display.alert(UI_STRINGS[st.lang].get(
                "hdr_on",
                "HDR is on for this display - the picture will look wrong. "
                "Turn HDR off for it."), duration=6.0)
            print("[main] HDR is on for the captured display - the network "
                  "is trained on SDR")
            return


def _gpu_label(index) -> str:
    """"<dxgi index>: <name>" for the picker - the card that will really run.

    The value in the config is a DXGI index and it can name something that
    is not an NVIDIA card (a hybrid laptop's integrated GPU sits at 0, which
    is the shipped default) or nothing at all. The worker treats the index
    as a wish and falls back to the first usable card; the menu has to agree
    with it, or the picker shows an empty field on the machines where the
    setting matters most (issue #34).
    """
    adapters = list_adapters()
    if not adapters:
        return ""
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        wanted = None
    for i, name in adapters:
        if i == wanted:
            return f"{i}: {name}"
    i, name = adapters[0]
    return f"{i}: {name}"


def _worker_idle(st) -> bool:
    """Is the network idling on an unchanged screen right now?

    The worker announces a stretch ONCE - "[skip] no new frame" - and
    announces its end when the screen moves again. So the state is the
    LATEST of those two markers, not the presence of the first one in the
    last few lines: the menu used to look at worker_logs[-3:], and one
    unrelated line was enough to push the single announcement out and turn
    the readout back into a frame rate while nothing was being processed
    (audit).

    The window is bounded because this runs on every frame the menu is open:
    during a stretch the worker is otherwise quiet, so 200 lines is a long
    way past the marker, and a scan of the whole 2000-line buffer sixty
    times a second is not worth the difference.
    """
    for line in reversed(st.worker_logs[-200:]):
        if "[skip] no new frame" in line:
            return True
        if "[skip] the screen changed" in line:
            return False
    return False


def menu_payload(st) -> dict:
    """The current state for the menu - a single source of truth."""
    refresh_gpu_ok(st)
    wins = list_capturable_windows()
    # The devicename is the stable identity: the menu hands it back
    # on a switch, so a reorder cannot redirect the capture.
    monitor_entries = [f"{i}: {w}x{h} ({dev})"
                       for i, w, h, dev in list_monitors()]
    return {
        "nr": not st.paused,
        "work_scale": st.work_scale,
        # Where the work size hits the 2560x1440 cap. Everything above
        # it lands on the same resolution, so the slider puts "the whole
        # screen" there instead of a dead stretch.
        "work_scale_cap": work_scale_cap(st),
        "work_scale_min": WORK_SCALE_MIN,
        "nr_small": st.nr_small,
        "screen_size": f"{st.width}x{st.height}",
        "profile": st.cfg["profile"],
        "profiles": list(PROFILES) + list(st.presets),
        "preset_active": st.cfg["profile"] in st.presets,
        "params": {k: st.params[k] for k in
                   ("intensity", "local_tone",
                    "local_structure", "skin_structure")},
        # What the CURRENT profile puts each parameter at. The menu draws it
        # as a tick under the slider, so "how far have I moved this from
        # Natural" is visible instead of remembered.
        "param_defaults": {
            k: float(v) for k, v in
            (PROFILES.get(st.cfg["profile"])
             or st.presets.get(st.cfg["profile"]) or {}).items()
            if k in ("intensity", "local_tone", "local_structure",
                     "skin_structure")},
        "lang": st.lang,
        "recording": st.recorder is not None,
        "work_size": f"{st.work_w}x{st.work_h}",
        "rec_seconds": (st.recorder.duration_ms / 1000.0) if st.recorder else 0.0,
        "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
        "screenshot_dir": st.cfg.get("screenshot_dir") or "",
        "spout": bool(st.cfg.get("spout", False)),
        "skip_static": bool(st.cfg.get("skip_static", True)),
        # Is the network idling on an unchanged screen right now? The
        # worker says so in its log; without this the menu shows a
        # healthy FPS while nothing is being processed, and the skip
        # reads as "it does not work" (user, 12.09).
        "idle": _worker_idle(st),
        "gpus": [f"{i}: {name}" for i, name in list_adapters()],
        # The saved index may name no NVIDIA card at all. On a hybrid laptop
        # adapter 0 is the integrated GPU and "gpu": 0 is what the program
        # ships with, so the picker came up EMPTY on exactly the machines
        # where the setting matters most (issue #34). The worker already
        # falls back to the first usable card in that case - the menu says
        # the same thing now instead of showing a blank.
        "gpu": _gpu_label(st.cfg.get("gpu")),
        "open_on_start": st.startup_menu,
        "autostart": _autostart_enabled(),
        "split": st.split_pos,
        "gpu_text": st.gpu_text,
        "gpu_ok": st.gpu_ok,
        "window_mode": st.window_hwnd is not None,
        "monitor_devicename": st.capture.devicename,
        "monitors": monitor_entries,
        "monitor": next(
            (m for m in monitor_entries
             if m.startswith(f"{st.monitor}: ")),
            str(st.monitor)),
        "windows": [f"{h:X}: {t}" for h, t in wins],
        "window_current": next(
            (f"{h:X}: {t}" for h, t in wins if h == st.window_hwnd), ""),
        "version": APP_VERSION,
        "channel": CHANNEL_LABEL,
    }


def save_menu_layout(st) -> bool:
    """Remember the panel size and position in config.json.

    We write on menu close and on exit rather than on every mouse
    move: dragging would otherwise hammer the file dozens of times
    per second. Returns False when the write failed - the callers
    that promise the user something (presets, hotkeys) show an
    alert then.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8"))
        data.update(_menu_layout_payload(
            st.cfg, st.params, st.monitor, st.lang, st.work_scale, st.split_pos,
            st.startup_menu, st.nr_small, st.display.menu))
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the menu layout: {exc}", file=sys.stderr)
        return False


def save_hotkeys(st, mapping: dict) -> bool:
    """Write the assignments into config.json.

    Separate from _save_menu_layout: that one runs on menu close,
    while the user expects a key to be saved right away. Returns
    False when the write failed - the caller shows an alert.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8"))
        data["hotkeys"] = dict(mapping)
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the hotkeys: {exc}",
              file=sys.stderr)
        return False
