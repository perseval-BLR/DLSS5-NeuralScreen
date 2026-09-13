"""The Spout bridge must live on the card the worker runs on.

SpoutBridgeInit is handed the worker's D3D12 device and then created its
own D3D11 device on the DEFAULT hardware adapter - adapter 0. With NS_GPU
pointing at another card the two differ, and every present copied the
frame across the bus into a texture that lives on a different GPU. It
works, because the texture is shared with an NT handle, so nothing ever
said a word: it just cost milliseconds a frame, on exactly the machines
that had a reason to choose a card in the first place (audit cpp-worker).

It now looks the adapter up by LUID and says which one it got. This runs
the real worker with NS_SPOUT=1 and reads that line back: the two LUIDs
have to be the same number. On a single-card machine that is the only
card - the check still fails if the lookup breaks and the fallback fires,
because the fallback says so out loud.

Run:  runtime\\python.exe tests\\test_spout_adapter.py
"""
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_FMT, PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 640, 360
LUID_LINE = re.compile(r"\[spout\] D3D11 on (.+) \(luid ([0-9A-F]{8}:[0-9A-F]{8}), "
                       r"the worker's: ([0-9A-F]{8}:[0-9A-F]{8})\)")


def main() -> int:
    failures = []
    source = (BASE / "native" / "spout_bridge.cpp").read_text(encoding="utf-8")
    if "GetAdapterLuid" not in source or "EnumAdapters1" not in source:
        failures.append("SpoutBridgeInit no longer looks the adapter up by "
                        "LUID - the D3D11 device is back on the default one")
    if "D3D_DRIVER_TYPE_UNKNOWN" not in source:
        failures.append("an explicit adapter needs D3D_DRIVER_TYPE_UNKNOWN - "
                        "passing an adapter AND a driver type fails the call")

    params = dict(PROFILES["Natural"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params["style"]), int(params["auto_mask"]),
                         int(params["ui_correction"]),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 1] = 180
    frame[..., 3] = 255
    motion = np.zeros((H, W, 2), dtype=np.float16)
    log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    log.close()
    with open(log.name, "wb") as err:
        proc = subprocess.Popen([str(WORKER_EXE), "--live"],
                                cwd=str(WORKER_EXE.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, env=dict(os.environ, NS_SPOUT="1"))
        try:
            proc.stdin.write(header)
            proc.stdin.flush()
            for i in range(3):
                proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                             1 if i == 0 else 0,
                                             FRAME_FLAG_WANT_PIXELS, i))
                proc.stdin.write(frame.tobytes())
                proc.stdin.write(motion.tobytes())
                proc.stdin.flush()
                head = proc.stdout.read(struct.calcsize(OUT_FMT))
                if len(head) < struct.calcsize(OUT_FMT):
                    break
                _m, _i, _ok, nbytes, _n, _p = struct.unpack(OUT_FMT, head)
                if nbytes:
                    proc.stdout.read(nbytes)
            proc.stdin.close()
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    Path(log.name).unlink(missing_ok=True)
    if "[spout] bridge enabled" not in text:
        failures.append("the bridge did not come up with NS_SPOUT=1 - nothing "
                        "was measured")
    if "falling back to the default one" in text:
        failures.append("the LUID lookup found nothing and the bridge fell "
                        "back to the default adapter")
    m = LUID_LINE.search(text)
    if m is None:
        failures.append("the bridge does not say which card it landed on")
    else:
        print(f"    spout on {m.group(1)}: {m.group(2)} vs worker {m.group(3)}")
        if m.group(2) != m.group(3):
            failures.append(f"the Spout device is on another card: "
                            f"{m.group(2)} against the worker's {m.group(3)} - "
                            f"every present crosses the bus")

    for f in failures:
        print("FAIL:", f)
    if failures:
        print(text[-800:])
        return 1
    print("OK: the Spout bridge is on the worker's own card")
    return 0


if __name__ == "__main__":
    sys.exit(main())
