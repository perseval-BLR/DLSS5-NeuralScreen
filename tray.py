"""TrayController - system tray icon for DLSS 5 Desktop NR.

Right-click menu: NR ON/OFF (with a checkmark), Scale (state), Settings,
Scale +0.05 / -0.05, Exit. Left click on the icon is the default action =
open the menu (Windows convention: right click for the menu, left for the
default). Commands go into a queue.Queue that the main loop drains.

The icon is the channel avatar (a black turbine fan with a gold "P"):
the same image the launcher and the taskbar button show. Loaded from
native/neuralscreen-tray.png, a round crop of the avatar.

Menu labels come from the caller: they are user-visible text, so they live
in i18n like the rest of the interface, not in this module.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

#: Fallback labels, used when the caller passes none.
DEFAULT_LABELS = {"settings": "Settings", "quit": "Exit"}


def _make_icon(size: int = 64) -> Image.Image:
    """The tray image: the round avatar crop at the requested size."""
    png = Path(__file__).resolve().parent / "native" / "neuralscreen-tray.png"
    if png.is_file():
        return Image.open(png).resize((size, size), Image.LANCZOS)
    # Fallback (the file is missing - a dev tree): the old placeholder, a
    # dark square with an amber accent.
    img = Image.new("RGBA", (size, size), (0x0D, 0x11, 0x17, 255))
    d = size // 8
    ImageDraw.Draw(img).rectangle((d, d, size - d, size - d),
                                  fill=(0xFF, 0xBF, 0x00, 255))
    return img


class TrayController:
    """Tray icon: commands into a queue, state for the menu to show."""

    def __init__(self, commands: queue.Queue, labels: dict | None = None):
        self._commands = commands
        self._labels = dict(DEFAULT_LABELS, **(labels or {}))
        self._state = {"nr": True, "scale": 0.5}
        self._icon = None
        self._thread = None

    def _set_state(self, **kw) -> None:
        self._state.update(kw)
        if self._icon is not None:
            try:
                self._icon.title = (f"NeuralScreen — NR {'ON' if self._state['nr'] else 'OFF'}"
                                    f" | scale {self._state['scale']:.2f}")
                self._icon.update_menu()
            except Exception:
                pass

    def _cmd(self, name: str) -> None:
        self._commands.put(name)

    def _toggle_nr(self, icon, item) -> None:
        self._set_state(nr=not self._state["nr"])
        self._cmd("toggle")

    def _open_settings(self, icon, item) -> None:
        self._cmd("settings")

    # Scale is NOT changed optimistically: only main knows the bounds and the
    # step (WORK_SCALE_MIN/MAX), and it may also defer applying because of the
    # cooldown. The actual value comes back through _set_state(scale=...).
    def _scale_up(self, icon, item) -> None:
        self._cmd("scale_up")

    def _scale_down(self, icon, item) -> None:
        self._cmd("scale_down")

    def _quit(self, icon, item) -> None:
        self._cmd("quit")

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("NR: ON", self._toggle_nr,
                             checked=lambda item: self._state["nr"]),
            pystray.MenuItem("NR: OFF", self._toggle_nr,
                             checked=lambda item: not self._state["nr"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Scale: {self._state['scale']:.2f}", None, enabled=False),
            pystray.MenuItem("Scale +0.05", self._scale_up),
            pystray.MenuItem("Scale -0.05", self._scale_down),
            pystray.Menu.SEPARATOR,
            # default=True: left click on the icon triggers this item
            pystray.MenuItem(self._labels["settings"], self._open_settings,
                             default=True),
            pystray.MenuItem(self._labels["quit"], self._quit),
        )

    def start(self) -> None:
        """Start the tray in its own thread (does not block main)."""
        self._icon = pystray.Icon("neuralscreen", _make_icon(),
                                  "NeuralScreen", self._build_menu())
        self._set_state()
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
