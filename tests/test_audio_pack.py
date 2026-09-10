"""The audio format structs match the real ABI (byte-packed) and reject
truncated extensible formats.

mmreg.h defines WAVEFORMATEX/WAVEFORMATEXTENSIBLE under pshpack1.h - the
structs are byte-packed, no alignment padding. Without _pack_ ctypes
aligns nAvgBytesPerSec/SubFormat and the structs come out larger than the
real ABI, so the layout read from the audio endpoint would be wrong
(code review finding from ttmullins/DLSS5-NeuralScreen#1).

Checked: the packed sizes match the C ABI (18/40 bytes), and _describe
raises on an extensible format with cbSize < 22 (truncated).
"""
import ctypes
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio  # noqa: E402


def main() -> int:
    failures = []

    # The C ABI: WAVEFORMATEX is 18 bytes (2+2+4+4+2+2+2), the extensible
    # form adds 2+4+16 = 22 bytes -> 40 total. Any padding breaks both.
    if ctypes.sizeof(audio.WAVEFORMATEX) != 18:
        failures.append(
            f"WAVEFORMATEX is {ctypes.sizeof(audio.WAVEFORMATEX)} bytes, "
            "expected 18 - the struct is not byte-packed")
    if ctypes.sizeof(audio.WAVEFORMATEXTENSIBLE) != 40:
        failures.append(
            f"WAVEFORMATEXTENSIBLE is {ctypes.sizeof(audio.WAVEFORMATEXTENSIBLE)} "
            "bytes, expected 40 - the struct is not byte-packed")

    # A truncated extensible format (cbSize < 22) must be rejected, not
    # cast past its end. Build a fake mix pointer with cbSize=10.
    class _FakeMix(ctypes.Structure):
        _fields_ = [("fmt", audio.WAVEFORMATEX)]

    fake = _FakeMix()
    fake.fmt.wFormatTag = audio.WAVE_FORMAT_EXTENSIBLE
    fake.fmt.wBitsPerSample = 32
    fake.fmt.nSamplesPerSec = 48000
    fake.fmt.nChannels = 2
    fake.fmt.nBlockAlign = 8
    fake.fmt.cbSize = 10  # truncated: extensible needs 22
    try:
        audio.LoopbackCapture._describe(ctypes.pointer(fake.fmt))
        failures.append("a truncated extensible format (cbSize=10) was "
                        "accepted instead of raising")
    except RuntimeError as exc:
        if "truncated" not in str(exc):
            failures.append(f"unexpected error text: {exc}")

    # A full extensible format (cbSize=22) must be accepted and read as
    # float32 (the common WASAPI mix format).
    fake.fmt.cbSize = 22
    ext = ctypes.cast(ctypes.pointer(fake.fmt), ctypes.POINTER(audio.WAVEFORMATEXTENSIBLE)).contents
    ext.wValidBitsPerSample = 32
    ext.dwChannelMask = 3
    ext.SubFormat = audio.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    try:
        desc = audio.LoopbackCapture._describe(ctypes.pointer(fake.fmt))
        if desc["dtype"] is not ctypes.c_float and desc["dtype"] is not __import__("numpy").float32:
            failures.append(f"float32 mix not detected: {desc['dtype']}")
        if desc["rate"] != 48000 or desc["src_channels"] != 2:
            failures.append(f"rate/channels misread: {desc}")
    except Exception as exc:
        failures.append(f"a full extensible format raised: {exc}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: audio format structs are byte-packed and truncated formats are rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
