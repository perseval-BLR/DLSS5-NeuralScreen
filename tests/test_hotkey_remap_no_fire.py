"""A key pressed during a remap must not fire after the rebind.

The user remaps a hotkey: the menu captures the key (hotkeys suspended),
then rebinds. The key they just pressed is still physically down when
the poller comes back - and the poller, whose `_poll_down` table never
saw that key, treated it as a fresh press and fired the command the user
just reassigned (issue: remapping Divide to Num2 auto-executed the
action).

The fix: `_unregister()` resets `_poll_down`/`_poll_last`, so the next
tick re-baselines from the live key state - a held key is the baseline,
not an event.

Checked: with the key held across suspend+rebind, no command fires;
after it is released and pressed again, the command fires exactly once.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hotkeys  # noqa: E402

VK_NUMPAD2 = 0x62


class _Queue:
    """A minimal queue stand-in: the test reads the commands as a list."""

    def __init__(self, out: list):
        self._out = out

    def put(self, cmd) -> None:
        self._out.append(cmd)


class FakeKeys:
    """A stand-in for the Win32 key state: the test controls it directly."""

    def __init__(self):
        self.down = set()

    def pressed(self, vk):
        return vk in self.down


def main() -> int:
    failures = []
    fake = FakeKeys()
    real_pressed = hotkeys._pressed
    hotkeys._pressed = fake.pressed
    try:
        commands = []
        hk = hotkeys.HotkeyController(
            _Queue(commands),
            {"settings": ("Num2", VK_NUMPAD2)})
        hk._active = True
        hk._bindings = {1: (0, VK_NUMPAD2, "settings", "Num2")}

        # 1. Baseline: the key is up, one quiet tick.
        hk._poll_tick()
        if commands:
            failures.append("baseline tick must not fire")

        # 2. The user starts a remap: the menu captures the key, the
        #    hotkeys are suspended (unregister + poller baseline reset).
        hk._unregister()
        if hk._active:
            failures.append("suspend must deactivate the hotkeys")
        if hk._poll_down:
            failures.append("suspend must reset the poller baseline")

        # 3. The user presses the new key while the capture is active.
        fake.down.add(VK_NUMPAD2)

        # 4. The rebind lands: the hotkeys come back, the poller ticks
        #    while the key is still physically down. It must treat the
        #    current state as the baseline - no command.
        hk._active = True
        hk._poll_tick()
        print(f"after rebind with the key held -> commands {commands}")
        if commands:
            failures.append("the key pressed for the remap must not fire")

        # 5. Released, then pressed again: exactly one command.
        fake.down.discard(VK_NUMPAD2)
        hk._poll_tick()
        fake.down.add(VK_NUMPAD2)
        hk._poll_tick()
        print(f"after a real press -> commands {commands}")
        if commands != ["settings"]:
            failures.append(f"a real press should fire once, got {commands}")

        # 6. Held again: no repeat.
        hk._poll_tick()
        if commands != ["settings"]:
            failures.append("holding must not repeat the command")
    finally:
        hotkeys._pressed = real_pressed

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: a key pressed during a remap is the baseline, not an event")
    return 0


if __name__ == "__main__":
    sys.exit(main())
