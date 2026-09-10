"""ScreenCapture - desktop capture for the DLSS 5 NR prototype (desktop-nr).

Backend: DXCamera (Windows Desktop Duplication API, DXGI).
Frames come back as np.ndarray shape (H, W, 4) dtype uint8 in RGBA (dxcam
does the BGRA conversion itself, into a reusable buffer).

Monitor identity: monitors are matched by their DXGI DeviceName
('\\\\.\\DISPLAY1'), not by a positional index. list_monitors() pairs each
EnumDisplayMonitors entry with the dxcam output whose devicename matches, so
the returned index is the dxcam output_idx for THAT monitor. A saved
devicename therefore keeps pointing at the same physical monitor when the
arrangement changes (cable unplug, display reorder, laptop dock).

Example:
    cap = ScreenCapture(monitor_idx=0)
    frame = cap.grab()          # (2160, 3840, 4) uint8 RGBA
    cap.close()
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np


class _MONITORINFOEXW(ctypes.Structure):
    """MONITORINFOEXW: the monitor rect plus the szDevice name."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _dxcam_output_index_by_devicename() -> dict[str, int]:
    """Map each DXGI devicename to its dxcam output_idx.

    dxcam.create(output_idx=N) indexes the outputs of the primary adapter
    (device_idx=0); the factory's outputs list is [adapter][output], and the
    index of an Output inside its adapter's list IS the output_idx. Reading
    the factory's Output objects is cheap (GetDesc only) - no DDA session is
    opened, unlike dxcam.create().
    """
    import dxcam

    mapping: dict[str, int] = {}
    for outputs in dxcam.__factory.outputs:
        for idx, output in enumerate(outputs):
            mapping.setdefault(output.devicename, idx)
    return mapping


def resolve_output_idx(devicename: str) -> int | None:
    """The dxcam output_idx for a DXGI devicename ('\\\\.\\DISPLAY1'), or None.

    None means the monitor is not present in the current DXGI output list
    (unplugged, dock changed, driver reset).
    """
    return _dxcam_output_index_by_devicename().get(devicename)


def devicename_for_output_idx(output_idx: int) -> str | None:
    """The DXGI devicename of the dxcam output at output_idx, or None.

    The inverse of resolve_output_idx - used when saving the config so the
    monitor is remembered by identity instead of by a positional index.
    """
    try:
        by_name = _dxcam_output_index_by_devicename()
    except Exception:
        return None
    for devicename, idx in by_name.items():
        if idx == output_idx:
            return devicename
    return None


def list_monitors() -> list[tuple[int, int, int, str]]:
    """Monitors as [(idx, w, h, devicename), ...].

    idx is the dxcam output index matched BY devicename (DXGI DeviceName,
    e.g. '\\\\.\\DISPLAY1'), not the EnumDisplayMonitors order - the two
    orders can differ after a cable unplug or a display reorder. When a
    monitor is not in the dxcam output list the positional index is used as
    a fallback (the old behavior). DPI awareness must already be set in the
    calling process, otherwise the sizes come back in scaled pixels.
    """
    monitors: list[tuple[int, int, int, str]] = []

    def _cb(hmon, _hdc, lprect, _lparam) -> bool:
        r = lprect.contents
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        devicename = ""
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            devicename = "".join(info.szDevice).rstrip("\x00")
        monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                         devicename))
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0)

    try:
        by_name = _dxcam_output_index_by_devicename()
    except Exception:
        # dxcam unavailable or its factory failed - fall back to the
        # positional order (the old behavior).
        by_name = {}
    return [
        (by_name.get(devicename, i), w, h, devicename)
        for i, (_x, _y, w, h, devicename) in enumerate(monitors)
    ]


class ScreenCapture:
    """Monitor capture through DXCamera (Desktop Duplication API)."""

    def __init__(self, monitor_idx: int = 0, devicename: str | None = None):
        import dxcam

        self._dxcam = dxcam
        if devicename is not None:
            # Resolve by identity: the devicename is the stable handle, the
            # output index is whatever dxcam assigns today.
            resolved = resolve_output_idx(devicename)
            if resolved is None:
                raise ValueError(
                    f"no dxcam output matches devicename {devicename!r}")
            monitor_idx = resolved
        self.monitor_idx = monitor_idx
        # output_color="RGBA": dxcam converts BGRA->RGBA into its own reusable
        # buffer. This used to be a cv2.cvtColor right here — an extra 33 MB
        # allocated for every 4K frame.
        self._camera = dxcam.create(
            output_idx=monitor_idx,
            output_color="RGBA",
        )
        if self._camera is None:
            raise RuntimeError(
                f"dxcam.create(output_idx={monitor_idx}) returned None — "
                "monitor not found or capture unavailable"
            )
        # The monitor identity of the output that was actually opened.
        output = getattr(self._camera, "_output", None)
        self.devicename = getattr(output, "devicename", None) or devicename or ""
        # Monitor resolution (W, H) from the output description
        res = getattr(output, "resolution", None)
        if res is not None:
            self.resolution = (int(res[0]), int(res[1]))
        else:
            # Fallback: the first frame
            probe = self._camera.grab()
            if probe is None:
                raise RuntimeError(
                    "could not grab a first frame to determine the resolution")
            self.resolution = (probe.shape[1], probe.shape[0])

    @classmethod
    def resolve_monitor(cls, devicename: str) -> int | None:
        """The dxcam output_idx for a devicename, or None when it is gone."""
        return resolve_output_idx(devicename)

    def grab(self) -> np.ndarray:
        """Grab the monitor's current frame.

        Returns:
            np.ndarray shape (H, W, 4) dtype uint8, RGBA channels,
            C-contiguous (frombuffer/tobytes without a copy).
            May return None when the frame is not ready yet (rare).

        Every grab() hands back a separate array: neighbouring frames do not
        share memory (checked — _work/test_capture_rgba.py), so a frame can be
        held across a loop iteration.
        """
        return self._camera.grab()

    def close(self) -> None:
        """Release the capture resources."""
        if self._camera is not None:
            self._camera.release()
            self._camera = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
