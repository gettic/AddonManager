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
