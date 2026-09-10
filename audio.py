"""LoopbackCapture - system audio ("what you hear") through WASAPI loopback.

Records what the default playback device is playing, so a recording carries the
game or video sound without a virtual cable and without a microphone.

Written on raw ctypes/comtypes on purpose: comtypes is already in the runtime
(dxcam pulls it), while sounddevice/PortAudio or pyaudiowpatch would add a
dependency and megabytes to a runtime that was deliberately slimmed down.

WASAPI in loopback mode has one trap worth knowing: while nothing is playing at
all, the endpoint hands back NO data - not silence, nothing. A recorder that
just concatenates what it gets ends up with audio shorter than the video and
drifting away from it. So read() reports how many frames it actually got and
the caller pads the gaps from the clock (see recorder.VideoRecorder).

Usage:
    cap = LoopbackCapture()
    cap.start()
    while ...:
        chunk = cap.read()      # (n, 2) float32 or None
    cap.close()
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import POINTER, byref, c_void_p
from ctypes.wintypes import DWORD, WORD

import numpy as np
from comtypes import COMMETHOD, GUID, CoCreateInstance, CoInitialize, CoUninitialize, IUnknown

# --- WASAPI constants ------------------------------------------------------
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

EDATAFLOW_RENDER = 0
EROLE_CONSOLE = 0
CLSCTX_ALL = 23

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID("{00000003-0000-0010-8000-00AA00389B71}")

#: Buffer the endpoint keeps for us, in 100 ns units. 400 ms is generous: the
#: reader polls far more often, and the slack only matters when the machine
#: stalls (a fullscreen game starting, say). Losing audio there is worse than
#: holding a bigger buffer.
BUFFER_DURATION_100NS = 4_000_000


class WAVEFORMATEX(ctypes.Structure):
    # pshpack1.h in mmreg.h: the structs are byte-packed, no alignment
    # padding. Without _pack_ ctypes aligns nAvgBytesPerSec/SubFormat and
    # the structs come out larger than the real ABI - the layout read from
    # the audio endpoint would be wrong (code review finding).
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", WORD),
        ("dwChannelMask", DWORD),
        ("SubFormat", GUID),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                  (["out"], POINTER(ctypes.c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64QPCPosition")),
        COMMETHOD([], ctypes.HRESULT, "ReleaseBuffer",
                  (["in"], ctypes.c_uint32, "NumFramesRead")),
        COMMETHOD([], ctypes.HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(ctypes.c_uint32), "pNumFramesInNextPacket")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "Initialize",
                  (["in"], ctypes.c_uint32, "ShareMode"),
                  (["in"], DWORD, "StreamFlags"),
                  (["in"], ctypes.c_int64, "hnsBufferDuration"),
                  (["in"], ctypes.c_int64, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], ctypes.HRESULT, "GetBufferSize",
                  (["out"], POINTER(ctypes.c_uint32), "pNumBufferFrames")),
        COMMETHOD([], ctypes.HRESULT, "GetStreamLatency",
                  (["out"], POINTER(ctypes.c_int64), "phnsLatency")),
        COMMETHOD([], ctypes.HRESULT, "GetCurrentPadding",
                  (["out"], POINTER(ctypes.c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], ctypes.HRESULT, "IsFormatSupported",
                  (["in"], ctypes.c_uint32, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], ctypes.HRESULT, "GetMixFormat",
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], ctypes.HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(ctypes.c_int64), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(ctypes.c_int64), "phnsMinimumDevicePeriod")),
        COMMETHOD([], ctypes.HRESULT, "Start"),
        COMMETHOD([], ctypes.HRESULT, "Stop"),
        COMMETHOD([], ctypes.HRESULT, "Reset"),
        COMMETHOD([], ctypes.HRESULT, "SetEventHandle",
                  (["in"], c_void_p, "eventHandle")),
        COMMETHOD([], ctypes.HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(c_void_p), "ppv")),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], DWORD, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(c_void_p), "ppInterface")),
        COMMETHOD([], ctypes.HRESULT, "OpenPropertyStore",
                  (["in"], DWORD, "stgmAccess"),
                  (["out"], POINTER(c_void_p), "ppProperties")),
        COMMETHOD([], ctypes.HRESULT, "GetId",
                  (["out"], POINTER(ctypes.c_wchar_p), "ppstrId")),
        COMMETHOD([], ctypes.HRESULT, "GetState",
                  (["out"], POINTER(DWORD), "pdwState")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "EnumAudioEndpoints",
                  (["in"], DWORD, "dataFlow"),
                  (["in"], DWORD, "dwStateMask"),
                  (["out"], POINTER(c_void_p), "ppDevices")),
        COMMETHOD([], ctypes.HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], DWORD, "dataFlow"),
                  (["in"], DWORD, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], ctypes.HRESULT, "GetDevice",
                  (["in"], ctypes.c_wchar_p, "pwstrId"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        COMMETHOD([], ctypes.HRESULT, "RegisterEndpointNotificationCallback",
                  (["in"], c_void_p, "pClient")),
        COMMETHOD([], ctypes.HRESULT, "UnregisterEndpointNotificationCallback",
                  (["in"], c_void_p, "pClient")),
    ]


class LoopbackCapture:
    """WASAPI loopback on the default playback device.

    All the COM work lives in one thread: MMDevice objects are apartment-bound,
    and passing them between threads is exactly the kind of thing that fails
    once in a while rather than every time. The thread pushes numpy chunks into
    a list under a lock; read() takes everything accumulated so far.

    Output is always float32 (n, 2): the encoder wants one shape and does not
    care what the endpoint happens to run at. The sample rate is whatever the
    device mixes at (sample_rate) - resampling here would be pointless work,
    the AAC encoder takes 48 kHz just as happily as 44.1.
    """

    #: How long the reader thread sleeps between polls. The endpoint hands out
    #: packets at the device period (~10 ms), so polling faster only burns CPU.
    POLL_S = 0.005

    def __init__(self):
        self.sample_rate = 0
        self.channels = 0
        self.error: str | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # -- public API --------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """Start capturing. Returns False when audio is unavailable.

        Never raises: a machine with no playback device, or with WASAPI
        refusing the loopback, must still record video.
        """
        self._thread = threading.Thread(target=self._run, name="ns-audio",
                                        daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        return self.error is None and self.sample_rate > 0

    def read(self) -> np.ndarray | None:
        """Everything captured since the previous call, or None if nothing.

        Returns float32 (n, 2). A None means the endpoint had nothing - which
        in loopback mode means silence, not a failure.
        """
        with self._lock:
            if not self._chunks:
                return None
            chunks, self._chunks = self._chunks, []
        return chunks[0] if len(chunks) == 1 else np.concatenate(chunks, axis=0)

    def discard(self) -> None:
        """Drop everything captured so far (the endpoint spin-up).

        The samples that arrive between client.Start() and the recorder's
        clock are earlier than the video PTS=0; keeping them would make the
        audio track lead the picture (audit #3, D2).
        """
        with self._lock:
            self._chunks.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -- capture thread ----------------------------------------------------

    def _run(self) -> None:
        client = None
        try:
            CoInitialize()
        except Exception:
            pass
        try:
            client, capture, fmt = self._open()
            self.sample_rate = fmt["rate"]
            self.channels = 2
            self._started.set()
            client.Start()
            self._pump(capture, fmt)
        except Exception as exc:                      # noqa: BLE001
            self.error = str(exc)
            print(f"[audio] loopback unavailable: {exc}", file=sys.stderr)
            self._started.set()
        finally:
            try:
                if client is not None:
                    client.Stop()
            except Exception:
                pass
            try:
                CoUninitialize()
            except Exception:
                pass

    def _open(self):
        enumerator = CoCreateInstance(CLSID_MMDeviceEnumerator,
                                      IMMDeviceEnumerator, CLSCTX_ALL)
        device = enumerator.GetDefaultAudioEndpoint(EDATAFLOW_RENDER,
                                                    EROLE_CONSOLE)
        ptr = device.Activate(byref(IAudioClient._iid_), CLSCTX_ALL, None)
        client = ctypes.cast(ptr, POINTER(IAudioClient))

        mix = client.GetMixFormat()
        fmt = self._describe(mix)
        # Shared mode accepts only the endpoint's own mix format, so we hand
        # back exactly what GetMixFormat gave us. hnsPeriodicity must be 0 in
        # shared mode - the engine picks the period itself.
        client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                          AUDCLNT_STREAMFLAGS_LOOPBACK,
                          BUFFER_DURATION_100NS, 0, mix, None)
        ptr = client.GetService(byref(IAudioCaptureClient._iid_))
        capture = ctypes.cast(ptr, POINTER(IAudioCaptureClient))
        return client, capture, fmt

    @staticmethod
    def _describe(mix) -> dict:
        """Read the mix format: rate, channels, and how to read the samples."""
        wfx = mix.contents
        tag = wfx.wFormatTag
        bits = wfx.wBitsPerSample
        is_float = tag == WAVE_FORMAT_IEEE_FLOAT
        if tag == WAVE_FORMAT_EXTENSIBLE:
            # WAVEFORMATEXTENSIBLE carries 22 bytes of extension after the
            # base WAVEFORMATEX. A smaller cbSize means the endpoint handed
            # us a truncated format - casting past it would read garbage
            # (code review finding).
            if wfx.cbSize < 22:
                raise RuntimeError(
                    f"truncated extensible format: cbSize={wfx.cbSize} < 22")
            ext = ctypes.cast(mix, POINTER(WAVEFORMATEXTENSIBLE)).contents
            is_float = ext.SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
        if is_float and bits == 32:
            dtype, scale = np.float32, 1.0
        elif not is_float and bits == 16:
            dtype, scale = np.int16, 1.0 / 32768.0
        elif not is_float and bits == 32:
            dtype, scale = np.int32, 1.0 / 2147483648.0
        else:
            raise RuntimeError(
                f"unsupported mix format: tag={tag} bits={bits} float={is_float}")
        return {"rate": int(wfx.nSamplesPerSec), "src_channels": int(wfx.nChannels),
                "dtype": dtype, "scale": scale, "block": int(wfx.nBlockAlign)}

    def _pump(self, capture, fmt: dict) -> None:
        dtype = fmt["dtype"]
        scale = fmt["scale"]
        src_ch = fmt["src_channels"]
        item = np.dtype(dtype).itemsize
        while not self._stop.is_set():
            got_any = False
            while True:
                try:
                    if capture.GetNextPacketSize() == 0:
                        break
                    data, frames, flags, _pos, _qpc = capture.GetBuffer()
                except Exception as exc:              # noqa: BLE001
                    self.error = str(exc)
                    print(f"[audio] capture stopped: {exc}", file=sys.stderr)
                    return
                try:
                    if frames:
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                            # The endpoint says "this packet is silence" and the
                            # buffer contents are undefined - it must not be read.
                            block = np.zeros((frames, 2), dtype=np.float32)
                        else:
                            raw = ctypes.string_at(data, frames * src_ch * item)
                            arr = np.frombuffer(raw, dtype=dtype)
                            arr = arr.reshape(frames, src_ch)
                            block = self._to_stereo(arr, scale)
                        with self._lock:
                            self._chunks.append(block)
                        got_any = True
                finally:
                    capture.ReleaseBuffer(frames)
            if not got_any:
                time.sleep(self.POLL_S)

    @staticmethod
    def _to_stereo(arr: np.ndarray, scale: float) -> np.ndarray:
        """Endpoint frames -> float32 (n, 2).

        Multichannel endpoints (5.1, 7.1) are cut down to the front pair rather
        than downmixed: a proper downmix needs the channel mask and per-channel
        gains, and a screen recorder that gets the front channels right is
        already the honest answer.
        """
        if arr.shape[1] >= 2:
            out = arr[:, :2]
        else:
            out = np.repeat(arr[:, :1], 2, axis=1)
        out = out.astype(np.float32, copy=True)
        if scale != 1.0:
            out *= scale
        return LoopbackCapture._limit(out)

    #: Soft limiter threshold. The system mix can hand back peaks above
    #: 0 dBFS (measured up to +7.9 dB on the bench), and AAC clips those
    #: peaks into distortion. Below the threshold the signal passes
    #: untouched; above it a tanh tail folds the peak toward 1.0. A hard
    #: clip would square off the waveform, a plain gain would duck the
    #: whole recording.
    LIMIT_THRESHOLD = 0.9

    @staticmethod
    def _limit(x: np.ndarray) -> np.ndarray:
        """Soft-clip peaks above LIMIT_THRESHOLD toward 1.0.

        Monotonic (louder in, louder out), sign-preserving, and a no-op
        below the threshold - the same array comes back, so quiet passages
        are bit-for-bit untouched.
        """
        if x.size == 0:
            return x
        if float(np.abs(x).max()) <= LoopbackCapture.LIMIT_THRESHOLD:
            return x
        sign = np.sign(x)
        a = (np.abs(x) - LoopbackCapture.LIMIT_THRESHOLD) / (
            1.0 - LoopbackCapture.LIMIT_THRESHOLD)
        return sign * (LoopbackCapture.LIMIT_THRESHOLD
                       + (1.0 - LoopbackCapture.LIMIT_THRESHOLD) * np.tanh(a))
