"""Which typefaces the overlay draws with.

Up to 1.6.0 every glyph in the program was Consolas - titles, labels,
hints, values alike. One monospaced face for a whole interface is why the
menu read as a debug console rather than as a program.

The split here is by ROLE, not by taste:

* proportional (Segoe UI) for language - titles, labels, hints, buttons;
* monospaced for READINGS - fps, resolutions, timers, key captions. A
  fixed advance keeps digits from dancing sideways as they change, which
  is the one thing Consolas was actually good for.

Faces are looked up by FILE, not by family name. pygame's SysFont
normalises "segoeui" to segoeuil.ttf - the Light weight, too thin over a
game - and does not resolve CascadiaMono.ttf at all. A `fonts/` directory
next to the program wins over the system ones, so a shipped face (IBM
Plex, say) replaces Segoe without touching this module: drop the files in
under the names listed below.

CJK is the exception that stays family-based: Chinese, Japanese and
Korean have no glyphs in the Latin faces (tofu boxes), and the system
fonts that do carry them differ per script.
"""
from __future__ import annotations

import os
from pathlib import Path

import pygame

from paths import BASE_DIR


# Shipped faces win over system ones - first name that exists is used.
UI_FACES = ("IBMPlexSans-Regular.ttf", "segoeui.ttf")
UI_BOLD_FACES = ("IBMPlexSans-SemiBold.ttf", "seguisb.ttf", "segoeuib.ttf")
MONO_FACES = ("IBMPlexMono-Regular.ttf", "CascadiaMono.ttf", "consola.ttf")
MONO_BOLD_FACES = ("IBMPlexMono-Medium.ttf", "CascadiaMono.ttf",
                   "consolab.ttf")

# Where to look, in order: our own folder first, then the system one.
FONT_DIRS = (BASE_DIR / "fonts",
             Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts")

# Per-script system families. Yu Gothic covers Japanese kanji, Malgun
# Gothic hangul, YaHei simplified Chinese.
CJK_FONTS = {"zh": "microsoftyahei", "ja": "yugothic", "ko": "malgungothic"}

_cache: dict = {}


def _find(names) -> Path | None:
    for directory in FONT_DIRS:
        for name in names:
            path = directory / name
            if path.exists():
                return path
    return None


def load(size: int, mono: bool = False, bold: bool = False,
         lang: str = "en"):
    """A font for `size` px. Cached - the loaders are called per redraw."""
    size = max(8, int(size))
    key = (size, mono, bold, lang if lang in CJK_FONTS else "")
    hit = _cache.get(key)
    if hit is not None:
        return hit
    font = _build(size, mono, bold, lang)
    _cache[key] = font
    return font


def _build(size: int, mono: bool, bold: bool, lang: str):
    if lang in CJK_FONTS:
        # The CJK families are only reachable by name - there is no stable
        # file name for them across Windows versions.
        try:
            return pygame.font.SysFont(CJK_FONTS[lang], size, bold=bold)
        except Exception:
            pass
    faces = ((MONO_BOLD_FACES if bold else MONO_FACES) if mono
             else (UI_BOLD_FACES if bold else UI_FACES))
    path = _find(faces)
    if path is not None:
        try:
            font = pygame.font.Font(str(path), size)
            # Cascadia and Consolas ship no semibold file: when the bold
            # face resolved to the regular one, synthesise the weight.
            if bold and path.name in ("CascadiaMono.ttf",):
                font.set_bold(True)
            return font
        except Exception:
            pass
    # Nothing found on disk: fall back the way the program did before.
    try:
        return pygame.font.SysFont("consolas", size, bold=bold)
    except Exception:
        return pygame.font.Font(None, size)


def clear_cache() -> None:
    """Drop the cached faces - used by the tests and after a scale change."""
    _cache.clear()
