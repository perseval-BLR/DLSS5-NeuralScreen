"""The capture opens on the card the NETWORK landed on, not on the one asked for.

NS_GPU is a DXGI index and the network path treats it as a wish: an index
that is not a usable NVIDIA adapter falls back to the first one that is, and
says so in the log. The capture device and Desktop Duplication used to take
the same number literally - EnumAdapters1(want), no vendor check, no
fallback.

That splits the pipeline across two cards, and the frame cannot follow: it
crosses to D3D12 through a shared handle, and a shared handle does not cross
adapters. On a hybrid laptop adapter 0 is the integrated GPU - and "gpu": 0
is what the program ships with - so the network ran on the NVIDIA card while
the capture was created on the iGPU and nothing appeared on screen. The
reporter found the workaround himself: set "gpu" to 1 (issue #34).

Reproduced here without a second card: this machine's adapter 1 is the
Microsoft Basic Render Driver, which is exactly as unusable as an iGPU, so
NS_GPU=1 takes the same branch. The worker is driven far enough to actually
open the capture, and the log has to show one adapter, not two.

Run:  runtime\\python.exe tests\\test_adapter_agreement.py
"""
import ctypes
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import (HEADER_FMT, NATIVE_DIR, PROFILES,  # noqa: E402
                  VIDEO_MAGIC, WORKER_EXE)
from protocol import DDA_FMT, DDA_MAGIC  # noqa: E402

W, H = 960, 540
WARMUP = 4
TIMEOUT = 40.0


def unusable_adapter_index() -> int | None:
    """A DXGI index that is NOT a usable NVIDIA card, or None if all are."""
    try:
        from dxcam.core.device import Device
        from dxcam.util.io import enum_dxgi_adapters
    except Exception:
        return None
    for idx, adapter in enumerate(enum_dxgi_adapters()):
        try:
            desc = Device(adapter).desc
        except Exception:
            continue
        if desc.VendorId != 0x10DE or (getattr(desc, "Flags", 0) & 2):
            return idx
    return None


def main() -> int:
    failures = []

    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    want = unusable_adapter_index()
    if want is None:
        print("SKIP: every DXGI adapter on this machine is a usable NVIDIA "
              "card - there is no wrong one to ask for")
        return 0
    print(f"    asking for adapter {want}, which is not a usable NVIDIA card")

    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    env = dict(os.environ, NS_GPU=str(want))
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                                env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            # Let the feature come up before asking for the capture.
            time.sleep(2.0)
            # DDA1 at the real screen size - this is the code path that used
            # to read NS_GPU on its own. 0x0 would mean "turn the capture
            # off", which is the opposite of what is being tested.
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            proc.stdin.write(struct.pack(DDA_FMT, DDA_MAGIC, sw, sh, 0, 0))
            proc.stdin.flush()
            deadline = time.monotonic() + TIMEOUT
            text = ""
            while time.monotonic() < deadline:
                time.sleep(0.3)
                err.seek(0)
                text = err.read().decode("utf-8", "replace")
                if "[cap]" in text or "[dda]" in text:
                    time.sleep(0.5)
                    err.seek(0)
                    text = err.read().decode("utf-8", "replace")
                    break
                if proc.poll() is not None:
                    break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()

    lines = text.splitlines()
    net = [l for l in lines if "runs the network and the capture" in l]
    if not net:
        failures.append("the worker did not say which adapter it settled on")
        resolved = None
    else:
        resolved = int(re.search(r"adapter (\d+)", net[-1]).group(1))
        print("   ", net[-1].strip()[:96])
        if resolved == want:
            failures.append(f"the network kept adapter {want}, which is not a "
                            f"usable NVIDIA card - the fallback is gone")

    # The capture must name the same adapter. Before the fix it named the one
    # that had just been rejected.
    cap = [l for l in lines if "[cap] adapter" in l or "[cap] no adapter" in l]
    for line in cap:
        print("   ", line.strip()[:96])
    if not cap:
        failures.append("the capture never opened - no [cap] adapter line")
    else:
        got = re.search(r"adapter (\d+)", cap[-1])
        if got is None:
            failures.append(f"cannot read the capture's adapter from: {cap[-1]!r}")
        elif resolved is not None and int(got.group(1)) != resolved:
            failures.append(
                f"the network runs on adapter {resolved} and the capture "
                f"opened on {got.group(1)} - the frame crosses to D3D12 "
                f"through a shared handle and a shared handle does not cross "
                f"adapters (issue #34)")

    # The other half of the same issue: the MENU has to agree with the
    # worker. The saved index can name no NVIDIA card at all - on a hybrid
    # laptop adapter 0 is the integrated GPU, and "gpu": 0 is the shipped
    # default - and the picker used to come up empty on exactly those
    # machines, so the user could not even see which card was running.
    import settings_io
    adapters = settings_io.list_adapters()
    if adapters:
        first = f"{adapters[0][0]}: {adapters[0][1]}"
        for bad in (None, 99, "not a number"):
            label = settings_io._gpu_label(bad)
            if label != first:
                failures.append(f"the picker shows {label!r} for a saved "
                                f"gpu={bad!r}; the worker would run on "
                                f"{first!r}")
        real = adapters[0][0]
        if settings_io._gpu_label(real) != first:
            failures.append("the picker does not show a valid saved index")
        print(f"    picker for an unusable saved index -> {first!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the capture follows the adapter the network settled on, and "
          "the picker says so")
    return 0


if __name__ == "__main__":
    sys.exit(main())
