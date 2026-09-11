"""OverlayMenu - the settings menu right inside the overlay layer, like ReShade.

It is drawn on the same pygame surface as the HUD and the alerts, so there is
still a single window: no fight over topmost and focus with the game.

Separation of duties: this module BUILDS THE LAYOUT and DRAWS it. It does not
touch pygame.display, does not read events and knows nothing about the worker.
The layout is a flat list of items with rectangles, and the same list serves as
the hit table for the mouse (hit()).

The layout is line-based: every control has its own label line and its own line
for the control itself. They used to share one line and the label ran into the
value and the arrows.

All sizes are given in 1440p base units and multiplied by scale - the same
factor the HUD uses (display.ui_scale_for).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pygame

from i18n import STRINGS

# --- Themes. The accent is shared; background and text change ------------
THEMES = {
    "light": {
        "bg": "#F0EEE6",       # warm cream panel background
        "surface": "#E8E5DC",  # slider tracks, fields
        "border": "#DCD8CC",
        "text": "#191919",
        "muted": "#79776F",
        "accent": "#D97757",   # clay accent
        "ok": "#5E8C61",       # green of the support indicator
        "danger": "#BC4C2E",
    },
    "dark": {
        "bg": "#262624",
        "surface": "#32312E",
        "border": "#403E3A",
        "text": "#F5F4EF",
        "muted": "#A3A099",
        "accent": "#D97757",
        "ok": "#7FB07F",
        "danger": "#E06C4F",
    },
}


# What we show in the remapping page and in which order. On the left is the
# command the hotkey lives under in hotkeys.DEFAULT_BINDINGS and in
# config["hotkeys"].
HOTKEY_ROWS = (
    ("toggle", "hk_nr"),
    ("settings", "hk_menu"),
    ("screenshot_menu", "hk_shot"),
    ("record", "hk_record"),
    ("window_mode", "hk_window"),
    ("scale_up", "hk_scale_up"),
    ("scale_down", "hk_scale_down"),
    ("quit", "hk_quit"),
)


# pygame.key.name() gives "page up" while the parser in hotkeys.parse_binding
# expects "PGUP", and the numpad comes back as "[1]" where the parser wants
# "NUM1". These are the only ones that differ.
_KEY_ALIASES = {"page up": "PGUP", "page down": "PGDN",
                "return": "ENTER", "escape": "ESC",
                "[.]": "Numdot", "[+]": "Numplus", "[-]": "Numminus",
                "[*]": "Nummul", "[/]": "Numdiv"}
# Mixed case on purpose: the parser upper-cases anyway, and "Num3" is what the
# default bindings print on the buttons.
_KEY_ALIASES.update({f"[{n}]": f"Num{n}" for n in range(10)})


def key_text(event) -> str | None:
    """Keyboard event -> a string like "Ctrl+Alt+Q" for parse_binding.

    None when only a modifier is pressed: there is no such thing as a binding
    made of a lone Ctrl.
    """
    name = pygame.key.name(event.key)
    if name in ("left ctrl", "right ctrl", "left alt", "right alt",
                "left shift", "right shift", "left meta", "right meta"):
        return None
    base = _KEY_ALIASES.get(name, name.upper())
    mods = pygame.key.get_mods()
    parts = []
    if mods & pygame.KMOD_CTRL:
        parts.append("Ctrl")
    if mods & pygame.KMOD_ALT:
        parts.append("Alt")
    if mods & pygame.KMOD_SHIFT:
        parts.append("Shift")
    parts.append(base)
    return "+".join(parts)


def palette(theme: str) -> dict:
    """The theme palette. The alerts in display.py need it too - same look."""
    return THEMES.get(theme, THEMES["light"])

# --- Base layout (1440p units) --------------------------------------------
PANEL_W = 540
PAD = 26
TITLE_H = 54
LABEL_H = 24
CTRL_H = 26
ROW_GAP = 20
SECTION_GAP = 14
SLIDER_H = 6
KNOB_R = 9
BTN_H = 42
BTN_PAD = 18
BTN_GAP = 10
ICON_W = 30        # header button: a rounded square, not a circle
ACTION_H = 46      # action button: title with the hotkey caption below it
EXIT_H = 64        # exit: plus an explanation on a third line
STAT_LINE_H = 24
STAT_PAD = 14
RADIUS = 10

FONT_SIZE = 17
TITLE_SIZE = 21
SMALL_SIZE = 14

PARAM_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")
PARAM_MIN, PARAM_MAX = 0.0, 2.5
SKIN_MIN = -1.0


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


@dataclass
class Item:
    """A layout item: what it is, where it sits, what it belongs to."""
    kind: str                      # "slider" | "button" | "toggle" | "choice"
    key: str
    rect: pygame.Rect              # mouse hit area
    lo: float = 0.0
    hi: float = 1.0
    value: float = 0.0
    payload: Any = None
    extra: dict = field(default_factory=dict)


class OverlayMenu:
    """The overlay menu: visibility, state, layout, drawing."""

    def __init__(self, scale: float, font_loader: Callable[[int], Any]):
        self.scale = scale
        # Manual multiplier: needed before the fonts are created, they size
        # themselves through _u
        self.user_scale = 1.0
        self._load_font = font_loader
        self.visible = False
        self.lang = "en"
        self.state: dict = {
            "nr": True,
            "work_scale": 1.0,
            # Where the work size hits the NGX cap. Sent by main because only it
            # knows the screen size. The slider runs one step past it, and that
            # last step means "the whole screen" - the reduced mode off.
            "work_scale_cap": 1.0,
            # The bottom of the resolution slider (WORK_SCALE_MIN), sent by
            # main so a config value below the range cannot misplace the knob.
            "work_scale_min": 0.1,
            "nr_small": False,
            "screen_size": "",
            "profile": "",
            "profiles": [],
            "params": {},
            # The profile's own numbers, drawn as a tick under each
            # parameter slider (see _draw_slider).
            "param_defaults": {},
            "preset_active": False,
            "recording": False,
            "work_size": "",
            "theme": "light",
            "rec_seconds": 0.0,
            "rec_indicator": True,
            "screenshot_dir": "",
            # The Spout2 bridge flag (RECORDING section). It was missing here
            # in v1.6.0, so set_state dropped it in silence and the toggle
            # always drew as off while the action behind it fired normally.
            "spout": False,
            # Skip static frames (processing section): no new capture frame -
            # the network idles instead of re-running.
            "skip_static": True,
            "open_on_start": True,
            "split": 0.0,
            # Which card this is and whether NR runs on it. gpu_ok:
            # True/False/None (None - the worker has not answered yet).
            "gpu_text": "",
            "gpu_ok": None,
            "window_mode": False,
            "monitor": "0",
            "monitors": [],
            # The current monitor's DXGI devicename - carried so a log or a
            # future control can name the exact display the capture is on.
            "monitor_devicename": "",
            "gpu": "0",
            "gpus": [],
            "autostart": False,
            "windows": [],
            "window_current": "",
            # The header shows the version; the channel label lives in the
            # settings page (user rule 2026-09-08).
            "version": "",
            "channel": "",
        }
        # Pipeline readings: the same ones the HUD shows. The menu is meant to
        # be the single place where both the settings and what is going on are
        # visible.
        self.stats: dict = {}
        self.items: list[Item] = []
        self._build_fonts()
        # The language list shows every language in its own script (Русский,
        # 中文, 日本語, 한국어). The current UI font cannot render CJK - the
        # loader picks the font by the language, so dedicated CJK fonts are
        # loaded once for those labels, chosen by the script: YaHei for
        # Chinese, Yu Gothic for Japanese (kanji/kana), Malgun Gothic for
        # Korean (hangul) (user: Asian names show as boxes).
        self._cjk_fonts = {}
        try:
            import pygame.font as _pf
            for _name in ("microsoftyahei", "yugothic", "malgungothic"):
                try:
                    self._cjk_fonts[_name] = _pf.SysFont(
                        _name, self._u(FONT_SIZE))
                except Exception:
                    pass
        except Exception:
            self._cjk_fonts = {}
        self.panel_rect = pygame.Rect(0, 0, 0, 0)
        self._stats_rect = pygame.Rect(0, 0, 0, 0)
        self._gpu_rect = pygame.Rect(0, 0, 0, 0)
        # The relative rects are computed in layout() for the main page
        # only; the defaults keep the non-main pages safe (the drawers are
        # skipped there anyway).
        self._stats_rel = pygame.Rect(0, 0, 0, 0)
        self._gpu_rel = pygame.Rect(0, 0, 0, 0)
        # The panel can be dragged by its title bar and stretched by its
        # corner. The offset is stored relative to the screen centre, so it
        # survives a resolution change without the window ending up off-screen.
        self.offset = [0, 0]
        self._grip = pygame.Rect(0, 0, 0, 0)
        self._title_bar = pygame.Rect(0, 0, 0, 0)
        self._move_from = None
        self._resize_from = None
        # Panel height: None means "fit the content". It is set by dragging the
        # bottom edge; content taller than the height scrolls.
        self.user_height: int | None = None
        self.scroll = 0
        self.content_height = 0
        self._max_scroll = 0
        self._resize_h_from = None
        # The last mouse position: in pygame the wheel arrives without
        # coordinates, and pygame.mouse.get_pos() in a click-through window is
        # not to be trusted.
        self._mouse = (0, 0)
        self._edge = pygame.Rect(0, 0, 0, 0)
        self._viewport = pygame.Rect(0, 0, 0, 0)
        self._scroll_track = pygame.Rect(0, 0, 0, 0)
        self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        # Which list is currently expanded (profile / language / theme).
        # Cycling with arrows is awkward once there are more than two options.
        self.open_choice: str | None = None
        # The expanded list scrolls: 12 languages do not fit the screen, the
        # panel cannot be stretched down, and the arrows must reach every
        # entry (user: the language list has no scroll). _opt_scroll is the
        # first visible row, _opt_index the highlighted one (arrows/Enter).
        self._opt_scroll = 0
        self._opt_index = 0
        self._opt_max_scroll = 0
        self._opt_track = pygame.Rect(0, 0, 0, 0)
        self._opt_thumb = pygame.Rect(0, 0, 0, 0)
        # The window under the cursor on the windows page: the hwnd whose
        # outline is highlighted on the real screen (None = nothing).
        self.hover_window: int | None = None
        # Menu page: the main window or the settings behind the gear.
        self.page = "main"
        # The command we are currently waiting for a keypress for (or None).
        self.capturing: str | None = None
        # Hotkey captions: command -> "Num1". They come from main together with
        # the bindings, so a remap shows up on the buttons immediately.
        self.hotkeys: dict = {}
        self._sections: list = []
        self._hint_rel = pygame.Rect(0, 0, 0, 0)
        self._rule_rel = pygame.Rect(0, 0, 0, 0)
        # What is under the cursor: "title" (draggable) or "grip" (resizable).
        # Without the highlight these zones are invisible and impossible to
        # find.
        self.hover: str | None = None

    @property
    def c(self) -> dict:
        """Colours of the current theme."""
        return palette(self.state.get("theme", "light"))

    # -- helpers -----------------------------------------------------------

    def _u(self, base: float) -> int:
        """1440p base units -> screen pixels."""
        return max(1, int(round(base * self.scale * self.user_scale)))

    def set_user_scale(self, value: float) -> None:
        """Manual panel stretching. The fonts have to be recreated."""
        value = min(2.0, max(0.6, round(value, 2)))
        if abs(value - self.user_scale) < 0.01:
            return
        self.user_scale = value
        self._build_fonts()

    def toggle(self) -> bool:
        self.visible = not self.visible
        if not self.visible:
            self._drag_item = None
        return self.visible

    @property
    def dragging(self) -> bool:
        """Whether a slider is being dragged right now - while dragging the
        state must not be updated, otherwise the value jumps between what the
        mouse shows and what main has already applied."""
        return getattr(self, "_drag_item", None) is not None

    def set_hotkeys(self, mapping: dict) -> None:
        """Hotkey captions: command -> "Num1". Sourced from the real bindings."""
        self.hotkeys = dict(mapping or {})

    def set_stats(self, hud: dict) -> None:
        self.stats = dict(hud or {})

    def set_state(self, payload: dict) -> None:
        for k, v in payload.items():
            if k == "lang":
                self.lang = v
                self._reload_fonts()
            elif k == "params" and isinstance(v, dict):
                self.state["params"] = dict(v)
            elif k in self.state:
                self.state[k] = v

    def _load(self, size: int, mono: bool = False):
        """One face, through the loader the caller handed us.

        The live app passes display._load_font, which takes the role; the
        offscreen renderers and the tests pass a one-argument callable, and
        those get the proportional face for everything - which is what they
        had before the split.
        """
        try:
            return self._load_font(size, mono=mono)
        except TypeError:
            return self._load_font(size)

    def _build_fonts(self) -> None:
        """The menu's faces, built together.

        Called from __init__ and after every scale/language change - the
        sizes and the language both change what the loader returns. Which
        faces those are lives in fonts.py; this only asks for them.

        Two roles, as fonts.py divides them: the proportional face carries
        language - titles, labels, hints, buttons - and the monospaced one
        carries readings, where a fixed advance keeps digits from dancing
        sideways as they change.
        """
        self._font = self._load(self._u(FONT_SIZE))
        self._title_font = self._load(self._u(TITLE_SIZE))
        self._small_font = self._load(self._u(SMALL_SIZE))
        self._mono = self._load(self._u(FONT_SIZE), mono=True)
        self._mono_small = self._load(self._u(SMALL_SIZE), mono=True)

    def _reload_fonts(self) -> None:
        """Recreate the fonts after a language switch.

        The CJK scripts (zh/ja/ko) have no glyphs in the default font -
        Consolas renders them as tofu boxes. The loader picks the font by
        the language, so the cached font objects must be rebuilt.
        """
        self._build_fonts()

    def title_center(self) -> tuple[int, int]:
        """The centre of the title bar in screen coordinates.

        The mouse lands here when the menu opens, so the user does not have
        to hunt for the pointer (user request). Valid after layout().
        """
        r = self._title_bar
        return (r.x + r.w // 2, r.y + r.h // 2)

    def _capture_mouse(self, on: bool) -> None:
        """Capture the mouse while dragging the panel by its title bar.

        Without it the drag dies the moment the cursor leaves the window:
        pygame stops delivering MOUSEMOTION outside the window, and the
        release click outside is lost too - the panel "stops and has to be
        grabbed again" (user report). SetCapture keeps the events coming
        until the button is released.
        """
        try:
            import ctypes
            hwnd = pygame.display.get_wm_info()["window"]
            if on:
                ctypes.windll.user32.SetCapture(hwnd)
            else:
                ctypes.windll.user32.ReleaseCapture()
        except Exception:
            pass

    # -- layout ------------------------------------------------------------

    def layout(self, screen_w: int, screen_h: int) -> None:
        """Recompute the rectangles.

        We walk top to bottom in relative coordinates, learn the height at the
        end and shift everything at once - that way the panel height cannot
        drift apart from the content (it used to come from a formula and lag
        behind).
        """
        s = STRINGS.get(self.lang, STRINGS["en"])
        w = self._u(PANEL_W)
        pad = self._u(PAD)
        label_h = self._u(LABEL_H)
        ctrl_h = self._u(CTRL_H)
        gap = self._u(ROW_GAP)
        inner_w = w - pad * 2
        act_h = self._u(ACTION_H)

        items: list[Item] = []
        # Header icons: help, settings and the collapse button. The collapse
        # used to be a footer button next to "quit the program" - the two
        # looked equally harmless, even though one hides the menu and the
        # other unloads the program. A real window has its collapse in the
        # title bar, so it moved here: [help] [gear] [min].
        iw = self._u(ICON_W)
        igap = self._u(8)
        iy = self._u(TITLE_H) // 2 - iw // 2
        order = (["help", "gear", "min"] if self.page == "main"
                 else ["close"] if self.page == "settings"
                 else [])  # the windows page: no header icons at all - the
        # Back button in the footer is the only way out (user rule 10.09).
        # Laid out right to left: the right edge is the last icon.
        for idx, kind in enumerate(reversed(order)):
            ix = pad + inner_w - iw - idx * (iw + igap)
            items.append(Item("icon", kind,
                              pygame.Rect(ix, iy, iw, iw)))
        cy = self._u(TITLE_H) + self._u(SECTION_GAP)

        # The readings block and the GPU line belong to the MAIN page only:
        # the settings and windows pages are about configuration, and the
        # live indicators (FPS/RES/WORK/FRAMES/REC/PROFILE + the GPU dot)
        # are noise there (user rule 10.09: the main page shows the state,
        # the other pages do the work). The rects are still computed for the
        # main page - the drawers check the page before drawing.
        if self.page == "main":
            # The readings block
            stat_h = self._u(STAT_LINE_H) * 2 + self._u(STAT_PAD) * 2
            self._stats_rel = pygame.Rect(pad, cy, inner_w, stat_h)
            cy += stat_h + self._u(6)

            # The GPU line: a status dot and the card model. A separate line
            # rather than a cell in the readings block - this is not a
            # pipeline reading but the answer to "does this work on my card
            # at all". The capture mode (fullscreen / window) sits on the
            # second line below it.
            gpu_h = self._u(SMALL_SIZE) * 2 + self._u(10)
            self._gpu_rel = pygame.Rect(pad, cy, inner_w, gpu_h)
            cy += gpu_h + gap

        # The content is split into titled blocks: eight identical rows in a
        # row gave the eye nothing to hold on to. The titles are not
        # interactive, so they live in their own list rather than in items.
        self._sections: list[tuple[str, pygame.Rect]] = []
        sec_h = self._u(SMALL_SIZE) + self._u(10)

        def section(title: str) -> None:
            nonlocal cy
            cy += self._u(6)
            self._sections.append((title, pygame.Rect(pad, cy, inner_w, sec_h)))
            cy += sec_h

        def slider(key: str, lo: float, hi: float, value: float,
                   label: str, hint: str = "", value_text: str = "",
                   mark: float | None = None,
                   ends: tuple | None = None) -> None:
            nonlocal cy
            items.append(Item("slider", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h),
                              lo=lo, hi=hi, value=value,
                              extra={"label": label, "hint": hint,
                                     "mark": mark, "ends": ends,
                                     "value_text": value_text,
                                     "label_h": label_h}))
            # End captions take the same line a hint would: a slider has
            # one or the other, never both (they would overprint).
            cy += label_h + ctrl_h + (self._u(SMALL_SIZE) + 4
                                      if (hint or ends) else 0) + gap

        def choice(key: str, label: str, current: str, options: list,
                   labels: list | None = None, hint: str = "") -> None:
            nonlocal cy
            extra = {"label": label, "current": current,
                     "labels": list(labels or options),
                     "label_h": label_h}
            hint_h = 0
            if hint:
                extra["hint"] = hint
                line_h = self._small_font.get_height() + self._u(4)
                hint_h = self._u(8) + (str(hint).count("\n") + 1) * line_h
            items.append(Item("choice", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h + hint_h),
                              payload=list(options),
                              extra=extra))
            cy += label_h + ctrl_h + hint_h + gap

        def segmented(key: str, label: str, current: str, options: list,
                      labels: list | None = None) -> None:
            """A two- or three-way switch instead of a drop-down list.

            A list for the sake of two values is an extra click and extra
            expand/collapse machinery; here both options are visible at once.
            """
            nonlocal cy
            seg_w = min(inner_w - self._u(150), self._u(60) * len(options) + self._u(60))
            rect = pygame.Rect(pad + inner_w - seg_w, cy, seg_w, ctrl_h)
            items.append(Item("segmented", key, rect, payload=list(options),
                              extra={"label": label, "current": current,
                                     "labels": list(labels or options)}))
            cy += ctrl_h + gap

        def toggle(key: str, label: str, on: bool, hint: str = "",
                   key_text: str = "") -> None:
            nonlocal cy
            extra = {"label": label, "key_text": key_text}
            hint_h = 0
            if hint:
                extra["hint"] = hint
                # Multi-line hints: the Spout2 toggle explains two capture
                # paths and does not fit one line at 1440p. The block is
                # measured with the real font height.
                line_h = self._small_font.get_height() + self._u(4)
                hint_h = self._u(8) + (str(hint).count("\n") + 1) * line_h
            items.append(Item("toggle", key,
                              pygame.Rect(pad, cy, inner_w, ctrl_h + hint_h),
                              value=1.0 if on else 0.0,
                              extra=extra))
            cy += ctrl_h + hint_h + gap

        # The windows page: the full list of capturable windows, one row per
        # window. Hovering a row highlights the real window's outline on the
        # screen (main draws the frame); clicking switches the capture.
        if self.page == "windows":
            section(s["sec_windows"])
            wins = self.state.get("windows") or []
            if not wins:
                items.append(Item("button", "no_windows",
                                  pygame.Rect(pad, cy, inner_w, act_h),
                                  extra={"label": s.get("win_none", "No windows"),
                                         "filled": False}))
                cy += act_h + pad
            else:
                row_h = self._u(CTRL_H) + self._u(8)
                for idx, wname in enumerate(wins):
                    items.append(Item("option", "window",
                                      pygame.Rect(pad, cy, inner_w, row_h),
                                      payload=wname,
                                      extra={"label": wname,
                                             "selected": wname == str(
                                                 self.state.get("window_current", ""))}))
                    cy += row_h + self._u(4)
            cy += gap

        elif self.page == "settings":
            section(s["sec_capture"])
            monitors = self.state.get("monitors") or []
            if monitors:
                choice("monitor", s.get("monitor", "Monitor"),
                       str(self.state.get("monitor", "0")), monitors)
            # The card the network and the capture run on. Shown only when
            # there is something to choose: on one card the row would be a
            # control that cannot do anything.
            gpus = self.state.get("gpus") or []
            if len(gpus) > 1:
                choice("gpu", s.get("gpu", "GPU"),
                       str(self.state.get("gpu", gpus[0])), gpus,
                       hint=s.get("gpu_hint", ""))
            # The screenshot folder: a plain button that opens the folder
            # picker (issue #20). The current value is shown as the caption
            # so the user sees what is configured.
            shot_dir = self.state.get("screenshot_dir") or ""
            label = s.get("shot_dir_btn", "Screenshot folder...")
            if shot_dir:
                label = f"{label}  ·  {shot_dir}"
            items.append(Item("button", "shot_dir",
                              pygame.Rect(pad, cy, inner_w, ctrl_h),
                              extra={"label": label}))
            cy += ctrl_h + gap

            # Recording: everything about what leaves the program besides
            # the screen itself. Spout2 (off by default) publishes the
            # processed picture for external recorders; the recording
            # indicator is a display preference of the same subject.
            section(s["sec_recording"])
            toggle("spout", s.get("spout", "Spout2 output (OBS)"),
                   bool(self.state.get("spout")),
                   hint=s.get("spout_hint", ""))
            toggle("rec_indicator", s.get("rec_indicator", "Recording indicator"),
                   bool(self.state.get("rec_indicator", True)))

            section(s["sec_behaviour"])
            toggle("open_on_start", s["open_on_start"],
                   bool(self.state.get("open_on_start")))
            toggle("autostart", s.get("autostart", "Autostart with Windows"),
                   bool(self.state.get("autostart")))

            section(s["sec_hotkeys"])
            # The remapping fields. The captions on the buttons come from these
            # same values, so a key change is visible across the whole menu at
            # once.
            field_h = self._u(CTRL_H)
            for cmd, label in HOTKEY_ROWS:
                items.append(Item("hotkey", cmd,
                                  pygame.Rect(pad, cy, inner_w, field_h),
                                  extra={"label": s.get(label, label),
                                         "key": self.hotkeys.get(cmd, "—"),
                                         "capturing": self.capturing == cmd}))
                cy += field_h + self._u(6)
            cy += gap
            self._hint_rel = pygame.Rect(pad, cy, inner_w,
                                         self._u(SMALL_SIZE) + self._u(6))
            cy += self._hint_rel.h + gap

            # Appearance: language and theme moved here from the main page
            # (user rule 10.09: the main page is the main page - settings
            # live behind the gear). The segmented controls emit the same
            # ("lang", ...) / ("theme", ...) actions main already handles.
            section(s["sec_view"])
            # The language list: a drop-down, not segments - the full set
            # of popular languages (12) cannot fit in a segmented row
            # (user rule 10.09: the list expands, it is not cycled).
            langs = list(STRINGS.keys())
            choice("lang", s["language"], self.lang, langs,
                   labels=[STRINGS[L].get(f"lang_{L}", L) for L in langs])
            segmented("theme", s["theme"], self.state.get("theme", "light"),
                      ["light", "dark"], [s["theme_light"], s["theme_dark"]])
            cy += gap

            # The channel label: the header shows the version, the channel
            # lives here (user rule 2026-09-08). A button item - the only
            # non-interactive kind the drawer supports - with the label as
            # its caption.
            channel = self.state.get("channel") or ""
            if channel:
                section(s["sec_about"])
                act_h = self._u(ACTION_H)
                items.append(Item("button", "channel",
                                  pygame.Rect(pad, cy, inner_w, act_h),
                                  extra={"label": channel, "filled": False}))
                cy += act_h + pad
        else:
            section(s["sec_processing"])
            nr_on = bool(self.state.get("nr"))
            hk_nr = self.hotkeys.get("toggle", "")
            toggle("nr", s["nr_on"] if nr_on else s["nr_off"], nr_on,
                   key_text=hk_nr)

            # The resolution the network runs at sits right under the
            # DLSS 5 switch, not in a section of its own further down:
            # it is the one control that trades quality for frames, and
            # users did not connect it with the network at all (user
            # report, 11.09).
            section(s["sec_resolution"])
            # One slider, not a toggle plus a slider. The two used to be
            # separate, and with the toggle off the slider still moved, still
            # showed a changing resolution and changed the picture by exactly
            # nothing - measured, bit for bit. A control that answers and does
            # nothing is worse than no control.
            cap = float(self.state.get("work_scale_cap", 1.0))
            full = not bool(self.state.get("nr_small"))
            # The extra step past the cap is "the whole screen". Below the cap
            # every position is a different resolution; above it they would all
            # be the same one, so there is exactly one position up there.
            pos = cap + 0.05 if full else float(self.state.get("work_scale", cap))
            if full:
                shown = str(self.state.get("screen_size")
                            or self.stats.get("resolution") or "")
                value_text = f"{shown} · {s['nr_res_full']}" if shown else s["nr_res_full"]
            else:
                value_text = str(self.state.get("work_size") or f"{pos:.2f}")
            # The lower bound follows WORK_SCALE_MIN (0.1), not a hardcoded
            # 0.30: a config value below the slider range would put the knob
            # at the bottom while the label shows a different resolution.
            lo = float(self.state.get("work_scale_min", 0.1))
            # The ends replace the hint here: "fewer pixels - more FPS" on
            # one side and "whole screen - all detail" on the other say the
            # same thing as the sentence did, in the place where the choice
            # is actually made.
            slider("nr_res", lo, cap + 0.05, pos, s["nr_res"],
                   value_text=value_text,
                   ends=(s.get("nr_res_low", ""), s.get("nr_res_high", "")))

            # Idle screens: no new frame arrives (the desktop did not change,
            # the window did not redraw) - the network waits instead of
            # chewing the same picture again. No visual price, a real one on
            # an idle desktop (issue #31 territory). A per-frame flag.
            toggle("skip_static", s.get("skip_static", "Skip static frames"),
                   bool(self.state.get("skip_static", True)),
                   hint=s.get("skip_static_hint", ""))

            # The profile and the four effect sliders are their own subject -
            # what the picture looks like, not how hard the network works.
            section(s["sec_effect"])
            choice("profile", s["profile"], str(self.state.get("profile", "")),
                   list(self.state.get("profiles") or []))
            params = self.state.get("params") or {}
            defaults = self.state.get("param_defaults") or {}
            for key in PARAM_KEYS:
                lo = SKIN_MIN if key == "skin_structure" else PARAM_MIN
                val = float(params.get(key, 0.0))
                slider(key, lo, PARAM_MAX, val, s[key], value_text=f"{val:.2f}",
                       mark=defaults.get(key))
            # Save / Delete preset: the user presets live in the same list
            # as the built-in profiles. Delete is only offered while a user
            # preset is active - the built-in profiles are not deletable.
            bgap = self._u(BTN_GAP)
            bw = (inner_w - bgap) // 2
            for idx, (key, label) in enumerate((
                    ("save_preset", s["save_preset"]),
                    ("delete_preset", s["delete_preset"]))):
                items.append(Item("button", key,
                                  pygame.Rect(pad + idx * (bw + bgap),
                                              cy, bw, act_h),
                                  extra={"label": label,
                                         "filled": False,
                                         "disabled": key == "delete_preset"
                                         and not self.state.get("preset_active")}))
            cy += act_h + self._u(8)

            section(s["sec_compare"])
            split_val = float(self.state.get("split", 0.0))
            slider("split", 0.0, 1.0, split_val, s["split"], hint=s["split_hint"],
                   value_text=(s.get("off", "off") if split_val <= 0.0
                               else f"{split_val:.2f}"))

            section(s["sec_actions"])
            # Two rows of two: Select window + Fullscreen on top, Screenshot
            # + Record below (user rule 10.09: the capture actions belong
            # together in one section, the footer keeps only Exit).
            bgap = self._u(BTN_GAP)
            bw = (inner_w - bgap) // 2
            rows = (
                (("windows", s["windows_btn"]),
                 ("fullscreen", s["fullscreen"])),
                (("screenshot", s["screenshot"]),
                 ("record", s["record_stop_short"] if self.state.get("recording")
                  else s["record"])),
            )
            for row in rows:
                for idx, (key, label) in enumerate(row):
                    items.append(Item("button", key,
                                      pygame.Rect(pad + idx * (bw + bgap),
                                                  cy, bw, act_h),
                                      extra={"label": label,
                                             "filled": False}))
                cy += act_h + self._u(8)
            cy += pad - self._u(8)

        # The footer: actions with the hotkey printed underneath. "Collapse"
        # and "Exit" used to look equally harmless, even though one hides the
        # menu and the other unloads the program.
        cy += self._u(6)
        self._rule_rel = pygame.Rect(pad, cy, inner_w, 1)
        cy += self._u(14)
        act_h = self._u(ACTION_H)
        if self.page in ("settings", "windows"):
            items.append(Item("action", "back",
                              pygame.Rect(pad, cy, inner_w, act_h),
                              extra={"label": s["back"],
                                     "filled": False}))
            cy += act_h + pad
        else:
            exit_h = self._u(EXIT_H)
            items.append(Item("action", "exit",
                              pygame.Rect(pad, cy, inner_w, exit_h),
                              extra={"label": s["exit_full"],
                                     "hotkey": self.hotkeys.get("quit", ""),
                                     "note": s["exit_note"], "danger": True}))
            cy += exit_h + pad

        # The content height is known. The panel may be shorter - then the
        # content scrolls: at 1080p a full panel took up almost the whole
        # screen and there was no way around it.
        content_h = cy
        title_h = self._u(TITLE_H)
        min_h = title_h + self._u(140)
        # We never stretch past the content: empty space at the bottom looks
        # broken, not spacious.
        h = content_h if self.user_height is None else int(self.user_height)
        h = max(min(h, content_h, max(0, screen_h - self._u(40))), min(min_h, content_h))
        self.content_height = content_h
        self._max_scroll = max(0, content_h - h)
        self.scroll = min(max(self.scroll, 0), self._max_scroll)
        # Offset from the centre plus a clamp, so the panel always stays
        # fully inside the screen: the user's saved position is honoured
        # (no jumping to a corner on mode switches), and a stale offset
        # (resolution change, monitor swap) is pulled back to the edge
        # instead of leaving the panel half off the desktop (user rule
        # 10.09: fixed position until the user drags it).
        x = (screen_w - w) // 2 + self.offset[0]
        y = (screen_h - h) // 2 + self.offset[1]
        x = min(max(x, 0), max(0, screen_w - w))
        y = min(max(y, 0), max(0, screen_h - h))
        self.panel_rect = pygame.Rect(x, y, w, h)
        self._title_bar = pygame.Rect(x, y, w, title_h)
        grip = self._u(26)
        self._grip = pygame.Rect(x + w - grip, y + h - grip, grip, grip)
        # The bottom edge drags the height while the corner stays in charge of
        # the scale - which is why the edge zone stops short of the corner.
        edge = self._u(7)
        self._edge = pygame.Rect(x, y + h - edge, max(0, w - grip), edge)
        self._viewport = pygame.Rect(x, y + title_h, w, max(0, h - title_h))
        # All the content lives shifted by the scroll; the title bar does not.
        sy = y - self.scroll
        # The stats/GPU rects exist only on the main page (the layout skips
        # them elsewhere) - keep the attributes defined so the drawers and
        # any hit-testing never see a stale rect from a previous page.
        self._stats_rect = (self._stats_rel.move(x, sy)
                            if self.page == "main" else pygame.Rect(0, 0, 0, 0))
        self._gpu_rect = (self._gpu_rel.move(x, sy)
                          if self.page == "main" else pygame.Rect(0, 0, 0, 0))
        self._hint_rect = self._hint_rel.move(x, sy)
        self._rule_rect = self._rule_rel.move(x, sy)
        self._section_rects = [(t, r.move(x, sy)) for t, r in self._sections]
        if self._max_scroll > 0:
            bar_w = max(2, self._u(3))
            view_h = self._viewport.h
            track = pygame.Rect(x + w - self._u(7) - bar_w,
                                y + title_h + self._u(4),
                                bar_w, max(1, view_h - self._u(8)))
            thumb_h = max(self._u(26), int(track.h * view_h / content_h))
            travel = track.h - thumb_h
            ty = track.y + int(travel * (self.scroll / self._max_scroll))
            self._scroll_track = track
            self._scroll_thumb = pygame.Rect(track.x, ty, bar_w, thumb_h)
        else:
            self._scroll_track = pygame.Rect(0, 0, 0, 0)
            self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        for it in items:
            # The header icons are pinned to the panel, not to the content:
            # they sit above the scroll area and used to travel with it under
            # the title bar.
            it.rect = it.rect.move(x, y if it.kind == "icon" else sy)
            if it.kind == "choice":
                # The select field is computed here rather than at draw time:
                # the layout of an expanded list is built before the first
                # draw. The strip is the CONTROL, not the whole row: a row
                # with a hint is taller, and the strip must stay the height
                # of the field or the value text and the list would centre
                # on the hint below it.
                it.extra["strip"] = pygame.Rect(
                    it.rect.x, it.rect.y + label_h, it.rect.w, self._u(CTRL_H))
        self.items = items

        # The entries of the expanded list. They lie on top of the rows below,
        # so they are added last and checked first on a mouse hit. The list
        # opens DOWN when the space below the strip fits it, otherwise UP
        # (over the panel content) - 12 languages would otherwise run off
        # the screen, and the panel cannot be stretched down (user: the
        # language list has no scroll).
        self.options: list[Item] = []
        self._opt_max_scroll = 0
        if self.open_choice:
            src = next((i for i in items if i.key == self.open_choice), None)
            if src is not None:
                strip = src.extra.get("strip")
                if strip is not None:
                    oh = self._u(CTRL_H) + self._u(6)
                    labels = src.extra.get("labels") or src.payload or []
                    total = len(src.payload or [])
                    # The list is bounded by the PANEL, not the screen: the
                    # panel is the window, and anything past its edge is
                    # clipped by it (user: the language list is cut off and
                    # the panel cannot be stretched down). Prefer opening
                    # down; flip up when the space below the strip cannot
                    # hold at least two rows and the space above can.
                    down_room = self.panel_rect.bottom - strip.bottom - self._u(8)
                    up_room = strip.top - self.panel_rect.top - self._u(8)
                    open_up = (down_room < 2 * oh and up_room > down_room)
                    room = up_room if open_up else down_room
                    max_rows = max(1, room // oh)
                    visible = min(total, max_rows)
                    self._opt_max_scroll = max(0, total - visible)
                    self._opt_scroll = min(max(0, self._opt_scroll),
                                           self._opt_max_scroll)
                    self._opt_index = min(max(0, self._opt_index), total - 1)
                    base_y = (strip.top - self._u(4) - visible * oh
                              if open_up else strip.bottom + self._u(4))
                    # The list's own scrollbar: a thin track on the right of
                    # the list, thumb proportional to the visible share.
                    self._opt_track = pygame.Rect(
                        strip.right - self._u(7) - max(2, self._u(3)),
                        base_y, max(2, self._u(3)), visible * oh)
                    if self._opt_max_scroll > 0:
                        thumb_h = max(self._u(12),
                                      int(self._opt_track.h * visible / total))
                        travel = self._opt_track.h - thumb_h
                        ty = self._opt_track.y + int(
                            travel * (self._opt_scroll / self._opt_max_scroll))
                        self._opt_thumb = pygame.Rect(
                            self._opt_track.x, ty, self._opt_track.w, thumb_h)
                    else:
                        self._opt_thumb = pygame.Rect(0, 0, 0, 0)
                    for idx in range(self._opt_scroll,
                                     min(total, self._opt_scroll + visible)):
                        opt = src.payload[idx]
                        self.options.append(Item(
                            "option", src.key,
                            pygame.Rect(strip.x, base_y
                                        + (idx - self._opt_scroll) * oh,
                                        strip.w, oh),
                            payload=opt,
                            extra={"label": str(labels[idx] if idx < len(labels)
                                                else opt),
                                   "selected": str(opt) == str(src.extra.get("current")),
                                   "highlighted": idx == self._opt_index}))

    # -- input -------------------------------------------------------------

    def handle_event(self, event) -> list[tuple]:
        """Handle a pygame event. Returns a list of actions for main.

        Actions: ("nr",), ("param", key, value), ("profile", name),
        ("lang", code), ("theme", name), ("button", key), ("drag", dx, dy).
        This module changes only its own display state - everything else is
        decided by main, which holds the source of truth.
        """
        if not self.visible:
            return []
        out: list[tuple] = []
        if self.capturing is not None and event.type == pygame.KEYDOWN:
            # While we wait for a key the keyboard belongs to the field. Esc
            # cancels, otherwise the menu would close instead of cancelling the
            # assignment.
            if event.key == pygame.K_ESCAPE:
                self.capturing = None
                out.append(("capture", None))
                return out
            text = key_text(event)
            if text is None:
                return out
            cmd, self.capturing = self.capturing, None
            out.append(("hotkey", cmd, text))
            out.append(("capture", None))
            return out
        if event.type == pygame.MOUSEMOTION:
            self._mouse = event.pos
            # The windows page: hovering a row highlights the real window's
            # outline on the screen. The hwnd is the hex prefix of the row.
            self.hover_window = None
            if self.page == "windows":
                for it in self.items:
                    if it.kind == "option" and it.rect.collidepoint(event.pos):
                        try:
                            self.hover_window = int(str(it.payload).split(":")[0], 16)
                        except (ValueError, IndexError):
                            self.hover_window = None
                        break
            if self._grip.collidepoint(event.pos):
                self.hover = "grip"
            elif self._edge.collidepoint(event.pos):
                self.hover = "edge"
            elif self._title_bar.collidepoint(event.pos):
                self.hover = "title"
                for it in self.items:
                    if it.kind == "icon" and it.rect.collidepoint(event.pos):
                        self.hover = f"icon:{it.key}"
                        break
            else:
                self.hover = None
                for it in self.items:
                    if it.kind in ("action", "hotkey", "button") and \
                            it.rect.collidepoint(event.pos):
                        self.hover = f"{it.kind}:{it.key}"
                        break
            # The windows page rows: the row itself is highlighted too, like
            # the buttons (the outline on the screen is easy to miss).
            if self.page == "windows" and self.hover_window is not None:
                for it in self.items:
                    if it.kind == "option" and it.rect.collidepoint(event.pos):
                        self.hover = f"woption:{it.payload}"
                        break
            # The expanded list rows: hovering a row highlights it (the rows
            # are a pop-up layer above the content, so they win over the items
            # underneath).
            if self.open_choice:
                for i, opt in enumerate(self.options):
                    if opt.rect.collidepoint(event.pos):
                        self.hover = f"option:{i}"
                        break
        if event.type == pygame.MOUSEWHEEL:
            # Scroll only while the cursor is over the panel: otherwise the
            # wheel inside the game would end up scrolling the menu. The
            # cached position is refreshed on MOUSEMOTION; when the menu was
            # just opened by a hotkey the cache may still be (0,0) - fall
            # back to the real cursor position once.
            if self._mouse == (0, 0):
                try:
                    self._mouse = pygame.mouse.get_pos()
                except Exception:
                    pass
            # The expanded list scrolls with the wheel whenever it is open:
            # the user opened it, so the wheel belongs to the list, not to
            # the page (12 languages do not fit - user: no scroll in the
            # language list).
            if self.open_choice and self._opt_max_scroll > 0:
                self._opt_scroll = min(
                    max(0, self._opt_scroll - event.y), self._opt_max_scroll)
                return out
            if self._max_scroll > 0 and self.panel_rect.collidepoint(self._mouse):
                self.scroll = min(max(self.scroll - event.y * self._u(48), 0),
                                  self._max_scroll)
            return out
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            # The icons sit in the header, and the header is the drag handle,
            # which returns immediately. So the icons are checked first.
            for it in self.items:
                if it.kind == "icon" and it.rect.collidepoint(event.pos):
                    out.extend(self._icon_click(it.key))
                    return out
            if self._grip.collidepoint(event.pos):
                self._resize_from = (event.pos, self.user_scale)
                return out
            if self._edge.collidepoint(event.pos):
                # Dragging the height. If the height has never been set, take
                # the current one - otherwise the very first pixel of the drag
                # would collapse the panel.
                base = self.user_height if self.user_height is not None \
                    else self.panel_rect.h
                self._resize_h_from = (event.pos[1], int(base))
                return out
            if self._title_bar.collidepoint(event.pos):
                self._move_from = (event.pos, tuple(self.offset))
                self._capture_mouse(True)
                return out
            item = self.hit(event.pos)
            if item is None:
                self._drag_item = None
                self.open_choice = None
                return out
            if item.kind == "action":
                out.extend(self._action_click(item.key))
            elif item.kind == "hotkey":
                self.capturing = item.key
                out.append(("capture", item.key))
            elif item.kind == "segmented":
                cells = item.extra.get("cells") or []
                for idx, cr in enumerate(cells):
                    if cr.collidepoint(event.pos) and idx < len(item.payload or []):
                        out.extend(self._pick(item.key, str(item.payload[idx])))
                        break
            elif item.kind == "toggle":
                out.append(("nr",) if item.key == "nr" else ("toggle", item.key))
            elif item.kind == "button":
                if not item.extra.get("disabled"):
                    out.extend(self._button_click(item.key))
            elif item.kind == "option":
                out.extend(self._pick(item.key, str(item.payload)))
                self.open_choice = None
            elif item.kind == "choice":
                self.open_choice = None if self.open_choice == item.key else item.key
            elif item.kind == "slider":
                self._drag_item = item
                out.extend(self._slide(item, event.pos[0]))
        elif event.type == pygame.MOUSEMOTION and self._resize_h_from is not None:
            start_y, base = self._resize_h_from
            self.user_height = max(1, base + event.pos[1] - start_y)
        elif event.type == pygame.MOUSEMOTION and self._move_from is not None:
            start, base = self._move_from
            self.offset = [base[0] + event.pos[0] - start[0],
                           base[1] + event.pos[1] - start[1]]
        elif event.type == pygame.MOUSEMOTION and self._resize_from is not None:
            start, base = self._resize_from
            # Dragging right and down grows the panel. The step is chosen so
            # that a pass across the screen diagonal gives roughly double the
            # size.
            delta = ((event.pos[0] - start[0]) + (event.pos[1] - start[1])) / 900.0
            self.set_user_scale(base + delta)
        elif event.type == pygame.MOUSEMOTION and getattr(self, "_drag_item", None):
            if event.buttons and event.buttons[0]:
                out.extend(self._slide(self._drag_item, event.pos[0]))
            else:
                self._drag_item = None
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._drag_item = None
            self._move_from = None
            self._resize_from = None
            self._capture_mouse(False)
            if self._resize_h_from is not None:
                self._resize_h_from = None
                # The layout clamps the height by the content and the screen -
                # we take the clamped value so nothing raw leaks into the
                # config.
                self.user_height = (None if self._max_scroll == 0
                                    else self.panel_rect.h)
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            out.append(("button", "close"))
        elif event.type == pygame.KEYDOWN and self.open_choice:
            # The expanded list is keyboard-navigable: Up/Down move the
            # highlight (scrolling the list into view), Enter picks, Esc
            # closes. Without this the 12-language list was unreachable by
            # keyboard (user: cannot step down the list with the arrows).
            total = len(self.options) + self._opt_scroll
            if event.key == pygame.K_UP:
                if self._opt_index > 0:
                    self._opt_index -= 1
                    if self._opt_index < self._opt_scroll:
                        self._opt_scroll = self._opt_index
            elif event.key == pygame.K_DOWN:
                if self._opt_index < total - 1:
                    self._opt_index += 1
                    if self._opt_index >= self._opt_scroll + len(self.options):
                        self._opt_scroll = self._opt_index - len(self.options) + 1
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                src = next((i for i in self.items
                            if i.key == self.open_choice), None)
                if src is not None and self._opt_index < len(src.payload or []):
                    out.extend(self._pick(self.open_choice,
                                          str(src.payload[self._opt_index])))
                    self.open_choice = None
            elif event.key == pygame.K_ESCAPE:
                self.open_choice = None
        return out

    def _icon_click(self, key: str) -> list[tuple]:
        """The header: help, settings, collapse, returning from settings."""
        if key == "help":
            return [("button", "github")]
        if key == "gear":
            self.page = "settings"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "min":
            # The collapse button: hide the menu, exactly like the old
            # footer "Collapse" did.
            return [("button", "close")]
        if key == "close":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        return []

    def _action_click(self, key: str) -> list[tuple]:
        """The footer. "Fullscreen" returns the capture to the whole screen
        (the window-mode exit), "Exit" unloads the program - which is why
        they are different actions with different captions rather than a
        single cross."""
        if key == "back":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            self.hover_window = None
            return [("capture", None)]
        return [("button", key)]

    def _button_click(self, key: str) -> list[tuple]:
        """A plain button. The windows button opens the window list page,
        the fullscreen button returns the capture to the whole screen (the
        window-mode exit, same as the Num5 hotkey)."""
        if key == "windows":
            self.page = "windows"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "fullscreen":
            return [("button", "window_mode")]
        return [("button", key)]

    def _pick(self, key: str, value: str) -> list[tuple]:
        """A list entry was picked."""
        if key == "profile":
            return [("profile", value)]
        if key == "lang":
            return [("lang", value)]
        if key == "theme":
            self.state["theme"] = value
            return [("theme", value)]
        if key == "monitor":
            return [("monitor", value)]
        if key == "gpu":
            return [("gpu", value)]
        if key == "window":
            return [("window", value)]
        return []

    def _slide(self, item: Item, mouse_x: int) -> list[tuple]:
        """The slider value from the mouse position, rounded to a 0.05 step."""
        track = item.extra.get("track")
        if track is None or track.w <= 0:
            return []
        frac = min(1.0, max(0.0, (mouse_x - track.x) / track.w))
        value = item.lo + frac * (item.hi - item.lo)
        value = round(round(value / 0.05) * 0.05, 2)
        if abs(value - item.value) < 1e-9:
            return []
        item.value = value
        if item.key == "split":
            self.state["split"] = value
            return [("split", value)]
        if item.key == "nr_res":
            cap = float(self.state.get("work_scale_cap", 1.0))
            # Reflect the choice straight away so the label does not lag a
            # frame behind the knob; main confirms it on the way back.
            self.state["nr_small"] = value <= cap + 1e-6
            if self.state["nr_small"]:
                self.state["work_scale"] = value
                # The label shows the work resolution; recompute it here so it
                # follows the knob while dragging (main confirms on the way
                # back). The formula mirrors _work_size in main.py.
                try:
                    sw, sh = (int(x) for x in
                              str(self.state.get("screen_size", "0x0")).split("x"))
                    w = max(64, int(round(sw * value / 2) * 2))
                    h = max(64, int(round(sh * value / 2) * 2))
                    if w > 2560 or h > 1440:
                        k = min(2560 / w, 1440 / h)
                        w = max(64, int(round(w * k / 2) * 2))
                        h = max(64, int(round(h * k / 2) * 2))
                    self.state["work_size"] = f"{w}x{h}"
                except Exception:
                    pass
            return [("nr_res", value)]
        params = dict(self.state.get("params") or {})
        params[item.key] = value
        self.state["params"] = params
        return [("param", item.key, value)]

    @property
    def desired_cursor(self):
        """The cursor for the current zone. display sets it - it owns pygame."""
        if self.hover == "grip" or self._resize_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENWSE
        if self.hover == "edge" or self._resize_h_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENS
        if isinstance(self.hover, str) and self.hover.startswith(
                ("icon:", "action:", "hotkey:", "button:")):
            return pygame.SYSTEM_CURSOR_HAND
        if self.hover == "title" or self._move_from is not None:
            return pygame.SYSTEM_CURSOR_SIZEALL
        return pygame.SYSTEM_CURSOR_ARROW

    def hit(self, pos: tuple[int, int]) -> Item | None:
        # Scrolled content is drawn clipped to _viewport, so hits outside it
        # must not count either: a row that has travelled under the title bar
        # is invisible, yet its rectangle still exists.
        for it in self.items:
            if it.kind == "icon" and it.rect.collidepoint(pos):
                return it
        if self._viewport.h > 0 and not self._viewport.collidepoint(pos):
            return None
        for opt in getattr(self, "options", []):
            if opt.rect.collidepoint(pos):
                return opt
        for item in self.items:
            if not item.rect.collidepoint(pos):
                continue
            # A hint under a toggle is a caption, not a hit target:
            # clicking it must not flip the switch (the Spout2 toggle
            # restarts the worker - a stray click on the explanation
            # would freeze the screen for seconds).
            if item.kind == "toggle" and item.extra.get("hint") and \
                    pos[1] > item.rect.y + self._u(CTRL_H):
                continue
            if item.kind == "choice" and item.extra.get("hint"):
                strip = item.extra.get("strip")
                if strip is not None and pos[1] > strip.bottom:
                    continue
            return item
        return None

    def inside(self, pos: tuple[int, int]) -> bool:
        return self.panel_rect.collidepoint(pos)

    # -- drawing -----------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        if not self.visible:
            return
        self.layout(surface.get_width(), surface.get_height())
        s = STRINGS.get(self.lang, STRINGS["en"])
        pad = self._u(PAD)
        r = self.panel_rect

        pygame.draw.rect(surface, _rgb(self.c["bg"]), r, border_radius=self._u(RADIUS))
        pygame.draw.rect(surface, _rgb(self.c["border"]), r, self._u(1),
                         border_radius=self._u(RADIUS))

        if self.hover == "title" or self._move_from is not None:
            # The title bar is the drag handle. We highlight it with a strip
            # and an accent edge: there is otherwise no way to guess it exists.
            tb = self._title_bar
            pygame.draw.rect(surface, _rgb(self.c["surface"]), tb,
                             border_top_left_radius=self._u(RADIUS),
                             border_top_right_radius=self._u(RADIUS))
            pygame.draw.line(surface, _rgb(self.c["accent"]),
                             (tb.x + self._u(RADIUS), tb.bottom - 1),
                             (tb.right - self._u(RADIUS), tb.bottom - 1),
                             max(2, self._u(2)))
        head = (s.get("settings_title", "Settings") if self.page == "settings"
                else s.get("windows_title", "Select window")
                if self.page == "windows" else s["title"])
        title = self._title_font.render(head, True, _rgb(self.c["text"]))
        surface.blit(title, (r.x + pad, r.y + self._u(16)))
        # The version right after the title: the top right corner is taken by
        # the icons. The channel label lives in the settings page (user rule
        # 2026-09-08).
        ver = self.state.get("version") or ""
        if ver:
            ver_text = self._mono_small.render(f"v{ver}", True,
                                               _rgb(self.c["muted"]))
            surface.blit(ver_text, (r.x + pad + title.get_width() + self._u(10),
                                    r.y + self._u(22)))

        # The content is drawn clipped to the scroll area, otherwise scrolled
        # rows would spill outside the panel.
        prev_clip = surface.get_clip()
        surface.set_clip(self._viewport)
        # The live indicators are main-page only (user rule 10.09): the
        # settings/windows pages skip them entirely - the rects are not even
        # computed there, so the drawers must not run.
        if self.page == "main":
            self._draw_stats(surface, s)
            self._draw_gpu(surface, s)
        self._draw_sections(surface)
        self._draw_rules(surface, s)
        # The resize corner: three short strokes, as resize handles usually go
        g = self._grip
        active = self.hover == "grip" or self._resize_from is not None
        if active:
            # A backing under the corner: three strokes on their own get lost
            # against the panel and the zone is invisible until you poke it.
            # NOT SRCALPHA: a translucent backing blends with the magenta
            # background (CHROMA_KEY) -> colour != key -> the colour key does
            # not cut it out -> a pink slab. An opaque backing in the panel
            # colour is cut out along with the background, and the accent
            # border stays.
            pad = pygame.Surface(g.size)
            pygame.draw.rect(pad, _rgb(self.c["bg"]), pad.get_rect(),
                             border_bottom_right_radius=self._u(RADIUS))
            pygame.draw.rect(pad, _rgb(self.c["accent"]), pad.get_rect(),
                             self._u(1),
                             border_bottom_right_radius=self._u(RADIUS))
            surface.blit(pad, g.topleft)
        color = self.c["accent"] if active else self.c["muted"]
        width = max(2, self._u(3 if active else 2))
        step = max(3, self._u(6))
        for i in range(1, 4):
            off = i * step
            pygame.draw.line(surface, _rgb(color),
                             (g.right - off, g.bottom - self._u(3)),
                             (g.right - self._u(3), g.bottom - off), width)
        for item in self.items:
            {"toggle": self._draw_toggle, "slider": self._draw_slider,
             "choice": self._draw_choice, "button": self._draw_button,
             "segmented": self._draw_segmented,
             "action": self._draw_action,
             "hotkey": self._draw_hotkey,
             # The header icons sit above the scroll area - we draw them after
             # the clip is lifted, otherwise they get cut off.
             "icon": lambda *_: None,
             # The windows page rows are drawn by _draw_options (they are
             # option items, like the entries of an expanded list).
             "option": lambda *_: None}[item.kind](surface, item, s)
        if self.open_choice and self.options:
            # The expanded list fades at its edges: a soft gradient around
            # the rows (top/bottom/left/right) instead of dimming the whole
            # panel - the list reads as a floating layer while the content
            # underneath stays fully readable.
            first = self.options[0].rect
            last = self.options[-1].rect
            list_rect = pygame.Rect(first.x, first.y, first.w,
                                     last.bottom - first.y)
            fade = self._u(18)
            shade = pygame.Surface(
                (list_rect.w + 2 * fade, list_rect.h + 2 * fade),
                pygame.SRCALPHA)
            for off in range(fade):
                a = int(95 * (1 - off / fade))
                # top edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (0, fade - off),
                                 (shade.get_width(), fade - off))
                # bottom edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (0, shade.get_height() - fade + off),
                                 (shade.get_width(),
                                  shade.get_height() - fade + off))
                # left edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (fade - off, 0),
                                 (fade - off, shade.get_height()))
                # right edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (shade.get_width() - fade + off, 0),
                                 (shade.get_width() - fade + off,
                                  shade.get_height()))
            surface.blit(shade, (list_rect.x - fade, list_rect.y - fade))
        self._draw_options(surface)
        surface.set_clip(prev_clip)
        for item in self.items:
            if item.kind == "icon":
                self._draw_icon(surface, item, s)
        self._draw_scrollbar(surface)

    def _draw_scrollbar(self, surface) -> None:
        """A thin strip at the right edge. It appears only when there is
        somewhere to scroll - a permanent bar would be noise."""
        if self._max_scroll <= 0:
            return
        radius = self._scroll_thumb.w // 2
        pygame.draw.rect(surface, _rgb(self.c["surface"]), self._scroll_track,
                         border_radius=radius)
        active = (self.hover in ("edge", "scroll")
                  or self._resize_h_from is not None)
        color = self.c["accent"] if active else self.c["muted"]
        pygame.draw.rect(surface, _rgb(color), self._scroll_thumb,
                         border_radius=radius)

    def _draw_stats(self, surface, s: dict) -> None:
        st = self.stats or {}
        rect = self._stats_rect
        pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                         border_radius=self._u(RADIUS // 2))
        fps = st.get("fps")
        mode = (s["mode_window"] if self.state.get("window_mode")
                else s["mode_fullscreen"])
        rows = (
            (("FPS", f"{fps:.1f}" if isinstance(fps, (int, float)) else "—"),
             ("RES", str(st.get("resolution", "—"))),
             ("MODE", mode)),
            (("FRAMES", str(st.get("frames", "—"))),
             ("REC", self._rec_text(s)),
             ("PROFILE", str(self.state.get("profile", "—")).split(" /")[0])),
        )
        pad = self._u(STAT_PAD)
        cell = (rect.w - pad * 2) // 3
        for ri, row in enumerate(rows):
            y = rect.y + pad + ri * self._u(STAT_LINE_H)
            for ci, (name, value) in enumerate(row):
                cx = rect.x + pad + ci * cell
                k = self._mono_small.render(name, True, _rgb(self.c["muted"]))
                v = self._mono_small.render(value, True, _rgb(self.c["accent"]))
                surface.blit(k, (cx, y))
                surface.blit(v, (cx + k.get_width() + self._u(6), y))

    def _draw_gpu(self, surface, s: dict) -> None:
        """Status dot and card model: green - NR works, red - it does not."""
        rect = getattr(self, "_gpu_rect", None)
        if rect is None:
            return
        ok = self.state.get("gpu_ok")
        color = (self.c["muted"] if ok is None
                 else self.c["ok"] if ok else self.c["danger"])
        r = max(3, self._u(5))
        cy = rect.y + rect.h // 2
        pygame.draw.circle(surface, _rgb(color), (rect.x + r, cy), r)
        text = self.state.get("gpu_text") or "—"
        name = self._small_font.render(text, True, _rgb(self.c["text"]))
        # The status text next to the card is gone: the dot colour already
        # answers "does it work" (user rule 10.09). The capture mode moved
        # into the stats block (MODE cell) - no duplicate line under the
        # card. The name gets the full row width now.
        avail = rect.right - (rect.x + r * 2 + self._u(8)) - self._u(8)
        if avail < self._u(24):
            avail = self._u(24)  # never let the name vanish entirely
        if name.get_width() > avail:
            clip = self._small_font.render(text + "…", True, _rgb(self.c["text"]))
            while clip.get_width() > avail and len(text) > 1:
                text = text[:-1]
                clip = self._small_font.render(text + "…", True, _rgb(self.c["text"]))
            name = clip
        surface.blit(name, (rect.x + r * 2 + self._u(8),
                            cy - name.get_height() // 2))

    def _rec_text(self, s: dict) -> str:
        """Recording state: the duration is more useful than a bare "on"."""
        if not self.state.get("recording"):
            return s.get("off", "off")
        secs = float(self.state.get("rec_seconds", 0.0))
        return f"{int(secs) // 60:d}:{int(secs) % 60:02d}"

    def _draw_hotkeys(self, surface, s: dict) -> None:
        """The hotkey line. Otherwise there is nowhere to learn about
        Num1/Num0/Ctrl+Alt+Q."""
        rect = getattr(self, "_hotkeys_rect", None)
        if rect is None:
            return
        text = s.get("hotkeys", "")
        line = self._small_font.render(text, True, _rgb(self.c["muted"]))
        surface.blit(line, (rect.x, rect.y))

    def _clip(self, font, text: str, color, max_w: int):
        """Render text clipped to max_w with an ellipsis.

        Long localized strings (French, German) and long window titles
        overflow their controls - the panel has no clipping surface, so the
        text bleeds over the neighbours (user: FR button label escapes the
        button, window names overflow the rows, the resolution label covers
        the value). Binary search the longest prefix that fits.
        """
        if max_w <= 8:
            return font.render("", True, color)
        img = font.render(text, True, color)
        if img.get_width() <= max_w:
            return img
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.render(text[:mid] + ell, True, color).get_width() <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return font.render(text[:lo] + ell, True, color)

    def _draw_toggle(self, surface, item: Item, s: dict) -> None:
        on = item.value > 0.5
        size = self._u(20)
        # A switch, not a checkbox: the track is a pill and the knob sits at
        # the end that matches the state. A square box could only be read by
        # the word beside it, and this one is read from the corner of the eye
        # while a game is running.
        track_w = int(size * 1.8)
        box = pygame.Rect(item.rect.x, item.rect.centery - size // 2,
                          track_w, size)
        # A hint grows the row; the box and the label stay on the first
        # line - only the hint is pushed under them.
        hint = item.extra.get("hint")
        if hint:
            box.y = item.rect.y + (self._u(CTRL_H) - size) // 2
        radius = size // 2
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if on else self.c["surface"]),
                         box, border_radius=radius)
        if not on:
            pygame.draw.rect(surface, _rgb(self.c["border"]), box, self._u(1),
                             border_radius=radius)
        knob_r = max(3, size // 2 - self._u(3))
        knob_x = (box.right - knob_r - self._u(3)) if on else (
            box.x + knob_r + self._u(3))
        pygame.draw.circle(surface,
                           _rgb(self.c["bg"] if on else self.c["muted"]),
                           (knob_x, box.centery), knob_r)
        text = item.extra.get("label")
        if not text:
            text = s["nr_on"] if on else s["nr_off"]
        # The key caption is a reading, not language: it takes the
        # monospaced face and sits after the label, so "Num1" here matches
        # the key fields on the settings page.
        key_text = item.extra.get("key_text") or ""
        key_img = (self._mono.render(key_text, True, _rgb(self.c["muted"]))
                   if key_text else None)
        room = item.rect.right - box.right - self._u(12)
        if key_img is not None:
            room -= key_img.get_width() + self._u(12)
        label = self._clip(self._font, text,
                           _rgb(self.c["text"] if on else self.c["muted"]),
                           room)
        surface.blit(label, (box.right + self._u(12),
                             box.y + (box.h - label.get_height()) // 2))
        if key_img is not None:
            surface.blit(key_img,
                         (box.right + self._u(12) + label.get_width()
                          + self._u(12),
                          box.y + (box.h - key_img.get_height()) // 2))
        if hint:
            y = box.bottom + self._u(8)
            for line in str(hint).split("\n"):
                img = self._clip(self._small_font, line, _rgb(self.c["muted"]),
                                 item.rect.w)
                surface.blit(img, (item.rect.x, y))
                y += self._small_font.get_height() + self._u(4)

    def _draw_slider(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        value_text = item.extra.get("value_text") or f"{item.value:.2f}"
        # The value sits on the label line, right-aligned; a long localized
        # label (FR: "Résolution de traitement du réseau") would run under
        # it - clip the label to the space left of the value instead.
        val = self._mono.render(value_text, True, _rgb(self.c["accent"]))
        label_max = item.rect.right - val.get_width() - self._u(12) - item.rect.x
        label = self._clip(self._font, item.extra.get("label", item.key),
                           _rgb(self.c["text"]), label_max)
        surface.blit(label, (item.rect.x, item.rect.y))
        surface.blit(val, (item.rect.right - val.get_width(), item.rect.y))

        track_y = item.rect.y + label_h + self._u(10)
        track = pygame.Rect(item.rect.x, track_y, item.rect.w, self._u(SLIDER_H))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), track,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        span = max(1e-6, item.hi - item.lo)
        frac = min(1.0, max(0.0, (item.value - item.lo) / span))
        fill = pygame.Rect(track.x, track.y, int(track.w * frac), track.h)
        pygame.draw.rect(surface, _rgb(self.c["accent"]), fill,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        # Where this value sits by default - the profile's own number, or
        # zero for a slider that runs both ways. Without it "how far have I
        # moved this" is a thing to remember rather than to see.
        mark = item.extra.get("mark")
        if mark is None and item.lo < 0.0 < item.hi:
            mark = 0.0
        if mark is not None and item.lo <= mark <= item.hi:
            mx = int(track.x + ((mark - item.lo) / span) * track.w)
            pygame.draw.rect(
                surface, _rgb(self.c["muted"]),
                pygame.Rect(mx - max(1, self._u(1)),
                            track.y - self._u(3),
                            max(2, self._u(2)),
                            track.h + self._u(6)),
                border_radius=max(1, self._u(1)))
        cx = int(track.x + frac * track.w)
        pygame.draw.circle(surface, _rgb(self.c["accent"]), (cx, track.centery), self._u(KNOB_R))
        # What the two ends mean. A number like 0.35 says nothing about
        # which way is more.
        ends = item.extra.get("ends")
        if ends:
            left, right = ends
            y = track.bottom + self._u(6)
            if left:
                surface.blit(self._small_font.render(
                    left, True, _rgb(self.c["muted"])), (track.x, y))
            if right:
                img = self._small_font.render(right, True, _rgb(self.c["muted"]))
                surface.blit(img, (track.right - img.get_width(), y))
        pygame.draw.circle(surface, _rgb(self.c["bg"]), (cx, track.centery), self._u(KNOB_R) // 2)
        item.extra["track"] = track

        hint = item.extra.get("hint")
        if hint:
            h = self._small_font.render(hint, True, _rgb(self.c["muted"]))
            surface.blit(h, (item.rect.x, track.bottom + self._u(6)))

    def _draw_choice(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        label = self._font.render(item.extra.get("label", item.key), True, _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x, item.rect.y))

        strip = pygame.Rect(item.rect.x, item.rect.y + label_h,
                            item.rect.w, item.rect.h - label_h)
        pygame.draw.rect(surface, _rgb(self.c["surface"]), strip,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface, _rgb(self.c["border"]), strip, self._u(1),
                         border_radius=self._u(RADIUS // 2))
        cur_val = str(item.extra.get("current", ""))
        labels = item.extra.get("labels") or item.payload or []
        if cur_val in (item.payload or []):
            cur_val = str(labels[item.payload.index(cur_val)])
        cur = self._font.render(cur_val, True,
                                _rgb(self.c["text"]))
        surface.blit(cur, (strip.x + self._u(12),
                           strip.centery - cur.get_height() // 2))
        cx = strip.right - self._u(16)
        cy = strip.centery
        size = self._u(5)
        up = self.open_choice == item.key
        pts = ([(cx - size, cy + size // 2), (cx + size, cy + size // 2), (cx, cy - size)]
               if up else
               [(cx - size, cy - size // 2), (cx + size, cy - size // 2), (cx, cy + size)])
        pygame.draw.polygon(surface, _rgb(self.c["accent"]), pts)
        item.extra["strip"] = strip
        hint = item.extra.get("hint")
        if hint:
            y = strip.bottom + self._u(8)
            for line in str(hint).split("\n"):
                img = self._clip(self._small_font, line, _rgb(self.c["muted"]),
                                 item.rect.w)
                surface.blit(img, (item.rect.x, y))
                y += self._small_font.get_height() + self._u(4)

    def _draw_options(self, surface) -> None:
        """The entries of the expanded list - above the rest of the content.

        The windows page rows are option items too, but they live in
        self.items (they are the page's content, not a pop-up list) - draw
        both sets.
        """
        rows = list(getattr(self, "options", []))
        rows += [i for i in self.items if i.kind == "option"]
        for i, opt in enumerate(rows):
            selected = opt.extra.get("selected")
            highlighted = opt.extra.get("highlighted")
            # The mouse hover: the pop-up rows are indexed by their position
            # in self.options; the windows page rows carry their payload
            # (the outline on the screen is easy to miss, so the row itself
            # is highlighted too).
            hovered = ((i < len(self.options)
                        and self.hover == f"option:{i}")
                       or self.hover == f"woption:{opt.payload}")
            if selected:
                fill = self.c["accent"]
            elif highlighted or hovered:
                fill = self.c["surface"]
            else:
                fill = self.c["bg"]
            pygame.draw.rect(surface, _rgb(fill), opt.rect,
                             border_radius=self._u(RADIUS // 2))
            pygame.draw.rect(surface, _rgb(self.c["border"]), opt.rect,
                             self._u(1), border_radius=self._u(RADIUS // 2))
            color = self.c["bg"] if selected else self.c["text"]
            text = opt.extra.get("label", "")
            # The language list shows every language in its own script; the
            # CJK names (中文, 日本語, 한국어) need a CJK font - the current
            # UI font renders them as boxes. Pick by the script: hangul
            # (AC00-D7AF) -> Malgun Gothic, kana (3040-30FF) -> Yu Gothic,
            # CJK ideographs -> YaHei (user: Asian names show as squares).
            font = self._font
            if self._cjk_fonts:
                if any(0xAC00 <= ord(ch) <= 0xD7AF for ch in text):
                    font = self._cjk_fonts.get("malgungothic") or font
                elif any(0x3040 <= ord(ch) <= 0x30FF for ch in text):
                    font = self._cjk_fonts.get("yugothic") or font
                elif any(0x4E00 <= ord(ch) <= 0x9FFF for ch in text):
                    font = self._cjk_fonts.get("microsoftyahei") or font
            label = self._clip(font, text, _rgb(color), opt.rect.w - self._u(24))
            surface.blit(label, (opt.rect.x + self._u(12),
                                 opt.rect.centery - label.get_height() // 2))
        # The list's scrollbar (only when the list actually scrolls).
        if getattr(self, "_opt_track", None) is not None and self._opt_track.w > 0 \
                and self._opt_max_scroll > 0:
            pygame.draw.rect(surface, _rgb(self.c["border"]), self._opt_track,
                             border_radius=self._u(2))
            pygame.draw.rect(surface, _rgb(self.c["muted"]), self._opt_thumb,
                             border_radius=self._u(2))

    def _draw_sections(self, surface) -> None:
        """A block title: small caps and a hairline out to the right edge."""
        for title, rect in getattr(self, "_section_rects", []):
            img = self._small_font.render(title.upper(), True,
                                          _rgb(self.c["muted"]))
            surface.blit(img, (rect.x, rect.y))
            ly = rect.y + img.get_height() // 2
            x0 = rect.x + img.get_width() + self._u(10)
            if x0 < rect.right:
                pygame.draw.line(surface, _rgb(self.c["border"]),
                                 (x0, ly), (rect.right, ly), 1)

    def _draw_rules(self, surface, s: dict) -> None:
        """The divider before the footer, plus the hint."""
        rect = getattr(self, "_rule_rect", None)
        if rect is not None and rect.w > 0:
            pygame.draw.line(surface, _rgb(self.c["border"]),
                             (rect.x, rect.y), (rect.right, rect.y), 1)
        hint = getattr(self, "_hint_rect", None)
        if self.page == "settings" and hint is not None and hint.w > 0:
            img = self._small_font.render(s["hotkey_hint"], True,
                                          _rgb(self.c["muted"]))
            surface.blit(img, (hint.x, hint.y))

    def _draw_segmented(self, surface, item: Item, s: dict) -> None:
        """Two or three options side by side: the chosen one is accent-filled."""
        label = item.extra.get("label")
        if label:
            img = self._font.render(label, True, _rgb(self.c["muted"]))
            surface.blit(img, (self.panel_rect.x + self._u(PAD),
                               item.rect.centery - img.get_height() // 2))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        options = item.payload or []
        labels = item.extra.get("labels") or options
        if not options:
            return
        cell = item.rect.w // len(options)
        current = str(item.extra.get("current", ""))
        cells = []
        for idx, opt in enumerate(options):
            cr = pygame.Rect(item.rect.x + idx * cell, item.rect.y,
                             cell, item.rect.h)
            cells.append(cr)
            active = str(opt) == current
            if active:
                pygame.draw.rect(surface, _rgb(self.c["accent"]), cr,
                                 border_radius=self._u(RADIUS // 2))
            txt = self._small_font.render(
                str(labels[idx]), True,
                _rgb(self.c["bg"] if active else self.c["muted"]))
            surface.blit(txt, (cr.centerx - txt.get_width() // 2,
                               cr.centery - txt.get_height() // 2))
        item.extra["cells"] = cells

    def _draw_icon(self, surface, item: Item, s: dict) -> None:
        """A header button: a rounded square with a glyph inside.

        At rest only the outline; on hover a fill and an accent outline:
        circles with a drawn gear looked homemade, and a properly drawn gear is
        unreadable at 30 px anyway - so instead there are three sliders, which
        is also closer in meaning to what the panel holds.
        """
        rect = item.rect
        hot = self.hover == f"icon:{item.key}"
        radius = self._u(8)
        if hot:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                             border_radius=radius)
        col = self.c["accent"] if hot else self.c["muted"]
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if hot else self.c["border"]),
                         rect, max(1, self._u(1)), border_radius=radius)
        cx, cy = rect.centerx, rect.centery
        lw = max(2, self._u(2))
        if item.key == "help":
            img = self._font.render("?", True, _rgb(col))
            surface.blit(img, (cx - img.get_width() // 2,
                               cy - img.get_height() // 2))
        elif item.key == "close":
            d = max(3, self._u(5))
            pygame.draw.line(surface, _rgb(col), (cx - d, cy - d),
                             (cx + d, cy + d), lw)
            pygame.draw.line(surface, _rgb(col), (cx + d, cy - d),
                             (cx - d, cy + d), lw)
        elif item.key == "min":
            # The collapse button: a short horizontal bar, like a window's
            # minimise glyph.
            half = max(5, self._u(7))
            pygame.draw.line(surface, _rgb(col), (cx - half, cy),
                             (cx + half, cy), lw)
        else:
            # Three sliders: a full-width line with a knob at its own place on
            # each. It reads smaller than a gear and draws without
            # antialiasing.
            half = max(5, self._u(7))
            step = max(3, self._u(5))
            knobs = (0.65, 0.35, 0.55)
            for i, kx in enumerate(knobs):
                ly = cy + (i - 1) * step
                pygame.draw.line(surface, _rgb(col), (cx - half, ly),
                                 (cx + half, ly), max(1, self._u(1)))
                px = int(cx - half + 2 * half * kx)
                pygame.draw.circle(surface, _rgb(self.c["bg"]), (px, ly),
                                   max(2, self._u(2)))
                pygame.draw.circle(surface, _rgb(col), (px, ly),
                                   max(2, self._u(2)), max(1, self._u(1)))

    def _draw_action(self, surface, item: Item, s: dict) -> None:
        """A footer button: the name, the hotkey below it, and for exit a note."""
        rect = item.rect
        filled = bool(item.extra.get("filled"))
        danger = bool(item.extra.get("danger"))
        hot = self.hover == f"action:{item.key}"
        radius = self._u(RADIUS // 2)
        if filled:
            pygame.draw.rect(surface, _rgb(self.c["accent"]), rect,
                             border_radius=radius)
            name_col = key_col = self.c["bg"]
        else:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                             border_radius=radius)
            # The danger tone marks the EDGE, not the label. As a label it
            # sat one step from the accent that paints every number on the
            # page, so the one destructive action read as another value.
            edge = (self.c["danger"] if danger
                    else self.c["accent"] if hot else self.c["border"])
            pygame.draw.rect(surface, _rgb(edge), rect,
                             self._u(2) if danger else self._u(1),
                             border_radius=radius)
            name_col = self.c["text"]
            key_col = self.c["muted"]
        name = self._font.render(item.extra.get("label", ""), True,
                                 _rgb(name_col))
        hk = item.extra.get("hotkey")
        note = item.extra.get("note")
        if hk or note:
            # The two-line layout: the name on top, the caption below. The
            # name sits a little below the top edge so the button reads as
            # centred (user: the Quit label was too close to the top).
            surface.blit(name, (rect.centerx - name.get_width() // 2,
                                rect.y + self._u(10)))
        else:
            # A single-line action (Back without a hotkey): centre it, the
            # top-anchored position was left over from the two-line layout
            # and looked off (user: the Back button is not centred).
            surface.blit(name, (rect.centerx - name.get_width() // 2,
                                rect.centery - name.get_height() // 2))
        if hk:
            img = self._small_font.render(hk, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(26)))
        if note:
            img = self._small_font.render(note, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(44)))

    def _draw_hotkey(self, surface, item: Item, s: dict) -> None:
        """A remap row: the action on the left, the key field on the right."""
        label = self._small_font.render(item.extra.get("label", ""), True,
                                        _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x,
                             item.rect.centery - label.get_height() // 2))
        fw = self._u(170)
        field = pygame.Rect(item.rect.right - fw, item.rect.y, fw, item.rect.h)
        capturing = bool(item.extra.get("capturing"))
        radius = self._u(RADIUS // 2)
        if capturing:
            pygame.draw.rect(surface, _rgb(self.c["bg"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface, _rgb(self.c["accent"]), field,
                             max(2, self._u(2)), border_radius=radius)
            txt = self._small_font.render(s["hotkey_press"], True,
                                          _rgb(self.c["accent"]))
        else:
            hot = self.hover == f"hotkey:{item.key}"
            pygame.draw.rect(surface, _rgb(self.c["surface"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if hot else self.c["border"]),
                             field, self._u(1), border_radius=radius)
            txt = self._mono_small.render(str(item.extra.get("key", "—")),
                                          True, _rgb(self.c["text"]))
        surface.blit(txt, (field.centerx - txt.get_width() // 2,
                           field.centery - txt.get_height() // 2))
        item.extra["field"] = field

    def _draw_button(self, surface, item: Item, s: dict) -> None:
        hot = self.hover == f"button:{item.key}"
        disabled = bool(item.extra.get("disabled"))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if hot and not disabled
                              else self.c["border"]),
                         item.rect, self._u(1), border_radius=self._u(RADIUS // 2))
        label = self._clip(self._font, item.extra.get("label", item.key),
                           _rgb(self.c["muted"] if disabled
                                else item.extra.get("color", self.c["text"])),
                           item.rect.w - self._u(16))
        surface.blit(label, (item.rect.centerx - label.get_width() // 2,
                             item.rect.centery - label.get_height() // 2))
