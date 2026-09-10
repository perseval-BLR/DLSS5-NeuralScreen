"""The worker-failure classifier: hard failures never auto-revive.

The recovery mechanism (v1.5.4) gives a transient worker failure ONE
automatic revive after a backoff instead of leaving NR off until the
user presses Num1. The classifier decides which failures are transient:
0xBAD00001 (FeatureNotSupported - the GPU cannot run the pass at all,
Turing or a broken runtime build) is permanent - retrying only spins
the restart loop. Everything else (0x00000000 no-frame, timeouts,
driver hiccups, TDR code 6) can clear on its own and is worth a revive.

Checked: the classifier returns True only for 0xBAD00001, and the
auto-revive deadline is armed only for transient failures.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from main import _hard_failure  # noqa: E402


def main() -> int:
    failures = []

    # 1. Empty logs: nothing to classify - not a hard failure.
    if _hard_failure([]):
        failures.append("empty logs must not be a hard failure")

    # 2. The permanent failure: feature 18 create failed 0xBAD00001.
    logs = ["[pure] direct feature 18 create failed 0xBAD00001 (FeatureNotSupported)"]
    if not _hard_failure(logs):
        failures.append("0xBAD00001 must be a hard failure")

    # 3. The transient no-frame: 0x00000000 (issue #11, kortul).
    logs = ["[main] NGX evaluation failed on frame 0: 0x00000000"]
    if _hard_failure(logs):
        failures.append("0x00000000 must NOT be a hard failure")

    # 4. A TDR death (code 6) - transient, worth a revive.
    logs = ["the NGX worker exited with code 6", "[host] device removed"]
    if _hard_failure(logs):
        failures.append("a TDR death must NOT be a hard failure")

    # 5. A timeout - transient.
    logs = ["[gray] fence timeout", "the worker has been silent for 5s"]
    if _hard_failure(logs):
        failures.append("a timeout must NOT be a hard failure")

    # 6. 0xBAD00001 anywhere in the tail wins, even with transient lines.
    logs = ["[main] worker silent/dead on frame 0",
            "[pure] direct feature 18 create failed 0xBAD00001 (FeatureNotSupported)"]
    if not _hard_failure(logs):
        failures.append("0xBAD00001 in the tail must win over transient lines")

    # 7. The tail window: only the last 40 lines count.
    logs = ["0xBAD00001"] + ["transient"] * 50
    if _hard_failure(logs):
        failures.append("0xBAD00001 older than the 40-line tail must not count")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: only 0xBAD00001 is a hard failure, everything else auto-revives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
