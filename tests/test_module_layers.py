"""The split into modules holds: no cycles back to main, no dangling names.

main.py used to be one 3800-line file where every helper saw every other
through the same closure. Splitting it apart broke that for free, and this
test is what keeps it broken apart:

* NOTHING imports main. main is the top of the pile - it wires the modules
  together and owns the loop. A module reaching back into it would recreate
  the tangle the split removed, and at import time it would be a cycle.

* EVERY module resolves its own names. Moving a function out of main leaves
  its imports behind, and the miss is invisible: the module imports fine and
  dies with a NameError months later, on the one branch nobody exercised.
  Two real regressions of the refactor were exactly this (settings_io without
  os/Path, pipeline without struct/subprocess). symtable answers the question
  the compiler already answered - which names the module reads as globals and
  never binds.

* EVERY module imports on its own, in a fresh interpreter. That is the part
  a static pass cannot fake: a cycle only shows up when Python actually walks
  the imports.
"""
import builtins
import subprocess
import symtable
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}

# The dev-only scripts at the root (_probe_*, _measure_*, the release
# tooling) are not part of the program and are allowed to import main.
SKIP = {"build_release_zip.py", "verify_github.py", "test_bake_menu.py"}


def app_modules() -> list:
    return sorted(p for p in BASE.glob("*.py")
                  if not p.name.startswith("_") and p.name not in SKIP)


def undefined_names(path: Path) -> list:
    """Globals the module reads but never binds - scope-aware.

    symtable is the compiler's own view: a parameter, a comprehension
    variable or a name from an enclosing function is not a global, so only
    the real misses come out.
    """
    top = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    known = {s.get_name() for s in top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_namespace()}
    missing = set()

    def walk(table):
        for sym in table.get_symbols():
            if sym.is_global() and sym.is_referenced():
                name = sym.get_name()
                if name not in known and name not in BUILTINS:
                    missing.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    return sorted(missing)


def main() -> int:
    failures = []
    modules = app_modules()
    if len(modules) < 10:
        failures.append(f"only {len(modules)} modules found - wrong root?")

    # 1. Nobody imports main.
    for path in modules:
        if path.name == "main.py":
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if (stripped.startswith("import main")
                    or stripped.startswith("from main import")):
                failures.append(f"{path.name}:{num} imports main: {stripped}")

    # 2. Every module resolves its own names.
    for path in modules:
        missing = undefined_names(path)
        if missing:
            failures.append(f"{path.name} reads undefined names: {missing}")

    # 3. Every module imports on its own, in a fresh interpreter.
    #    One process for all of them: an import cycle would show up on the
    #    module that closes it whichever order they are tried in.
    names = [p.stem for p in modules]
    code = ("import sys; sys.path.insert(0, r'%s')\n"
            "for m in %r:\n"
            "    __import__(m)\n"
            "print('ok')\n" % (BASE, names))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE),
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-4:]
        failures.append("importing the modules failed: " + " | ".join(tail))

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: {len(modules)} modules, none imports main, "
          f"no undefined names, all import clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
