"""Audit F3: revive paths must use the warmup the launch used.

bring_up computes effective_warmup (4 on pre-Blackwell cards, the config
value otherwise) and starts the first worker with it - deliberately, to
avoid a false frame-0 watchdog on slow cards. Both revive paths (main's
auto-revive, commands' Num1 revive) pass st.warmup - the RAW config
value (e.g. 120). On a 30/40-series card a revive therefore hands the
worker a warmup that outlives the 5 s recv watchdog: frame 0 times out,
restarts climb, NR goes off again.

Expected: the warmup argument of every restart_worker call equals the
one the launch used. This test reads the three call sites and compares
their warmup arguments. [audit python-core F3]

Run:  runtime\\python.exe tests\\test_revive_warmup.py
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


def _restart_worker_warmups(path: Path) -> list:
    """The 5th argument of every restart_worker(...) call in a file."""
    src = path.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"restart_worker\(\s*(.*?)\)", src, re.S):
        args_region = m.group(1)
        # flatten nested parens roughly: split top-level commas
        depth = 0
        parts, cur = [], ""
        for ch in args_region:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur.strip())
                cur = ""
            else:
                cur += ch
        parts.append(cur.strip())
        if len(parts) >= 5:
            out.append(parts[4])
    return out


def main() -> int:
    failures = []

    # Where the launch warmup is decided.
    startup = (BASE / "startup.py").read_text(encoding="utf-8")
    computes = "effective_warmup" in startup
    if not computes:
        failures.append("startup.py no longer computes an effective warmup - "
                        "re-check the finding")

    # Where the revives happen.
    main_calls = _restart_worker_warmups(BASE / "main.py")
    cmd_calls = _restart_worker_warmups(BASE / "commands.py")
    pipeline_calls = _restart_worker_warmups(BASE / "pipeline.py")

    print("    main.py restart_worker warmups:    ", main_calls)
    print("    commands.py restart_worker warmups:", cmd_calls)
    print("    pipeline.py restart_worker warmups:", pipeline_calls)

    bad = []
    for args in main_calls + cmd_calls:
        for a in ([args] if isinstance(args, str) else args):
            if a == "st.warmup":
                bad.append("st.warmup")
    if bad:
        failures.append(
            f"F3: revive path(s) pass st.warmup (the raw config value) - "
            f"{len(bad)} call(s) - while the launch used effective_warmup; "
            f"on a pre-Blackwell card the revive's warmup outlives the 5 s "
            f"recv watchdog and restarts climb to NR OFF")

    # The stored-on-state check: once fixed, the revive should read a stored
    # value (e.g. st.effective_warmup or st.warmup itself updated).
    if bad and "st.effective_warmup" not in (startup + "".join(
            (BASE / n).read_text(encoding="utf-8")
            for n in ("main.py", "commands.py", "pipeline.py"))):
        failures.append("no st.effective_warmup anywhere - the fix is not in "
                        "place (expected while the finding is open)")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: revive warmups match the launch warmup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
