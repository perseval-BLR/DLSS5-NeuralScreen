"""Native file dialogs and the screenshot writer.

Leaf Windows code: it takes a window handle, shows a system dialog and hands
back a path. Nothing here knows about the pipeline, which is why it could
leave main() - and why the screenshot writer belongs here too rather than
being copied into a test.

Both dialogs are the classic Win32 ones (GetSaveFileNameW,
SHBrowseForFolderW). They block the thread that calls them, so the caller
runs them off the main loop - the overlay must keep drawing while a dialog
is open.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path


class _OPENFILENAME(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


class _BROWSEINFO(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", wintypes.LPVOID),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", wintypes.LPVOID),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


def ask_save_path(parent_hwnd: int, default_name: str,
                  initial_dir: str | None = None,
                  fallback_dir: Path | None = None) -> Path | None:
    """The native "Save as" dialog. The chosen path, or None on cancel.

    The JPEG filter is the default and the extension is appended when the
    user leaves it out. initial_dir is the folder the dialog opens in (the
    configured screenshot folder, if any).

    If the dialog itself cannot be shown, a screenshot must not be lost: the
    answer is then a timestamped name inside fallback_dir.
    """
    try:
        buf = ctypes.create_unicode_buffer(1024)
        buf.value = default_name
        ofn = _OPENFILENAME()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAME)
        ofn.hwndOwner = parent_hwnd or None
        ofn.lpstrFilter = ("JPEG image (*.jpg)\0*.jpg\0PNG image (*.png)\0"
                           "*.png\0All files (*.*)\0*.*\0")
        ofn.lpstrFile = buf
        ofn.nMaxFile = 1024
        ofn.lpstrDefExt = "jpg"
        ofn.lpstrInitialDir = initial_dir or None
        # OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
        ofn.Flags = 0x00000002 | 0x00000008
        if not ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return None
        path = Path(buf.value.strip())
        if not path.suffix:
            path = path.with_suffix(".jpg")
        return path
    except Exception as exc:
        print(f"[dialogs] save dialog unavailable ({exc}) - "
              f"the screenshot goes to the fallback folder", file=sys.stderr)
        if fallback_dir is None:
            return None
        fallback_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
        return fallback_dir / f"neuralscreen-{stamp}.jpg"


def pick_directory(parent_hwnd: int, title: str) -> Path | None:
    """The classic folder picker. The chosen folder, or None on cancel.

    SHBrowseForFolder needs COM on the calling thread, and the caller runs
    this on a thread of its own (the dialog blocks), so the apartment is
    initialised and torn down here rather than assumed.
    """
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        try:
            shell32 = ctypes.WinDLL("shell32")
            shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFO)]
            shell32.SHBrowseForFolderW.restype = wintypes.LPVOID
            shell32.SHGetPathFromIDListW.argtypes = [wintypes.LPVOID,
                                                     wintypes.LPWSTR]
            shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(260)
            bi = _BROWSEINFO()
            bi.hwndOwner = parent_hwnd or None
            bi.lpszTitle = title
            bi.ulFlags = 0x0001  # BIF_RETURNONLYFSDIRS
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
            if pidl and shell32.SHGetPathFromIDListW(pidl, buf):
                return Path(buf.value.strip())
            return None
        finally:
            ole32.CoUninitialize()
    except Exception as exc:
        print(f"[dialogs] folder picker failed: {exc}", file=sys.stderr)
        return None


def save_jpeg(path: Path, rgba) -> bool:
    """Write an RGBA frame as a maximum-quality JPEG. True when it landed.

    imencode + write_bytes, NOT cv2.imwrite: OpenCV opens the file through
    the C runtime with the ANSI codepage, so a path with any non-ASCII
    character writes NOTHING - and imwrite still answers True. Measured: a
    Cyrillic folder gave "True" and no file, and the program told the user
    the screenshot had been saved. Encoding to memory and writing the bytes
    through Python leaves the path to Python, which handles it in UTF-16.

    The result is checked on disk before the answer: "the encoder said yes"
    is not the same as "the file is there".
    """
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA),
                           [cv2.IMWRITE_JPEG_QUALITY, 100])
    if not ok:
        return False
    path.write_bytes(buf.tobytes())
    return path.is_file() and path.stat().st_size > 0
