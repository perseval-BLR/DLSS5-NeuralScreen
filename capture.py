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
import sys
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


def _output_count() -> int:
    """The number of outputs dxcam currently knows on the primary adapter.

    0 means dxcam is unavailable or its factory failed - callers treat it
    as "no valid index", never as output 0.
    """
    try:
        import dxcam

        return len(dxcam.__factory.outputs[0])
    except Exception:
        return 0


def _refresh_dxcam_factory() -> None:
    """Re-enumerate the DXGI adapters/outputs in the dxcam factory.

    The factory is a process-wide Singleton built at the first import; a
    monitor unplugged or a dock changed after that leaves a stale outputs
    list, and dxcam.create(output_idx=N) then raises IndexError for an
    index that was valid a minute ago (issue #24/#26: 'list index out of
    range' on a monitor switch). Dropping the cached instance makes the
    next access re-enumerate.
    """
    try:
        import dxcam

        dxcam.Singleton._instances.pop(dxcam.DXFactory, None)
        dxcam.__factory = dxcam.DXFactory()
    except Exception:
        pass


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


#: The adapter list, enumerated once (see list_adapters).
_ADAPTERS: list | None = None


def list_adapters() -> list[tuple[int, str]]:
    """NVIDIA cards as [(dxgi_index, name), ...], in EnumAdapters1 order.

    The index is what matters: it is what the worker's NS_GPU takes and what
    its "[host] adapter N: ..." lines print, so the menu and the log agree on
    which card is which.

    Only NVIDIA, and never the software renderer: the network cannot run
    anywhere else, and the capture has to sit on the same card as the network
    (the frame reaches D3D12 through a shared handle, which does not cross
    adapters). A machine with one card gets a one-item list, and the menu
    hides the choice.

    Enumerated once per process and kept: menu_payload runs on every frame
    while the menu is open, and two DXGI enumerations per frame cost more
    than the network does (measured: 29 -> 13.6 FPS). Cards are not
    hot-plugged, and moving the worker to another one restarts it anyway.
    """
    global _ADAPTERS
    if _ADAPTERS is not None:
        return _ADAPTERS
    try:
        from dxcam.core.device import Device
        from dxcam.util.io import enum_dxgi_adapters
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    try:
        for idx, adapter in enumerate(enum_dxgi_adapters()):
            desc = Device(adapter).desc
            # 0x10DE is NVIDIA; flag 2 is DXGI_ADAPTER_FLAG_SOFTWARE.
            if desc.VendorId != 0x10DE or (getattr(desc, "Flags", 0) & 2):
                continue
            out.append((idx, str(desc.Description).strip()))
    except Exception as exc:
        print(f"[capture] could not enumerate the adapters: {exc}",
              file=sys.stderr)
        return []
    _ADAPTERS = out
    return out


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
    """Monitor capture through DXCamera (Desktop Duplication API).

    On hybrid-graphics laptops (Optimus) the internal display is wired to
    the iGPU and Windows refuses a cross-adapter DDA session
    (DXGI_ERROR_UNSUPPORTED, 0x887A0004 - issue #26). When dxcam cannot
    open at all, the capture falls back to mss (GDI BitBlt): slower, but
    it works on any display wiring. The worker's own DDA path is
    unaffected - this is the Python-side capture used when the worker
    cannot capture either.
    """

    def __init__(self, monitor_idx: int = 0, devicename: str | None = None):
        import dxcam

        self._dxcam = dxcam
        self._mss = None  # the GDI fallback session, when dxcam is unusable
        if devicename is not None:
            # Resolve by identity: the devicename is the stable handle, the
            # output index is whatever dxcam assigns today.
            resolved = resolve_output_idx(devicename)
            if resolved is None:
                raise ValueError(
                    f"no dxcam output matches devicename {devicename!r}")
            monitor_idx = resolved
        # The dxcam factory caches the output list at the first import; a
        # monitor unplugged or a dock changed since then leaves stale
        # indices, and dxcam.create() raises IndexError for them (issue
        # #24/#26). Validate, refresh the factory once, then fall back to
        # output 0 - the capture must never take the app down.
        if monitor_idx >= _output_count():
            print(f"[capture] output {monitor_idx} is gone - "
                  "re-enumerating the dxcam factory", file=sys.stderr)
            _refresh_dxcam_factory()
            if monitor_idx >= _output_count():
                print(f"[capture] output {monitor_idx} still missing - "
                      "falling back to output 0", file=sys.stderr)
                monitor_idx = 0
        self.monitor_idx = monitor_idx
        # output_color="RGBA": dxcam converts BGRA->RGBA into its own reusable
        # buffer. This used to be a cv2.cvtColor right here — an extra 33 MB
        # allocated for every 4K frame.
        try:
            self._camera = dxcam.create(
                output_idx=monitor_idx,
                output_color="RGBA",
            )
        except IndexError:
            # The factory was fresh a moment ago but the topology changed
            # between the check and the create - one more refresh, then the
            # primary output as the last resort.
            print(f"[capture] dxcam.create({monitor_idx}) raised IndexError - "
                  "re-enumerating and retrying", file=sys.stderr)
            _refresh_dxcam_factory()
            try:
                self._camera = dxcam.create(
                    output_idx=monitor_idx,
                    output_color="RGBA",
                )
            except IndexError:
                print("[capture] the chosen output is gone - "
                      "falling back to output 0", file=sys.stderr)
                self.monitor_idx = 0
                self._camera = dxcam.create(
                    output_idx=0,
                    output_color="RGBA",
                )
        except Exception as exc:
            # DXGI_ERROR_UNSUPPORTED on hybrid graphics (issue #26): the
            # display is wired to the iGPU and DDA refuses a cross-adapter
            # session. mss (GDI) captures any display - slower, but it
            # works. The worker's own DDA path is tried separately and
            # falls back to Python-side frames the same way.
            print(f"[capture] dxcam unavailable ({exc}) - "
                  "falling back to mss (GDI)", file=sys.stderr)
            self._camera = None
        if self._camera is None:
            self._open_mss(monitor_idx)
            return
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

    def _open_mss(self, monitor_idx: int) -> None:
        """Open the GDI fallback (mss) for the given monitor index."""
        import mss

        # mss 10 deprecated the lowercase factory ("will be removed in a
        # future release"); the bundled runtime already warns about it. Use
        # the new name where it exists so a runtime bump does not take the
        # fallback down with it.
        self._mss = (mss.MSS if hasattr(mss, "MSS") else mss.mss)()
        # mss.monitors[0] is the virtual all-in-one screen; the physical
        # monitors start at index 1 (the stas2192 pattern, issue #26).
        real_idx = monitor_idx + 1
        if real_idx >= len(self._mss.monitors):
            real_idx = 1 if len(self._mss.monitors) > 1 else 0
        self._monitor = self._mss.monitors[real_idx]
        self.resolution = (int(self._monitor["width"]),
                           int(self._monitor["height"]))
        self.devicename = f"\\\\.\\DISPLAY{monitor_idx + 1}"
        print(f"[capture] mss (GDI) capture active: {self.resolution}",
              file=sys.stderr)

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
        if self._camera is not None:
            return self._camera.grab()
        # The mss (GDI) fallback: BGRA -> RGBA in one indexed copy (the raw
        # buffer belongs to mss and is reused). GDI leaves the alpha byte at
        # 0; the pipeline treats the frame as opaque RGBA8, so alpha is
        # forced to 255 rather than left as a trap for whatever reads it
        # next (a screenshot encoder, a texture upload).
        raw = self._mss.grab(self._monitor)
        img = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
            (raw.height, raw.width, 4))
        rgba = img[:, :, [2, 1, 0, 3]]      # one copy, channels in place
        rgba[:, :, 3] = 255
        return rgba

    def close(self) -> None:
        """Release the capture resources."""
        if self._camera is not None:
            self._camera.release()
            self._camera = None
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
