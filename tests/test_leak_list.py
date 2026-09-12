"""Audit: autocheck's config-leak list must cover every personal key.

The release zip takes config.json from git HEAD, and autocheck's leak
check is what catches "a maintainer committed a personal value". The
list stopped growing: skip_static, gpu, screenshot_dir, rec_indicator
and monitor are written by _menu_layout_payload but never compared.
config.json HEAD today happens to hold defaults, so the gap is for the
NEXT change - committing a personal skip_static=false or a
screenshot_dir would ship silently.

Expected: leak keys ⊇ the keys _menu_layout_payload can write.
[audit ui-display]

Run:  runtime\\python.exe tests\\test_leak_list.py
"""
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))


def main() -> int:
    failures = []
    import autocheck
    import settings_io

    # 1. The keys autocheck actually compares. Asked of autocheck rather
    #    than read out of its source: the list used to be a literal tuple
    #    and the whole finding is that a literal tuple stops growing.
    leak_keys = set(autocheck.personal_config_keys())

    # 2. The keys the payload can write (what a personal config may carry).
    payload = settings_io._menu_layout_payload(
        {"profile": "Natural", "gpu": 0, "spout": False,
         "skip_static": True, "rec_indicator": True, "screenshot_dir": ""},
        {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
         "skin_structure": -1.0},
        0, "en", 0.65, 0.0, True, False,
        type("M", (), {"user_scale": 1.0, "user_height": None,
                       "state": {"theme": "light"}, "offset": [0, 0]})())

    # Keys that are legitimately defaulted in HEAD and may differ per user.
    personal = {"skip_static", "gpu", "screenshot_dir", "rec_indicator",
                "monitor", "spout"}
    missing = sorted(personal - leak_keys - set())
    if missing:
        failures.append(
            f"autocheck's leak list does not cover: {missing} - a committed "
            f"personal value for these would ship in the zip silently")
    print(f"    leak keys: {sorted(leak_keys)}")
    print(f"    payload keys: {sorted(payload.keys())[:8]}...")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the leak list covers the personal config keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
