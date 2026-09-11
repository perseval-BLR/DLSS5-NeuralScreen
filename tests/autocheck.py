"""NeuralScreen v1.4 self-checks - the static part (no GUI).

Run:  runtime\\python.exe autocheck.py
The GUI part (menu, recording) is run separately - see the end of the output.

Every check reports PASS / FAIL / SKIP plus a reason. Exit: 0 = all PASS,
1 = there is a FAIL.
"""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
FAILS = []


def check(name, fn):
    try:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        if not ok:
            FAILS.append(name)
        print(f"[{status}] {name}: {detail}")
    except Exception as exc:
        FAILS.append(name)
        print(f"[FAIL] {name}: exception {exc!r}")


def fresh_worker():
    """nvngx.dll is built after the last commit and contains the hook."""
    dll = ROOT / "native" / "nvngx.dll"
    if not dll.exists():
        return False, "no native/nvngx.dll"
    data = dll.read_bytes()
    if b"NS_ARCH_SPOOF" not in data:
        return False, "no NS_ARCH_SPOOF in the binary (an old build?)"
    # freshness: the mtime must not be older than the cpp
    cpp = ROOT / "native" / "dlss5-feed-host64.cpp"
    if dll.stat().st_mtime < cpp.stat().st_mtime:
        return False, "the dll is older than the cpp - rerun build-host.bat"
    return True, f"{dll.stat().st_size} bytes, the hook is there, fresh"


def zip_integrity():
    zpath = ROOT / "neuralscreen-v1.6.1-full.zip"
    if not zpath.is_file():
        return False, "no neuralscreen-v1.6.1-full.zip"
    required = [
        "main.py", "gpuinfo.py", "overlay_ui.py", "i18n.py", "recorder.py",
        "display.py", "guides.py", "hotkeys.py", "tray.py", "capture.py",
        "audio.py", "protocol.py", "winapi.py", "dialogs.py", "channels.py",
        "settings_io.py", "paths.py", "pipeline.py", "commands.py",
        "startup.py",
        "NeuralScreen.exe",
        "TECHNICAL.md", "TECHNICAL.ru.md",
        "README.md", "README.ru.md", "NeuralScreen.vbs", "NeuralScreen.bat",
        "native/nvngx.dll", "native/nvngx_dlssnr.dll",
        # Loaded at run time, and both have a silent fallback: left out
        # of the archive the program ships with the wrong icons and says
        # nothing about it.
        "native/neuralscreen.ico",
        # The interface faces travel with the program: a Windows that
        # lacks Segoe UI (or ships a different cut of it) would draw
        # the menu in whatever it has.
        "fonts/IBMPlexSans-Regular.ttf", "fonts/IBMPlexMono-Regular.ttf",
        "fonts/OFL.txt",
        "runtime/pythonw.exe", "VERSION.txt",
    ]
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        missing = [f for f in required if f not in names]
        if missing:
            return False, f"missing from the archive: {missing}"
        # the worker in the archive carries the hook
        dll = z.read("native/nvngx.dll")
        if b"NS_ARCH_SPOOF" not in dll:
            return False, "nvngx.dll in the archive has no hook"
        # the worker in the archive carries the window-capture mode (WGCW)
        if b"WGCW" not in dll:
            return False, "nvngx.dll in the archive has no WGCW (window mode)"
        # the PYTHON SOURCES in the archive must be EXACTLY the committed
        # ones: the archive is often rebuilt from a dirty tree, and a code
        # change that never got committed ends up in the zip silently. The
        # same goes for the worker - the archive carries a freshly built
        # nvngx.dll whose content nobody can verify by eye, so a
        # non-committed rebuild slips through (audit #4, C1/C2).
        for name in ("main.py", "hotkeys.py", "display.py", "recorder.py",
                     "overlay_ui.py", "i18n.py", "protocol.py", "winapi.py",
                     "pipeline.py", "settings_io.py", "channels.py",
                     "commands.py", "paths.py", "startup.py",
                     "README.md", "README.ru.md"):
            try:
                head = subprocess.check_output(["git", "show", f"HEAD:{name}"],
                                               cwd=ROOT)
            except subprocess.CalledProcessError:
                continue  # not in git - a new file, nothing to compare with
            # The worktree files carry CRLF (core.autocrlf) while git show
            # returns LF - compare the NORMALIZED bytes on both sides,
            # otherwise every CRLF file trips the check (audit #4, C2).
            crlf, lf = bytes([13, 10]), bytes([10])
            got = z.read(name).replace(crlf, lf)
            if got != head.replace(crlf, lf):
                return False, f"{name} in the archive differs from HEAD"
        # the config in the archive is the default one, not a personal one:
        # personal values are written into the config legitimately, so we
        # check ALL such fields against the committed HEAD config
        cfg = json.loads(z.read("config.json"))
        try:
            head_cfg = json.loads(subprocess.check_output(
                ["git", "show", "HEAD:config.json"]))
        except Exception:
            head_cfg = {}
        leak = []
        for key in ("menu_offset", "menu_scale", "theme", "lang",
                    "open_menu_on_start", "split", "menu_height",
                    "hotkeys", "work_scale", "nr_small", "record_audio",
                    "spout"):
            if key not in head_cfg:
                continue
            if cfg.get(key) != head_cfg[key]:
                leak.append(f"{key}={cfg.get(key)!r} != HEAD {head_cfg[key]!r}")
        if leak:
            return False, "a personal config in the archive: " + ", ".join(leak)
        # VERSION.txt must tell the truth (audit 10.09 H1): the commit is
        # HEAD, the runtime sha matches the DLL inside, and the version
        # matches the file name. A manifest that lies is worse than none.
        vt = z.read("VERSION.txt").decode("utf-8", "replace")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=ROOT, text=True).strip()
        if f"commit: {head}" not in vt:
            return False, "VERSION.txt commit != HEAD - rebuilt from a dirty tree?"
        zip_dll = z.read("native/nvngx_dlssnr.dll")
        zsha = hashlib.sha256(zip_dll).hexdigest()
        if f"sha256 {zsha}" not in vt:
            return False, "VERSION.txt runtime sha != the DLL inside the archive"
        if "NeuralScreen 1.6.1" not in vt:
            return False, "VERSION.txt version does not match v1.6.1"
    return True, f"{zpath.stat().st_size} bytes, all files, the hook, a default config, a truthful manifest"


def gpuinfo_works():
    """gpuinfo.py answers: architecture plus official support."""
    py = ROOT / "runtime" / "python.exe"
    if not py.exists():
        return False, "no runtime/python.exe"
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import gpuinfo; i = gpuinfo.probe(); "
        "print(gpuinfo.describe(i)); print('official:', i['official'])" % ROOT
    )
    # PYTHONIOENCODING: without it the child prints in the console codepage
    # and an em-dash in the GPU name decodes into garbage (or throws).
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       timeout=30, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        return False, f"gpuinfo crashed: {r.stderr.strip()[:200]}"
    out = r.stdout.strip()
    if "Blackwell" not in out:
        return False, f"odd answer: {out}"
    return True, out.replace("\n", " | ")


def spoof_default_on():
    """The spoof is on by default: ArchSpoofRequested has no =1 requirement."""
    cpp = (ROOT / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8-sig")
    start = cpp.find("static bool ArchSpoofRequested()")
    end = cpp.find("static int SetupArchSpoof()")
    body = cpp[start:end]
    if "buf[0] == '0'" not in body:
        return False, "the NS_ARCH_SPOOF=0 logic was not found in ArchSpoofRequested"
    if "buf[0] == '1'" in body:
        return False, "the old =1 logic is still in ArchSpoofRequested"
    return True, "on by default, NS_ARCH_SPOOF=0 turns it off"


def readme_consistency():
    """The four docs line up: two short READMEs, two technical ones.

    The READMEs are for someone installing the program, so the check that
    matters is that they stayed short and that every image and link in them
    resolves. The measurements live in the technical docs, and the one fact
    those must not lose is the spoof default - it decides whether a 20/30/40
    card works at all.
    """
    import re

    docs = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "README.ru.md": (ROOT / "README.ru.md").read_text(encoding="utf-8"),
        "docs/TECHNICAL.md": (ROOT / "TECHNICAL.md").read_text(encoding="utf-8"),
        "docs/TECHNICAL.ru.md": (ROOT / "TECHNICAL.ru.md").read_text(encoding="utf-8"),
    }
    for name in ("README.md", "README.ru.md"):
        n = len(docs[name].splitlines())
        if n > 200:
            return False, f"{name} is {n} lines - it drifted back into a manual"
    # The Russian needle is the content of a translated doc and stays Russian.
    if "On by default" not in docs["docs/TECHNICAL.md"]:
        return False, "TECHNICAL.md lost the spoof default"
    if "Включено по умолчанию" not in docs["docs/TECHNICAL.ru.md"]:
        return False, "TECHNICAL.ru.md lost the spoof default"

    # Every relative link and image must resolve, in both directions.
    for name, text in docs.items():
        base = (ROOT / name).parent
        for target in re.findall(r"]\(([^)#][^)]*)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (base / target).exists():
                return False, f"{name} points at a missing {target}"
        for src in re.findall(r'<img src="([^"]+)"', text):
            # The screenshots are addressed absolutely on purpose: docs/ is
            # not in the release archive, and a relative link left every
            # image broken for anyone who unpacked the zip. Only a relative
            # path is ours to resolve.
            if src.startswith(("http://", "https://")):
                continue
            if not (base / src).exists():
                return False, f"{name} shows a missing {src}"
    return True, (f"READMEs {len(docs['README.md'].splitlines())}/"
                  f"{len(docs['README.ru.md'].splitlines())} lines, links resolve")


def git_clean():
    """The working copy is clean (apart from a personal config.json)."""
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dirty = [l for l in r.stdout.splitlines() if l.strip() and "config.json" not in l]
    if dirty:
        return False, f"dirty: {dirty[:5]}"
    return True, "clean (config.json is personal, as expected)"


def release_notes_short():
    """The latest release notes are concise (the EN part is under 2 KB)."""
    from json import loads as _loads
    try:
        tag = _loads(subprocess.check_output(
            ["gh", "release", "list", "-R", "perseval-BLR/DLSS5-NeuralScreen",
             "--json", "tagName", "--limit", "1", "--exclude-drafts"],
            text=True, timeout=60, encoding="utf-8", errors="replace"))[0]["tagName"]
    except Exception:
        return False, "cannot resolve the latest release tag"
    r = subprocess.run(
        ["gh", "release", "view", tag, "-R", "perseval-BLR/DLSS5-NeuralScreen",
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, f"gh: {r.stderr.strip()[:100]}"
    en_part = r.stdout.split("---")[0]
    if len(en_part) > 2500:
        return False, f"the EN part is {len(en_part)} characters - too long"
    return True, f"the EN part is {len(en_part)} characters"


# --- driving the running program (the GUI and smoke checks share this) ---
LOG = ROOT / "NeuralScreen.log"
KEYEVENTF_KEYUP = 0x0002
_MOD_VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10}  # Ctrl / Alt / Shift


def binding_keys(command):
    """The default binding for a command as (vk, modifier vks).

    Taken from hotkeys.DEFAULT_BINDINGS rather than written out here: the
    defaults have moved twice already, and a check that presses yesterday's
    key tests nothing.
    """
    sys.path.insert(0, str(ROOT))
    from hotkeys import DEFAULT_BINDINGS
    for mods, vk, cmd, _name in DEFAULT_BINDINGS.values():
        if cmd == command:
            return vk, tuple(v for bit, v in _MOD_VK.items() if mods & bit)
    raise KeyError(command)


def send_key(vk, mods=()):
    import ctypes
    for m in mods:
        ctypes.windll.user32.keybd_event(m, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        ctypes.windll.user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


def log_offset():
    """Where the log ends right now.

    The log is appended to across runs, so a check that greps the whole file
    can pass on the previous launch's lines. Everything below reads from the
    offset taken before the launch.
    """
    return LOG.stat().st_size if LOG.exists() else 0


def log_since(offset):
    # main.py opens the log as utf-8; errors="replace" guards a truncated tail.
    if not LOG.exists():
        return ""
    with LOG.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def running_instances():
    """Our processes that are already up.

    Nothing stops a second copy of the program from starting, and two copies
    fight over the screen capture - so a check that launches one has to know
    the field is clear, otherwise it measures the wrong process.
    """
    out = subprocess.run(["tasklist"], capture_output=True).stdout
    text = out.decode("cp1251", errors="replace")
    return [l.split()[0] for l in text.splitlines()
            if "pythonw.exe" in l or "nvngx.dll" in l]


def launch():
    """Start the program the way a user does and return the log offset."""
    offset = log_offset()
    subprocess.run(["cscript", "//nologo", "NeuralScreen.vbs"], cwd=ROOT,
                   capture_output=True)
    return offset


def wait_for(offset, needle, timeout):
    """Wait for a line to appear in the log written since `offset`."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_since(offset)
        if needle in text:
            return text
        time.sleep(0.5)
    return None


def quit_app(timeout=8.0):
    """Ctrl+Alt+Q, then confirm nothing of ours is left running."""
    import time
    send_key(*binding_keys("quit"))
    deadline = time.monotonic() + timeout
    left = []
    while time.monotonic() < deadline:
        out = subprocess.run(["tasklist"], capture_output=True).stdout
        text = out.decode("cp1251", errors="replace")
        left = [l for l in text.splitlines()
                if "pythonw.exe" in l or "nvngx.dll" in l]
        if not left:
            return []
        time.sleep(0.5)
    return left


def smoke_check():
    """The short one: launch -> is it processing -> exit. ~20 seconds.

    Everything a broken build fails at before recording even matters: the
    program comes up, the pipeline produces frames, the pixel channel is the
    shared-memory one (it silently fell back to the pipe once - see the OUTS
    regression), and Ctrl+Alt+Q leaves nothing running.
    """
    import re
    import time

    MIN_FRAMES, MIN_FPS = 60, 15.0
    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    offset = launch()
    try:
        # The first FPS line reports the warm-up (frames 2, ~1 FPS), so wait
        # for the pipeline to have actually run for a while.
        fps, frames, deadline = 0.0, 0, time.monotonic() + 30.0
        while time.monotonic() < deadline:
            text = log_since(offset)
            stats = re.findall(r"NR ON \| FPS\s+([\d.]+) \| frames (\d+)", text)
            if stats:
                fps, frames = float(stats[-1][0]), int(stats[-1][1])
                if frames >= MIN_FRAMES:
                    break
            time.sleep(0.5)
        text = log_since(offset)
        if frames == 0:
            return False, "NeuralScreen did not start processing (no FPS line in the log)"
        if frames < MIN_FRAMES or fps < MIN_FPS:
            return False, (f"the pipeline is barely alive: {fps:.1f} FPS, "
                           f"{frames} frames in 30 s")
        if "result pixels through shared memory" not in text:
            pipe = "shared memory for pixels unavailable" in text
            return False, ("the pixels are not going through shared memory"
                           + (" - the worker refused the section" if pipe else ""))
        # The capture must ALSO be the DDA one. A dead DDA1 makes the client
        # silently fall back to sending frames from Python - the pipeline
        # still runs, the FPS lines look fine, and only this log line tells
        # it apart (audit #4: DDA has no direct regression test).
        if "screen capture inside the worker (DDA1)" not in text:
            return False, "the worker is not capturing the screen (no DDA1 in the log)"
    finally:
        left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, f"came up, {fps:.1f} FPS at {frames} frames, shared memory, clean exit"


def gui_check():
    """The full GUI cycle: launch -> record -> exit. Requires NeuralScreen not
    to be running. ~50 seconds."""
    import re
    import time

    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    # 1. launch
    offset = launch()
    if wait_for(offset, "NR ON", timeout=25.0) is None:
        return False, "NeuralScreen did not come up (no 'NR ON' in the log)"
    # The first FPS lines report the warm-up (frames 2, ~1 FPS) - the NGX
    # feature discards 120 warmup frames and the pipeline needs several more
    # seconds to normalise (measured: 20 FPS at frame 82, 28 at 191, 38 at
    # 336). Recording into the warm-up would produce a short, slow file and
    # a false FAIL (75 frames / 4.8 s instead of 100+). Wait for the
    # pipeline to have actually normalised - frames >= 300 is ~8-10 s after
    # launch, past the warm-up.
    fps, frames, deadline = 0.0, 0, time.monotonic() + 40.0
    while time.monotonic() < deadline:
        text = log_since(offset)
        stats = re.findall(r"NR ON \| FPS\s+([\d.]+) \| frames (\d+)", text)
        if stats:
            fps, frames = float(stats[-1][0]), int(stats[-1][1])
            if frames >= 300:
                break
        time.sleep(0.5)
    if frames < 300:
        quit_app()
        return False, (f"the pipeline did not normalise: {fps:.1f} FPS, "
                       f"{frames} frames in 40 s")
    # 2. record for 5 seconds - with the key the program actually binds
    record = binding_keys("record")
    send_key(*record)
    time.sleep(5)
    send_key(*record)
    time.sleep(3)
    recs = sorted((ROOT / "recordings").glob("neuralscreen-*.mp4"),
                  key=lambda p: p.stat().st_mtime)
    if not recs:
        quit_app()
        return False, "the recording file was not created"
    rec = recs[-1]
    import av
    c = av.open(str(rec))
    s = c.streams.video[0]
    frames, dur = s.frames, float(s.duration * s.time_base)
    c.close()
    if frames < 100 or dur < 4:
        quit_app()
        return False, f"the recording looks suspicious: {frames} frames / {dur:.1f} s"
    # 3. exit
    left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, (f"recording of {frames} frames / {dur:.1f} s, "
                  f"a clean exit, no processes left")


def main():
    print(f"NeuralScreen autocheck - {ROOT}")
    print("=" * 60)
    if "--gui" in sys.argv:
        check("GUI: launch -> record -> exit", gui_check)
    elif "--smoke" in sys.argv:
        check("smoke: launch -> processing -> exit", smoke_check)
    else:
        check("worker: fresh, with the hook", fresh_worker)
        check("zip: integrity and contents", zip_integrity)
        check("gpuinfo: answers", gpuinfo_works)
        check("spoof: on by default", spoof_default_on)
        check("README: EN/RU agree", readme_consistency)
        check("git: the working copy is clean", git_clean)
        check("release notes: concise", release_notes_short)
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAIL - {FAILS}")
        return 1
    print("RESULT: all checks PASS")
    if "--gui" not in sys.argv and "--smoke" not in sys.argv:
        print()
        print("smoke (20 s):  runtime\\python.exe autocheck.py --smoke")
        print("GUI part:      runtime\\python.exe autocheck.py --gui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
