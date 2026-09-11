"""The theme the user picked survives a pipeline rebuild.

A rebuild - a monitor switch, a GPU switch, one-window mode - recreates the
menu and restores its theme from st.cfg. The theme was only written to
config.json when the menu closed, so a switch while the menu was open
restored the OLD value and threw the user back to light (issue #33, seen in
a user's log: "menu theme -> dark", then a monitor change, then light
again).

So the action lands in st.cfg immediately, and the file write stays where it
was. This test drives the menu action and then the restore that
rebuild_pipeline performs.
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import commands  # noqa: E402


class _Menu:
    def __init__(self):
        self.state = {"theme": "light"}

    def set_state(self, payload):
        for k, v in payload.items():
            if k in self.state:
                self.state[k] = v


def _state():
    menu = _Menu()
    return types.SimpleNamespace(
        cfg={"theme": "light"}, lang="en",
        display=types.SimpleNamespace(menu=menu, alert=lambda *a, **kw: None))


def _restore(st):
    """What rebuild_pipeline does with the theme after recreating the menu."""
    saved = st.cfg.get("theme")
    if isinstance(saved, str) and saved in ("light", "dark"):
        st.display.menu.set_state({"theme": saved})


def main() -> int:
    failures = []

    # 1. Picking dark reaches the state, not just the menu.
    st = _state()
    st.display.menu.set_state({"theme": "dark"})      # the menu applies it itself
    commands.apply_menu_action(st, ("theme", "dark"))
    if st.cfg.get("theme") != "dark":
        failures.append(f"the state says theme={st.cfg.get('theme')!r}")

    # 2. A rebuild restores what the user picked, not what was on disk.
    st.display.menu = _Menu()                          # the rebuild recreates it
    _restore(st)
    if st.display.menu.state["theme"] != "dark":
        failures.append(f"a rebuild restored "
                        f"{st.display.menu.state['theme']!r}, not dark")

    # 3. Back to light works the same way round.
    commands.apply_menu_action(st, ("theme", "light"))
    st.display.menu = _Menu()
    st.display.menu.state["theme"] = "dark"
    _restore(st)
    if st.cfg.get("theme") != "light" or st.display.menu.state["theme"] != "light":
        failures.append("switching back to light did not survive the rebuild")

    # 4. A value that is not a theme is refused rather than stored.
    commands.apply_menu_action(st, ("theme", "chartreuse"))
    if st.cfg.get("theme") != "light":
        failures.append(f"a bogus theme was stored: {st.cfg.get('theme')!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the chosen theme survives a rebuild")
    return 0


if __name__ == "__main__":
    sys.exit(main())
