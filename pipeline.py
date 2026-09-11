"""The pipeline's lifecycle: build it, tear it down, point it somewhere else.

Everything the program does between frames. A pipeline is built for ONE frame
size, and the worker, the shared memory, the overlay and every negotiated
channel are built for that size - so changing what is captured (another
monitor, one window, a different work resolution) means tearing the whole
thing down and building it again. That is why these six belong together, and
why they were the hardest part of main() to reach: they rebind everything.

Two rules the hard way:

  * the work resolution and the guides change TOGETHER. Assigning one and not
    the other sent motion of the old size to a worker reading exactly
    new_w*new_h*4 bytes, and the pipeline hung waiting for bytes that never
    came.
  * a window's size and its position are different problems. A move is a
    SetWindowPos; a resize is a rebuild, and it waits for the size to settle
    first - rebuilding on every pixel of a drag would be unusable.
"""
from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import threading
import time

import numpy as np

import channels
import settings_io
from capture import (ScreenCapture, _refresh_dxcam_factory,
                     devicename_for_output_idx, monitor_size,
                     resolve_output_idx)
from display import Display
from guides import TemporalGuideGenerator
from i18n import STRINGS as UI_STRINGS
from paths import NATIVE_DIR, WORKER_EXE
from protocol import (HEADER_FMT, VIDEO_MAGIC, SharedFrameBuffer,
                      WorkerReader, _negotiate_shm, send_dda, send_resize)
from settings_io import _work_size, hotkey_labels
from winapi import window_frame_rect


# Worker lines that always reach the shared log. These are the ones a user
# needs to answer "which card is running the network, and did it come up":
# the adapter list and the NS_GPU pick ([host]), the NGX create/init result
# ([pure]), the architecture spoof ([arch]), the capture path ([cap]) and
# the overlay/spout lifecycle. Before this they were gated behind
# NS_PHASE=1 and a user's log could not tell a working GPU from a Turing
# card that silently fell into SAFE PASSTHROUGH (issue #29: the 2060 Super
# case - the network never came up and nothing said so).
#: Always let through: the pipeline diagnostics. NS_PHASE=1 adds the
#: per-frame profiler lines ([phase]/[pw]) on top of these.
_LOG_ALWAYS = ("[host]", "[pure]", "[arch]", "[cap]", "[dda]", "[present]",
               "[spout]", "[wgc]", "[video]", "[skip]")
#: [video] lines that are a heartbeat rather than a diagnostic: the "delivered
#: frame N" line is printed every 30 frames and would bury the log.
_LOG_SKIP = ("delivered frame",)


def _log_wanted(line: str) -> bool:
    """Whether a worker stderr line goes into the shared log.

    The diagnostics above always do; the per-frame profiler only under
    NS_PHASE=1 (a line per frame would otherwise bury the log). [video]
    carries the one line that says the network never came up - "NR feature
    unavailable (0xBAD00001) - SAFE PASSTHROUGH" - and in issue #29 a user's
    log had no way to show it.
    """
    for skip in _LOG_SKIP:
        if skip in line:
            return False
    for tag in _LOG_ALWAYS:
        if tag in line:
            return True
    if os.environ.get("NS_PHASE") == "1":
        return "[phase]" in line or "[pw]" in line
    return False


def _drain_stderr(worker, logs: list[str], stop: threading.Event) -> None:
    """Background drain of the worker's stderr (otherwise the buffer fills up
    and the worker hangs).

    One thread per worker; it finishes on EOF (the process died) or on the
    stop event (shutdown_worker). After a restart the old thread reads from
    the CLOSED stderr of the old worker: readline() returns b"" (EOF) and the
    thread exits - it does not hang and does not read the new worker's stderr.
    """
    try:
        for raw in iter(worker.stderr.readline, b""):
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace").rstrip()
            logs.append(line)
            # The list grows without bound (NS_PHASE=1 adds a line per frame)
            # - every consumer reads only the tail, so we keep 2000.
            if len(logs) > 2000:
                del logs[: len(logs) - 2000]
            if _log_wanted(line):
                print(line)
    except Exception:
        pass


def start_worker(params: dict, width: int, height: int, warmup: int,
                 full_w: int = 0, full_h: int = 0,
                 shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str]]:
    """Start the NGX worker in --live mode and send the header.

    width/height is the work resolution (the NGX feature), full_w/full_h is
    the size of the input frames coming from Python (the worker resizes them
    on the GPU through NGX Upscaling; full_w=0 -> the old 1:1 mode).

    Returns (worker, logs, reader, stop): reader is the permanent stdout
    reader thread (see WorkerReader), stop is the event used to finish
    _drain_stderr on shutdown.
    """
    if not WORKER_EXE.is_file():
        raise FileNotFoundError(
            f"worker not found: {WORKER_EXE}\n"
            "Copy nvngx.dll (the built worker) and nvngx_dlssnr.dll into native/."
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    worker = subprocess.Popen(
        [str(WORKER_EXE), "--live"],
        cwd=str(NATIVE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    logs: list[str] = []
    stop = threading.Event()
    threading.Thread(target=_drain_stderr, args=(worker, logs, stop), daemon=True).start()
    # In upscale mode (full_w>0) the worker returns full-res frames - the
    # reader must expect the full sizes, otherwise byte_count will not match.
    out_w = full_w if full_w else width
    out_h = full_h if full_h else height
    reader = WorkerReader(worker, out_w, out_h, shm)

    header = struct.pack(
        HEADER_FMT,
        VIDEO_MAGIC, width, height, int(warmup), 0,  # frame_count=0 -> an endless loop
        params["profile"], params["preset"], params["style"],
        params["auto_mask"], params["ui_correction"],
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    )
    worker.stdin.write(header)
    worker.stdin.flush()
    if shm is not None:
        _negotiate_shm(worker, reader, shm)
    return worker, logs, reader, stop


def restart_worker(worker: subprocess.Popen, params: dict, width: int, height: int,
                   warmup: int, full_w: int = 0, full_h: int = 0,
                   stop: threading.Event | None = None,
                   shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str], WorkerReader, threading.Event]:
    """Restart the worker at a new resolution (a work_scale change).

    The worker creates the NGX feature from the header sizes and reads exactly
    w*h*4 bytes per frame - the resolution cannot be changed on the fly, only
    by a restart. Warmup on a restart is smaller (30) so the screen does not
    freeze.

    A 2 s pause between shutdown and start: the old worker holds the NGX GPU
    resources (nvngx_dlssnr.dll, 165 MB + a D3D12 device) - initialising a new
    process concurrently on the same GPU hangs or kills it (observed: exit 127
    and a hung recv after apply_settings).

    The old reader/drain die on EOF of the old worker's closed pipes
    (shutdown_worker terminates the process) - there is no read race with the
    new worker: the pipes are different and the old thread physically cannot
    read the stdout of the new process.
    """
    shutdown_worker(worker, stop)
    time.sleep(2.0)
    return start_worker(params, width, height, warmup, full_w, full_h, shm)


def shutdown_worker(worker: subprocess.Popen, stop: threading.Event | None = None) -> None:
    """Graceful shutdown: close stdin (EOF -> the worker exits with code 0), wait 10 s.

    stop is the finish event for _drain_stderr (from start_worker): it is set
    immediately so the drain thread does not hang on readline() of a closed
    stderr (on Windows closing a pipe from another thread does not wake
    readline - the thread exits only on EOF after the process dies, or on
    stop).
    """
    if worker.poll() is not None:
        if stop is not None:
            stop.set()
        return
    if stop is not None:
        stop.set()
    try:
        if worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
    except OSError:
        pass
    try:
        code = worker.wait(timeout=10)
        print(f"[main] worker exited cleanly (code {code})")
    except subprocess.TimeoutExpired:
        print("[main] worker did not exit within 10 s - forcing termination")
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()


# Applying settings is coalesced: a slider dragged across its range would
# otherwise restart the pipeline on every step. The intermediate values are
# dropped and only the last one is applied.
# 0.5 s rather than 2 s: the change goes through RNSZ inside the live worker
# process, not through a restart with an NGX init/shutdown plus sleep(2) -
# the expensive path is only a fallback now.
RESTART_COOLDOWN = 0.5  # seconds
RESTART_WARMUP = 10     # warmup after a resolution change (do not freeze the screen)
RACK_TIMEOUT = 20.0     # seconds to wait for RACK after RNSZ


# How many restarts in a row are allowed before NR is turned off: past this
# the worker is not coming back on its own, and spinning through restarts
# only keeps the screen frozen. The user gets an alert instead.
MAX_CONSECUTIVE_RESTARTS = 3
# A transient failure (no-frame, driver hiccup) gets ONE automatic revive
# after this backoff instead of leaving NR off until the user presses Num1.
# A hard failure (0xBAD00001) never auto-revives.
AUTO_REVIVE_BACKOFF = 30.0  # seconds


def teardown_pipeline(st) -> None:
    """Stop everything that is sized to the current width/height.

    Shared by the monitor switch and the window switch: the worker,
    the shared memory and a running recording are all built for one
    frame size and cannot survive a change of it.
    """
    if st.recorder is not None:
        try:
            st.recorder.close()
        except Exception as exc:
            print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
        st.recorder = None
    st.pending_shot = None
    shutdown_worker(st.worker, st.worker_stop)
    try:
        st.shm.close()
    except Exception:
        pass


def rebuild_pipeline(st, note: str) -> None:
    """Build the worker, the shm and the overlay for the current size.

    The second half of what used to be _switch_monitor: it reads
    st.width/height/work_w/work_h and rebuilds everything that depends
    on them, resetting the per-worker flags so the main loop
    negotiates DDA1/WGCW, GRAY, OUTS and the window again.
    """
    # Freeze the last picture with a spinner before the old worker
    # dies: the rebuild takes ~1 s (new worker, NGX warm-up) and the
    # bare desktop would flash underneath (user: mode-switch flashes).
    # The overlay spans the whole monitor even when the next mode is
    # one window - no bare desktop at the edges of the spinner.
    st.display.enter_switch_mode(st.output_rgba, *st.capture.resolution)
    menu_was_open = st.display.menu.visible
    full_w = st.width if (st.work_w != st.width or st.work_h != st.height) else 0
    full_h = st.height if (st.work_w != st.width or st.work_h != st.height) else 0
    st.shm = SharedFrameBuffer(st.width, st.height)
    st.worker, st.worker_logs, st.reader, st.worker_stop = start_worker(
        st.params, st.work_w, st.work_h, st.warmup, full_w, full_h, st.shm)
    # The window and the menu are rebuilt, keeping the user settings.
    # A soft resize instead of close()+recreate: the old code went
    # through pygame.quit() and built a fresh window - the screen went
    # black for a moment on every Num5 (user: screen flashes on mode
    # switches). The worker and the shm MUST be torn down and rebuilt
    # (new size), the SDL window does not have to be.
    recreated = False
    try:
        st.display.resize(st.width, st.height)
        recreated = False
    except Exception as exc:
        print(f"[main] soft resize failed ({exc}) - recreating the window")
        # The menu is recreated with the window: snapshot its live
        # state (position, scale, height) into cfg so the restore
        # below picks up where the user left it, not the stale
        # values from the last menu close (user rule 10.09: fixed
        # position until the user drags it).
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
        # A fresh window starts at (0,0) - the primary monitor. The origin
        # belongs to the CHOSEN monitor and must survive the rebuild.
        st.display.set_origin(*getattr(st, "mon_origin", (0, 0)))
        recreated = True
    # In one-window mode the overlay stops hiding from screen capture:
    # the input is that window, not the desktop, so there is no
    # self-capture loop to break - and an outside recorder can see the
    # result. The worker does the same for its picture window.
    st.display.set_excluded_from_capture(st.window_hwnd is None)
    st.display.set_lang(st.lang)
    st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))
    saved_theme = st.cfg.get("theme")
    if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
        st.display.menu.set_state({"theme": saved_theme})
    st.display.menu.set_state({"lang": st.lang})
    # The position/scale/height restore applies ONLY to a recreated
    # menu (the window was rebuilt). On a soft resize the menu is
    # alive and keeps exactly what the user set - re-applying the
    # cfg values here would snap it back to the last saved state on
    # every mode switch (user: menu returns to the launch position
    # and scale after picking a window).
    if recreated:
        st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
        saved_offset = st.cfg.get("menu_offset")
        if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
            st.display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
        saved_height = st.cfg.get("menu_height")
        if isinstance(saved_height, (int, float)) and saved_height > 0:
            st.display.menu.user_height = int(saved_height)
    if menu_was_open:
        st.display.menu.set_state(settings_io.menu_payload(st))
        st.display.menu.visible = True
        st.display.set_menu_opaque(True)
        st.display.set_menu_input(True)
        # The saved offset is honoured as-is: the panel stays where
        # the user left it, clamped to the screen by layout() (user
        # rule 10.09: fixed position until the user drags it).
        if st.window_hwnd is not None:
            st.display.set_fullscreen_layer(st.mon_w, st.mon_h)
    # guides and the buffers follow the new resolution.
    st.guides = TemporalGuideGenerator(st.work_w, st.work_h, emit_small=st.motion_small)
    st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
    # Pipeline flags - the new worker knows nothing.
    st.present_mode = False
    st.present_attempted = False
    st.dda_mode = False
    st.dda_attempted = False
    st.gray_active = False
    st.motion_small = False
    st.motion_attempted = False
    st.out_shm = False
    st.out_attempted = False
    st.gpu_ok = None  # a new worker means a new verdict on feature 18
    st.gpu_alerted = False           # and a fresh chance for the alert to speak
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None
    # The last NR frame belongs to the previous monitor and size.
    # Without the reset a screenshot right after the switch would
    # save it.
    st.output_rgba = None
    settings_io.save_menu_layout(st)
    print(f"[main] pipeline rebuilt: {st.width}x{st.height}, "
          f"work {st.work_w}x{st.work_h} - {note}")
    # The mode-change alert must survive a rebuild: the pipeline
    # teardown clears the alert list, and in a game the user has no
    # time to read a 2.5 s toast. 6 s is long enough to read while
    # the game keeps running (user: "the Num5 alert disappears too
    # fast").
    st.display.alert(note, duration=6.0)


def switch_monitor(st, new_monitor: int | str) -> None:
    """Switch the capture/output monitor - a full pipeline restart.

    The resolution, the capture, the window, the worker and the shm
    are all tied to the monitor - it cannot be switched on the fly.
    Recording stops (the frame size changes). The menu is recreated
    with its theme/language/layout preserved.

    new_monitor is the dxcam output index, or a DXGI devicename
    ('\\\\.\\DISPLAY1') - the menu hands over the devicename so the
    switch is by identity, not by position.
    """
    # Everything downstream of the size - the worker, the shm, the
    # overlay, the flags - is rebuilt by _rebuild_pipeline, which owns
    # those names; this function only picks the monitor and the size.
    if isinstance(new_monitor, str):
        resolved = resolve_output_idx(new_monitor)
        if resolved is None:
            print(f"[main] monitor {new_monitor!r} is not connected",
                  file=sys.stderr)
            return
        new_monitor = resolved
    if new_monitor == st.monitor:
        return
    print(f"[main] monitor change: {st.monitor} -> {new_monitor}")
    teardown_pipeline(st)
    try:
        st.capture.close()
    except Exception:
        pass
    # The new monitor: its real resolution, and where it sits.
    st.monitor = new_monitor
    st.cfg["monitor"] = st.monitor
    try:
        st.capture = ScreenCapture(monitor_idx=st.monitor)
    except Exception as exc:
        # The chosen output is gone (unplugged between the menu
        # render and the click, dock changed, driver reset) - the
        # capture must never take the app down. Fall back to the
        # primary output and tell the user.
        print(f"[main] monitor {st.monitor} failed to open: {exc}",
              file=sys.stderr)
        st.capture = ScreenCapture(monitor_idx=0)
        st.monitor = st.capture.monitor_idx
        st.cfg["monitor"] = st.monitor
        st.display.alert(UI_STRINGS[st.lang]["mon_fail"])
    st.width, st.height = st.capture.resolution
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale)
    # The worker reads NS_OUTPUT / NS_WINDOW_POS at every OpenDda/OpenPresent,
    # and the overlay is rebuilt below - so the new monitor's identity goes
    # out before the rebuild (issues #28, #33).
    from startup import _apply_monitor_env
    st.mon_origin = _apply_monitor_env(st.capture)
    st.display.set_origin(*st.mon_origin)
    rebuild_pipeline(st, f"Monitor {st.monitor}: {st.width}x{st.height}")


def switch_window(st, hwnd: int) -> None:
    """Point the capture at one window (hwnd) or back at the desktop (0).

    The window's capture size is not something to guess: GetWindowRect
    includes the invisible resize borders and the DWM frame, while the
    capture produces the compositor's own surface. So the running
    worker is asked first (WGCW answers with the real size), and the
    pipeline is rebuilt for exactly that.
    """
    if hwnd and not st.want_dda:
        st.display.alert(UI_STRINGS[st.lang]["win_fail"])
        print("[main] window mode needs capture in the worker "
              "(capture_in_worker is off)", file=sys.stderr)
        return
    # The switch overlay goes up BEFORE the probe: the probe can take
    # ~1 s (WGCW round trip with the running worker) and the old
    # pipeline is already dead by then - without the overlay the
    # desktop sits bare (user: black gap on one-window mode switch).
    # enter_switch_mode is idempotent and covers the rebuild too.
    st.display.enter_switch_mode(st.output_rgba, *st.capture.resolution)
    if hwnd:
        # A minimised window has nothing to capture: WGC would answer with
        # the last size it had and then go silent. The list offers them
        # (a menu showing one of five open programs reads as broken), so
        # picking one restores it first and lets the frame arrive.
        if ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)):
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
            time.sleep(0.25)  # the window has to be drawn before it is measured
        try:
            aw, ah = channels.probe_window_capture(st, hwnd)
        except Exception as exc:
            # The probe left the worker inside a WGCW session that
            # may be half-open: put the source back on the desktop
            # before bailing out (audit #4, F2).
            try:
                send_dda(st.worker, st.width, st.height)
            except Exception:
                pass
            st.display.alert(UI_STRINGS[st.lang]["win_fail"])
            print(f"[main] the worker cannot capture that window: {exc}",
                  file=sys.stderr)
            st.display.exit_switch_mode()  # the overlay was raised before the probe
            return
        if aw < 64 or ah < 64:
            # Below the work-resolution floor there is nothing to
            # process - and a work size larger than the frame is how
            # the worker gets killed. The probe above already switched
            # the worker's source to WGCW as a side effect: put it
            # back on the desktop, otherwise the frozen tiny window
            # becomes the picture until the next rebuild (audit #4,
            # F2).
            send_dda(st.worker, st.width, st.height)
            print(f"[main] the window is {aw}x{ah} - too small to process",
                  file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["win_fail"])
            st.display.exit_switch_mode()  # the overlay was raised before the probe
            return
        teardown_pipeline(st)
        st.window_hwnd = int(hwnd)
        st.width, st.height = int(aw), int(ah)
        note = UI_STRINGS[st.lang]["win_mode_on"]
    else:
        teardown_pipeline(st)
        st.window_hwnd = None
        st.width, st.height = st.capture.resolution
        note = UI_STRINGS[st.lang]["win_mode_off"]
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale)
    st.follow_pos = None        # a fresh overlay starts at (0,0)
    st.follow_resize = None
    # The window size this pipeline was built for, as WE measure it. It is
    # NOT st.width/height: that is the size the WORKER's capture reported,
    # and on Windows 10 the two are different numbers for the same window
    # (see follow_window).
    rect = window_frame_rect(hwnd) if hwnd else None
    st.follow_size = rect[2:] if rect else None
    rebuild_pipeline(st, note)


def apply_spout(st, enabled: bool) -> None:
    """Toggle the Spout2 bridge: the worker must be restarted.

    The bridge is initialised once inside the worker process
    (SpoutBridgeInit reads NS_SPOUT at startup) - there is no protocol
    message for it, so the only way in or out is a fresh worker. The
    same path the monitor switch takes: teardown, set the environment,
    rebuild. The picture size does not change, so the overlay, the
    menu and the capture source survive.
    """
    st.cfg["spout"] = bool(enabled)
    os.environ["NS_SPOUT"] = "1" if enabled else "0"
    settings_io.save_menu_layout(st)
    print(f"[main] Spout2 output: {'on' if enabled else 'off'} - restarting the worker")
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "spout_on" if enabled else "spout_off",
        "Spout2 output ON" if enabled else "Spout2 output OFF"))


def follow_monitor(st) -> None:
    """Rebuild when the desktop resolution changes under a running pipeline.

    Everything downstream of the size is built once: the worker's NGX
    feature, the shared memory, the overlay window. Nothing was watching
    the monitor itself, so switching the desktop from 1440p to 4K left the
    program processing a 2560x1440 island in the corner of a 4K screen,
    with the overlay unable to grow past its old bounds (user report).

    st.capture.resolution is no help - it is what the monitor was when the
    capture session opened. The size is asked of Windows directly, and the
    rebuild waits half a second for it to settle: a mode change goes
    through intermediate sizes, and rebuilding on each one would mean
    several worker restarts for one switch.

    Window mode is not affected: there the frame follows the window, and
    follow_window already owns that.
    """
    if st.window_hwnd is not None or st.worker_failed or not st.running:
        return
    size = monitor_size(st.capture.devicename)
    if size is None or size == (st.width, st.height):
        st.mon_resize = None
        return
    now = time.monotonic()
    if st.mon_resize is None or st.mon_resize[0] != size:
        st.mon_resize = (size, now)
        return
    if now - st.mon_resize[1] < 0.5:
        return
    st.mon_resize = None
    print(f"[main] the monitor is now {size[0]}x{size[1]} "
          f"(was {st.width}x{st.height}) - rebuilding the pipeline")
    teardown_pipeline(st)
    # The dxcam factory caches the outputs it enumerated at import, and a
    # mode change is exactly what makes that cache wrong - a fresh capture
    # built on it would come back at the old size.
    try:
        st.capture.close()
    except Exception:
        pass
    _refresh_dxcam_factory()
    try:
        st.capture = ScreenCapture(monitor_idx=st.monitor)
    except Exception as exc:
        print(f"[main] the capture did not survive the mode change: {exc}",
              file=sys.stderr)
        st.capture = ScreenCapture(monitor_idx=0)
        st.monitor = st.capture.monitor_idx
    st.width, st.height = st.capture.resolution
    st.mon_w, st.mon_h = st.width, st.height
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale)
    rebuild_pipeline(st, f"{st.width}x{st.height}")


def apply_gpu(st, index: int) -> None:
    """Run the worker on another card: a restart, like the Spout toggle.

    The adapter is chosen before the D3D12 device exists, so there is no
    way to move a running worker - NS_GPU is read once at process start.
    The frame size does not change, so the overlay, the menu and the
    capture source survive the restart.

    Both the network and the capture move together: the captured frame
    reaches D3D12 through an NT-shared texture, and a shared handle does
    not cross adapters. Choosing a card that drives no display therefore
    fails in the worker, with the reason in the log, rather than showing
    a black picture.
    """
    previous = int(st.cfg.get("gpu", 0))
    if int(index) == previous:
        return
    st.cfg["gpu"] = int(index)
    os.environ["NS_GPU"] = str(int(index))
    # If the chosen card turns out to drive no display, the capture stays on
    # the display card and every frame crosses through shared memory - a
    # split pipeline. That is worth one alert, and only after a deliberate
    # switch (on an Optimus laptop it is the normal state from launch).
    st.gpu_switch_pending = True
    print(f"[main] GPU: adapter {index} - restarting the worker")
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get("gpu_switched", "GPU switched"))
    if gpu_came_up(st):
        # Only now: a config that remembers a card the network cannot use
        # comes back on the same dead card at the next launch, and the menu
        # to change it back is inside the overlay that a dead worker hides
        # (issue #33).
        settings_io.save_menu_layout(st)
        return
    print(f"[main] adapter {index} cannot run the network - back to {previous}",
          file=sys.stderr)
    st.cfg["gpu"] = previous
    os.environ["NS_GPU"] = str(previous)
    st.gpu_switch_pending = False
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "gpu_reverted", "That GPU cannot run the neural pass - previous card"))
    st.display.alert(UI_STRINGS[st.lang].get(
        "gpu_reverted", "That GPU cannot run the neural pass - previous card"))


#: How long a fresh worker is given to say whether the network came up on
#: the card just chosen. NGX answers in well under a second; five seconds
#: is the margin for a cold driver, and a slower one is not called failed.
GPU_VERDICT_TIMEOUT = 5.0


def gpu_came_up(st, timeout: float = GPU_VERDICT_TIMEOUT) -> bool:
    """Did the network come up on the card the worker was just started on?

    The worker says so itself: "feature 18 ready" against "feature 18
    create failed" / "NGX unavailable" - the same lines refresh_gpu_ok
    reads - and a process that exited says it without words.

    Silence is NOT failure. A card that takes its time still works, and
    turning a slow start into an automatic revert would be worse than the
    bug this guards against.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = getattr(st, "worker", None)
        if worker is not None and worker.poll() is not None:
            return False
        for line in reversed(st.worker_logs[-120:]):
            if "feature 18 ready" in line:
                return True
            if ("feature 18 create failed" in line
                    or "NGX unavailable" in line
                    or "no NVIDIA adapter found" in line):
                return False
        time.sleep(0.1)
    return True


def follow_window(st) -> None:
    """Keep the HUD layer on the window being processed.

    Position every frame - it is one DWM call and a SetWindowPos only
    when the window actually moved. A SIZE change is a different
    animal: the worker, the shared memory and every texture are built
    for one frame size, so it means rebuilding the pipeline - and doing
    that on every pixel while someone drags a resize handle would be
    unusable. The new size has to hold still for half a second first.
    """
    if st.window_hwnd is None:
        return
    # The worker is dead: the overlay must stay hidden (issue #3) -
    # nothing would fill it, and showing it covers the desktop with
    # a black window.
    if st.worker_failed:
        return
    rect = window_frame_rect(st.window_hwnd)
    if rect is None:
        return
    x, y, w, h = rect
    if ctypes.windll.user32.IsIconic(ctypes.c_void_p(st.window_hwnd)):
        # Minimised: the capture goes silent (the worker hides its own
        # window for the same reason), so the HUD goes with it rather
        # than floating over whatever is underneath.
        if st.display.is_visible():
            st.display.set_visible(False)
            st.follow_pos = None
        return
    if not st.display.is_visible():
        st.display.set_visible(True)
    moved = (x, y) != st.follow_pos
    # While the menu is open the user may be dragging it by its title
    # bar - following the captured window would yank the HUD (and the
    # menu with it) back onto the window every frame, which is the
    # "does not grab, stutters, flickers" report. The position is
    # re-synced on the first frame after the menu closes.
    if moved and not st.display.menu.visible:
        st.display.move_to(x, y)
        st.follow_pos = (x, y)
    # Both windows are topmost, and within that group the one raised
    # last is on top. The worker re-asserts its picture window every
    # time the target moves, so the HUD has to keep coming back up -
    # otherwise the menu ends up UNDER the picture, invisible both to
    # the user and to a recorder. Measured: without this the menu
    # changed 0% of what an outside capture saw.
    if moved or st.frame_index % 30 == 0:
        st.display.raise_topmost()
    # Against the size WE measured when the pipeline was built - not
    # against st.width/height, which is what the worker's capture
    # reported. On Windows 10 those are two different numbers for one
    # window: WGC hands back the GetWindowRect size, including the
    # invisible resize border, while this is the DWM extended frame.
    # A user's log (issue #30) showed capture 1354x853 against frame
    # 1340x846 - 14 and 7 pixels of border - so the comparison was never
    # equal, the pipeline rebuilt every half second, and the screen blinked
    # once every two seconds until window mode was turned off. On Windows 11
    # the two agree, which is why it never showed up here.
    if st.follow_size is None:
        st.follow_size = (w, h)
    if (w, h) != st.follow_size:
        now = time.monotonic()
        if st.follow_resize is None or st.follow_resize[0] != (w, h):
            st.follow_resize = ((w, h), now)
        elif now - st.follow_resize[1] > 0.5:
            st.follow_resize = None
            print(f"[main] the window is now {w}x{h} - rebuilding the pipeline")
            switch_window(st, st.window_hwnd)
    else:
        st.follow_resize = None


def do_restart(st, new_scale: float, new_profile: str, new_params: dict,
                full: bool = False, new_small: bool | None = None) -> None:
    """Change work_scale/profile/parameters WITHOUT recreating pygame or the capture.

    The main path is RNSZ: the worker recreates the NGX feature inside
    the same process and answers RACK (~0.3 s instead of ~3 s for a
    restart). If RNSZ did not go through - a full restart of the worker
    process.

    CRITICAL: guides is recreated at the NEW work resolution and
    stored back into the state (st.guides). It used to be assigned to
    a local by mistake - the outer guides stayed at the old size and
    main sent motion of the old size, while the worker reads exactly
    new_w*new_h*4 bytes:
      * scaling up   -> the worker waits for the missing bytes and goes
        silent, main hangs in reader.recv(60 s), the window stops
        pumping messages -> Application Hang (Event Id 1002) -> exit 127;
      * scaling down -> the extra bytes desynchronise the stream, the
        worker sees a foreign magic and exits -> BrokenPipe -> a restart
        loop.
    Reproduced in isolation: _work/test_stale_motion_repro.py (case A -
    TimeoutError, B - BrokenPipeError, C - the control, OK).
    pygame/D3D11 had nothing to do with the crashes.
    """
    st.work_scale = new_scale
    st.cfg["profile"] = new_profile
    st.params = new_params
    if new_small is not None and new_small != st.nr_small:
        st.nr_small = new_small
        st.cfg["nr_small"] = st.nr_small
        # The environment is what a freshly started worker reads; the
        # live one is told through the resize below.
        os.environ["NS_NR_SMALL"] = "1" if st.nr_small else "0"
        settings_io.save_menu_layout(st)
    new_w, new_h = _work_size(st.width, st.height, st.work_scale)
    new_full_w = st.width if (new_w != st.width or new_h != st.height) else 0
    new_full_h = st.height if (new_w != st.width or new_h != st.height) else 0
    print(f"[main] applying: profile {new_profile!r}, "
          f"work_scale {st.work_scale:.2f} ({new_w}x{new_h}), params {st.params}")
    st.display.alert(UI_STRINGS[st.lang]["settings_applied"])

    applied = False
    if st.worker.poll() is None and not full:
        try:
            t_rnsz = time.perf_counter()
            send_resize(st.worker, st.params, new_w, new_h, RESTART_WARMUP,
                        new_full_w, new_full_h, st.nr_small)
            st.reader.wait_rack(timeout=RACK_TIMEOUT)
            st.reader.set_output_size(new_full_w or new_w, new_full_h or new_h)
            applied = True
            print(f"[main] RNSZ applied: {new_w}x{new_h} in "
                  f"{(time.perf_counter() - t_rnsz) * 1000:.0f} ms")
        except Exception as exc:
            print(f"[main] RNSZ did not go through ({exc}) - full worker restart",
                  file=sys.stderr)
    if not applied:
        st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
            st.worker, st.params, new_w, new_h, RESTART_WARMUP,
            new_full_w, new_full_h, st.worker_stop, st.shm)
        channels.forget_present(st)
        # The new worker knows nothing about DDA/gray: reset the flags
        # so the main loop sends DDA1/GRAY again. Otherwise the frames
        # go out with NO_COLOR to a worker that is not capturing - a
        # desync and a restart loop.
        channels.forget_dda(st)
        channels.forget_out(st)

    # The order matters: work_w/work_h and guides change TOGETHER,
    # otherwise the motion size drifts away from what the worker
    # expects (see the docstring).
    st.work_w, st.work_h = new_w, new_h
    st.guides = TemporalGuideGenerator(st.work_w, st.work_h, emit_small=st.motion_small)
    channels.sync_motion_size(st)  # the flow resolution may have changed
    channels.sync_gray(st)         # the gray channel lives in the worker, size = guides flow
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None  # the indices are reset - a fresh grab is needed
    st.tray._set_state(scale=st.work_scale)
    st.last_restart = time.monotonic()


def request_apply(st, new_scale: float, new_profile: str, new_params: dict,
                  new_small: bool | None = None) -> None:
    """Apply the settings with coalescing over RESTART_COOLDOWN.

    The single entry point for the settings window, the tray and the
    hotkeys: the cooldown check used to live only on the settings
    path, while the tray and the arrows called _do_restart directly -
    key repeat on an arrow produced a flood of RNSZ.
    """
    if time.monotonic() - st.last_restart < RESTART_COOLDOWN:
        st.pending_apply = (new_scale, new_profile, new_params, new_small)
        print(f"[main] apply deferred (cooldown {RESTART_COOLDOWN:.1f} s), "
              f"the last value will be applied")
    else:
        do_restart(st, new_scale, new_profile, new_params, new_small=new_small)
