"""The worker logs its DRED/device-removed diagnostics at startup.

Issue #1 (Win10 TDR) died with only "code 6" in the log - no reason, no
breadcrumbs. The worker now enables DRED breadcrumbs when the OS supports
them and logs the outcome either way; on a removed device BeginCommands
logs GetDeviceRemovedReason plus breadcrumbs. This test checks the startup
half: the log must contain a DRED line (enabled or unavailable-with-hr),
and the worker must still run NR afterwards.

Run:  runtime\\python.exe test_dred_diag.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG = BASE / "NeuralScreen.log"
PY = BASE / "runtime" / "python.exe"


def main() -> int:
    failures = []
    # The worker is a separate process; the launcher starts it via the VBS.
    # For the test we run main.py directly with NS_PHASE=1 so the [host]
    # lines reach the log.
    LOG.write_text("", encoding="utf-8")
    env = dict(os.environ, NS_PHASE="1")
    proc = subprocess.Popen([str(PY), "-u", "main.py"], cwd=str(BASE),
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        dred_line = None
        nr_line = None
        while time.time() < deadline:
            text = LOG.read_text(encoding="utf-8", errors="replace")
            if dred_line is None:
                m = re.search(r"\[host\] DRED (breadcrumbs enabled|settings unavailable)",
                              text)
                if m:
                    dred_line = m.group(0)
            if nr_line is None and "NR ON" in text:
                nr_line = "NR ON"
            if dred_line and nr_line:
                break
            time.sleep(0.5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # terminate() kills only python.exe - the worker (nvngx.dll) is a
        # child of main.py and survives, which makes the next GUI test fail
        # with "NeuralScreen is already running". Kill it by name.
        subprocess.run(["taskkill", "/F", "/IM", "nvngx.dll"],
                       capture_output=True)

    if dred_line is None:
        failures.append("no DRED line in the log at all")
    else:
        print(f"DRED line: {dred_line}")
    if nr_line is None:
        failures.append("NR did not come up after the DRED init")
    else:
        print("NR came up after the DRED init")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: DRED diagnostics logged, NR runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
