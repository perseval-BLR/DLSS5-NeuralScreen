"""Display module for the DLSS 5 Desktop NR prototype.

Borderless fullscreen window for the frame + the in-overlay settings
menu. The window is hidden at creation and revealed on the first real
frame (no blank flash at launch); it is excluded from screen capture
via SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) so that
screen-capture tools (dxcam, OBS) do not see it. Mode switches (Num5,
monitor change) are covered by a semi-transparent blur + spinner
overlay (enter_switch_mode / exit_switch_mode).

Usage:
    disp = Display(2560, 1440)
    disp.set_hud({"fps": 60, "status": "NR ON", "resolution": "2560x1440",
                  "params": {"intensity": 0.5, "local_tone": 0.3,
                             "local_structure": 0.7}, "frames": 1234})
    while True:
        disp.show(frame_bgra)
        for ev in disp.poll_events():
            if ev == "quit":
                break
    disp.close()
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from ctypes import wintypes
from typing import Dict, List

# --- argtypes for user32: WITHOUT them ctypes passes ints as a 32-bit c_int.
# HWND_TOPMOST=-1 turns into 0xFFFFFFFF instead of 0xFFFFFFFFFFFFFFFF and
# SetWindowPos silently FAILS (ret=0) on 64-bit Windows - topmost is not
# applied. hwnd (64-bit) would also be truncated at values >= 2^31.
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


CURSOR_SHOWING = 0x00000001


def system_cursor_visible() -> bool:
    """Is there a mouse pointer on screen right now? (Nothing uses this yet.)

    Kept for the open problem it belongs to: a fullscreen game hides the
    cursor and our menu is then unusable. Drawing our own pointer was tried
    and reverted - it produced a SECOND pointer in real use, which is worse
    than none. See "The menu pointer is missing or frozen" in the README.

    A fullscreen game hides it (ShowCursor(FALSE) on its own input queue) and
    it stays hidden while our menu is up. When it is gone the overlay draws
    its own; when it is there, drawing one would mean two pointers.

    Unknown counts as visible: a missing answer must not put a second pointer
    on a normal desktop.
    """
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    try:
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return True
    except Exception:
        return True
    return bool(ci.flags & CURSOR_SHOWING)

# DPI-aware BEFORE import pygame: on load SDL freezes the process awareness,
# and a later SetProcessDpiAwarenessContext no longer takes effect (it returns
# an error). Without this Windows scales the window: 125% -> 3072x1728 instead
# of 3840x2160 and the frame no longer matches the screen. Verified by
# diagnostics.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import numpy as np
import pygame

import fonts
from overlay_ui import OverlayMenu, palette as ui_palette

from i18n import STRINGS

# --- Brand palette (DLSS5-Video-Converter) -------------------------------
BG_COLOR = (0x0D, 0x11, 0x17)      # #0D1117 dark background
BG_ALPHA = 235                     # HUD panel translucency (nearly opaque, so text stays readable)
SWITCH_ALPHA = 170                 # mode-switch overlay: the live desktop shows through it
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Chroma key for HUD mode: pixels of exactly this colour are not drawn at all
# by the layered window, and the worker's overlay shows through them. Picked so
# it cannot occur in the HUD palette (#0D1117 / #FFBF00 / #E6EDF3 / #8B949E).
CHROMA_KEY = (0xFF, 0x00, 0xFF)
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2

# Which faces the program draws with lives in fonts.py - including the
# per-script CJK families, re-exported here because the docs renderer and
# the offscreen menu renderer import them from this module.
CJK_FONTS = fonts.CJK_FONTS
# The base layout sizes are set for 1440p. On taller screens the interface is
# scaled up, on shorter ones it stays as is: there is nowhere left to shrink to,
# the text would become unreadable. Hence the "up only" rule (see ui_scale).
FONT_SIZE = 18
UI_BASE_HEIGHT = 1800
ALERT_FONT_SIZE = 28


def ui_scale_for(height: int) -> float:
    """Interface multiplier derived from the screen height.

    Up only: 1.0 at 1800p and below, 1.2 at 4K. Scaling proportionally down at
    1080p would give a nine-pixel font, so we never scale down. The base was
    raised from 1440 to 1800 - at 1.5 the interface came out too large.
    """
    return max(1.0, float(height) / UI_BASE_HEIGHT)


class Display:
    """Fullscreen borderless window that renders frames + branded HUD."""

    def __init__(self, width: int, height: int, fullscreen: bool = True, click_through: bool = True):
        # DPI awareness is already set at module level (before import pygame).
        # Calling it again here has no effect - it is kept as a fallback for
        # cases where the module is imported without the top block.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        # SDL hints BEFORE pygame.init():
        # SDL_WINDOWS_DPI_AWARENESS=permonitorv2 - without it Windows scales
        # the window (125% -> 3072x1728 instead of 3840x2160).
        # SDL_MOUSE_FOCUS_CLICKTHROUGH=1 - a click on the window is not
        # intercepted by SDL (the window is not activated, focus is not taken
        # away), so clicks reach the applications underneath the overlay.
        try:
            os.environ["SDL_WINDOWS_DPI_AWARENESS"] = "permonitorv2"
        except Exception:
            pass
        try:
            os.environ["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"
        except Exception:
            pass
        pygame.init()
        pygame.display.set_caption("NeuralScreen")
        # Borderless windowed instead of FULLSCREEN: a pygame fullscreen window
        # loses its rendering on click/focus (the screen freezes while the loop
        # keeps spinning). A window the size of the monitor at position (0,0)
        # looks the same but is stable, and click-through works.
        flags = pygame.NOFRAME
        self._flags = flags
        # pygame.HIDDEN (128, SDL_WINDOW_HIDDEN): create the window invisible.
        # NOTE: the raw SDL flag 0x8 is IGNORED by pygame 2.6 (verified
        # experimentally) - the window comes up visible. pygame.HIDDEN works;
        # the explicit SW_HIDE below is a belt-and-suspenders fallback. Shown
        # only once the first real frame arrives - otherwise a blank window
        # sits over the desktop during the NGX warm-up (user: screen flashes
        # on startup / on mode switches because a new window pops up empty).
        hidden = pygame.NOFRAME | pygame.HIDDEN
        self.screen = pygame.display.set_mode((width, height), hidden)
        self.width, self.height = self.screen.get_size()
        # The window starts hidden; reveal() shows it after the first real
        # frame. set_visible/is_visible interplay: is_visible() is consulted
        # by _follow_window before any show/hide decision, so the initial
        # state must match the real (hidden) window.
        self._visible = False
        self._reveal_pending = True
        # pygame 2.6 ignores SDL_WINDOW_HIDDEN (verified experimentally: the
        # window is VISIBLE right after set_mode with the 0x8 flag) - hide it
        # explicitly or a blank window flashes over the desktop during the
        # NGX warm-up (user: translucent/blank flash on startup).
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass
        # Where this monitor's top-left corner is on the virtual desktop.
        # (0,0) is the primary monitor; a second one can sit anywhere. Set
        # through set_origin() once the monitor is known (main owns that).
        self._origin = (0, 0)
        self._move_to_origin()
        # Force the physical window size: even if DPI awareness did not apply
        # (a 3072x1728 window instead of 3840x2160), we stretch the window to
        # the requested size so the pygame surface matches.
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0004)  # SWP_NOZORDER
        except Exception:
            pass
        self._set_topmost()
        self.clock = pygame.time.Clock()
        self._hud: Dict = {}
        self._alerts: List[tuple[str, float]] = []  # (text, expires_at)
        # Interface scale and the layout sizes derived from it.
        self.ui_scale = ui_scale_for(self.height)
        self.font_size = max(8, int(round(FONT_SIZE * self.ui_scale)))
        # HUD language (NR ON/NR OFF), see set_lang(). Must exist before the
        # menu: the loader picks the face by the language, and OverlayMenu
        # builds its fonts inside its constructor - with this assignment
        # below the menu, that first build fell through to pygame's default
        # face instead of ours.
        self._lang = "ru"
        # The settings menu lives in this same layer. A separate window on top
        # of the game would steal focus and fight for topmost, whereas here we
        # are already above the frame and already transparent by key.
        self.menu = OverlayMenu(self.ui_scale, self._load_font)
        # The HUD is nothing but readings - fps, resolution, frame counter -
        # so it takes the monospaced face whole; the menu picks per element.
        self._font = self._load_font(size=self.font_size, mono=True)
        self._alert_font = self._load_font(
            size=max(10, int(round(ALERT_FONT_SIZE * self.ui_scale))))
        # Disable vsync: flip() must not wait for vblank (otherwise the FPS is
        # tied to the monitor refresh rate and frames are lost on a slow
        # pipeline).
        try:
            pygame.display.set_swap_interval(0)
        except Exception:
            pass
        self._excluded = self._exclude_from_capture()
        self._click_through = False
        self._menu_input = False
        if click_through:
            self._set_click_through()
        # NOTE: _visible/_reveal_pending are set right after set_mode - the
        # window starts hidden and reveal() shows it after the first frame.
        # HUD mode: the worker draws the frame, the window shows only the HUD
        self._hud_only = False
        self._last_overlay = 0.0
        self._last_alert_count = 0
        # The mode-switch overlay (blur + spinner): shown while the pipeline
        # is rebuilt and the new worker warms up, so the screen does not sit
        # bare for a second on every Num5 (user: mode-switch flashes).
        self._switch_active = False
        self._switch_bg: "pygame.Surface | None" = None
        self._switch_dim: "pygame.Surface | None" = None
        self._switch_t0 = 0.0
        self._switch_ret = (0, 0)  # layer size to restore on exit
        self._switch_pending = None  # deferred resize (w, h) while active

    def _sync_cursor(self) -> None:
        """Cursor over the menu area: move and resize arrows.

        Set only on change - calling set_cursor every frame makes the cursor
        flicker noticeably.
        """
        want = (self.menu.desired_cursor if self.menu.visible
                else pygame.SYSTEM_CURSOR_ARROW)
        if want == getattr(self, "_cursor", None):
            return
        self._cursor = want
        try:
            pygame.mouse.set_cursor(want)
        except Exception:
            pass

    @property
    def theme(self) -> dict:
        """The menu theme palette - alerts are kept in the same look."""
        return ui_palette(self.menu.state.get("theme", "light"))

    def get_hwnd(self) -> int:
        """HWND of the overlay window - the parent for native dialogs."""
        try:
            return int(pygame.display.get_wm_info()["window"])
        except Exception:
            return 0

    @staticmethod
    def _rgb(color: str) -> tuple[int, int, int]:
        c = color.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    def set_lang(self, lang: str) -> None:
        """Switch the HUD status language (en/ru/fr/...)."""
        if lang in STRINGS and lang != self._lang:
            self._lang = lang
            # The HUD/alert fonts follow the language: CJK scripts have no
            # glyphs in the default font (tofu boxes).
            self._font = self._load_font(size=self.font_size, mono=True)
            self._alert_font = self._load_font(size=ALERT_FONT_SIZE)

    def set_visible(self, visible: bool) -> None:
        """Show/hide the window (SW_SHOW/SW_HIDE).

        With NR OFF the window is hidden completely so the desktop does not
        slow down (no capture/blit/flip). With NR ON it is shown again.
        """
        if visible and self._reveal_pending:
            # The first frame has not arrived yet (the window is hidden on
            # purpose - SDL_WINDOW_HIDDEN at creation to avoid the blank
            # flash). _follow_window would happily show it early; only
            # reveal() may show the window for the first time.
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32.ShowWindow(hwnd, 5 if visible else 0)  # SW_SHOW=5, SW_HIDE=0
            self._visible = visible
        except Exception:
            pass

    def reveal(self) -> None:
        """Show the window once a real frame has arrived.

        The window is created hidden (SDL_WINDOW_HIDDEN) to avoid a blank
        flash over the desktop during the NGX warm-up. Called after the
        first successful frame exchange in main; idempotent - after the
        first call the window is simply visible and the mode switches go
        through set_visible() (which hides it for minimised windows etc).
        """
        if self._reveal_pending:
            try:
                hwnd = pygame.display.get_wm_info()["window"]
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                self._visible = True
            finally:
                self._reveal_pending = False

    def is_visible(self) -> bool:
        return getattr(self, "_visible", True)

    def _set_topmost(self) -> None:
        """HWND_TOPMOST - the window always sits above the rest (overlay)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002)  # SWP_NOSIZE|NOMOVE
        except Exception:
            pass

    def move_to(self, x: int, y: int) -> None:
        """Put the overlay's top-left corner at (x, y) on the desktop.

        The one-window mode needs it: the overlay is the size of the window
        being processed and has to sit exactly on it. Topmost is reasserted on
        the way (the insert-after argument), so a game raising itself does not
        end up above the HUD.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            # SWP_NOSIZE | SWP_NOACTIVATE - move only, never take the focus.
            user32.SetWindowPos(hwnd, -1, int(x), int(y), 0, 0, 0x0001 | 0x0010)
        except Exception as exc:
            print(f"Display: WARNING could not move the overlay: {exc}")

    def set_origin(self, x: int, y: int) -> None:
        """Where this monitor's top-left corner sits on the virtual desktop.

        The overlay is the size of ONE monitor; only the primary has its
        corner at (0,0). A window created at (0,0) while the capture runs
        on a second monitor covers the PRIMARY screen - the user sees
        nothing where they are looking (issues #28, #33).
        """
        self._origin = (int(x), int(y))
        self._move_to_origin()

    def _move_to_origin(self) -> None:
        """Move the window to the monitor's top-left corner (self._origin)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            x, y = getattr(self, "_origin", (0, 0))
            # NO SWP_SHOWWINDOW here: the window is created hidden
            # (SDL_WINDOW_HIDDEN) and revealed only after the first real
            # frame - otherwise the blank window flashes over the desktop
            # during the NGX warm-up.
            flags = 0x0001 | 0x0010  # SWP_NOSIZE | SWP_NOACTIVATE
            if (x, y) == (0, 0):
                # The primary monitor: SDL already placed it there, and the
                # pre-multi-monitor behaviour (no move at all) stays intact.
                flags |= 0x0002  # SWP_NOMOVE
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, flags)
        except Exception:
            pass

    # -- window plumbing --------------------------------------------------

    def _load_font(self, size: int = FONT_SIZE, mono: bool = False,
                   bold: bool = False):
        """The loader handed to the menu - see fonts.py for the faces."""
        return fonts.load(size, mono=mono, bold=bold,
                          lang=getattr(self, "_lang", "en"))

    def _set_click_through(self) -> bool:
        """Click-through: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED.

        Verified by diagnostics (_work/diag_click.py, Win11 4K): WS_EX_TRANSPARENT
        WITHOUT WS_EX_LAYERED does NOT work - WindowFromPoint still returns the
        overlay and the click goes to the overlay. The working combination (per
        MSDN "Layered Windows"): a layered window + WS_EX_TRANSPARENT -> hit
        testing ignores the window shape and clicks go to the window under the
        cursor.

        The order is mandatory:
          1. SetWindowLongW(GWL_EXSTYLE, ... | WS_EX_LAYERED | WS_EX_TRANSPARENT
             | WS_EX_NOACTIVATE)
          2. SetLayeredWindowAttributes(alpha=255, LWA_ALPHA) - activates
             layered mode (without the call the window stays ordinary: the
             LAYERED flag is in exstyle but hit testing does not change)
          3. SetWindowPos(SWP_FRAMECHANGED) - drops the window style cache
             (required by the SetWindowLongW documentation)
        WS_EX_NOACTIVATE: the window does not take focus, the keyboard stays
        with the active application. Control is via global hotkeys
        (GetAsyncKeyState in main.py).
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOOLWINDOW = 0x00000080
            LWA_ALPHA = 0x2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                                  | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
            # Activate layered mode: alpha=255 (an opaque window, only hit
            # testing changes, visually we touch nothing)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            # Drop the style cache - without SWP_FRAMECHANGED the
            # SetWindowLongW changes may not take effect
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            self._click_through = True
            return True
        except Exception as exc:
            print(f"Display: WARNING click-through failed: {exc}")
            return False

    def _exclude_from_capture(self) -> bool:
        """SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on the window.

        Makes the window invisible to screen capture (dxcam, OBS, etc.).
        Best-effort: warn on failure, never crash.
        """
        return self.set_excluded_from_capture(True)

    def set_excluded_from_capture(self, hide: bool) -> bool:
        """Hide the overlay from screen capture, or stop hiding it.

        Hiding is mandatory while the input is Desktop Duplication of the whole
        screen: without it the pipeline would capture its own output. With one
        window as the input there is no such loop, and then hiding is pure
        loss - it is what stops OBS from seeing the overlay and stops the
        NVIDIA App from recording at all.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32 = ctypes.windll.user32
            want = WDA_EXCLUDEFROMCAPTURE if hide else 0  # 0 = WDA_NONE
            ok = user32.SetWindowDisplayAffinity(hwnd, want)
            if not ok:
                print(f"Display: WARNING SetWindowDisplayAffinity({want}) failed "
                      f"(the overlay may be visible to screen capture)")
                return False
            self._excluded = bool(hide)
            return True
        except Exception as exc:
            print(f"Display: WARNING cannot change the capture affinity: {exc}")
            return False

    # -- public API -------------------------------------------------------

    def set_menu_input(self, enabled: bool) -> None:
        """Whether input should reach our layer (while the menu is open).

        Normally the window is click-through: WS_EX_TRANSPARENT gives clicks to
        whatever is underneath. While the menu is up the flag is removed - the
        clicks are ours and the game under the overlay does not get them.
        Exactly the ReShade behaviour. WS_EX_NOACTIVATE is removed too,
        otherwise there is no keyboard.
        """
        self._menu_input = bool(enabled)
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING no hwnd for menu input: {exc}")
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        # WS_EX_TOOLWINDOW stays on in both states: the overlay is an
        # instrument window, not an application - it must never create a
        # second taskbar button / Alt+Tab entry next to the 1x1 APPWINDOW
        # button (user: two thumbnails in the taskbar, a narrow settings one
        # and the big overlay one).
        style |= WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        if enabled:
            # Focus is needed for the keyboard. The mouse works without it -
            # the click goes to the window under the cursor now that it is no
            # longer transparent. SetForegroundWindow alone is refused when
            # the foreground window belongs to another process that has not
            # received input from the user (a game in the foreground): the
            # system blocks the steal. AttachThreadInput is the standard
            # workaround - it makes the foreground thread share its input
            # state with ours, so the activation is treated as user-initiated.
            try:
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None)
                my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                if fg_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(my_tid, fg_tid, True)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                if fg_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
            except Exception:
                pass
        self._click_through = not enabled

    def resize(self, w: int, h: int) -> None:
        """Resize the HUD layer WITHOUT destroying the SDL window.

        pygame.display.set_mode() on the same display reuses the existing
        window (a soft resize), which is what a one-window mode switch needs:
        the old code went through display.close() -> pygame.quit() and rebuilt
        the whole window from scratch - the screen went black for a moment on
        every Num5 (user: screen flashes on mode switches).

        set_mode returns a NEW surface - it must become self.screen,
        otherwise main keeps drawing on the old (window-sized) surface and
        the layer never actually resizes. set_mode alone does NOT resize the
        physical window in SDL2, so the caller forces it with SetWindowPos.
        The recreated window loses EVERYTHING (layered attributes, capture
        affinity, input styles) - restore them here, once.

        While the mode-switch overlay is up the resize is DEFERRED: the
        overlay spans the full monitor on purpose (no bare desktop around the
        spinner), and exit_switch_mode() applies the pipeline size when the
        overlay comes down.
        """
        if self._switch_active:
            self._switch_pending = (w, h)
            return
        self.screen = pygame.display.set_mode((w, h), self._flags)
        self.width, self.height = w, h
        # A set_mode can recreate the physical window; put it back on the
        # chosen monitor (the origin belongs to the pipeline, not to SDL).
        self._move_to_origin()
        self.set_hud_only(self._hud_only, force=True)
        self.set_menu_opaque(self.menu.visible)
        self.set_excluded_from_capture(self._excluded)
        self.set_menu_input(self._menu_input)

    def set_fullscreen_layer(self, full_w: int, full_h: int) -> None:
        """Expand the HUD layer to the whole screen (menu open in window mode).

        In one-window mode the layer is the size of the captured window, so a
        menu dragged near the edge would be clipped by the window bounds. While
        the menu is open the layer is expanded to the full screen - the menu
        is then always fully visible - and set_window_layer() puts it back on
        the window when the menu closes. The capture affinity is re-applied
        because the window is recreated (user: menu lost outside a small
        window).

        While the mode-switch overlay is up this is a NO-OP: the layer was
        already expanded to the full screen by enter_switch_mode, and the
        layering attributes (LWA_COLORKEY/ALPHA) belong to the veil until
        exit_switch_mode re-applies the pipeline's ones - tearing them down
        mid-spinner turns the translucent veil opaque (audit M1).
        """
        if self._switch_active:
            return
        try:
            # set_mode returns a NEW surface - it must become self.screen,
            # otherwise main keeps drawing the menu on the old (window-sized)
            # surface and the layer never actually expands (user: the menu
            # was clipped and could not be dragged above the captured window).
            self.screen = pygame.display.set_mode((full_w, full_h), self._flags)
            self.width, self.height = full_w, full_h
            # set_mode alone does NOT resize the physical window in SDL2 -
            # force it, exactly like __init__ does (SWP_NOZORDER, with size),
            # at the chosen monitor's origin.
            hwnd = pygame.display.get_wm_info()['window']
            x, y = getattr(self, "_origin", (0, 0))
            user32.SetWindowPos(hwnd, 0, x, y, full_w, full_h, 0x0004)
            self._set_topmost()
            # The recreated window lost EVERYTHING: the layered attributes
            # (colorkey + alpha), the capture affinity and the input styles.
            self.set_hud_only(self._hud_only, force=True)
            self.set_menu_opaque(self.menu.visible)
            self.set_excluded_from_capture(self._excluded)
            self.set_menu_input(self._menu_input)
        except Exception as exc:
            print(f'Display: WARNING cannot expand the layer: {exc}')

    def set_window_layer(self, x: int, y: int, w: int, h: int) -> None:
        """Shrink the HUD layer back onto the captured window."""
        try:
            self.resize(w, h)
            hwnd = pygame.display.get_wm_info()['window']
            # Force the physical size AND the position in one call (set_mode
            # alone does not resize the window in SDL2).
            user32.SetWindowPos(hwnd, -1, int(x), int(y), w, h, 0x0010)
        except Exception as exc:
            print(f'Display: WARNING cannot shrink the layer: {exc}')

    def set_menu_opaque(self, opaque: bool) -> None:
        """Drop the global window translucency while the menu is open.

        The layer lives at BG_ALPHA so the HUD does not plaster over the
        picture. But the menu panel is nearly black and a bright frame shows
        through it - it reads as "too transparent". While the menu is up we set
        255.
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        alpha = 255 if opaque else BG_ALPHA
        user32.SetLayeredWindowAttributes(hwnd, key, alpha, LWA_COLORKEY | LWA_ALPHA)

    def set_hud_only(self, enabled: bool, force: bool = False) -> None:
        """HUD mode: the worker draws the frame in its own window, only the HUD
        stays here.

        The background is filled with CHROMA_KEY and made transparent through
        SetLayeredWindowAttributes(LWA_COLORKEY) - the worker's overlay shows
        through it, while the HUD, alerts and watermark are drawn on top as
        before. Turning it off restores the ordinary opaque mode (LWA_ALPHA).
        force=True: reapply the attributes even when the mode did not change -
        z-order operations (SetWindowPos/TopMost after the settings menu) can
        drop LWA_COLORKEY, and an early return would leave the window opaque.
        """
        if enabled == self._hud_only and not force:
            return
        self._hud_only = enabled
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING cannot get hwnd for HUD mode: {exc}")
            return
        if enabled:
            # COLORREF is 0x00BBGGRR, not RGB
            r, g, b = CHROMA_KEY
            key = (b << 16) | (g << 8) | r
            # The panel translucency comes from the window's GLOBAL alpha
            # (LWA_ALPHA), NOT from the alpha of the panel pixels: a
            # semi-transparent SRCALPHA blend with the magenta background would
            # give a colour != key and a pink slab (the colour key does not cut
            # out a blended colour). The colour key removes the background
            # entirely, and the opaque panel (plus text) becomes slightly
            # see-through through the global alpha.
            ok = user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)
        else:
            ok = user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        if not ok:
            print(f"Display: WARNING SetLayeredWindowAttributes failed "
                  f"(HUD mode {'on' if enabled else 'off'})")
        self._last_overlay = 0.0  # the next draw_overlay redraws immediately

    def raise_topmost(self) -> None:
        """Raise the window above the worker's window.

        Both windows are topmost, and inside that group the one raised last
        ends up on top. The worker creates its window after ours, so after
        every raise of its overlay the HUD has to be brought back up, otherwise
        it ends up under the frame and becomes invisible.
        """
        # SWP_NOACTIVATE: the 30-frame re-assert must not steal the keyboard
        # focus back from the user (audit 10.09 F2: with the menu open the
        # overlay has WS_EX_NOACTIVATE removed, and a SetWindowPos that
        # activates re-steals focus <=0.5 s after Alt+Tab / minimizing
        # another window).
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass

    def enter_switch_mode(self, last_frame: "np.ndarray | None" = None,
                          full_w: int = 0, full_h: int = 0) -> None:
        """Freeze a blurred copy of the screen with a spinner on top.

        Called when the pipeline is being rebuilt (window-mode switch, monitor
        change, resolution change): the previous worker dies, the next one
        warms up for ~1 s and the desktop would sit bare underneath. The layer
        is expanded to the full screen (full_w/full_h) and made opaque - the
        blur of the last frame (darkened) plus a spinner hide the gap (user:
        mode-switch flashes black/translucent).
        """
        if self._switch_active:
            return
        cw, ch = self.screen.get_size()
        fw = full_w if full_w > 0 else cw
        fh = full_h if full_h > 0 else ch
        self._switch_active = True
        self._switch_t0 = time.monotonic()
        self._switch_ret = (cw, ch)
        print(f"[main] switch overlay ON (layer {cw}x{ch} -> {fw}x{fh}, "
              f"frame={'yes' if last_frame is not None else 'none'})")
        try:
            bg = None
            if last_frame is not None:
                frame = pygame.image.frombuffer(
                    last_frame, (last_frame.shape[1], last_frame.shape[0]), "RGBX")
                # Blur cheaply: downscale to a small target in one step,
                # upscale back. The downscale ratio sets the blur radius.
                small = pygame.transform.smoothscale(
                    frame, (max(8, fw // 3), max(8, fh // 3)))
                bg = pygame.transform.smoothscale(small, (fw, fh))
            if bg is not None:
                # Darken the frozen frame: the pipeline is being rebuilt, the
                # picture is stale. A 40% veil reads as "transition" rather
                # than "frozen desktop".
                dim = bg.copy()
                dim.fill((96, 96, 96), special_flags=pygame.BLEND_RGB_MULT)
                self._switch_dim = dim
        except Exception:
            self._switch_dim = None
        # Expand the layer to the full screen. The overlay is SEMI-transparent
        # (LWA_ALPHA): the live desktop stays visible behind it, so when no
        # frozen frame exists (WNDO mode - the pixels never come to Python)
        # the user sees the desktop through a light veil instead of a black
        # screen (user: the switch overlay must not go dark). The window is
        # set directly - set_hud_only() would rewrite self._hud_only, which
        # must keep the pipeline's state for exit_switch_mode().
        try:
            self.screen = pygame.display.set_mode((fw, fh), self._flags)
            hwnd = pygame.display.get_wm_info()["window"]
            x, y = getattr(self, "_origin", (0, 0))
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, fw, fh, 0x0004)
            self._set_topmost()
            user32.SetLayeredWindowAttributes(hwnd, 0, SWITCH_ALPHA, LWA_ALPHA)
            # set_mode re-created the window: every exstyle bit is gone
            # (TOOLWINDOW/TRANSPARENT/NOACTIVATE/LAYERED). Re-assert them or
            # the overlay gets a taskbar thumbnail again and eats clicks
            # during the switch (audit 10.09: enter/exit_switch_mode was the
            # only re-creation path that never restored the styles - a
            # fullscreen->fullscreen Num5 with the menu closed lost them
            # until the next menu open or pipeline resize).
            self.set_menu_input(self._menu_input)
        except Exception as exc:
            print(f"Display: WARNING cannot set the switch overlay up: {exc}")
        self.draw_overlay(0.0)

    def exit_switch_mode(self) -> None:
        """Drop the switch overlay - the first frame of the new pipeline is
        on its way (the caller shows it right after)."""
        if not self._switch_active:
            return
        print("[main] switch overlay OFF")
        self._switch_active = False
        self._switch_bg = None
        self._switch_dim = None
        # The resize that was deferred while the overlay was up (the pipeline
        # rebuilt underneath, the window size changed); apply it now, before
        # the layered attributes are re-asserted. NOT while the menu is open:
        # the menu keeps the layer expanded to the full screen
        # (set_fullscreen_layer owns the size in that state) - shrinking it
        # here is exactly the clipped-menu regression (user: menu cut off
        # near the middle after a window-mode switch).
        #
        # A pending resize WINS over the restore-to-_switch_ret: on a monitor
        # switch the pending size IS the new monitor's resolution (the overlay
        # was expanded to it by enter_switch_mode) - falling through to the
        # else would shrink the layer back to the OLD monitor size and leave
        # stale geometry for the whole session (audit C1).
        pending = self._switch_pending
        self._switch_pending = None
        if pending is not None and not self.menu.visible and \
                (self.screen.get_width(), self.screen.get_height()) != pending:
            self.resize(pending[0], pending[1])
        elif pending is None:
            ret = self._switch_ret
            # The layer was expanded to the full screen for the overlay; put
            # it back on the size the pipeline expects when no explicit
            # resize came in (resize also re-applies the layered attributes
            # and the capture affinity). Still not while the menu is open -
            # the same clipped-menu rule as above.
            if not self.menu.visible and \
                    (self.screen.get_width(), self.screen.get_height()) != ret:
                self.resize(ret[0], ret[1])
        # The layer is back to its regular appearance: transparent for the HUD
        # mode, or an opaque fullscreen layer, whatever the pipeline wants.
        self.set_hud_only(self._hud_only, force=True)
        if not self._hud_only:
            self.set_menu_opaque(self.menu.visible)
        self._last_overlay = 0.0  # the next draw_overlay redraws immediately

    def is_switch_active(self) -> bool:
        """True while the mode-switch overlay (blur + spinner) is up."""
        return self._switch_active

    def _draw_switch(self) -> None:
        """Blit the frozen blurred frame and the spinner (time-based)."""
        w, h = self.screen.get_size()
        if self._switch_dim is not None:
            # The frozen blurred frame, darkened slightly. The window itself
            # is translucent (SWITCH_ALPHA), so the live desktop also shows
            # through it - and with a real frame there is no black at all.
            self.screen.blit(self._switch_dim, (0, 0))
        else:
            # No frozen frame (WNDO mode - pixels never reach Python): fill
            # with a light semi-opaque veil, NOT black - the live desktop
            # keeps showing through (user: the switch overlay is too dark).
            self.screen.fill((36, 34, 38))
        cx, cy = w // 2, h // 2
        r = 44  # spinner radius (in 4K pixels, scaled down via ui_scale)
        r = int(r * self.ui_scale)
        t = time.monotonic() - self._switch_t0
        # 12 dots around a circle, a comet: the leading dot (i=0, same angle
        # as the bright head below) is the brightest, the trail fades out
        # towards i=11. One full turn per ~1.2 seconds.
        for i in range(12):
            ang = i * (2 * math.pi / 12) + t * 1.6
            dx = math.cos(ang) * r
            dy = math.sin(ang) * r
            # Brightness falls off with the distance BEHIND the head: i=0 at
            # the head's angle is full, i=11 (the tail) is dark.
            shade = max(0, min(255, int(255 * (1 - i / 12))))
            pygame.draw.circle(self.screen, (shade, shade, shade),
                               (cx + int(dx), cy + int(dy)), max(4, r // 10))
        # A bright leading dot on top.
        ang = t * 1.6
        pygame.draw.circle(self.screen, (255, 255, 255),
                           (cx + int(math.cos(ang) * r),
                            cy + int(math.sin(ang) * r)),
                           max(5, r // 8))


    def refresh_colorkey(self) -> None:
        """Reapply LWA_COLORKEY on the pygame window (the HUD layer).

        Needed after operations that can drop the window's layered attributes
        (z-order shuffling with the tkinter settings menu: the window stays
        opaque and the HUD is not visible). Recreates nothing - only
        SetLayeredWindowAttributes, unlike set_hud_only(force=True).
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)

    def draw_capture_overlay(self, surface: pygame.Surface) -> None:
        """Bake the open menu into a recorded or screenshot frame.

        The frame is grabbed WITHOUT our pygame layer: the window is marked
        WDA_EXCLUDEFROMCAPTURE, otherwise DDA would capture our own output.
        So everything that must end up in the file is drawn here, on top of the
        pixels already received.

        We bake the menu only. The HUD and the watermark used to be baked too,
        but they are not on screen - in the file they looked like someone
        else's caption.
        """
        if not self.menu.visible:
            return
        # The "grabbed an edge" highlight is cursor state and has no place in
        # the file: it would freeze on the frame for no visible reason.
        hover, self.menu.hover = self.menu.hover, None
        try:
            self.menu.set_stats(self._hud)
            self.menu.draw(surface)
        finally:
            self.menu.hover = hover

    def draw_overlay(self, min_interval: float = 0.1) -> None:
        """Redraw the HUD over the frame the worker is showing.

        Throttling: a 4K fill plus flip costs ~5 ms while the HUD changes a
        couple of times per second - doing it every frame is wasted time.
        Alerts appear and disappear out of band, so for them the redraw is
        immediate.
        """
        try:
            pygame.event.pump()
        except Exception:
            pass
        now = time.monotonic()
        alerts = len(self._alerts)
        # With the menu open throttling is disabled: 10 Hz is enough for a
        # static HUD, but a slider under the mouse jitters at that rate.
        if self.menu.visible:
            min_interval = 0.0
        if now - self._last_overlay < min_interval and alerts == self._last_alert_count:
            return
        self._last_overlay = now
        self._last_alert_count = alerts
        if self._switch_active:
            # The switch overlay replaces everything else: no menu, no HUD,
            # no cursor - just the frozen frame and the spinner. The spinner
            # animates, so the throttle must not skip the redraw.
            self._draw_switch()
            pygame.display.flip()
            return
        self.screen.fill(CHROMA_KEY)
        self._draw_alerts()
        self._draw_rec_indicator()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._draw_window_highlight()
        self._sync_cursor()
        pygame.display.flip()

    def _draw_window_highlight(self) -> None:
        """The amber outline around the window hovered in the windows page.

        The menu reports the hwnd under the cursor (menu.hover_window);
        the frame is drawn in the overlay's own coordinates, so it sits on
        top of everything, exactly like the menu itself. The outline is
        amber (#FFBF00, the brand accent), 3 px, with a 1 px dark inner
        line so it reads on both light and dark windows.
        """
        hwnd = getattr(self.menu, "hover_window", None)
        if not hwnd:
            return
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return
            r = pygame.Rect(rect.left, rect.top,
                            rect.right - rect.left, rect.bottom - rect.top)
            lw = 3
            pygame.draw.rect(self.screen, (0x0D, 0x11, 0x17), r, lw + 2,
                             border_radius=4)
            pygame.draw.rect(self.screen, (0xFF, 0xBF, 0x00), r, lw,
                             border_radius=4)
        except Exception:
            pass

    def set_hud(self, data: dict) -> None:
        """Update HUD data: fps, status, resolution, params, frames."""
        self._hud = dict(data)

    def alert(self, text: str, duration: float = 2.5) -> None:
        """Show a pop-up alert centred on the screen (amber border)."""
        self._alerts.append((text, time.monotonic() + duration))

    def show(self, frame_rgba: np.ndarray) -> None:
        """Blit frame (RGBA uint8) fullscreen and draw the HUD on top.

        The frame: pygame.image.frombuffer(frame_rgba, ..., "RGBX") is
        zero-copy (the surface references the numpy buffer, without tobytes and
        a 33 MB copy at 4K). The RGBX format (32-bit, no alpha channel) blits
        to the screen faster than an RGBA surface with SRCALPHA (no alpha
        blending needed). The frame's alpha is unused - the frame is opaque
        (A=255).

        IMPORTANT: frombuffer does NOT copy the data - the surface lives as
        long as the numpy buffer does. The frame arrives from WorkerReader as a
        separate array per frame, so the surface is recreated each time (2 ms
        at 4K). event.pump() drains the Windows message queue (WM_PAINT and
        friends) - without it the window freezes on click/focus.
        """
        if frame_rgba is None:
            return
        try:
            pygame.event.pump()
        except Exception:
            pass
        try:
            surface = pygame.image.frombuffer(
                frame_rgba, (frame_rgba.shape[1], frame_rgba.shape[0]), "RGBX")
        except Exception:
            # Fallback: not a numpy array / incompatible shape - via tobytes
            surface = pygame.image.frombuffer(
                frame_rgba.tobytes(), (frame_rgba.shape[1], frame_rgba.shape[0]),
                "RGBA")
        # One blit+flip for both paths (the main RGBX and the RGBA fallback)
        self.screen.blit(surface, (0, 0))
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        pygame.display.flip()

    def poll_events(self) -> List[str]:
        """Return event names: 'quit' (Esc / window close), 'toggle' (Num1/F10).

        This is the local path, for when our own window has the focus - the
        global hotkeys are RegisterHotKey in hotkeys.py. Num1 matches the
        default binding; F10 stays as a local-path compatibility key.
        """
        events = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                events.append("quit")
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    events.append("quit")
                elif event.key in (pygame.K_KP1, pygame.K_F10):
                    events.append("toggle")
        return events

    def close(self) -> None:
        pygame.quit()

    # -- HUD --------------------------------------------------------------

    def _draw_rec_indicator(self) -> None:
        """The recording indicator outside the menu: a red dot + timer.

        The menu shows the REC cell, but with the menu closed there was
        no sign that a recording is running (user 5080 request). The
        indicator lives in the top-right corner of the HUD layer, drawn
        over the worker's window. It is hidden from the recorded frame
        itself: draw_capture_overlay bakes only the menu, and the HUD
        layer is excluded from the capture - the indicator is a live
        status, not part of the file.

        The badge is OPAQUE: the HUD window carries a global alpha
        (LWA_ALPHA), so a bare dot and text blend with the worker's
        frame underneath and read as a translucent raspberry smear. A
        solid badge (like the alerts) keeps the colours true.
        """
        if not self._hud.get("recording"):
            return
        if not self._hud.get("rec_indicator", True):
            return
        c = self.theme
        secs = float(self._hud.get("rec_seconds", 0.0))
        text = f"REC {int(secs) // 60:d}:{int(secs) % 60:02d}"
        surf = self._font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(10 * self.ui_scale))
        pad_y = int(round(6 * self.ui_scale))
        r = int(round(5 * self.ui_scale))
        dot_d = r * 2 + int(round(4 * self.ui_scale))
        w = dot_d + pad_x + surf.get_width() + pad_x
        h = surf.get_height() + pad_y * 2
        x = self.width - w - int(round(14 * self.ui_scale))
        y = int(round(14 * self.ui_scale))
        rect = pygame.Rect(x, y, w, h)
        radius = int(round(8 * self.ui_scale))
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect,
                         border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        # The dot pulses: a static dot is easy to miss, a blinking one
        # is what every recorder does.
        if int(time.monotonic() * 2) % 2 == 0:
            cy = rect.y + rect.h // 2
            pygame.draw.circle(self.screen, self._rgb(c["danger"]),
                               (rect.x + pad_x + r, cy), r)
        self.screen.blit(surf, (rect.x + dot_d + pad_x,
                                rect.y + pad_y))

    def _draw_alerts(self) -> None:
        """Pop-up alert: the menu palette, top centre.

        It used to hang a third of the way down, centred, as a dark slab with
        an amber border - it clashed with the overall look and got into the
        middle of the frame.
        """
        now = time.monotonic()
        self._alerts = [(text, expires) for text, expires in self._alerts if expires > now]
        if not self._alerts:
            return
        c = self.theme
        text, _ = self._alerts[-1]
        surf = self._alert_font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(26 * self.ui_scale))
        pad_y = int(round(14 * self.ui_scale))
        w = surf.get_width() + pad_x * 2
        h = surf.get_height() + pad_y * 2
        rect = pygame.Rect((self.width - w) // 2, int(round(self.height * 0.045)), w, h)
        radius = int(round(10 * self.ui_scale))
        # The panel is opaque: translucency would blend with the chroma key and
        # give a dirty tint (the colour key does not cut out a blended colour).
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect, border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        self.screen.blit(surf, (rect.x + pad_x, rect.y + pad_y))

def main() -> int:
    """Standalone smoke test: gradient frames + HUD until Esc/Num1."""
    disp = Display(2560, 1440)
    disp.set_hud({
        "fps": 60.0,
        "status": "NR ON",
        "resolution": "2560x1440",
        "params": {"intensity": 0.5, "local_tone": 0.3,
                   "local_structure": 0.7},
        "frames": 0,
    })
    h, w = disp.height, disp.width
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 0] = (xx * 255 // max(w - 1, 1)).astype(np.uint8)   # B
    frame[..., 1] = (yy * 255 // max(h - 1, 1)).astype(np.uint8)   # G
    frame[..., 2] = 40                                              # R
    frame[..., 3] = 255                                             # A
    n = 0
    running = True
    while running:
        disp.show(frame)
        for ev in disp.poll_events():
            if ev == "quit":
                running = False
        n += 1
        disp.set_hud({"fps": disp.clock.get_fps(), "status": "NR ON",
                      "resolution": f"{w}x{h}",
                      "params": {"intensity": 0.5, "local_tone": 0.3,
                                 "local_structure": 0.7},
                      "frames": n})
        disp.clock.tick(60)
    disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
