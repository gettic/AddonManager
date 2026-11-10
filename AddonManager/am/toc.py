"""Read an addon's version from its <Folder>_Forever.toc."""

import os
import re

_VERSION_RE = re.compile(r"^\s*##\s*Version\s*:\s*(.*?)\s*$", re.IGNORECASE)
_COLOR_RE = re.compile(r"\|c[0-9a-fA-F]{8}|\|r")


def toc_name(folder):
    return f"{folder}_Forever.toc"


def toc_path(addons_dir, folder):
    return os.path.join(addons_dir, folder, toc_name(folder))


def parse_version(text):
    """The '## Version:' value from .toc text, or None."""
    for line in text.splitlines():
        m = _VERSION_RE.match(line)
        if m:
            value = _COLOR_RE.sub("", m.group(1)).strip()
            if not value or "@" in value:  # e.g. @project-version@
                return None
            return value
    return None


def decode(data):
    return data.decode("utf-8-sig", errors="replace")


# ---------- any addon's toc (addons from a file; see am\fileinstall.py) ----------
# warcraft.wiki.gg TOC_format: the client loads <Folder>_<game type>.toc first (Forever's
# game type is "Forever", alias "Camelot"), then the family one (_Mainline), then <Folder>.toc.
# "-" works like "_" in older clients; accepted here too.
FOREVER_INTERFACE = 16001
FOREVER_SUFFIXES = ("forever", "camelot")
FAMILY_SUFFIXES = ("mainline",)
OTHER_SUFFIXES = ("standard", "classic", "vanilla", "tbc", "bcc", "wrath", "wotlkc", "cata",
                  "mists", "plunderstorm", "wowlabs", "wowhack")
KNOWN_SUFFIXES = FOREVER_SUFFIXES + FAMILY_SUFFIXES + OTHER_SUFFIXES
FOREVER, OTHER_VERSION, NOT_FOREVER = "forever", "other", "not_forever"

_INTERFACE_RE = re.compile(r"^\s*##\s*Interface\s*:\s*(.*?)\s*$", re.IGNORECASE)
_TITLE_RE = re.compile(r"^\s*##\s*Title\s*:\s*(.*?)\s*$", re.IGNORECASE)
_TEXTURE_RE = re.compile(r"\|T[^|]*\|t|\|A[^|]*\|a")
_ESCAPE_RE = re.compile(r"\|c[0-9a-fA-F]{8}|\|r|\|n")


def split_toc_name(name):
    """'Details_Forever.toc' -> ('details', 'forever'); 'Details.toc' -> ('details', '');
    a suffix that isn't a known game type stays part of the name. None if not a .toc."""
    low = name.lower()
    if not low.endswith(".toc") or len(low) <= 4:
        return None
    stem = low[:-4]
    for sep in ("_", "-"):
        base, _, suffix = stem.rpartition(sep)
        if base and suffix in KNOWN_SUFFIXES:
            return base, suffix
    return stem, ""


def _key_for(toc_file, folder):
    """The suffix ('' = plain) if toc_file is a toc of addon folder `folder`, else None."""
    split = split_toc_name(toc_file)
    if not split:
        return None
    if toc_file[:-4].lower() == folder.lower():
        return ""   # <folder>.toc, also for a folder named like "X_Classic"
    return split[1] if split[0] == folder.lower() else None


def belongs(toc_file, folder):
    """True if WoW could load toc_file for an addon folder named folder (any case)."""
    return _key_for(toc_file, folder) is not None


def pick(names, folder):
    """The toc WoW Forever loads for `folder` from the file names in it (any case):
    _Forever/_Camelot, then _Mainline, then <folder>.toc ('-' like '_'). None if WoW
    Forever loads none of them (e.g. only _Classic/_Vanilla tocs)."""
    by_key = {}
    for n in sorted(names):
        key = _key_for(n, folder)
        if key is not None:
            by_key.setdefault(key, n)
    for suffix in FOREVER_SUFFIXES + FAMILY_SUFFIXES + ("",):
        if suffix in by_key:
            return by_key[suffix]
    return None


def interfaces(text):
    """The numbers on the '## Interface:' line (comma separated), or []."""
    for line in text.splitlines():
        m = _INTERFACE_RE.match(line)
        if m:
            return [int(v) for v in re.findall(r"\d+", m.group(1))][:20]
    return []


def is_forever_interface(number):
    """16001 now; later WoW Forever patches are expected to stay 160xx."""
    return 16000 <= number < 16100


def title(text, fallback):
    """'## Title:' without colour codes and icons, at most 60 characters; else fallback."""
    for line in text.splitlines():
        m = _TITLE_RE.match(line)
        if m:
            value = _ESCAPE_RE.sub("", _TEXTURE_RE.sub("", m.group(1)))
            value = " ".join("".join(c for c in value if c.isprintable()).split())
            return value[:60] if value else fallback
    return fallback


def forever_status(toc_file, text):
    """FOREVER, OTHER_VERSION (a toc WoW Forever loads but made for another version: WoW
    may call it out of date) or NOT_FOREVER (toc_file None: Forever loads no toc)."""
    if toc_file is None:
        return NOT_FOREVER
    return FOREVER if any(is_forever_interface(n) for n in interfaces(text)) else OTHER_VERSION


def read_text(path):
    """A toc file's text (first 1 MiB), or '' if it can't be read."""
    try:
        with open(path, "rb") as f:
            return decode(f.read(1024 * 1024))
    except OSError:
        return ""


def describe(folder_path, folder):
    """(toc file or None, title, version or None, forever_status) of an addon folder on disk."""
    try:
        names = [e.name for e in os.scandir(folder_path) if e.is_file(follow_symlinks=False)]
    except OSError:
        names = []
    chosen = pick(names, folder)
    text = read_text(os.path.join(folder_path, chosen)) if chosen else ""
    return chosen, title(text, folder), parse_version(text), forever_status(chosen, text)


def installed(addons_dir, folder):
    """(exists, version): exists is True if the addon folder is there."""
    folder_path = os.path.join(addons_dir, folder)
    if not os.path.isdir(folder_path):
        return False, None
    try:
        with open(toc_path(addons_dir, folder), "rb") as f:
            return True, parse_version(decode(f.read(1024 * 1024)))
    except OSError:
        return True, None
