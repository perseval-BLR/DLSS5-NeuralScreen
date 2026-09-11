"""A screenshot into a folder with a non-ASCII name is really written.

cv2.imwrite opens the file through the C runtime with the ANSI codepage: any
non-ASCII character in the path and it writes NOTHING - while still answering
True. The program then told the user "Screenshot: ..." and there was no file.
Measured before the fix:

    ascii    -> imwrite True, file exists
    cyrillic -> imwrite True, file MISSING

So the encoder writes to memory and Python writes the bytes. This test pins
both halves: that the helper still produces a readable JPEG, and that it does
so under a path OpenCV cannot open itself.

Run:  runtime\\python.exe tests\\test_shot_unicode.py
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# The console here is cp1251; the names below are not. Printing them must not
# be what fails.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# The names a Russian, Japanese and Greek user would actually have.
NAMES = ["скриншоты", "スクリーンショット", "στιγμιότυπα"]


from dialogs import save_jpeg as write_jpeg  # noqa: E402

# The real writer, not a copy of it: a test that re-implements the code it
# checks proves only that the copy works.


def main() -> int:
    import cv2

    rgba = np.zeros((64, 96, 4), dtype=np.uint8)
    rgba[..., 0] = 200          # a colour that survives JPEG unmistakably
    rgba[..., 3] = 255
    root = Path(tempfile.mkdtemp(prefix="ns-shot-"))
    failures = []
    try:
        for name in NAMES:
            path = root / name / "снимок.jpg"
            if not write_jpeg(path, rgba):
                failures.append(f"{name}: nothing was written")
                continue
            data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None or img.shape[:2] != rgba.shape[:2]:
                failures.append(f"{name}: the file does not decode back")
                continue
            print(f"    {name}: {path.stat().st_size} bytes, decodes to "
                  f"{img.shape[1]}x{img.shape[0]}")

        # The control: what the old code did. An ASCII folder with a
        # non-ASCII FILE name is the sharp case - imwrite answers True there
        # and writes nothing, which is how the program came to report a
        # screenshot that did not exist.
        legacy = root / "снимок.jpg"
        said = cv2.imwrite(str(legacy), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        print(f"    control - cv2.imwrite on the same path: returned {said}, "
              f"file {'exists' if legacy.exists() else 'MISSING'}")
        if said and not legacy.exists():
            print("    (that is the bug this test exists for: True and no file)")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: screenshots land on non-ASCII paths and decode back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
