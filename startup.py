"""Getting the program ready: from a config file to a running pipeline.

A straight line that runs once, kept away from the loop that runs sixty times
a second. Two halves, in the order they happen:

  configure()  decides what the program is going to do - the config and the
               NR parameters, which monitor, and at what resolution. The
               resolution comes from the REAL monitor rather than from the
               config: a display switched to 1440p while config.json still
               says 4K would leave the overlay, the recording and the worker
               window drifting away from the screen.
  bring_up()   makes it exist - the shared memory, the worker, the overlay,
               the tray icon, the taskbar button, the hotkeys, the guides,
               and every default on the state object the loop then reads.

Order is the whole content of this module. The worker is started before the
overlay because it is the slow part; the menu captions are set only after the
hotkeys are registered, because before that there are no bindings to name;
and the state defaults come last, so nothing the loop reads is missing when
the first frame arrives.
"""
from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading

import numpy as np

from capture import ScreenCapture, resolve_output_idx
from display import Display
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from guides import TemporalGuideGenerator
from hotkeys import (HotkeyController, build_bindings,
                     describe as describe_hotkeys, numlock_needed,
                     numlock_on)
from i18n import STRINGS as UI_STRINGS
from paths import BASE_DIR
from pipeline import start_worker
from protocol import SharedFrameBuffer, WorkerReader
from recorder import VideoRecorder
from settings_io import (APP_VERSION, _work_size, hotkey_labels, load_config,
                         load_presets, resolve_params)
from taskbar import TaskbarWindow
from tray import TrayController


# --- Log to a file instead of the console --------------------------------
# The release is launched through pythonw.exe (no console window): stdout and
# stderr are None there and any print would fail. We redirect them into
# NeuralScreen.log next to main.py - every print keeps working and the user
# reads the log as a file rather than a window. Startup errors (a missing DLL
# and the like) are additionally shown in a message box (see the bottom of
# this file).
LOG_PATH = BASE_DIR / "NeuralScreen.log"


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


def _apply_spout_env(cfg: dict) -> None:
    """The Spout2 bridge flag reaches the worker through the environment.

    SpoutBridgeInit in the worker reads NS_SPOUT once, at process start -
    there is no protocol message for the bridge, so the config flag
    becomes the environment before the first worker is launched (and
    again on every restart, see pipeline.apply_spout). "0" and unset
    both mean off; the worker treats anything but "1" as disabled.
    """
    os.environ["NS_SPOUT"] = "1" if cfg.get("spout") else "0"


def _apply_gpu_env(cfg: dict) -> None:
    """Which card the worker runs on, through the environment.

    NS_GPU is read once per worker process - the adapter is chosen before
    the device exists - so the config flag becomes the environment before
    the first worker is launched, and again on every restart (see
    pipeline.apply_gpu). Unset means the worker's own default: the first
    NVIDIA adapter for the network, adapter 0 for the capture.
    """
    gpu = cfg.get("gpu")
    if gpu is None:
        os.environ.pop("NS_GPU", None)
    else:
        os.environ["NS_GPU"] = str(int(gpu))


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


def configure(st) -> None:
    """Read the config and decide what the program is going to do.

    st.cfg_path is already set - the caller owns argparse, this module does
    not. Everything else lands on the state: the config, the NR parameters,
    the presets, the monitor and the resolution the pipeline will run at.
    """
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
    # The Spout2 bridge is the same story: the worker reads NS_SPOUT once
    # at startup (SpoutBridgeInit), so the config flag becomes the
    # environment before the first worker is launched. Off by default -
    # the bridge costs a full-frame GPU copy on every Present, and it is
    # only useful to someone recording through OBS.
    _apply_spout_env(st.cfg)
    # The same for the card: NS_GPU is read once per worker process.
    _apply_gpu_env(st.cfg)
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


def bring_up(st) -> None:
    """Make it exist: the worker, the overlay, the tray, the hotkeys.

    Runs inside main's try/finally - everything created here is torn down
    there, which is why the handles go onto the state as they appear rather
    than being returned in a bundle at the end.
    """
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
    st.gpu_alerted = False          # the "cannot run the pass" alert, once per verdict
    st.gpu_switch_pending = False   # set by apply_gpu: a split pipeline is worth an alert
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
    st.taskbar = TaskbarWindow(st.tray_commands, "NeuralScreen")
    st.taskbar.start()
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


    st.running = True
    # Protection against rapid changes (arrow key repeat, a jerked slider):
    # the intermediate values are coalesced and only the last one is applied.
    # 0.5 s rather than 2 s: the change goes through RNSZ inside the live
    # worker process, not through a restart with an NGX init/shutdown plus
    # sleep(2) - the expensive path is only a fallback now.
    st.last_restart = 0.0
    st.pending_apply: tuple | None = None  # the deferred (scale, profile, params)
    st.next_auto_revive = 0.0      # monotonic deadline; 0 = no revive pending
    st.consecutive_restarts = 0
    st.guide_fails = 0
