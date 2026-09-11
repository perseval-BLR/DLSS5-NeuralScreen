"""What the user asked for, turned into something that happens.

Three ways in, one subject. The overlay menu reports an action and says
nothing about what it means; the tray, the taskbar button and the hotkeys all
push a word into one queue; and the screenshot flow starts with a click and
finishes several frames later with a file on disk. The decision in every case
is taken here, where the config, the params and the pipeline are all reachable.

The screenshot is the part worth explaining. GetSaveFileNameW is modal: run
from the main loop it freezes the overlay on the last frame, and with a
recording running that pause lands in the MP4 as a still, because the PTS
comes from the clock rather than from the frame count. So the dialog runs in
its own thread and the answer comes back through a queue the loop drains -
which is why opening the dialog and handling its answer are two functions and
not one.
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pygame  # the menu is baked into the screenshot

import channels
import dialogs
import pipeline
import settings_io
from hotkeys import build_bindings, parse_binding
from i18n import STRINGS as UI_STRINGS
from paths import BASE_DIR
from pipeline import restart_worker
from recorder import VideoRecorder
from settings_io import (CHANNEL_URL, PROFILES, REPO_URL, WORK_SCALE_MAX,
                         WORK_SCALE_MIN, WORK_SCALE_STEP,
                         _autostart_enabled, _next_preset_name,
                         _set_autostart, _work_size, hotkey_labels)
from winapi import window_frame_rect, window_under_cursor


def save_screenshot(st, path: Path, rgba) -> None:
    """Save the frame as a maximum-quality JPEG.

    An open menu ends up in the screenshot: our layer is excluded from
    capture, so we draw it onto the frame ourselves.
    """
    try:
        surf = pygame.image.frombuffer(
            rgba, (rgba.shape[1], rgba.shape[0]), "RGBX")
        st.display.draw_capture_overlay(surf)
    except Exception as exc:
        print(f"[main] menu was not baked into the screenshot: {exc}", file=sys.stderr)
    try:
        ok = dialogs.save_jpeg(path, rgba)
        if ok:
            print(f"[main] screenshot: {path}")
            st.display.alert(f"Screenshot: {path.name}")
        else:
            print(f"[main] failed to write the screenshot: {path}", file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["shot_fail"])
    except Exception as exc:
        print(f"[main] screenshot failed: {exc}", file=sys.stderr)
        st.display.alert(UI_STRINGS[st.lang]["shot_fail"])


def open_save_dialog(st) -> None:
    """Show "Save as" without stalling the pipeline.

    GetSaveFileNameW is modal: in the main loop it would freeze the
    overlay on the last frame, and with a recording running the pause
    over the dialog would land in the MP4 as a still (PTS comes from
    the clock). So the dialog lives in its own thread and the path
    comes back through a queue. A second dialog is not opened - one
    window is already up.

    A configured screenshot_dir is the folder the dialog opens in,
    not a replacement for it (issue #20).
    """
    if st.shot_dialog_open:
        return
    st.shot_dialog_open = True
    hwnd = st.display.get_hwnd()
    default_name = f"neuralscreen-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
    shot_dir = st.cfg.get("screenshot_dir")
    initial_dir = str(shot_dir) if isinstance(shot_dir, str) and shot_dir.strip() else None

    def _run() -> None:
        try:
            st.shot_paths.put(dialogs.ask_save_path(
                hwnd, default_name, initial_dir,
                fallback_dir=BASE_DIR / "screenshots"))
        except Exception as exc:
            print(f"[main] the save dialog crashed: {exc}", file=sys.stderr)
            st.shot_paths.put(None)

    threading.Thread(target=_run, name="save-dialog", daemon=True).start()


def drain_save_dialog(st) -> None:
    """Take the path from the dialog if the user has already answered."""
    try:
        while True:
            shot_path = st.shot_paths.get_nowait()
            st.shot_dialog_open = False
            if shot_path is None:
                print("[main] screenshot cancelled by the user")
                continue
            if shot_path.is_dir():
                # The folder picker answered: remember the folder
                # and let the next screenshot go there without a
                # dialog (issue #20).
                st.cfg["screenshot_dir"] = str(shot_path)
                settings_io.save_menu_layout(st)
                st.display.menu.set_state({"screenshot_dir": str(shot_path)})
                print(f"[main] screenshot folder -> {shot_path}")
                st.display.alert(f"Screenshot folder: {shot_path}")
                continue
            if st.present_mode:
                st.pending_shot = shot_path
                print(f"[main] screenshot from the next frame: {shot_path}")
            elif st.output_rgba is not None:
                save_screenshot(st, shot_path, st.output_rgba)
            else:
                st.display.alert("No frame yet")
    except queue.Empty:
        pass


def apply_menu_action(st, action: tuple) -> None:
    """A menu action -> a real setting.

    The menu changes nothing on its own: it reports what the user
    wants and the decision is taken here, where params and cfg live.
    """
    kind = action[0]
    if kind == "nr":
        st.tray_commands.put("toggle")
    elif kind == "nr_res":
        # One control, one meaning: how much resolution the network
        # sees. Above the cap there is nothing left to reduce, so that
        # end of the slider is "the whole screen" - which is the same
        # thing as the reduced mode being off.
        want = float(action[1])
        cap = settings_io.work_scale_cap(st)
        if want > cap + 1e-6:
            pipeline.request_apply(st, 1.0, st.cfg["profile"], st.params, new_small=False)
        else:
            pipeline.request_apply(st, want, st.cfg["profile"], st.params, new_small=True)
    elif kind == "split":
        # No need to recreate the worker: the wipe position rides in
        # every frame's header.
        st.split_pos = min(1.0, max(0.0, float(action[1])))
    elif kind == "toggle" and action[1] == "open_on_start":
        st.startup_menu = not st.startup_menu
        settings_io.save_menu_layout(st)
        print(f"[main] menu at startup: {'yes' if st.startup_menu else 'no'}")
    elif kind == "toggle" and action[1] == "autostart":
        # Autostart with Windows (HKCU Run). The state lives in the
        # registry, not in the config - read it and invert.
        new_state = not _autostart_enabled()
        if _set_autostart(new_state):
            print(f"[main] autostart with Windows: {'on' if new_state else 'off'}")
            st.display.alert(UI_STRINGS[st.lang].get(
                "autostart_on" if new_state else "autostart_off",
                "Autostart ON" if new_state else "Autostart OFF"))
        else:
            st.display.alert(UI_STRINGS[st.lang].get("autostart_err", "Autostart failed"))
    elif kind == "toggle" and action[1] == "rec_indicator":
        # The recording indicator outside the menu: a config flag,
        # the HUD reads it on every redraw.
        st.cfg["rec_indicator"] = not bool(st.cfg.get("rec_indicator", True))
        settings_io.save_menu_layout(st)
        print(f"[main] recording indicator: {'on' if st.cfg['rec_indicator'] else 'off'}")
    elif kind == "toggle" and action[1] == "spout":
        # The Spout2 bridge: the worker reads NS_SPOUT only at startup,
        # so the toggle goes through a worker restart (pipeline.apply_spout
        # owns the whole path, including the config write).
        pipeline.apply_spout(st, not bool(st.cfg.get("spout", False)))
    elif kind == "param":
        new_params = dict(st.params)
        new_params[action[1]] = float(action[2])
        pipeline.request_apply(st, st.work_scale, st.cfg["profile"], new_params)
    elif kind == "profile":
        if action[1] in PROFILES:
            pipeline.request_apply(st, st.work_scale, action[1], dict(PROFILES[action[1]]))
        elif action[1] in st.presets:
            pipeline.request_apply(st, st.work_scale, action[1], dict(st.presets[action[1]]))
        else:
            print(f"[main] unknown profile {action[1]!r} - ignored",
                  file=sys.stderr)
    elif kind == "lang":
        if action[1] in UI_STRINGS and action[1] != st.lang:
            st.lang = action[1]
            st.display.set_lang(st.lang)
            st.display.menu.set_state({"lang": st.lang})
            print(f"[main] interface language -> {st.lang}")
    elif kind == "capture":
        # While the menu waits for a keypress the global hotkeys must
        # be suspended: otherwise Num2 toggles the menu instead of
        # landing in the field.
        if action[1]:
            st.hotkeys.suspend()
        else:
            st.hotkeys.resume()
    elif kind == "hotkey":
        cmd, text = action[1], action[2]
        parsed = parse_binding(text)
        if parsed is None:
            print(f"[main] could not parse the combination {text!r}", file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["hotkey_bad"])
        else:
            over = st.cfg.get("hotkeys")
            over = dict(over) if isinstance(over, dict) else {}
            over[cmd] = text
            st.cfg["hotkeys"] = over
            st.hotkey_bindings = build_bindings(over)
            st.hotkeys.rebind(st.hotkey_bindings)
            st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))
            if not settings_io.save_hotkeys(st, over):
                # The assignment works for this session but will not
                # survive a restart - the user must know.
                st.display.alert(UI_STRINGS[st.lang]["save_fail"])
                return
            print(f"[main] {cmd} -> {text}")
            st.display.alert(UI_STRINGS[st.lang]["settings_applied"])
    elif kind == "theme":
        # The menu has already applied the theme to itself
        # (overlay_ui); here we only remember it for config.json -
        # settings_io.save_menu_layout(st) runs on menu close and on exit.
        print(f"[main] menu theme -> {action[1]}")
    elif kind == "monitor":
        # The value arrives as "N: WxH (\\\\.\\DISPLAY1)" - the
        # devicename is the identity, the index is only a label.
        try:
            new_monitor = str(action[1]).split(" (")[1].rstrip(")")
        except (ValueError, IndexError):
            print(f"[main] invalid monitor: {action[1]!r}", file=sys.stderr)
            return
        if new_monitor != st.capture.devicename:
            pipeline.switch_monitor(st, new_monitor)
    elif kind == "window":
        # The window list in the menu: the value is "hwnd: title".
        try:
            target = int(str(action[1]).split(":")[0], 16)
        except (ValueError, IndexError):
            print(f"[main] invalid window: {action[1]!r}", file=sys.stderr)
            return
        if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(target)):
            print(f"[main] the window 0x{target:X} is gone", file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["win_fail"])
            return
        # Bring the chosen window to the front: the capture follows
        # it, and a window buried under others would show through
        # the overlay as a half-covered picture (user: the chosen
        # window must come to the foreground, no overlaps).
        user32 = ctypes.windll.user32
        user32.BringWindowToTop(ctypes.c_void_p(target))
        user32.SetForegroundWindow(ctypes.c_void_p(target))
        print(f"[main] window mode on from the menu - target hwnd "
              f"0x{target:X}")
        pipeline.switch_window(st, target)
    elif kind == "button":
        name = action[1]
        if name == "close":
            st.display.menu.visible = False
            st.display.set_menu_opaque(False)
            st.display.set_menu_input(False)
            settings_io.save_menu_layout(st)
        elif name == "exit":
            print(f"[main] exit: button in the overlay menu "
                  f"(frames processed {st.frame_index})")
            st.running = False
        elif name == "record":
            st.tray_commands.put("record")
        elif name == "screenshot":
            st.tray_commands.put("screenshot_menu")
        elif name == "window_mode":
            # The fullscreen button in the footer: the same action
            # as the Num5 hotkey - in window mode it returns to the
            # whole screen, in fullscreen mode it is a no-op with an
            # alert (the user asked for a visible "already active").
            if st.window_hwnd is not None:
                print("[main] window mode off - back to the whole screen")
                pipeline.switch_window(st, 0)
            else:
                st.display.alert(UI_STRINGS[st.lang]["fs_active"])
        elif name == "shot_dir":
            # The screenshot folder picker (issue #20). The dialog
            # is modal, so it lives in its own thread; the chosen
            # folder comes back through the same queue as the save
            # dialog, and the config is written on the main thread.
            if st.shot_dialog_open:
                return
            st.shot_dialog_open = True
            hwnd = st.display.get_hwnd()  # captured here: pygame is not thread-safe

            def _pick_dir() -> None:
                # The picker blocks its thread; the answer
                # (or None on cancel) goes back through the
                # queue the main loop drains.
                st.shot_paths.put(dialogs.pick_directory(
                    hwnd, "Select the screenshot folder"))

            threading.Thread(target=_pick_dir, name="folder-picker",
                             daemon=True).start()
        elif name == "github":
            # The hotkeys, profiles and requirements are described
            # only in the README - there was no way to learn about
            # them from the program itself.
            try:
                webbrowser.open(REPO_URL)
                st.display.alert(UI_STRINGS[st.lang]["github_opened"])
            except Exception as exc:
                print(f"[main] could not open {REPO_URL}: {exc}",
                      file=sys.stderr)
        elif name == "save_preset":
            # The current slider values, snapshotted as a named
            # preset. The NGX plumbing of the active profile rides
            # along, so the preset reproduces the exact look it was
            # saved with.
            name = _next_preset_name(st.presets)
            st.presets[name] = dict(st.params)
            st.cfg["presets"] = st.presets
            if not settings_io.save_menu_layout(st):
                # The preset lives in memory but not on disk - the
                # user must know it will not survive a restart.
                del st.presets[name]
                st.cfg["presets"] = st.presets
                st.display.alert(UI_STRINGS[st.lang]["save_fail"])
                return
            st.display.menu.set_state(
                {"profiles": list(PROFILES) + list(st.presets)})
            print(f"[main] preset saved: {name}")
            st.display.alert(f"Preset saved: {name}")
        elif name == "delete_preset":
            # Only a user preset can be deleted - the built-in
            # profiles are not deletable.
            if st.cfg["profile"] in st.presets:
                del st.presets[st.cfg["profile"]]
                st.cfg["presets"] = st.presets
                if not settings_io.save_menu_layout(st):
                    st.display.alert(UI_STRINGS[st.lang]["save_fail"])
                    return
                st.display.menu.set_state(
                    {"profiles": list(PROFILES) + list(st.presets)})
                print(f"[main] preset deleted: {st.cfg['profile']}")
                st.display.alert(f"Preset deleted: {st.cfg['profile']}")
                pipeline.request_apply(st, st.work_scale, "Natural",
                              dict(PROFILES["Natural"]))
        elif name == "channel":
            # The channel label in the settings page opens the
            # channel (user rule 2026-09-08).
            try:
                webbrowser.open(CHANNEL_URL)
                st.display.alert(UI_STRINGS[st.lang]["github_opened"])
            except Exception as exc:
                print(f"[main] could not open {CHANNEL_URL}: {exc}",
                      file=sys.stderr)


def drain_commands(st) -> bool:
    """Handle the tray/hotkey commands; False when the program must quit.

    Called from the main loop AND from inside the recv wait: at 4K a
    heavy scene can take ~1 s per NGX frame, and the hotkeys must
    stay responsive while main waits for the worker (user: "NR toggle
    does not always fire in Cyberpunk").
    """
    try:
        while True:
            cmd = st.tray_commands.get_nowait()
            if cmd == "quit":
                print(f"[main] exit: tray or the quit hotkey "
                      f"(frames processed {st.frame_index})")
                st.running = False
            elif cmd == "settings":
                # Num2 and a left click on the tray open the overlay
                # menu - the only place the settings live.
                st.display.menu.set_state(settings_io.menu_payload(st))
                opened = st.display.menu.toggle()
                st.display.set_menu_opaque(opened)
                st.display.set_menu_input(opened)
                if opened:
                    # In one-window mode the HUD layer is the size of
                    # the captured window - a menu near the edge would
                    # be clipped by it. Expand the layer to the whole
                    # monitor while the menu is open, so the menu is
                    # always fully visible (user: menu lost outside a
                    # small window). The saved offset is honoured -
                    # layout() clamps it to the screen (user rule
                    # 10.09: fixed position until the user drags it).
                    if st.window_hwnd is not None:
                        st.display.set_fullscreen_layer(st.mon_w, st.mon_h)
                    # The mouse lands on the title bar, so the user
                    # does not have to hunt for the pointer (user
                    # request). The layout must be current for the
                    # title rect to be valid.
                    try:
                        st.display.menu.layout(
                            st.display.screen.get_width(),
                            st.display.screen.get_height())
                        cx, cy = st.display.menu.title_center()
                        ctypes.windll.user32.SetCursorPos(cx, cy)
                    except Exception:
                        pass
                else:
                    # The menu closed: put the HUD layer back on the
                    # captured window.
                    if st.window_hwnd is not None:
                        rect = window_frame_rect(st.window_hwnd)
                        if rect is not None:
                            st.display.set_window_layer(*rect)
                    settings_io.save_menu_layout(st)
                print(f"[main] overlay menu {'opened' if opened else 'closed'}")
            elif cmd == "toggle":
                st.paused = not st.paused
                if not st.paused:
                    st.work_frame = None  # a fresh grab after the pause
                    if st.worker_failed:
                        # The worker died and was shut down (issue #3):
                        # revive it - a fresh process may succeed (a
                        # transient GPU conflict, a driver hiccup).
                        st.worker_failed = False
                        print("[main] reviving the worker after the failure")
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
                        except Exception as exc:
                            print(f"[main] worker revive failed ({exc}) - "
                                  f"staying NR OFF", file=sys.stderr)
                            st.paused = True
                            st.worker_failed = True
                    st.display.set_visible(True)
                print(f"[main] NR {'OFF (bypass NGX)' if st.paused else 'ON'}")
                st.display.alert(UI_STRINGS[st.lang]["nr_off" if st.paused else "nr_on"])
                st.tray._set_state(nr=not st.paused)
            elif cmd == "screenshot_menu":
                open_save_dialog(st)
            elif cmd == "record":
                # Num0: record the NR frame into an MP4. The frames
                # are requested from the worker through
                # FRAME_FLAG_WANT_PIXELS (the screenshot mechanism,
                # but for every recorded frame).
                if st.recorder is None:
                    rec_dir = BASE_DIR / "recordings"
                    rec_dir.mkdir(exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    # Two recordings within one second must not
                    # overwrite each other - we add milliseconds.
                    stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
                    path = str(rec_dir / f"neuralscreen-{stamp}.mp4")
                    try:
                        # 30 fps, not 60: every recorded frame is a
                        # full 33 MB round-trip from the worker
                        # (FRAME_FLAG_WANT_PIXELS -> pipe), and the
                        # measurement showed 60 fps recording costs
                        # ~36% of the FPS (101 -> 65). Halving the
                        # frame rate halves that cost; the picture
                        # quality per frame is identical.
                        st.recorder = VideoRecorder(path, st.width, st.height, fps=30,
                                                 audio=st.record_audio)
                    except Exception as exc:
                        print(f"[main] recording did not start: {exc}", file=sys.stderr)
                        st.display.alert(f"REC ERROR: {exc}")
                        st.recorder = None
                    else:
                        print(f"[main] recording started: {path}")
                        st.display.alert(UI_STRINGS[st.lang]["record_on"])
                else:
                    rec_path = st.recorder.path
                    try:
                        st.recorder.close()
                    except Exception as exc:
                        print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
                        st.display.alert(UI_STRINGS[st.lang]["rec_save_fail"])
                    secs = st.recorder.duration_ms / 1000.0
                    print(f"[main] recording finished: {rec_path} "
                          f"({st.recorder.written} frames, {secs:.1f}s)")
                    st.display.alert(UI_STRINGS[st.lang]["record_off"])
                    st.recorder = None
            elif cmd == "window_mode":
                # The window under the cursor wins: it works on the
                # desktop too (the focused window there is Progman,
                # which is not capturable), and it is what the user
                # is looking at. Fall back to the last focused
                # foreign window when the cursor is over nothing
                # capturable (our own overlay, the desktop).
                if st.window_hwnd is not None:
                    print("[main] window mode off - back to the whole screen")
                    pipeline.switch_window(st, 0)
                else:
                    target = window_under_cursor() or st.last_foreground
                    if target:
                        print(f"[main] window mode on - target hwnd "
                              f"0x{target:X}")
                        pipeline.switch_window(st, target)
                    else:
                        # Nothing but our own windows has had the
                        # focus, so there is nothing to capture but
                        # ourselves.
                        print("[main] window mode: no window to capture "
                              "(only our own windows have had the focus)",
                              file=sys.stderr)
                        st.display.alert(UI_STRINGS[st.lang]["win_none"])
            elif cmd in ("scale_up", "scale_down"):
                delta = WORK_SCALE_STEP if cmd == "scale_up" else -WORK_SCALE_STEP
                new_scale = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, st.work_scale + delta))
                if abs(new_scale - st.work_scale) > 1e-6:
                    new_w, new_h = _work_size(st.width, st.height, new_scale)
                    print(f"[main] work_scale -> {new_scale:.2f} ({new_w}x{new_h})")
                    st.display.alert(UI_STRINGS[st.lang]["work_scale_changed"].format(new_scale, new_w, new_h))
                    pipeline.request_apply(st, new_scale, st.cfg["profile"], st.params)
    except queue.Empty:
        pass
    return st.running
