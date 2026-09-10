"""Run everything: the static checks, the regression tests, the smoke test.

There is one reason this file exists. The tests were run by hand, one at a
time, and that is how a regression shipped: audit #3 broke the shared-memory
pixel channel, `test_out_shm.py` caught it, and nobody ran `test_out_shm.py`.
One command, one verdict, a non-zero exit on any failure.

    runtime\\python.exe run_tests.py            static + tests + smoke
    runtime\\python.exe run_tests.py --gui      ... and the full GUI cycle
    runtime\\python.exe run_tests.py --only out_shm      one test by name
    runtime\\python.exe run_tests.py --no-smoke          skip the smoke test

Two of the stages take over the screen for a few seconds each (the overlay is
raised for real) and the machine should be left alone while they run. Nothing
here needs the network except the release-notes check inside autocheck.

Only tests tracked by git are run: the working copy also holds throwaway
probes, and those are nobody's regression suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Console encodings, both directions: the children are told to print utf-8 so
# their output decodes here, and our own stdout replaces anything the console
# codepage cannot represent instead of dying on it.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
PY = ROOT / "runtime" / "python.exe"
TIMEOUT = 600

# What each test is for, in one line - a failing name should not send anyone
# digging through the file to find out what broke.
ABOUT = {
    "test_bypass.py": "NR OFF shows the raw capture and the pipeline survives",
    "test_audio_limiter.py": "the soft limiter keeps the recording from clipping",
    "test_audio_pack.py": "the audio format structs are byte-packed, truncated formats rejected",
    "test_capture_visibility.py": "outside capture sees the overlay only without WDA",
    "test_config.py": "the config loader validates, clamps and resolves",
    "test_config_atomic.py": "the config write is atomic and persists profile/params/monitor",
    "test_dred_diag.py": "the worker logs DRED/device-removed diagnostics at startup",
    "test_env_header.py": "the log header carries version/OS/HDR and survives broken probes",
    "test_hotkey_once.py": "one press of a hotkey fires exactly one command",
    "test_hotkey_rebind.py": "hotkeys can be reassigned from the config",
    "test_hotkey_bindings.py": "parsing, aliases and defaults survive binding changes",
    "test_hotkey_held.py": "a key held at startup is the baseline, not an event",
    "test_hotkey_remap_no_fire.py": "a key pressed during a remap is the baseline, not an event",
    "test_header_footer.py": "the header collapse icon and the one-window footer button",
    "test_i18n.py": "every language has the same keys, none empty",
    "test_menu_position.py": "the menu position is fixed - saved offset honoured, clamped",
    "test_monitor_identity.py": "monitors are identified by DXGI devicename, not by position",
    "test_menu_scroll.py": "the menu scrolls and the wheel lands where it should",
    "test_overlay_toolwindow.py": "the overlay is a tool window - one taskbar button only",
    "test_motion_small.py": "the downscaled motion field is upscaled on the GPU",
    "test_mv_validation.py": "noise-floor motion vectors are zeroed, real motion survives",
    "test_nr_small.py": "the reduced-resolution mode produces a real picture",
    "test_out_shm.py": "the pixel channel through shared memory",
    "test_out_status.py": "0x00000000 is a skipped frame, only 0xBAD00000 raises",
    "test_odd_frame_size.py": "a frame whose row pitch needs padding survives",
    "test_recorder_audio.py": "the audio track keeps up with the video",
    "test_recorder_fallback.py": "the NVENC codec chain falls back AV1->HEVC->H.264",
    "test_rec_indicator.py": "the recording indicator draws only while recording, never in the file",
    "test_shot_dir.py": "the screenshot folder is configured, persisted and shown on the button",
    "test_recovery.py": "only 0xBAD00001 is a hard failure, everything else auto-revives",
    "test_recorder_thread.py": "the encoder thread and a clean close",
    "test_residual.py": "the matched residual composite keeps 1:1 detail at reduced work",
    "test_residual_split.py": "residual and the wipe compose in the same frame",
    "test_resize.py": "the on-the-fly resize reconfigures the feature without a restart",
    "test_reveal.py": "the present window stays hidden until the first Present",
    "test_split.py": "the before/after wipe leaves the left side untouched",
    "test_taskbar_window.py": "the taskbar button exists, opens the menu, closes cleanly",
    "test_ui_buttons.py": "every control on every page fires the right command",
    "test_wgc_capture.py": "the worker captures one window (the single-window input)",
    "test_window_filter.py": "the window list holds only real taskbar windows",
    "test_windows_page.py": "the windows page lists, highlights and switches",
    "test_window_mode.py": "the one-window hotkey switches the pipeline and back",
    "test_window_mode_menu.py": "the menu stays fully visible across the window-mode switch",
}


def tracked_tests() -> list:
    try:
        out = subprocess.check_output(["git", "ls-files", "tests/test_*.py"], cwd=ROOT,
                                      text=True, encoding="utf-8", errors="replace")
        names = [n.strip() for n in out.splitlines() if n.strip()]
        if names:
            return sorted(names)
    except Exception as exc:
        print(f"(git ls-files failed: {exc!r} - falling back to a glob)")
    return sorted(p.name for p in ROOT.glob("tests/test_*.py"))


def run(label: str, args: list, note: str = "") -> dict:
    print(f"\n>>> {label}" + (f"  ({note})" if note else ""))
    started = time.monotonic()
    try:
        args = [a if a.startswith("tests/") else f"tests/{a}" for a in args]
        r = subprocess.run([str(PY)] + args, cwd=ROOT, timeout=TIMEOUT,
                           capture_output=True, text=True, env=CHILD_ENV,
                           encoding="utf-8", errors="replace")
        took = time.monotonic() - started
        ok = r.returncode == 0
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-3:]
        if not ok:
            tail = ([l for l in (r.stdout or "").splitlines() if l.strip()][-8:]
                    or [(r.stderr or "").strip()[-400:]])
        for l in tail:
            print(f"    {l}")
        print(f"    [{'PASS' if ok else 'FAIL'}] {took:.1f}s")
        return {"label": label, "ok": ok, "took": took}
    except subprocess.TimeoutExpired:
        took = time.monotonic() - started
        print(f"    [FAIL] timed out after {took:.0f}s")
        return {"label": label, "ok": False, "took": took}


def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    if not PY.exists():
        print(f"no {PY} - run this with the bundled runtime")
        return 1

    print(f"NeuralScreen test run - {ROOT}")
    print("The screen is taken over a few times; leave the machine alone.")
    print("=" * 70)
    results = []

    if only is None:
        results.append(run("static checks", ["autocheck.py"],
                           "build, archive, docs, git"))
    for name in tracked_tests():
        if only is not None and only not in name:
            continue
        results.append(run(name, [name], ABOUT.get(name, "")))
    if only is None and "--no-smoke" not in sys.argv:
        results.append(run("smoke", ["autocheck.py", "--smoke"],
                           "launch -> processing -> exit"))
    if "--gui" in sys.argv:
        results.append(run("GUI cycle", ["autocheck.py", "--gui"],
                           "launch -> record -> exit"))

    print("\n" + "=" * 70)
    width = max(len(r["label"]) for r in results)
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['label']:<{width}}  {r['took']:6.1f}s")
    failed = [r["label"] for r in results if not r["ok"]]
    total = sum(r["took"] for r in results)
    print("=" * 70)
    if failed:
        print(f"RESULT: {len(failed)} of {len(results)} FAILED in {total:.0f}s - {failed}")
        return 1
    print(f"RESULT: all {len(results)} green in {total:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
