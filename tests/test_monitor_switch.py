"""Monitor switch must never crash the app (issues #24/#26).

The dxcam factory caches the output list at the first import; a monitor
unplugged or a dock changed after that leaves stale indices, and
dxcam.create(output_idx=N) raises IndexError for them ('list index out of
range' in the user log). ScreenCapture now validates the index, refreshes
the factory once, and falls back to output 0 - the capture must never take
the app down.

The real ScreenCapture.__init__ runs in every scenario; only the dxcam
module (create + factory) and the two helpers are faked.

Checked:
* an out-of-range index triggers a factory refresh and falls back to 0;
* an IndexError from dxcam.create() (topology changed between the check
  and the create) is caught, the factory is refreshed, and the capture
  still opens on the same index;
* when the output is really gone (IndexError on every create) the
  exception propagates - main._switch_monitor wraps the call and falls
  back to the primary output;
* a devicename that resolves to a stale index still opens (the same
  refresh path).
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, capture.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import capture  # noqa: E402
from capture import ScreenCapture  # noqa: E402


class _FakeCamera:
    """The minimal surface ScreenCapture touches after dxcam.create()."""

    def __init__(self, devicename="\\\\.\\DISPLAY1", resolution=(1920, 1080)):
        self._output = types.SimpleNamespace(
            devicename=devicename, resolution=resolution)
        self.released = False

    def grab(self):
        return None

    def release(self):
        self.released = True


def _install_fake_dxcam(create_impl):
    """Replace sys.modules['dxcam'] with a fake whose create() is scripted.

    Returns the previous module for restoration.
    """
    fake = types.ModuleType("dxcam")
    fake.create = create_impl
    fake.__factory = types.SimpleNamespace(outputs=[[None]])
    old = sys.modules.get("dxcam")
    sys.modules["dxcam"] = fake
    return old


def main() -> int:
    failures = []
    real_count = capture._output_count
    real_refresh = capture._refresh_dxcam_factory
    real_resolve = capture.resolve_output_idx
    real_dxcam = sys.modules.get("dxcam")

    # 1. Out-of-range index: refresh the factory, then fall back to 0.
    events = []
    calls = []
    capture._output_count = lambda: 1  # only output 0 exists
    capture._refresh_dxcam_factory = lambda: events.append("refresh")

    def create1(output_idx=0, **kw):
        calls.append(output_idx)
        return _FakeCamera()

    _install_fake_dxcam(create1)
    try:
        cap = ScreenCapture(monitor_idx=5)
        if cap.monitor_idx != 0:
            failures.append(f"out-of-range index did not fall back: "
                            f"monitor_idx={cap.monitor_idx}")
        if "refresh" not in events:
            failures.append("out-of-range index did not refresh the factory")
        if calls != [0]:
            failures.append(f"create called with {calls}, want [0]")
    finally:
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    # 2. IndexError from dxcam.create(): refresh + retry, still opens.
    events = []
    calls = []
    capture._output_count = lambda: 2  # the index looks valid
    capture._refresh_dxcam_factory = lambda: events.append("refresh")

    def create2(output_idx=0, **kw):
        calls.append(output_idx)
        if len(calls) == 1:
            raise IndexError("list index out of range")
        return _FakeCamera()

    _install_fake_dxcam(create2)
    try:
        cap = ScreenCapture(monitor_idx=1)
        if cap.monitor_idx != 1:
            failures.append(f"IndexError retry lost the index: "
                            f"monitor_idx={cap.monitor_idx}")
        if "refresh" not in events:
            failures.append("IndexError path did not refresh the factory")
        if calls != [1, 1]:
            failures.append(f"create called with {calls}, want [1, 1]")
    finally:
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    # 3. IndexError on every create (the output is really gone): the
    #    exception propagates - main._switch_monitor wraps the call.
    calls = []
    capture._output_count = lambda: 2
    capture._refresh_dxcam_factory = lambda: None

    def create3(output_idx=0, **kw):
        calls.append(output_idx)
        raise IndexError("list index out of range")

    _install_fake_dxcam(create3)
    try:
        try:
            ScreenCapture(monitor_idx=1)
            failures.append("double IndexError did not raise")
        except IndexError:
            pass
        if calls != [1, 1, 0]:
            failures.append(f"create called with {calls}, want [1, 1, 0]")
    finally:
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    # 4. A devicename resolving to a stale index still opens (the same
    #    refresh path as #1, through the devicename branch).
    events = []
    calls = []
    capture.resolve_output_idx = lambda dev: 7  # stale index
    capture._output_count = lambda: 1
    capture._refresh_dxcam_factory = lambda: events.append("refresh")

    def create4(output_idx=0, **kw):
        calls.append(output_idx)
        return _FakeCamera()

    _install_fake_dxcam(create4)
    try:
        cap = ScreenCapture(devicename="\\\\.\\DISPLAY1")
        if cap.monitor_idx != 0:
            failures.append(f"stale devicename index did not fall back: "
                            f"monitor_idx={cap.monitor_idx}")
        if "refresh" not in events:
            failures.append("stale devicename index did not refresh")
        if calls != [0]:
            failures.append(f"create called with {calls}, want [0]")
    finally:
        capture.resolve_output_idx = real_resolve
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    for fl in failures:
        print("FAIL:", fl)
    if failures:
        return 1
    print("OK: monitor switch cannot crash the capture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
