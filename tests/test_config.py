"""The config loader: validation, clamping and profile resolution.

Pure unit test - no worker, no window, no program launch. It feeds
load_config() and resolve_params() from settings_io.py with crafted configs and
checks the contract:

* missing required fields raise;
* a stale profile falls back to Natural;
* work_scale is clamped to 0.1..1.0 (the slider can never ask for less);
* an unknown lang falls back to the default;
* resolve_params merges the profile with non-null overrides.
"""
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (settings_io.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

from settings_io import DEFAULT_LANG, PROFILES, WORK_SCALE_MAX, WORK_SCALE_MIN, load_config, resolve_params  # noqa: E402

GOOD = {
    "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
    "warmup": 120, "work_scale": 0.65, "lang": "en",
    "profile": "Strong / Cinematic",
    "intensity": None, "local_tone": None, "local_structure": None,
    "skin_structure": None,
}


def write_cfg(data) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return Path(f.name)


def main() -> int:
    failures = []

    # 1. A good config loads and keeps its values.
    p = write_cfg(GOOD)
    try:
        cfg = load_config(p)
        if cfg["work_scale"] != 0.65:
            failures.append(f"work_scale changed: {cfg['work_scale']}")
        if cfg["lang"] != "en":
            failures.append(f"lang changed: {cfg['lang']}")
    finally:
        p.unlink()

    # 2. Missing required fields raise.
    for missing in ("monitor", "width", "profile"):
        bad = dict(GOOD)
        del bad[missing]
        p = write_cfg(bad)
        try:
            try:
                load_config(p)
                failures.append(f"missing {missing} did not raise")
            except ValueError:
                pass
        finally:
            p.unlink()

    # 3. A non-object JSON root raises a ValueError.
    for root in (None, [], 1):
        p = write_cfg(root)
        try:
            try:
                load_config(p)
                failures.append(f"non-object JSON root {root!r} did not raise")
            except ValueError:
                pass
        finally:
            p.unlink()

    # 4. A non-string profile raises before profile membership is checked.
    p = write_cfg(dict(GOOD, profile=[]))
    try:
        try:
            load_config(p)
            failures.append("a non-string profile did not raise")
        except ValueError as exc:
            if "profile" not in str(exc):
                failures.append(f"non-string profile error was not field-specific: {exc}")
    finally:
        p.unlink()

    # 5. Boolean dimensions and warmup values are not positive integers.
    for key in ("width", "height", "warmup"):
        bad = dict(GOOD)
        bad[key] = True
        p = write_cfg(bad)
        try:
            try:
                load_config(p)
                failures.append(f"boolean {key} did not raise")
            except ValueError as exc:
                if key not in str(exc):
                    failures.append(f"boolean {key} error was not field-specific: {exc}")
        finally:
            p.unlink()

    # 6. Boolean overrides are rejected with a field-specific error.
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        bad = dict(GOOD)
        bad[key] = True
        p = write_cfg(bad)
        try:
            try:
                load_config(p)
                failures.append(f"boolean {key} override did not raise")
            except ValueError as exc:
                if key not in str(exc):
                    failures.append(f"boolean {key} error was not field-specific: {exc}")
        finally:
            p.unlink()

    # 7. Invalid non-null overrides are rejected with field-specific errors.
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        invalid = ("abc", [], {}, float("nan"), float("inf"), -float("inf"), 10 ** 4000)
        invalid += (-0.1, 2.6) if key != "skin_structure" else (-1.1, 2.6)
        for value in invalid:
            bad = dict(GOOD)
            bad[key] = value
            p = write_cfg(bad)
            try:
                try:
                    load_config(p)
                    failures.append(f"invalid {key} override {value!r} did not raise")
                except ValueError as exc:
                    if key not in str(exc):
                        failures.append(f"invalid {key} error was not field-specific: {exc}")
            finally:
                p.unlink()

    # 8. Numeric strings and range boundaries survive load and resolution.
    for key, raw, want in (
            ("intensity", "0.0", 0.0),
            ("local_tone", "2.5", 2.5),
            ("local_structure", 0.0, 0.0),
            ("skin_structure", "-1.0", -1.0),
            ("skin_structure", 2.5, 2.5)):
        bad = dict(GOOD)
        bad[key] = raw
        p = write_cfg(bad)
        try:
            loaded = load_config(p)
            params = resolve_params(loaded)
            if loaded[key] != raw:
                failures.append(f"load_config rewrote {key}: {loaded[key]!r}")
            if params[key] != want:
                failures.append(f"resolve_params changed {key}: {params[key]} vs {want}")
        finally:
            p.unlink()

    # 9. An unknown profile falls back to Natural instead of raising
    #    (a stale reference to a deleted user preset must not crash).
    bad = dict(GOOD, profile="Ultra Turbo")
    p = write_cfg(bad)
    try:
        cfg = load_config(p)
        if cfg["profile"] != "Natural":
            failures.append(f"unknown profile must fall back to Natural, "
                            f"got {cfg['profile']!r}")
    finally:
        p.unlink()

    # 10. work_scale clamps: below the floor, above the ceiling, and a string.
    for raw, want in ((0.01, WORK_SCALE_MIN), (5.0, WORK_SCALE_MAX), ("0.5", 0.5)):
        p = write_cfg(dict(GOOD, work_scale=raw))
        try:
            cfg = load_config(p)
            if abs(cfg["work_scale"] - want) > 1e-9:
                failures.append(f"work_scale {raw!r} -> {cfg['work_scale']}, want {want}")
        finally:
            p.unlink()

    # 11. Unknown lang falls back to the default.
    p = write_cfg(dict(GOOD, lang="klingon"))
    try:
        cfg = load_config(p)
        if cfg["lang"] != DEFAULT_LANG:
            failures.append(f"unknown lang -> {cfg['lang']}, want {DEFAULT_LANG}")
    finally:
        p.unlink()

    # 12. resolve_params: profile defaults, overridden by non-null values.
    params = resolve_params(GOOD)
    prof = PROFILES["Strong / Cinematic"]
    for k in ("intensity", "local_tone", "local_structure", "skin_structure"):
        if params[k] != prof[k]:
            failures.append(f"resolve_params changed {k}: {params[k]} vs {prof[k]}")
    custom = dict(GOOD, intensity=0.5, local_tone=1.2)
    params = resolve_params(custom)
    if params["intensity"] != 0.5 or params["local_tone"] != 1.2:
        failures.append("resolve_params did not apply the overrides")
    if params["local_structure"] != prof["local_structure"]:
        failures.append("resolve_params changed a non-overridden field")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the config loader validates, clamps and resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
