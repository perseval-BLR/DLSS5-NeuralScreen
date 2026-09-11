"""Verify what actually landed on GitHub after a push / release.

The release is not done when the local tree is committed: the READMEs,
the screenshots and the release assets must be verified against what
GitHub actually serves. This script checks, for the current version:

1. docs/screenshot-*.png - local sha256 == raw.githubusercontent.com/main
2. README.md / README.ru.md / TECHNICAL.md / TECHNICAL.ru.md - same
3. the release tag: assets (zip + 4 docs), zip digest == local, Latest
4. the repository description carries the current feature markers

Usage:  runtime\\python.exe verify_github.py [tag]
        (tag defaults to the version in build_release_zip.py)

Exit code 0 = everything on GitHub matches the local tree.
"""
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = "https://raw.githubusercontent.com/perseval-BLR/DLSS5-NeuralScreen/main"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            dest.write_bytes(r.read())
        return True
    except Exception as exc:
        print(f"    [FAIL] could not fetch {url}: {exc}")
        return False


def _gh(args: list) -> str:
    """gh, decoded as UTF-8 whatever the console codepage is.

    text=True alone decodes with the ANSI codepage (cp1251 on this machine),
    and the v1.6.1 release body - English and Russian in one document - has
    bytes it cannot decode: the reader thread died, stdout came back None
    and the verifier crashed on a release that was perfectly fine.
    """
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def main() -> int:
    # Read VERSION from the source text - importing build_release_zip would
    # EXECUTE the build (it is a script, not a module) and rebuild the zip,
    # changing its digest (VERSION.txt carries a build timestamp).
    import re
    src = (ROOT / "build_release_zip.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
    version = m.group(1) if m else "?"
    tag = sys.argv[1] if len(sys.argv) > 1 else f"v{version}"
    failures = []

    # 1-2. The docs and the READMEs, byte for byte. Text files are compared
    # against the git blob (LF), not the working copy (CRLF on Windows) -
    # GitHub serves the blob, and a CRLF working copy would be a false
    # mismatch.
    files = ["docs/screenshot-main-light.png", "docs/screenshot-main-dark.png",
             "docs/screenshot-settings.png", "docs/screenshot-windows.png",
             "README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md"]
    tmp = ROOT / "_work" / "verify-github"
    tmp.mkdir(parents=True, exist_ok=True)
    for name in files:
        if name.endswith(".png"):
            local = _sha256(ROOT / name)
        else:
            blob = subprocess.run(["git", "show", f"HEAD:{name}"],
                                  capture_output=True, check=True).stdout
            local = hashlib.sha256(blob).hexdigest()
        remote_path = tmp / name.replace("/", "_")
        if not _fetch(f"{RAW}/{name}", remote_path):
            failures.append(f"{name}: fetch failed")
            continue
        remote = _sha256(remote_path)
        if local != remote:
            failures.append(f"{name}: MISMATCH local={local[:12]} "
                            f"github={remote[:12]}")
        else:
            print(f"    [OK] {name} matches GitHub ({local[:12]})")

    # 3. The release: assets, zip digest, Latest.
    try:
        rel = json.loads(_gh(["api",
                               f"repos/perseval-BLR/DLSS5-NeuralScreen/"
                               f"releases/tags/{tag}"]))
    except subprocess.CalledProcessError:
        failures.append(f"release {tag} does not exist")
        rel = {}
    if rel:
        names = [a["name"] for a in rel.get("assets", [])]
        zip_name = f"neuralscreen-v{version}-full.zip"
        want = [zip_name,
                "README.md", "README.ru.md", "TECHNICAL.md", "TECHNICAL.ru.md"]
        for w in want:
            if w not in names:
                failures.append(f"release {tag} is missing asset {w}")
        if zip_name in names:
            local_zip = _sha256(ROOT / zip_name)
            dl = tmp / zip_name
            if _fetch(rel["assets"][names.index(zip_name)]["browser_download_url"],
                      dl):
                remote_zip = _sha256(dl)
                if local_zip != remote_zip:
                    failures.append(f"zip digest MISMATCH local={local_zip[:12]} "
                                    f"github={remote_zip[:12]}")
                else:
                    print(f"    [OK] zip digest matches GitHub ({local_zip[:12]})")
        latest = _gh(["release", "list", "--limit", "1"])
        if not latest.startswith(tag):
            failures.append(f"{tag} is not the Latest release")

    # 4. The repository description carries the current feature markers.
    desc = _gh(["repo", "view", "perseval-BLR/DLSS5-NeuralScreen",
                "--json", "description", "-q", ".description"])
    for marker in ("user presets", "12 languages"):
        if marker not in desc:
            failures.append(f"repo description lost the marker {marker!r}")
    print(f"    [OK] repo description: {desc}")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {tag} on GitHub matches the local tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
