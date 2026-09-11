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
from capture import devicename_for_output_idx, list_monitors
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
APP_VERSION = "1.5.6"


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
    }




def work_scale_cap(st) -> float:
    """The scale above which the work size just hits the NGX cap.

    Rounded down to the slider's own step so the value is reachable:
    a cap the slider cannot land on exactly would leave the top of the
    range doing nothing, which is the whole thing being fixed here.
    """
    raw = min(1.0, WORK_MAX_W / max(1, st.width), WORK_MAX_H / max(1, st.height))
    return max(0.35, int(raw / 0.05) * 0.05)


def refresh_gpu_ok(st) -> None:
    """Whether NR works - from the worker's answer, not the architecture.

    Only the worker knows for sure: it calls CreateFeature and gets
    the NGX code back. The architecture only tells us what NVIDIA
    promises. Once decided, the answer is not revisited - worker
    restarts add lines but the verdict does not change.
    """
    if st.gpu_ok is not None:
        return
    for line in reversed(st.worker_logs[-80:]):
        if "feature 18 ready" in line:
            st.gpu_ok = True
            return
        # The real refusal line from the worker is "[pure] direct
        # feature 18 create failed"; "Unsupported GPU architecture"
        # lives inside nvngx_dlssnr.dll and never reaches its stderr.
        # SAFE PASSTHROUGH (the worker stays alive and shows the raw
        # frame) is the same verdict: no feature, no NR.
        if "feature 18 create failed" in line or "NR feature unavailable" in line:
            st.gpu_ok = False
            return


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
        "lang": st.lang,
        "recording": st.recorder is not None,
        "work_size": f"{st.work_w}x{st.work_h}",
        "rec_seconds": (st.recorder.duration_ms / 1000.0) if st.recorder else 0.0,
        "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
        "screenshot_dir": st.cfg.get("screenshot_dir") or "",
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
