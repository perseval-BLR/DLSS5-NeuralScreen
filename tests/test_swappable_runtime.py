"""The swappable runtime: a configured nr_dll reaches the worker.

The worker loads nvngx_dlssnr.dll by name; NS_NR_DLL lets a different
build be loaded without rebuilding the worker (the RHI
dlss_manifest.json pattern). main.py reads nr_dll from the config and
puts it into the environment, which subprocess inherits.

Checked: the config flag sets NS_NR_DLL in the environment; without the
flag the variable is not set (the bundled DLL is the default).
"""
import importlib.util
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

spec = importlib.util.spec_from_file_location("ns_main", str(BASE / "main.py"))
ns_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []

    # 1. The config flag sets NS_NR_DLL in the environment.
    os.environ.pop("NS_NR_DLL", None)
    cfg = {"nr_dll": r"D:\runtimes\310.8.2\nvngx_dlssnr.dll"}
    ns_main._apply_nr_dll(cfg)
    if os.environ.get("NS_NR_DLL") != r"D:\runtimes\310.8.2\nvngx_dlssnr.dll":
        failures.append("NS_NR_DLL was not set from the config")

    # 2. Without the flag the variable stays unset (the bundled DLL).
    os.environ.pop("NS_NR_DLL", None)
    ns_main._apply_nr_dll({})
    if "NS_NR_DLL" in os.environ:
        failures.append("NS_NR_DLL was set without a config flag")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the swappable runtime is driven by the config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
