"""The HDR shaders, compiled and run - on WARP, so anywhere.

hdr_gpu.cpp (PR #36) takes the two shader sources the worker actually
compiles - kHdrCaptureHlsl and kHdrCompositeHlsl, out of native/hdr_shaders.h -
runs them on the WARP software device and reads the pixels back. No HDR
monitor, no NVIDIA runtime, no NGX: it checks the arithmetic of the HDR path,
which is the part that decides whether the picture is right.

It was reachable only by hand (native\\test-hdr.bat), and a check nobody runs
is a check nobody has. This wrapper puts it in the suite: the suite already
insists on a freshly built worker, so the machine running it has the compiler.

What the WARP run covers: shader compilation, highlights above 1.0 surviving,
the signed part of the gamut surviving, a zero neural edit reproducing the
original EXACTLY (the residual must not tint an untouched frame), bypass, the
before/after wipe, no infinities or NaNs, the SDR export and channel order.

Run:  runtime\\python.exe tests\\test_hdr_shaders.py
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BAT = BASE / "native" / "test-hdr.bat"


def main() -> int:
    if not BAT.exists():
        print(f"FAIL: no {BAT}")
        return 1
    try:
        r = subprocess.run(["cmd", "/c", str(BAT)], cwd=str(BAT.parent),
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print("FAIL: the WARP shader run did not finish in 300s")
        return 1
    out = (r.stdout or "") + (r.returncode and (r.stderr or "") or "")
    for line in (r.stdout or "").splitlines():
        if line.strip() and not line.startswith("hdr_gpu.cpp"):
            print(f"    {line.strip()}")
    if "is not recognized" in out or "vcvars64" in out and r.returncode != 0:
        print("FAIL: no Visual Studio build tools - the same ones the worker "
              "is built with (native\\build-host.bat)")
        return 1
    if r.returncode != 0 or "PASS" not in (r.stdout or ""):
        print((r.stderr or "").strip()[-600:])
        print(f"FAIL: the HDR shaders did not pass on WARP (exit {r.returncode})")
        return 1
    print("OK: the HDR capture and composite shaders are right on WARP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
