"""Is the game running? Matches process NAMES from tasklist only: it never opens,
reads or attaches to the game process (tos condition)."""

import csv
import io
import logging
import subprocess

log = logging.getLogger(__name__)

GAME_EXES = {
    "wow.exe", "wow-64.exe", "wowt.exe", "wowb.exe",
    "wowclassic.exe", "wowclassict.exe", "wowclassicb.exe",
    "wowforever.exe",
}


def _run_tasklist():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True, timeout=15, creationflags=flags)
    return result.stdout


def process_names(output):
    """First column of tasklist CSV output, lower-cased."""
    names = set()
    for row in csv.reader(io.StringIO(output)):
        if row:
            names.add(row[0].strip().lower())
    return names


def game_running(run=_run_tasklist):
    """True if a game process name is in the task list. False if unsure."""
    try:
        return bool(process_names(run()) & GAME_EXES)
    except Exception:
        log.exception("Could not check running programs")
        return False
