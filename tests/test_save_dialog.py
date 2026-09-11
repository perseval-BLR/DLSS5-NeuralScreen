"""The "Save as" dialog is actually callable - the struct fill does not raise.

This is the test that was missing. `ask_save_path` builds an OPENFILENAME and
calls GetSaveFileNameW; the struct fill raised TypeError on every machine
(`ofn.lpstrFile = buf` - a c_wchar array into an LPWSTR field), the wrapper
caught it, and every screenshot went silently to the fallback folder. The
only evidence was one line in the log, which is where it sat until a user
attached their log to issue #30.

Nothing here opens a dialog: the struct is built by `_save_dialog_struct`,
which is exactly the part that broke, and the fallback path is checked
through `ask_save_path` with a deliberately impossible request.
"""
import ctypes
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import dialogs  # noqa: E402


def main() -> int:
    failures = []

    # 1. The struct fills without raising, and the fields carry what the
    #    dialog needs: the name buffer, its size, the initial folder.
    try:
        ofn, buf = dialogs._save_dialog_struct(0, "shot.jpg", r"C:\Shots")
    except Exception as exc:
        print(f"FAIL: building the dialog struct raised {exc!r}")
        return 1

    if buf.value != "shot.jpg":
        failures.append(f"the name buffer holds {buf.value!r}")
    if ofn.nMaxFile != 1024:
        failures.append(f"nMaxFile is {ofn.nMaxFile}, the buffer is 1024")
    if ofn.lpstrInitialDir != r"C:\Shots":
        failures.append(f"initial dir is {ofn.lpstrInitialDir!r}")
    if ofn.lStructSize != ctypes.sizeof(dialogs._OPENFILENAME):
        failures.append("lStructSize does not match the structure")
    # The pointer must address the buffer itself - the dialog writes the
    # chosen path through it, and a copy would leave the caller reading an
    # empty buffer. Read as a raw address: touching ofn.lpstrFile in Python
    # gives back a str, not the pointer that was stored.
    addr = ctypes.c_void_p.from_buffer(
        ofn, dialogs._OPENFILENAME.lpstrFile.offset).value
    if addr != ctypes.addressof(buf):
        failures.append("lpstrFile does not point at the name buffer")

    # 2. What the dialog writes into the buffer is what the caller reads.
    #    (The real GetSaveFileNameW writes through the same pointer.)
    if addr:
        chosen = r"D:\pics\a.jpg"
        ctypes.memmove(addr, ctypes.create_unicode_buffer(chosen),
                       (len(chosen) + 1) * 2)
        if buf.value != chosen:
            failures.append(f"a write through lpstrFile did not reach the "
                            f"buffer: {buf.value!r}")

    # 3. No initial dir is allowed - the field is simply null.
    try:
        ofn2, _ = dialogs._save_dialog_struct(0, "shot.jpg", None)
        if ofn2.lpstrInitialDir is not None:
            failures.append(f"initial dir should be null, is "
                            f"{ofn2.lpstrInitialDir!r}")
    except Exception as exc:
        failures.append(f"building without an initial dir raised {exc!r}")

    # 4. The fallback still works when the dialog cannot be shown: a
    #    timestamped name inside the fallback folder, not a lost screenshot.
    with tempfile.TemporaryDirectory() as tmp:
        real = dialogs._save_dialog_struct

        def boom(*a, **kw):
            raise OSError("no dialog on this session")

        dialogs._save_dialog_struct = boom
        try:
            got = dialogs.ask_save_path(0, "shot.jpg", None, Path(tmp))
        finally:
            dialogs._save_dialog_struct = real
        if got is None:
            failures.append("the fallback returned nothing - a screenshot is lost")
        elif got.parent != Path(tmp) or got.suffix != ".jpg":
            failures.append(f"the fallback path is {got}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the save dialog struct is valid and the fallback still catches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
