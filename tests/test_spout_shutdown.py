"""Audit (C++ report): SpoutBridgeShutdown must be called on the exit path.

spout_bridge.cpp defines SpoutBridgeShutdown (releasing the DX11 sender
and the shared texture), but a grep over native/ finds it referenced
only in its own file/header - neither RunVideo's exit clusters nor main()
call it. Every other subsystem is torn down explicitly on exit
(CleanupVideoNgx, CloseDda, CloseGray, CloseOut, ClosePresent...), so
the bridge is the one unclosed resource, kept alive only by process
teardown.

Expected: the worker's exit path calls SpoutBridgeShutdown.
[audit cpp-worker]

Run:  runtime\\python.exe tests\\test_spout_shutdown.py
"""
import re
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))


def main() -> int:
    failures = []
    cpp = (BASE / "native" / "dlss5-feed-host64.cpp").read_text(
        encoding="utf-8", errors="replace")

    calls = [m.start() for m in re.finditer(r"\bSpoutBridgeShutdown\s*\(\)", cpp)]
    print(f"    SpoutBridgeShutdown() call sites in dlss5-feed-host64.cpp: "
          f"{len(calls)}")
    if not calls:
        failures.append(
            "SpoutBridgeShutdown is never called by the worker - the DX11 "
            "sender and its 4K shared texture survive until process exit "
            "while every other subsystem is torn down explicitly")

    # And: the init must not silently bind adapter 0 when NS_GPU picks another.
    hdr = (BASE / "native" / "spout_bridge.h").read_text(
        encoding="utf-8", errors="replace")
    br = (BASE / "native" / "spout_bridge.cpp").read_text(
        encoding="utf-8", errors="replace")
    takes_dev = "ID3D12Device" in hdr or "ID3D12Device" in br
    uses_default_adapter = "D3D11CreateDevice(nullptr" in br
    if takes_dev and uses_default_adapter:
        print("    note: SpoutBridgeInit takes h.dev but creates its D3D11 "
              "device on the default adapter (cross-adapter copy under NS_GPU) - "
              "recorded in the report; not asserted here")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the spout bridge is shut down on the worker's exit path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
