"""DLSS 5 Desktop NR - the integration skeleton of the prototype.

The loop: desktop capture (capture.ScreenCapture) -> motion guides
(guides.TemporalGuideGenerator) -> the NGX worker (native/nvngx.dll in
--live mode) -> fullscreen output (display.Display).

Controls (global hotkeys, RegisterHotKey + a polling fallback - see
hotkeys.py). Num Lock must be on: the numpad sends Insert/End/arrows
without it:
    Num1          - NR on/off
    Num2          - the settings menu
    Num3          - screenshot
    Num0          - recording
    Num4 / Num6   - processing resolution down / up
    Num5          - process one window instead of the whole screen
    Ctrl+Alt+Q    - quit (the same as "Exit" in the tray)

Every key can be reassigned in the settings menu (config.json
"hotkeys").

Run:
    python main.py [--config config.json]
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# --- Log to a file instead of the console --------------------------------
# The release is launched through pythonw.exe (no console window): stdout and
# stderr are None there and any print would fail. We redirect them into
# NeuralScreen.log next to main.py - every print keeps working and the user
# reads the log as a file rather than a window. Startup errors (a missing DLL
# and the like) are additionally shown in a message box (see the bottom of
# this file).
LOG_PATH = Path(__file__).resolve().parent / "NeuralScreen.log"


def _init_logging() -> None:
    """Redirect stdout/stderr into NeuralScreen.log (utf-8)."""
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception:
        pass  # it did not work - the prints just vanish, we do not crash


def _apply_nr_dll(cfg: dict) -> None:
    """The swappable runtime: a configured nr_dll reaches the worker.

    The worker loads nvngx_dlssnr.dll by name; NS_NR_DLL lets a different
    build be loaded without rebuilding the worker (the RHI
    dlss_manifest.json pattern). The path is put into the environment,
    which subprocess inherits. Without the flag the bundled DLL stays the
    default.
    """
    if cfg.get("nr_dll"):
        os.environ["NS_NR_DLL"] = str(cfg["nr_dll"])


def _log_environment(cfg: dict) -> None:
    """Print the environment header into the log: version, OS, HDR, driver.

    Users paste NeuralScreen.log into issues; the header answers the
    questions we would otherwise have to ask (which version, which
    Windows, is HDR on, which driver). Every probe is wrapped: a missing
    API or a stripped system must not crash the startup - the line is
    simply skipped.
    """
    try:
        import platform
        import sys as _sys
        win = _sys.getwindowsversion()
        print(f"[env] NeuralScreen {APP_VERSION} | Windows {win.major}.{win.minor} "
              f"(build {win.build}) | {platform.platform()}")
    except Exception:
        print(f"[env] NeuralScreen {APP_VERSION} | Windows unknown")
    try:
        import gpuinfo
        g = gpuinfo.probe()
        print(f"[env] GPU: {g.get('name') or 'unknown'} "
              f"({g.get('family') or '?'}, arch 0x{g.get('arch_group', 0):X})")
    except Exception:
        pass
    try:
        # The NVIDIA driver version from the display-class registry key.
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        for idx in range(10):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    f"{base}\\{idx:04d}") as key:
                    desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                    if "NVIDIA" in str(desc):
                        ver, _ = winreg.QueryValueEx(key, "DriverVersion")
                        print(f"[env] driver: {ver}")
                        break
            except OSError:
                continue
    except Exception:
        pass
    try:
        # HDR: the monitor data store in the registry carries HDREnabled.
        # One read, no deep API digging - if the key is not there the
        # line just says unknown.
        import winreg
        base = (r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
                r"\MonitorDataStore")
        hdr = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as mon:
                            try:
                                val, _ = winreg.QueryValueEx(mon, "HDREnabled")
                                hdr = bool(val)
                                break
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            pass
        print(f"[env] HDR: {'on' if hdr else 'off' if hdr is not None else 'unknown'}")
    except Exception:
        pass
    try:
        numlock = bool(ctypes.windll.user32.GetKeyState(0x90) & 1)
        print(f"[env] Num Lock: {'on' if numlock else 'off'} | "
              f"lang: {cfg.get('lang', 'en')} | "
              f"profile: {cfg.get('profile', '?')} | "
              f"work_scale: {cfg.get('work_scale', '?')}")
    except Exception:
        pass

# DPI awareness BEFORE any import (cv2, capture, display, tray): if some
# module sets awareness first (dxcam, for instance, calls
# SetProcessDpiAwareness(2) when creating an Output), a second call returns
# ERROR_ACCESS_DENIED and the pygame window ends up scaled (125% ->
# 3072x1728). PER_MONITOR_AWARE_V2 = -4. Errors are ignored: display.py
# repeats the call.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Embedded Python (python313._pth) does not add cwd to sys.path - we add the
# script folder by hand so the local modules work (capture, display, guides).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
import pygame  # HUD overlay on the recorded frame (image.frombuffer)

from capture import (ScreenCapture, devicename_for_output_idx, list_monitors,
                     resolve_output_idx)
from display import Display
from guides import TemporalGuideGenerator
from hotkeys import (HotkeyController, build_bindings,
                     describe as describe_hotkeys, numlock_needed, numlock_on,
                     parse_binding)
from recorder import VideoRecorder
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from i18n import STRINGS as UI_STRINGS

from tray import TrayController
from taskbar import TaskbarWindow
import dialogs
import channels
import commands
import settings_io
import pipeline
# The restart cooldown is the loop's business too: it is what the
# deferred apply waits for.
from pipeline import RESTART_COOLDOWN  # noqa: F401
# The pieces below live in their own modules now; re-exported because
# the rest of the program and the tests look them up in main.
from paths import BASE_DIR, NATIVE_DIR, WORKER_EXE  # noqa: F401
from protocol import SharedFrameBuffer, _negotiate_shm  # noqa: F401
from pipeline import (_drain_stderr, restart_worker,  # noqa: F401
                      shutdown_worker, start_worker)
from settings_io import _work_size, hotkey_labels  # noqa: F401
# The settings layer owns these now; re-exported because the rest
# of the program and the tests look them up in main.
from settings_io import (  # noqa: F401
    CHANNEL_URL, PRESET_NAME_PREFIX, REPO_URL, WORK_SCALE_MAX,
    WORK_SCALE_STEP, _next_preset_name, _set_autostart)
from settings_io import (  # noqa: F401
    APP_VERSION, CHANNEL_LABEL, PROFILES, WORK_MAX_W, WORK_MAX_H, WORK_SCALE_MIN, _atomic_write_json, _autostart_enabled, _menu_layout_payload)
# The Win32 window helpers live in winapi.py now. They are re-exported here
# on purpose: main is where the rest of the program - and the tests - look
# them up, and moving code must not move its callers.
from winapi import (DWMWA_EXTENDED_FRAME_BOUNDS, _RECT,  # noqa: F401
                    _is_desktop_window, _is_taskbar_window,
                    foreign_foreground, list_capturable_windows,
                    window_frame_rect, window_under_cursor)

# The worker protocol lives in protocol.py now - the magics, the formats, the
# senders and the reader thread. Re-exported here because main is where the
# rest of the program and the tests look them up, and because moving code
# must not move its callers.
# Named one by one rather than with a star: a star import would drag
# protocol's own imports into this namespace too.
from protocol import (  # noqa: F401
    DDA_ACK_FMT, DDA_ACK_MAGIC, DDA_FMT, DDA_MAGIC, FRAME_FLAG_BYPASS,
    FRAME_FLAG_MOTION_SMALL, FRAME_FLAG_NO_COLOR, FRAME_FLAG_SHM,
    FRAME_FLAG_SPLIT, FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,
    GRAY_ACK_FMT, GRAY_ACK_MAGIC, GRAY_FMT, GRAY_MAGIC, HEADER_FMT,
    MOTION_ACK_FMT, MOTION_ACK_MAGIC, MOTION_FMT, MOTION_MAGIC,
    OUTS_ACK_FMT, OUTS_ACK_MAGIC, OUTS_FMT, OUTS_MAGIC, OUT_BYTES_IN_SHM,
    OUT_FMT, OUT_MAGIC, RACK_FMT, RESIZE_ACK_MAGIC, RESIZE_FLAG_NR_SMALL,
    RESIZE_FMT, RESIZE_MAGIC, SHM_ACK_FMT, SHM_ACK_MAGIC, SHM_FMT,
    SHM_MAGIC, VIDEO_MAGIC, WGC_ACK_FMT, WGC_ACK_MAGIC, WGC_FMT,
    WGC_MAGIC, WINDOW_ACK_FMT, WINDOW_ACK_MAGIC, WINDOW_FLAG_CAPTURABLE,
    WINDOW_FLAG_DISABLE, WINDOW_FMT, WINDOW_MAGIC, WorkerReader,
    _read_exact, send_dda, send_frame, send_gray, send_motion_size,
    send_out, send_resize, send_wgc, send_window)


# The four sliders a user preset stores. The same keys as PROFILES carries,
# minus the NGX plumbing (profile/preset/style/auto_mask/ui_correction stay
# tied to the built-in profile the preset was saved from).
PRESET_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")
PARAM_MIN, PARAM_MAX = 0.0, 2.5
SKIN_MIN = -1.0


FPS_LOG_INTERVAL = 2.0  # seconds, FPS log to the console
PERF_LOG_INTERVAL = 5.0  # seconds, log of the mean pipeline stage timings
PERF_KEYS = ("grab", "resize_full", "guides", "send", "recv", "show")


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


            # A reply for a frame main no longer waits for (after a timeout) - skip


def check_worker(worker: subprocess.Popen, logs: list[str]) -> None:
    """If the worker died - print the last stderr lines and raise."""
    code = worker.poll()
    if code is not None:
        tail = "\n".join(logs[-40:]) or "(stderr empty)"
        raise RuntimeError(
            f"the NGX worker exited with code {code}.\n"
            f"last stderr lines:\n{tail}"
        )


def _hard_failure(logs: list[str]) -> bool:
    """Whether the worker's tail shows a HARD failure - 0xBAD00001.

    FeatureNotSupported (0xBAD00001) means the GPU cannot run the neural
    pass at all (Turing, a broken runtime build): no auto-recovery will
    ever clear it, and retrying only spins the restart loop. Transient
    failures (0x00000000 no-frame, timeouts, driver hiccups) can clear
    on their own - those are the ones worth an automatic revive.
    """
    return any("0xBAD00001" in line for line in logs[-40:])




class _Pipeline:
    """Everything main() rebinds while the program runs.

    These 56 names used to be locals of main() reached through 39
    `nonlocal` statements and 1041 references: every nested function
    could rebind any of them, and nothing said which part of the pipeline
    owned what. They are fields of one object now, so a function that takes
    `st` declares by that alone that it touches the pipeline, and the reader
    can see where a value comes from.

    __slots__ is the point, not an optimisation: a typo in a field name
    raises AttributeError here instead of quietly creating a new attribute
    that nothing ever reads.
    """

    __slots__ = (
        "buf_full",
        "capture",
        "cfg",
        "consecutive_restarts",
        "dda_attempted",
        "dda_mode",
        "display",
        "follow_pos",
        "follow_resize",
        "frame_index",
        "gpu_ok",
        "gray_active",
        "guide_fails",
        "guides",
        "height",
        "hotkey_bindings",
        "hotkeys",
        "lang",
        "last_foreground",
        "last_restart",
        "mon_h",
        "mon_w",
        "monitor",
        "motion_attempted",
        "motion_small",
        "next_auto_revive",
        "nr_small",
        "out_attempted",
        "out_shm",
        "output_rgba",
        "params",
        "paused",
        "pending_apply",
        "pending_shot",
        "perf",
        "present_attempted",
        "present_mode",
        "presets",
        "pts",
        "reader",
        "record_audio",
        "recorder",
        "running",
        "shm",
        "shot_dialog_open",
        "shot_paths",
        "split_pos",
        "startup_menu",
        "tray",
        "tray_commands",
        "width",
        "window_hwnd",
        "work_frame",
        "work_h",
        "work_scale",
        "work_w",
        "worker",
        "worker_failed",
        "worker_logs",
        "worker_stop",
        "want_dda",
        "want_motion_small",
        "want_out_shm",
        "want_present",
        "cfg_path",
        "gpu_text",
        "warmup",
    )


def main() -> int:
    # The pipeline's mutable state (see _Pipeline): one object
    # instead of 56 closure variables.
    st = _Pipeline()
    parser = argparse.ArgumentParser(description="DLSS 5 Desktop NR prototype")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json",
                        help="path to config.json (defaults to next to main.py)")
    args = parser.parse_args()
    _init_logging()  # pythonw: stdout/stderr -> NeuralScreen.log

    # One instance only: two copies fight over the screen capture (the
    # second one gets a dead DDA and the first one loses frames). The
    # mutex is the standard Windows single-instance mechanism - it lives
    # in the kernel and dies with the process, so a crashed copy does not
    # block the next launch.
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "NeuralScreen_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[main] another NeuralScreen is already running - this copy exits", file=sys.stderr)
        return 1

    # The config file's path, kept in the state: the settings module writes
    # back into it and has no business knowing what argparse is.
    st.cfg_path = args.config
    st.cfg = load_config(st.cfg_path)
    st.params = resolve_params(st.cfg)
    st.presets = load_presets(st.cfg)
    _apply_nr_dll(st.cfg)
    _log_environment(st.cfg)
    st.width, st.height = int(st.cfg["width"]), int(st.cfg["height"])
    monitor_cfg = st.cfg["monitor"]
    if isinstance(monitor_cfg, str):
        # New configs store the DXGI devicename - resolve it to the current
        # output index; a monitor that is not connected falls back to 0.
        st.monitor = resolve_output_idx(monitor_cfg)
        if st.monitor is None:
            print(f"[main] monitor {monitor_cfg!r} from config.json is not "
                  "connected - using monitor 0", file=sys.stderr)
            st.monitor = 0
    else:
        # Old configs store the positional index.
        st.monitor = int(monitor_cfg)
    st.warmup = int(st.cfg["warmup"])
    st.work_scale = float(st.cfg["work_scale"])
    # The worker reads NS_NR_SMALL once, at startup: with it on, Neural
    # Rendering runs at the work resolution and the result is scaled back up
    # instead of the network chewing the whole screen. Off by default - it is
    # faster but softer, and an update must not change how the picture looks
    # without being asked. Toggling it later restarts the worker, which is why
    # it lives in the environment rather than in the frame protocol.
    st.nr_small = bool(st.cfg.get("nr_small", False))
    os.environ["NS_NR_SMALL"] = "1" if st.nr_small else "0"
    st.lang = str(st.cfg["lang"])

    # The output resolution comes FROM THE REAL MONITOR, not from a stale
    # config.json (the monitor may have been switched to 1440p while the
    # config still remembers 4K - the overlay, the recording and the worker
    # window would start drifting away from the screen).
    st.capture = ScreenCapture(monitor_idx=st.monitor)
    st.mon_w, st.mon_h = st.capture.resolution
    if st.mon_w > 0 and st.mon_h > 0 and (st.mon_w, st.mon_h) != (st.width, st.height):
        print(f"[main] monitor {st.monitor} is {st.mon_w}x{st.mon_h} (config: {st.width}x{st.height}), "
              f"taking the real resolution")
        st.width, st.height = st.mon_w, st.mon_h

    print(f"[main] NeuralScreen - profile {st.cfg['profile']!r}, "
          f"resolution {st.width}x{st.height}, monitor {st.monitor}")
    print(f"[main] NGX parameters: {st.params}")
    print(f"[main] work_scale {st.work_scale:.2f} (NGX resolution "
          f"{int(st.width * st.work_scale)}x{int(st.height * st.work_scale)})")

    st.worker: subprocess.Popen | None = None
    st.reader: WorkerReader | None = None
    st.worker_stop: threading.Event | None = None
    st.shm: SharedFrameBuffer | None = None
    st.display: Display | None = None
    st.tray: TrayController | None = None
    st.hotkeys: HotkeyController | None = None
    st.recorder: VideoRecorder | None = None
    try:
        # The worker and guides run at the work resolution (the NGX feature is
        # created from the header sizes; guides' assert requires them to match)
        st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale)
        # The v3 protocol (full_w/full_h) ONLY when work != full: at work==full
        # (scale 1.0) the worker crashes or hangs in upscale mode (verified in
        # isolation) - we use legacy full_w=0, as in D5V2.
        full_w = st.width if (st.work_w != st.width or st.work_h != st.height) else 0
        full_h = st.height if (st.work_w != st.width or st.work_h != st.height) else 0
        # Shared memory for the input frame: its size does not depend on
        # work_scale (see SharedFrameBuffer), so it is created once per process.
        st.shm = SharedFrameBuffer(st.width, st.height)
        # Which card this is and whether NR works on it. The model comes from
        # nvapi, but the support verdict comes from the worker rather than the
        # architecture: only it knows whether feature 18 was created.
        gpu_info = gpu_probe()
        st.gpu_text = gpu_describe(gpu_info)
        st.gpu_ok: bool | None = None
        print(f"[main] GPU: {st.gpu_text or 'unknown'} "
              f"(group 0x{gpu_info['arch_group']:X}, officially supported: "
              f"{'yes' if gpu_info['official'] else 'no'})")
        # The stock warm-up is 120 discarded evaluations. On a fast Blackwell
        # card that is a second or two; on Turing/Ampere/Ada it can take far
        # longer than the frame watchdog, which then kills the worker on
        # frame 0 and starts a restart storm (seen on RTX 2070 at ~1 FPS and
        # on RTX 3060 Ti at ~18 FPS). Unsupported/pre-Blackwell cards get a
        # short warm-up; the actual effect is still evaluated normally
        # afterwards.
        effective_warmup = st.warmup
        if not gpu_info["official"] and st.warmup > 4:
            effective_warmup = 4
            print(f"[main] pre-Blackwell GPU: warmup {st.warmup} -> "
                  f"{effective_warmup} to avoid a false frame-0 watchdog "
                  f"timeout")
        st.worker, st.worker_logs, st.reader, st.worker_stop = start_worker(
            st.params, st.work_w, st.work_h, effective_warmup, full_w, full_h, st.shm)
        print(f"[main] worker started (pid {st.worker.pid}), header sent "
              f"({st.work_w}x{st.work_h})")

        print(f"[main] capturing monitor {st.monitor}: {st.capture.resolution}")

        st.display = Display(st.width, st.height, fullscreen=bool(st.cfg["fullscreen"]))
        st.display.set_lang(st.lang)
        # The program draws over the desktop and gives no sign of itself -
        # without this it is unclear after launch whether it is running.
        st.startup_menu = bool(st.cfg.get("open_menu_on_start", True))
        # The before/after wipe: the share of the frame the worker leaves raw.
        st.split_pos = min(1.0, max(0.0, float(st.cfg.get("split", 0.0))))
        startup_pending = True
        # The menu size, position and theme - exactly as the user left them.
        st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
        saved_theme = st.cfg.get("theme")
        if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
            st.display.menu.set_state({"theme": saved_theme})
        saved_offset = st.cfg.get("menu_offset")
        if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
            st.display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
        saved_height = st.cfg.get("menu_height")
        if isinstance(saved_height, (int, float)) and saved_height > 0:
            st.display.menu.user_height = int(saved_height)
        print(f"[main] output window {st.display.width}x{st.display.height}")

        # Tray icon: commands go into a queue, the main loop reads them
        st.tray_commands = queue.Queue()
        # Answers from the "Save as" dialog. The dialog is modal and lives in
        # its own thread (see _open_save_dialog); the path arrives here.
        st.shot_paths = queue.Queue()
        st.shot_dialog_open = False
        st.tray = TrayController(st.tray_commands, labels={
            "settings": UI_STRINGS[st.lang].get("settings_title", "Settings"),
            "quit": UI_STRINGS[st.lang].get("exit", "Exit"),
        })
        st.tray._set_state(nr=True, scale=st.work_scale)
        st.tray.start()
        print("[main] tray icon started")

        # Taskbar button: the overlay and the worker window are tool
        # windows, so the program lived only in the tray. A 1x1 APPWINDOW
        # window gives the program a real taskbar button; clicking it sends
        # the same "settings" command as a left click on the tray (user
        # rule 2026-09-09: the program must always show in the taskbar).
        taskbar = TaskbarWindow(st.tray_commands, "NeuralScreen")
        taskbar.start()
        print("[main] taskbar window started")

        # Global hotkeys: RegisterHotKey rather than polling the key state.
        # The system gives the keypress to us alone and does not pass it to the
        # active application - Num1 inside a game toggles NR and the game never
        # sees the key (the polling fallback does not swallow it, but the numpad
        # is free in games). The commands go into the same queue the tray uses. The
        # user's bindings come from config.json ("hotkeys": {"toggle": "Num1", ...}).
        hotkey_overrides = st.cfg.get("hotkeys")
        if not isinstance(hotkey_overrides, dict):
            hotkey_overrides = {}
        st.hotkey_bindings = build_bindings(hotkey_overrides)
        st.hotkeys = HotkeyController(st.tray_commands, st.hotkey_bindings)
        st.hotkeys.start()
        if st.hotkeys.registered:
            print(f"[main] hotkeys registered: {', '.join(st.hotkeys.registered)} "
                  f"({describe_hotkeys(st.hotkey_bindings)})")
        if st.hotkeys.failed:
            print(f"[main] hotkeys taken by another program: {', '.join(st.hotkeys.failed)}",
                  file=sys.stderr)
        # The numpad sends different key codes with Num Lock off, so those
        # bindings do not misbehave - they are simply absent. Say so, or it
        # looks like the program ignores the keyboard.
        numpad = numlock_needed(st.hotkey_bindings)
        if numpad and not numlock_on():
            print(f"[main] Num Lock is off: the numpad hotkeys "
                  f"({', '.join(numpad)}) will not fire until it is on",
                  file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["numlock_off"], duration=6.0)
        # The captions on the menu buttons come from the same bindings that were
        # registered. Strictly after build_bindings: before that they do not exist.
        st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))

        # The settings live in the overlay menu (Num2). There is no separate
        # window any more: it was a second interface over the same fields, it
        # stole focus from the game and dragged the whole of tcl/tk into the
        # runtime.

        st.guides = TemporalGuideGenerator(st.work_w, st.work_h)

        # A reused buffer: every frame allocated ~100 MB (a 4K grab plus the
        # resizes plus flow), the GC could not keep up -> OOM around frame 1900.
        # The buffer is reused through cv2.resize(dst=...). work/out buffers are
        # not needed: in v3 the full->work->full resize is done by the worker on
        # the GPU (NGX Upscaling).
        st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)

        st.paused = False
        # The worker died and exhausted the restart budget: the pipeline is
        # stopped (no send/recv, no more restarts) and the overlay is hidden
        # so the desktop is not covered by a black window (issue #3: black
        # screen on a GPU where feature 18 cannot be created). Cleared when
        # the user turns NR back on.
        st.worker_failed = False
        st.frame_index = 0
        st.pts = 0
        guide = None  # initialised before the loop: Num1 before the first NR frame must not raise NameError
        st.output_rgba = None  # the last NR frame (for a screenshot); None until the first one
        # WNDO mode: the worker shows the frame, no pixels come back to Python.
        st.want_present = bool(st.cfg.get("worker_present", True))
        st.want_motion_small = bool(st.cfg.get("motion_on_gpu", True))
        st.want_dda = bool(st.cfg.get("capture_in_worker", True))  # DDA: the worker takes the colour
        # The result pixels come back through shared memory, not the pipe.
        st.want_out_shm = bool(st.cfg.get("pixels_in_shm", True))
        # System audio ("what you hear") as a second track in the recording.
        # A config flag rather than a menu item: it is a decision made once,
        # not something to reach for while the overlay is up.
        st.record_audio = bool(st.cfg.get("record_audio", True))
        st.out_shm = False
        st.out_attempted = False
        st.motion_small = False  # the worker upscales the motion field itself
        st.motion_attempted = False  # already tried for the current worker
        st.present_mode = False      # the worker window is up right now
        st.present_attempted = False  # already tried for the current worker (do not spam)
        st.dda_mode = False          # the worker captures the screen itself
        st.dda_attempted = False     # already tried for the current worker (do not spam)
        st.window_hwnd = None        # WGCW target; None = the whole desktop (DDA1)
        st.last_foreground = 0       # the last focused window that was not ours
        st.follow_pos = None         # where the overlay currently sits (window mode)
        st.follow_resize = None      # a pending size change, waiting to settle
        st.mon_w, st.mon_h = st.width, st.height  # the full monitor size (for the menu layer)
        st.gray_active = False       # guides take luminance from the worker's gray channel
        st.pending_shot: Path | None = None  # a screenshot waiting for a frame with pixels
        st.recorder: VideoRecorder | None = None  # recording (Num0), MP4 AV1 NVENC
        st.work_frame = None  # the current work frame; None -> grab at the top of the loop
        fps_window: list[float] = []
        last_log = time.monotonic()
        last_fps = 0.0
        # Stage timings: mean ms over PERF_LOG_INTERVAL (the [perf] log)
        st.perf = {k: [] for k in PERF_KEYS}
        last_perf_log = time.monotonic()


        def _perf(key: str, t0: float) -> None:
            """Record the stage duration (ms) into the timings dictionary."""
            st.perf[key].append((time.perf_counter() - t0) * 1000.0)
        st.running = True
        # Protection against rapid changes (arrow key repeat, a jerked slider):
        # the intermediate values are coalesced and only the last one is applied.
        # 0.5 s rather than 2 s: the change goes through RNSZ inside the live
        # worker process, not through a restart with an NGX init/shutdown plus
        # sleep(2) - the expensive path is only a fallback now.
        st.last_restart = 0.0
        st.pending_apply: tuple | None = None  # the deferred (scale, profile, params)
        # Auto-recovery limit: if the worker dies N times in a row we turn NR
        # off (pause) and raise an alert instead of spinning through restarts.
        MAX_CONSECUTIVE_RESTARTS = 3
        # A transient failure (no-frame, driver hiccup) gets ONE automatic
        # revive after this backoff instead of leaving NR off until the user
        # presses Num1. A hard failure (0xBAD00001) never auto-revives.
        AUTO_REVIVE_BACKOFF = 30.0  # seconds
        st.next_auto_revive = 0.0      # monotonic deadline; 0 = no revive pending
        st.consecutive_restarts = 0
        st.guide_fails = 0

        def _recreate_capture() -> None:
            """Recreate the capture (a fresh DDA session) after a failure or mode change."""
            try:
                st.capture.close()
            except Exception:
                pass
            st.capture = ScreenCapture(monitor_idx=st.monitor)

        def _safe_grab() -> np.ndarray | None:
            """grab() that recreates the capture on failure.

            Launching a game in fullscreen invalidates Desktop Duplication
            (DXGI_ERROR_ACCESS_LOST / a mode change) - dxcam may raise instead
            of returning None. We recreate the DDA session and return None (the
            loop skips the iteration).
            """
            try:
                return st.capture.grab()
            except Exception as exc:
                print(f"[main] capture failed ({exc}) - recreating the DDA session")
                try:
                    _recreate_capture()
                except Exception as exc2:
                    print(f"[main] recreating the capture failed: {exc2}",
                          file=sys.stderr)
                return None


        while st.running:
            loop_start = time.perf_counter()
            now = time.monotonic()

            if not commands.drain_commands(st):
                break

            # The worker is gone (restart budget exhausted): the pipeline is
            # stopped. Commands still run (Num1 revives it), but no frame is
            # grabbed or sent - the worker is dead and would only be
            # restarted in vain (issue #3: endless restart loop on a GPU
            # where feature 18 cannot be created). A transient failure gets
            # one automatic revive after the backoff (recovery pattern from
            # dlss5-video-player 0.17.2: CreateFeature-once, retries as a
            # fallback - the revive is a fresh process, not a feature
            # recreation).
            if st.worker_failed:
                if st.next_auto_revive and time.monotonic() >= st.next_auto_revive:
                    st.next_auto_revive = 0.0
                    st.worker_failed = False
                    print("[main] auto-reviving the worker after the transient failure")
                    try:
                        st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                            st.worker, st.params, st.work_w, st.work_h, st.warmup,
                            st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                            st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                            st.worker_stop, st.shm)
                        channels.forget_present(st)
                        channels.forget_dda(st)
                        channels.forget_out(st)
                        channels.sync_motion_size(st)
                        st.frame_index = 0
                        st.pts = 0
                        st.paused = False
                        st.display.set_visible(True)
                        st.display.alert(UI_STRINGS[st.lang]["nr_on"])
                        st.tray._set_state(nr=True)
                    except Exception as exc:
                        print(f"[main] auto-revive failed ({exc}) - staying NR OFF",
                              file=sys.stderr)
                        st.worker_failed = True
                time.sleep(0.05)
                continue

            # Deferred apply (coalescing): if a restart happened recently, we
            # apply the last value once the pause is over
            if st.pending_apply is not None and time.monotonic() - st.last_restart >= RESTART_COOLDOWN:
                p_scale, p_profile, p_params, p_small = st.pending_apply
                st.pending_apply = None
                print("[main] applying the deferred settings")
                pipeline.do_restart(st, p_scale, p_profile, p_params, new_small=p_small)

            if not st.running:
                break

            # NR OFF - bypass: the pipeline keeps spinning (grab -> show the
            # raw frame in the worker's window) but the NGX effect is skipped.
            # The overlay (picture + HUD) stays alive and predictable; we hide
            # everything only on a real exit. A bypass frame is sent like any
            # other (the flag lives in the header) so send/recv stay paired.
            bypass = st.paused
            # (for readability: send_frame is called with bypass=bypass)

            # The answer from the "Save as" dialog (it runs in its own thread).
            commands.drain_save_dialog(st)

            if st.want_present and not st.present_mode and not st.present_attempted:
                channels.enable_present(st)
            # Who has the focus, for the window-mode hotkey: by the time it
            # is pressed the menu may be in front, so the last window that was
            # not ours is remembered continuously.
            fg = foreign_foreground()
            if fg:
                st.last_foreground = fg
            # A game that goes fullscreen raises itself above every topmost
            # window, ours included, and then the menu is drawn but not on
            # screen. While it is open we keep coming back up; a SetWindowPos
            # that changes nothing is cheap, and 30 frames is fast enough that
            # nobody sees the menu disappear.
            if st.display.menu.visible and st.frame_index % 30 == 0:
                st.display.raise_topmost()
            # The same for the HUD even when the menu is closed: a borderless
            # game (Cyberpunk) keeps itself on top and our HUD stays
            # underneath it forever. Re-assert only when the topmost window
            # is NOT ours - in the steady state this is zero SetWindowPos
            # calls, so no DWM flicker (user: flicker + invisible HUD over
            # borderless games).
            if st.frame_index % 30 == 0:
                try:
                    top = ctypes.windll.user32.GetTopWindow(0)
                    if top and top != st.display.get_hwnd():
                        st.display.raise_topmost()
                except Exception:
                    pass
            if st.window_hwnd is not None:
                if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(st.window_hwnd)):
                    print("[main] the captured window closed - back to full screen",
                          file=sys.stderr)
                    pipeline.switch_window(st, 0)
                    continue
                pipeline.follow_window(st)
            if st.want_dda and not st.dda_mode and not st.dda_attempted:
                if st.window_hwnd is not None:
                    # The channel module opens channels; deciding that the
                    # window is gone and the whole screen comes back is the
                    # pipeline's call, and it lives here.
                    if not channels.enable_wgc(st):
                        pipeline.switch_window(st, 0)
                else:
                    channels.enable_dda(st)
            if st.want_motion_small and not st.motion_small and not st.motion_attempted:
                st.motion_attempted = True
                channels.sync_motion_size(st)
            if st.want_out_shm and not st.out_shm and not st.out_attempted:
                channels.enable_out_shm(st)

            # --- Input for the overlay menu --------------------------
            # Events are read only while the menu is open: the rest of the
            # time the window is click-through, there are no events, and an
            # extra get() would eat the queue from pump() inside drawing.
            if st.display.menu.visible:
                for ev in pygame.event.get():
                    for action in st.display.menu.handle_event(ev):
                        commands.apply_menu_action(st, action)
                if not st.display.menu.dragging:
                    st.display.menu.set_state(settings_io.menu_payload(st))

            # --- Grab ahead: while NGX computes frame N we grab N+1 -------
            # work_frame == None happens on the first frame, after a worker
            # restart (a work_scale change) and after grab()==None. Then the
            # grab happens at the start of the iteration, BEFORE send - the
            # synchronisation with the worker is not lost (send/recv are
            # always paired, recv is mandatory after any send).
            # The worker (v3, NGX Upscaling) resizes full->work->full on the
            # GPU itself: Python sends a full-res frame, motion at work-res
            # (guides is built with work_w/work_h and downsamples its own
            # input) and receives full-res back.
            if st.work_frame is None and not st.gray_active:
                t0 = time.perf_counter()
                frame = _safe_grab()
                _perf("grab", t0)
                if frame is None:
                    continue  # the frame is not ready yet - skip the iteration
                if frame.shape[1] != st.width or frame.shape[0] != st.height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(frame, (st.width, st.height), interpolation=cv2.INTER_LANCZOS4, dst=st.buf_full)
                    except cv2.error:
                        # The monitor resolution changed: buf_full was
                        # preallocated for the old size - recreate and retry
                        st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
                        cv2.resize(frame, (st.width, st.height), interpolation=cv2.INTER_LANCZOS4, dst=st.buf_full)
                    _perf("resize_full", t0)
                    frame = st.buf_full
                else:
                    frame = np.ascontiguousarray(frame, dtype=np.uint8)
                st.work_frame = frame

            # --- Sending the frame with auto-recovery ---
            # The worker can die or hang (NGX after RNSZ, a GPU conflict) -
            # instead of crashing, main restarts the worker with the current
            # parameters and carries on. This is the last line of defence:
            # the program does not fall over.
            try:
                t0 = time.perf_counter()
                if st.gray_active:
                    guide = st.guides.process(gray=st.shm.read_gray())
                else:
                    guide = st.guides.process(st.work_frame)
                _perf("guides", t0)
            except Exception as guide_exc:
                # guides is not critical: ValueError/TypeError/cv2.error (the
                # shape of the gray frame, a division by zero) must not take
                # the process down. We skip the frame - the worker gets the
                # next one. But a persistent error (an incompatible gray
                # channel, a broken shape) would spin main at 100% CPU -
                # after 5 failures in a row we fall back to zero motion: the
                # frames keep flowing and the picture does not freeze.
                print(f"[main] guides.process failed ({guide_exc}) - frame skipped",
                      file=sys.stderr)
                st.guide_fails += 1
                if st.guide_fails >= 5:
                    print(f"[main] guides.process is unstable - zero motion "
                          f"(frames keep flowing)", file=sys.stderr)
                    st.guide_fails = 0
                    guide = st.guides.zero_guide()
                else:
                    continue
            try:
                check_worker(st.worker, st.worker_logs)
                t0 = time.perf_counter()
                send_frame(st.worker, st.frame_index, st.work_frame, guide.motion, guide.reset,
                           st.pts, st.shm, want_pixels=(st.pending_shot is not None
                                                   or (st.recorder is not None
                                                       and st.recorder.needs_frame())),
                           motion_small=st.motion_small,
                           no_color=bool(st.dda_mode),
                           bypass=bypass,
                           split=st.split_pos)
                _perf("send", t0)
            except (BrokenPipeError, OSError, EOFError, RuntimeError) as exc:
                st.consecutive_restarts += 1
                if st.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] the worker died {st.consecutive_restarts} times in a row - NR OFF")
                    st.paused = True
                    st.worker_failed = True
                    st.display.alert(UI_STRINGS[st.lang]["nr_off"])
                    st.tray._set_state(nr=False)
                    st.consecutive_restarts = 0
                    st.work_frame = None
                    # The worker is gone and will not come back on its own:
                    # stop hammering it, hide the overlay so the desktop is
                    # not covered by a black window (issue #3), and wait for
                    # the user to turn NR back on. A HARD failure
                    # (0xBAD00001 - the GPU cannot run the pass at all) is
                    # permanent; a transient one (no-frame, driver hiccup)
                    # gets one automatic revive after a backoff instead of
                    # leaving the user with NR off until they press Num1.
                    if not _hard_failure(st.worker_logs):
                        st.next_auto_revive = time.monotonic() + AUTO_REVIVE_BACKOFF
                        print(f"[main] transient worker failure - auto-revive "
                              f"in {AUTO_REVIVE_BACKOFF:.0f}s")
                    try:
                        shutdown_worker(st.worker, st.worker_stop)
                    except Exception:
                        pass
                    st.display.set_visible(False)
                    continue
                print(f"[main] worker lost while sending ({exc}) - restarting "
                      f"({st.consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                if st.worker_logs:
                    print("[main] worker stderr (tail):")
                    for line in st.worker_logs[-15:]:
                        print(f"  {line}")
                st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                    st.worker, st.params, st.work_w, st.work_h, 10,
                    st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.worker_stop, st.shm)
                channels.forget_present(st)
                channels.forget_dda(st)
                channels.forget_out(st)
                channels.sync_motion_size(st)
                st.frame_index = 0
                st.pts = 0
                st.work_frame = None
                continue

            # Grab the next frame WHILE the worker computes the current one
            # (NGX is ~70-100 ms/frame - the bottleneck). dxcam is thread-safe
            # within one thread - a second thread is unnecessary, we simply
            # move grab() between send and recv. Buffers: send_frame copies
            # the data into the pipe (tobytes) and guides.process keeps no
            # references to its input - buf_full can be reused right away.
            # In DDA mode the worker grabs the frame itself - Python does not.
            next_frame = None
            if not st.gray_active:
                t0 = time.perf_counter()
                next_frame = _safe_grab()
                _perf("grab", t0)
            if next_frame is not None:
                if next_frame.shape[1] != st.width or next_frame.shape[0] != st.height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(next_frame, (st.width, st.height), interpolation=cv2.INTER_LANCZOS4, dst=st.buf_full)
                    except cv2.error:
                        st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
                        cv2.resize(next_frame, (st.width, st.height), interpolation=cv2.INTER_LANCZOS4, dst=st.buf_full)
                    _perf("resize_full", t0)
                    next_frame = st.buf_full
                else:
                    next_frame = np.ascontiguousarray(next_frame, dtype=np.uint8)
            # next_frame == None: the frame is not ready - the start of the
            # next iteration will do the grab (work_frame = None). The
            # synchronisation with the worker is not lost: send has already
            # gone out and the recv below is mandatory.

            t0 = time.perf_counter()
            try:
                st.output_rgba = None
                recv_reader = st.reader
                recv_deadline = time.monotonic() + 5.0
                while time.monotonic() < recv_deadline:
                    try:
                        st.output_rgba = st.reader.recv(st.frame_index, timeout=0.05)
                        break
                    except TimeoutError:
                        # A heavy 4K scene can take ~1 s per NGX frame -
                        # keep the hotkeys alive while main waits (user:
                        # "NR toggle does not always fire in Cyberpunk").
                        # The switch overlay's spinner must keep animating
                        # while the new worker warms up.
                        if st.display.is_switch_active():
                            st.display.draw_overlay(0.0)
                        if not commands.drain_commands(st):
                            st.running = False
                            break
                        if st.reader is not recv_reader:
                            break  # a command restarted the worker
                        continue
                else:
                    raise TimeoutError(
                        f"the worker has been silent for 5s on frame {st.frame_index} - NGX did not answer after the restart")
                if not st.running:
                    break
                if st.reader is not recv_reader:
                    continue  # the worker was restarted by a command
            except (TimeoutError, EOFError, RuntimeError, OSError) as exc:
                st.consecutive_restarts += 1
                if st.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] worker silent/dying {st.consecutive_restarts} times in a row - NR OFF")
                    st.paused = True
                    st.worker_failed = True
                    st.display.alert(UI_STRINGS[st.lang]["nr_off"])
                    st.tray._set_state(nr=False)
                    st.consecutive_restarts = 0
                    st.work_frame = None
                    # No frame will ever arrive - the switch overlay must not
                    # hang over the desktop forever (audit M2: the veil is
                    # removed only on a received frame).
                    st.display.exit_switch_mode()
                    # Same for the overlay itself: hide it so the desktop is
                    # not covered by a black window (issue #3). A HARD
                    # failure (0xBAD00001) is permanent; a transient one gets
                    # one automatic revive after a backoff.
                    if not _hard_failure(st.worker_logs):
                        st.next_auto_revive = time.monotonic() + AUTO_REVIVE_BACKOFF
                        print(f"[main] transient worker failure - auto-revive "
                              f"in {AUTO_REVIVE_BACKOFF:.0f}s")
                    try:
                        shutdown_worker(st.worker, st.worker_stop)
                    except Exception:
                        pass
                    st.display.set_visible(False)
                    continue
                print(f"[main] worker silent/dead on frame {st.frame_index} ({exc}) - restarting "
                      f"({st.consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                    st.worker, st.params, st.work_w, st.work_h, 10,
                    st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.worker_stop, st.shm)
                channels.forget_present(st)
                channels.forget_dda(st)
                channels.forget_out(st)
                channels.sync_motion_size(st)
                st.frame_index = 0
                st.pts = 0
                st.work_frame = None
                continue
            _perf("recv", t0)
            # A frame arrived - the failure chain is broken. Without the reset
            # the counter accumulated across the whole session and three
            # unrelated failures (even an hour apart) turned NR off.
            st.consecutive_restarts = 0
            status = "NR OFF" if st.paused else "NR ON"
            st.pts += 1

            t0 = time.perf_counter()
            try:
                if st.recorder is not None and st.output_rgba is not None:
                    # Our layer is excluded from capture
                    # (WDA_EXCLUDEFROMCAPTURE), so we bake the open menu onto
                    # the frame ourselves. frombuffer references the numpy
                    # buffer (no copy): the blit writes straight into
                    # output_rgba.
                    try:
                        surf = pygame.image.frombuffer(
                            st.output_rgba, (st.output_rgba.shape[1], st.output_rgba.shape[0]), "RGBX")
                        st.display.draw_capture_overlay(surf)
                    except Exception as menu_exc:
                        print(f"[main] menu was not baked into the recorded frame: {menu_exc}",
                              file=sys.stderr)
                    # The recording gets its own try: an encoder failure must
                    # NOT land in the "output failed" except (that one
                    # recreates the pygame window on every frame - an endless
                    # loop). A recording error stops the recording, not the
                    # window.
                    try:
                        st.recorder.write(st.output_rgba)
                    except Exception as rec_exc:
                        print(f"[main] frame write failed ({rec_exc}) - "
                              f"stopping the recording", file=sys.stderr)
                        try:
                            st.recorder.close()
                        except Exception:
                            pass
                        st.recorder = None
                if st.present_mode:
                    # In WNDO mode the worker draws the frame on screen; in
                    # Python the pixels arrive ONLY on want_pixels
                    # (recording/screenshot). There is no need to show them in
                    # pygame: that is a pointless 4K blend (~22 ms) and a
                    # flicker of the frame in the HUD layer above the worker's
                    # window. The HUD is refreshed by draw_overlay() with
                    # throttling (not every frame).
                    st.display.exit_switch_mode()  # the new worker is presenting
                    st.display.reveal()  # a real frame exchange happened
                    if st.pending_shot is not None and st.output_rgba is not None:
                        commands.save_screenshot(st, st.pending_shot, st.output_rgba)
                        st.pending_shot = None
                    st.display.draw_overlay()
                elif st.output_rgba is None:
                    # The frame is already on screen - the worker showed it, only the HUD here
                    # (WGCW/DDA without want_pixels: no colour reaches Python).
                    # This is still a live exchange with the rebuilt worker: the
                    # switch overlay must come down or the menu stays hidden
                    # behind the veil forever (user: clipped/blank after Num5).
                    st.display.exit_switch_mode()
                    # reveal() is THE only way to show the window while
                    # _reveal_pending is set (audit H1): this branch is hit on
                    # every frame when the WNDO window is unavailable and DDA/
                    # WGCW works (fallback config) - without the call the HUD
                    # and the menu stay invisible forever in that setup.
                    st.display.reveal()
                    st.display.draw_overlay()
                else:
                    st.display.exit_switch_mode()  # the next frame replaces the overlay
                    st.display.reveal()  # a real frame exchange happened
                    st.display.show(st.output_rgba)
                    if st.pending_shot is not None:
                        commands.save_screenshot(st, st.pending_shot, st.output_rgba)
                        st.pending_shot = None
            except Exception as exc:
                # A display mode change (entering/leaving a fullscreen game)
                # can kill the pygame/SDL context - recreate the window.
                print(f"[main] output failed ({exc}) - recreating the window")
                # Snapshot the live menu state before the window dies - the
                # restore below must pick up where the user left it, not the
                # stale cfg values (user rule 10.09: fixed position until
                # the user drags it).
                st.cfg["menu_offset"] = [int(st.display.menu.offset[0]),
                                      int(st.display.menu.offset[1])]
                st.cfg["menu_scale"] = round(st.display.menu.user_scale, 2)
                st.cfg["menu_height"] = (None if st.display.menu.user_height is None
                                      else int(st.display.menu.user_height))
                try:
                    st.display.close()
                except Exception:
                    pass
                st.display = Display(st.width, st.height, fullscreen=bool(st.cfg["fullscreen"]))
                st.display.set_lang(st.lang)
                # In one-window mode the overlay must stay visible to outside
                # recorders: the NEW window comes up with the WDA flag set
                # (the Display default), so state it explicitly here - the
                # same call _rebuild_pipeline makes. Without this, any
                # display-mode change while in window mode silently drops
                # the overlay from NVIDIA App / OBS capture until the next
                # pipeline rebuild (audit #4, F1).
                st.display.set_excluded_from_capture(st.window_hwnd is None)
                # The menu is created together with the window - we give it
                # back its size, position, theme and language, otherwise after
                # a game starts it jumps to the centre, turns light and
                # switches to en.
                st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
                st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))
                saved_theme = st.cfg.get("theme")
                if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
                    st.display.menu.set_state({"theme": saved_theme})
                st.display.menu.set_state({"lang": st.lang})
                saved = st.cfg.get("menu_offset")
                if isinstance(saved, (list, tuple)) and len(saved) == 2:
                    st.display.menu.offset = [int(saved[0]), int(saved[1])]
                if st.present_mode:
                    # The new window must become a transparent layer over the worker again
                    st.display.set_hud_only(True)
                    st.display.raise_topmost()
                st.display.alert(UI_STRINGS[st.lang]["nr_on"])
            _perf("show", t0)
            st.display.set_hud({
                "fps": last_fps,
                "status": status,
                "resolution": f"{st.width}x{st.height}",
                "profile": st.cfg["profile"],
                "params": {k: v for k, v in st.params.items() if k not in ("profile", "preset", "style", "auto_mask", "ui_correction")},
                "frames": st.frame_index,
                # The recording indicator outside the menu: the HUD is drawn
                # over the worker's window, so the user sees the REC state
                # even with the menu closed (user 5080 request).
                "recording": st.recorder is not None,
                "rec_seconds": (st.recorder.duration_ms / 1000.0) if st.recorder else 0.0,
                "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
            })

            st.frame_index += 1
            if startup_pending and st.frame_index >= 2:
                # Wait for the first displayed frame: an open menu over a
                # window that is not filled yet flashes black.
                startup_pending = False
                if st.startup_menu:
                    st.display.menu.set_state(settings_io.menu_payload(st))
                    st.display.menu.visible = True
                    st.display.set_menu_opaque(True)
                    st.display.set_menu_input(True)
                    print("[main] menu opened at startup")
                else:
                    st.display.alert(UI_STRINGS[st.lang]["started"], 3.5)
            st.work_frame = next_frame  # None -> grab at the start of the next iteration
            fps_window.append(time.perf_counter() - loop_start)
            if len(fps_window) > 120:
                fps_window.pop(0)

            if now - last_log >= FPS_LOG_INTERVAL:
                last_fps = len(fps_window) / sum(fps_window) if fps_window else 0.0
                scene = f" | scene {guide.scene_score:.3f}" if guide is not None else ""
                print(f"[main] {status} | FPS {last_fps:5.1f} | frames {st.frame_index} | "
                      f"work {st.work_w}x{st.work_h}{scene}")
                last_log = now

            if now - last_perf_log >= PERF_LOG_INTERVAL:
                parts = []
                for key in PERF_KEYS:
                    samples = st.perf[key]
                    if samples:
                        parts.append(f"{key} {sum(samples) / len(samples):.1f}ms")
                    samples.clear()
                if parts:
                    print("[perf] " + " | ".join(parts))
                last_perf_log = now

        print("[main] exiting at the user's request")
    except KeyboardInterrupt:
        print("\n[main] interrupted (Ctrl+C)")
    except Exception as exc:
        print(f"[main] ERROR: {exc}", file=sys.stderr)
        if st.worker is not None and st.worker.poll() is not None:
            print("[main] the worker crashed; last stderr lines:", file=sys.stderr)
            for line in st.worker_logs[-40:]:
                print(f"  {line}", file=sys.stderr)
        return 1
    finally:
        # A recording may have been running at exit: without close() the moov
        # atom is not written and the file stays broken (players refuse it).
        if st.recorder is not None:
            try:
                st.recorder.close()
            except Exception as exc:
                print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
        if st.worker is not None:
            shutdown_worker(st.worker, st.worker_stop)
        if st.shm is not None:
            st.shm.close()
        try:
            settings_io.save_menu_layout(st)
        except Exception:
            pass
        if st.capture is not None:
            try:
                st.capture.close()
            except Exception as exc:
                print(f"[main] failed to close the capture: {exc}", file=sys.stderr)
        if st.display is not None:
            try:
                st.display.close()
            except Exception as exc:
                print(f"[main] failed to close the window: {exc}", file=sys.stderr)
        try:
            st.hotkeys.stop()
        except Exception:
            pass
        try:
            st.tray.stop()
        except Exception:
            pass
        try:
            taskbar.stop()
        except Exception:
            pass
        print("[main] resources released")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # pythonw: there is no console - show the reason in a message box and
        # keep the details in NeuralScreen.log.
        import traceback
        traceback.print_exc()
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(
                None,
                f"NeuralScreen failed to start: {exc}\n\nDetails in NeuralScreen.log next to the program.",
                "NeuralScreen", 0x10)  # MB_ICONERROR
        except Exception:
            pass
        sys.exit(1)
