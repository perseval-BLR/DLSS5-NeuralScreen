"""Where the program's own files live.

One place, imported by everything that needs a path and importing nothing
itself - which is what keeps it out of the import cycles the rest of the
split had to avoid.
"""
from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


NATIVE_DIR = BASE_DIR / "native"


# IMPORTANT: NGX Core returns FAIL_PlatformError from Init_Ext for ANY process
# name other than nvngx.dll (verified experimentally). The file name is part of
# the NGX contract.
WORKER_EXE = NATIVE_DIR / "nvngx.dll"
