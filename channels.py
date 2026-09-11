"""The worker's side-channels: open one, remember that it is open.

Every channel here has the same shape - send a command, wait for its
acknowledgement, set a flag, and treat a refusal as "carry on without it".
None of them is fatal: the pipeline has a slower path for each one and says
so in the log.

    present    the worker shows the frame in its own window (WNDO)
    dda        the worker captures the screen itself (DDA1)
    wgc        the worker captures ONE window (WGCW)
    gray       the worker writes luminance back for the flow guides (GRAY)
    motion     the motion field travels at flow size, upscaled on the GPU
    out_shm    the result pixels come back through a section, not the pipe

They all take the pipeline state, because the worker, the reader and the
shared memory are REBOUND on every restart - a module that kept its own
references would be one restart away from writing into a dead pipe.

`*_attempted` is not `*_mode`: it means "this worker was already asked, do
not ask again every frame". A restart clears it, which is what the forget_*
functions are for.
"""
from __future__ import annotations

import ctypes
import sys

from i18n import STRINGS as UI_STRINGS
from protocol import (send_dda, send_gray, send_motion_size, send_out,
                      send_wgc, send_window)


def enable_out_shm(st) -> None:
    """OUTS: agree that the result pixels will go through a section.

    Called after every worker start: the command lives inside its
    process and a new one knows nothing about it. A refusal is not
    fatal - the pixels travel down the pipe as before.
    """
    st.out_attempted = True
    if not st.want_out_shm:
        return
    try:
        st.shm.open_out(st.width, st.height)
        send_out(st.worker, st.width, st.height, st.shm.out_name)
        st.reader.wait_oak(timeout=15.0)
        st.out_shm = True
        print(f"[main] result pixels through shared memory "
              f"({st.width}x{st.height}, {st.shm.out_bytes / 1024 / 1024:.0f} MB)")
    except Exception as exc:
        st.out_shm = False
        print(f"[main] shared memory for pixels unavailable ({exc}) - "
              f"they go through the pipe", file=sys.stderr)


def sync_motion_size(st) -> None:
    """MOTS: agree the motion field resolution with the worker.

    Called after guides is created and after every worker start: the
    command lives inside the worker process and a new one knows
    nothing about it. A refusal is not fatal - we do the upscale on
    the CPU, as before.
    """
    st.motion_attempted = True
    if not st.want_motion_small:
        return
    st.guides.emit_small = True
    try:
        send_motion_size(st.worker, st.guides.motion_width, st.guides.motion_height)
        st.reader.wait_mack(timeout=15.0)
        st.motion_small = True
        print(f"[main] motion field {st.guides.motion_width}x{st.guides.motion_height} - "
              f"upscaled by the worker on the GPU")
    except Exception as exc:
        st.guides.emit_small = False
        st.motion_small = False
        print(f"[main] GPU motion upscale unavailable ({exc}) - doing it on the CPU",
              file=sys.stderr)


def enable_present(st) -> None:
    """Ask the worker to present the frame itself (WNDO).

    A refusal is not fatal: we stay on returning pixels to Python and
    drawing them in pygame - that path has not gone anywhere.
    """
    st.present_attempted = True
    try:
        send_window(st.worker, st.width, st.height, 0)
        st.reader.wait_wack(timeout=15.0)
        st.present_mode = True
        st.display.set_hud_only(True)
        st.display.raise_topmost()  # the HUD must be ABOVE the worker's window
        print("[main] presenting in the worker window: no frame comes back to Python")
    except Exception as exc:
        st.present_mode = False
        st.display.set_hud_only(False)
        print(f"[main] worker window unavailable ({exc}) - output through pygame",
              file=sys.stderr)


def forget_present(st) -> None:
    """The worker restarted - its window and settings died with the process."""
    st.present_mode = False
    st.present_attempted = False
    st.motion_small = False
    st.motion_attempted = False
    st.display.set_hud_only(False)


def sync_gray(st) -> None:
    """GRAY: renegotiate the reverse luminance channel for guides.

    The worker writes exactly the flow size of guides into the
    mapping. The channel changes together with guides (flow may
    change after RNSZ), so a resync is needed in enable_dda and
    after apply. A refusal is not fatal - guides stay on dxcam.
    """
    if not st.dda_mode:
        return
    try:
        gw, gh = st.guides.flow_width, st.guides.flow_height
        st.shm.open_gray(gw, gh)
        send_gray(st.worker, gw, gh, st.shm.gray_name)
        st.reader.wait_gak(timeout=15.0)
        st.gray_active = True
        print(f"[main] gray channel {gw}x{gh}: guides take luminance from the worker")
    except Exception as exc:
        st.gray_active = False
        print(f"[main] gray channel unavailable ({exc}) - guides through dxcam",
              file=sys.stderr)


def enable_dda(st) -> None:
    """Ask the worker to capture the screen itself (DDA1).

    While it is active FRM1 frames carry FRAME_FLAG_NO_COLOR - no
    colour goes down the pipe, the worker takes it from Desktop
    Duplication straight on the GPU. Together with DDA we activate
    the reverse gray channel: the worker writes luminance there (the
    flow field size), guides read it and no longer depend on dxcam.
    A refusal is not fatal: we stay on sending frames from Python.
    """
    st.dda_attempted = True
    try:
        # In DDA mode guides still need the frame (motion), so dxcam
        # keeps running - we simply stop sending colour to the worker.
        send_dda(st.worker, st.width, st.height, 0)
        st.reader.wait_dack(timeout=15.0)
        st.dda_mode = True
        st.gpu_switch_pending = False  # the card runs capture - nothing is split
        sync_gray(st)
        print("[main] screen capture inside the worker (DDA1): no colour through the pipe")
    except Exception as exc:
        st.dda_mode = False
        print(f"[main] capture inside the worker unavailable ({exc}) - frames through Python",
              file=sys.stderr)
        # The chosen card could not open a capture session - it drives no
        # display. The pipeline is SPLIT now: the network runs on the chosen
        # card while the capture stays on the display card and every frame
        # crosses through shared memory. In issue #29 exactly this happened
        # after a GPU switch and nothing on the screen said so.
        if st.gpu_switch_pending:
            st.gpu_switch_pending = False
            st.display.alert(UI_STRINGS[st.lang].get(
                "gpu_split", "The chosen card drives no display - the capture stays on the display card"))


def enable_wgc(st) -> bool:
    """Ask the worker to capture the target WINDOW (WGCW).

    The same deal as DDA1 - the colour stops going down the pipe and the
    reverse gray channel feeds the guides - except the source is one window,
    which is why the overlay does not have to hide from screen capture.

    False means the window is not usable any more: closed, minimised, or the
    worker refused it. Rebuilding the pipeline for the whole screen is the
    caller's business - this module opens channels, it does not decide what
    the program shows, and that callback into the pipeline was the one thing
    keeping this code inside main().
    """
    st.dda_attempted = True
    if st.window_hwnd is None:
        return True
    if not ctypes.windll.user32.IsWindow(st.window_hwnd):
        print("[main] the captured window is gone - back to full screen",
              file=sys.stderr)
        return False
    try:
        send_wgc(st.worker, st.window_hwnd)
        aw, ah = st.reader.wait_wgak(timeout=15.0)
        st.dda_mode = True
        sync_gray(st)
        print(f"[main] window capture inside the worker (WGCW): "
              f"{aw}x{ah}, no colour through the pipe")
        return True
    except Exception as exc:
        st.dda_mode = False
        print(f"[main] window capture unavailable ({exc}) - back to full screen",
              file=sys.stderr)
        st.display.alert(UI_STRINGS[st.lang]["win_fail"])
        return False


def forget_dda(st) -> None:
    """The worker restarted - its DDA capture died with the process."""
    st.dda_mode = False
    st.dda_attempted = False
    st.gray_active = False


def forget_out(st) -> None:
    """The worker restarted - it knows nothing about the OUTS section."""
    st.out_shm = False
    st.out_attempted = False


def probe_window_capture(st, hwnd: int) -> tuple:
    """Ask the CURRENT worker for the capture size of a window.

    It switches that worker's source as a side effect, which is
    harmless: the caller tears it down immediately afterwards.
    """
    send_wgc(st.worker, hwnd)
    return st.reader.wait_wgak(timeout=15.0)
