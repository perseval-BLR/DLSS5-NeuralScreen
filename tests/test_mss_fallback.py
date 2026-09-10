"""The GDI fallback (mss) opens when dxcam cannot (issue #26, Optimus).

On hybrid-graphics laptops the internal display is wired to the iGPU and
Windows refuses a cross-adapter DDA session (DXGI_ERROR_UNSUPPORTED,
0x887A0004). ScreenCapture now falls back to mss (GDI BitBlt) when
dxcam.create() raises anything - the capture must still open, and the
frames must come back as RGBA uint8 like the dxcam path.

The real ScreenCapture.__init__ runs; only the dxcam module is faked.

Checked:
* dxcam.create() raising DXGI_ERROR_UNSUPPORTED -> the mss fallback opens
  and grab() returns a real RGBA frame of the monitor size;
* the fallback is used only when dxcam fails - a working dxcam keeps the
  dxcam path;
* close() releases the mss session.
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
    """Replace sys.modules['dxcam'] with a fake whose create() is scripted."""
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
    real_dxcam = sys.modules.get("dxcam")

    # 1. dxcam raises DXGI_ERROR_UNSUPPORTED -> the mss fallback opens and
    #    grab() returns a real RGBA frame of the monitor size.
    capture._output_count = lambda: 1
    capture._refresh_dxcam_factory = lambda: None

    def create_unsupported(output_idx=0, **kw):
        raise OSError("DXGI_ERROR_UNSUPPORTED (0x887A0004)")

    _install_fake_dxcam(create_unsupported)
    try:
        cap = ScreenCapture(monitor_idx=0)
        if cap._camera is not None:
            failures.append("dxcam path used despite the failure")
        if cap._mss is None:
            failures.append("the mss fallback did not open")
        frame = cap.grab()
        if frame is None:
            failures.append("grab() returned None on the mss path")
        else:
            h, w, ch = frame.shape
            if ch != 4:
                failures.append(f"mss frame has {ch} channels, want 4")
            if (w, h) != cap.resolution:
                failures.append(f"mss frame {w}x{h} != resolution {cap.resolution}")
            # RGBA, not BGRA: the red channel must not be swapped.
            if frame[..., 0].mean() == frame[..., 2].mean():
                failures.append("mss frame looks like BGRA (R==B)")
        cap.close()
        if cap._mss is not None:
            failures.append("close() left the mss session open")
    finally:
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    # 2. A working dxcam keeps the dxcam path (no fallback).
    capture._output_count = lambda: 1
    capture._refresh_dxcam_factory = lambda: None

    def create_ok(output_idx=0, **kw):
        return _FakeCamera()

    _install_fake_dxcam(create_ok)
    try:
        cap = ScreenCapture(monitor_idx=0)
        if cap._camera is None:
            failures.append("dxcam path not used when it works")
        if cap._mss is not None:
            failures.append("the mss fallback opened despite a working dxcam")
        cap.close()
    finally:
        capture._output_count = real_count
        capture._refresh_dxcam_factory = real_refresh
        if real_dxcam is not None:
            sys.modules["dxcam"] = real_dxcam

    for fl in failures:
        print("FAIL:", fl)
    if failures:
        return 1
    print("OK: the GDI fallback (mss) opens when dxcam cannot, "
          "RGBA frames, clean close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
