"""Audit: every key overlay_ui indexes directly exists in every language.

test_i18n checks set parity across languages, but the menu also indexes
some keys directly (s["sec_hotkeys"], s["open_on_start"], ...). If a
language table misses one, layout() raises KeyError inside draw() inside
the main loop - the window-recreate branch fires every frame and the
user sees an endless recreate + alert storm. The failure mode is
disproportionate, so the direct lookups get their own guard.

Expected: every s["..."] key in overlay_ui.py exists in all 12 tables.
[audit ui-display]

Run:  runtime\\python.exe tests\\test_i18n_direct_keys.py
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

from i18n import STRINGS  # noqa: E402

# s["key"] / s['key'] - the direct lookups, plus .get("key", ...) is safe.
PATTERN = re.compile(r"""\bs\[\s*["']([a-z0-9_]+)["']\s*\]""")


def main() -> int:
    failures = []
    src = (BASE / "overlay_ui.py").read_text(encoding="utf-8")
    keys = sorted(set(PATTERN.findall(src)))
    if not keys:
        failures.append("no direct s[...] lookups found - wrong regex?")
    for key in keys:
        missing = [lang for lang, table in STRINGS.items() if key not in table]
        if missing:
            failures.append(
                f"overlay_ui indexes s[{key!r}] directly, missing in: "
                f"{missing} - a KeyError in layout() = recreate loop")
    print(f"    {len(keys)} direct keys checked against {len(STRINGS)} languages")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: every directly-indexed key exists in every language")
    return 0


if __name__ == "__main__":
    sys.exit(main())
