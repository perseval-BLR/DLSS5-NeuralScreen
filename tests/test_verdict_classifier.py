"""One classifier decides whether the neural pass came up, not two.

Two places ask the worker the same question and act on the answer:

  settings_io.refresh_gpu_ok - the dot in the menu and the "this GPU cannot
                               run the neural pass" alert;
  pipeline.gpu_came_up       - whether a GPU switch is kept or reverted.

They used to carry a list of log tokens each, and the lists had already
drifted: "NR feature unavailable" was known only to the first, "NGX
unavailable" and "no NVIDIA adapter found" only to the second. Renaming one
line in the worker would have blinded exactly one of them - the other would
have gone on working, which is the worst shape a bug like this can take.

So this pins three things:

* both callers go through settings_io.nr_verdict, and neither carries a
  token of its own (a re-inlined list is how the drift started);
* the classifier answers every line the worker can print;
* silence is None, not failure - a card that takes its time still works,
  and the callers rely on that distinction.

[audit: cpp-worker / python-core]

Run:  runtime\\python.exe tests\\test_verdict_classifier.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import settings_io  # noqa: E402

# Every line the worker can print about the neural pass, with the verdict
# each one carries:
#   [pure] direct feature 18 ready: ...
#   [pure] direct feature 18 create failed 0x...
#   [video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH
#   [host] NGX unavailable
#   [host] no NVIDIA adapter found
LINES = {
    "[pure] direct feature 18 ready: 2496x1404 preset=0": True,
    "[pure] direct feature 18 create failed 0xBAD0000B": False,
    "[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH": False,
    "[host] NGX unavailable": False,
    "[host] no NVIDIA adapter found": False,
}
TOKENS = ("feature 18 ready", "feature 18 create failed",
          "NR feature unavailable", "NGX unavailable",
          "no NVIDIA adapter found")


def _body(path: Path, func_name: str) -> str:
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"def {func_name}\(.*?(?=\ndef |\Z)", src, re.S)
    return m.group(0) if m else ""


def main() -> int:
    failures = []

    bodies = {"settings_io.refresh_gpu_ok": _body(BASE / "settings_io.py",
                                                  "refresh_gpu_ok"),
              "pipeline.gpu_came_up": _body(BASE / "pipeline.py",
                                            "gpu_came_up")}
    for name, body in bodies.items():
        if not body:
            failures.append(f"{name} is gone - re-check the finding")
            continue
        # Strip docstrings: prose naming a line is not a token list.
        clean = re.sub(r'""".*?"""', "", body, flags=re.S)
        if "nr_verdict(" not in clean:
            failures.append(f"{name} does not go through nr_verdict - it "
                            f"decides the verdict on its own again")
        own = sorted(t for t in TOKENS if f'"{t}' in clean)
        if own:
            failures.append(f"{name} carries its own tokens {own} - that is "
                            f"how the two drifted apart the first time")

    # The classifier answers every line, and newest-first wins.
    for line, want in LINES.items():
        got = settings_io.nr_verdict([line])
        if got is not want:
            failures.append(f"nr_verdict({line!r}) -> {got}, expected {want}")
    if settings_io.nr_verdict(["[host] adapter 0: NVIDIA GeForce RTX 5070 Ti",
                               "[present] window 1"]) is not None:
        failures.append("nr_verdict called a verdict on lines that carry none "
                        "- silence must not read as failure")
    # A refusal ahead of an older success: the newest line decides, and the
    # caller passes them newest-first.
    if settings_io.nr_verdict(["[host] NGX unavailable",
                               "[pure] direct feature 18 ready: 1920x1080"]) is not False:
        failures.append("nr_verdict did not take the newest line")

    print(f"    tokens: {len(TOKENS)}, both callers delegate: "
          f"{all('nr_verdict(' in b for b in bodies.values())}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: one classifier, it knows every line, and silence is not failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
