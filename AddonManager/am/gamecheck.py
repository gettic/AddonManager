"""Is the game running? Matches process NAMES only: it never opens, reads or attaches
to the game process (tos condition).

Windows: the names from tasklist. Linux (the game runs in Wine/Proton): for each
process only /proc/<pid>/comm, or if that doesn't match, the first argument of
/proc/<pid>/cmdline cut to its base name at once. The rest of the command line is
never kept, logged or shown; mem, maps and environ are never read."""

import csv
import io
import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)

GAME_EXES = {
    "wow.exe", "wow-64.exe", "wowt.exe", "wowb.exe",
    "wowclassic.exe", "wowclassict.exe", "wowclassicb.exe",
    "wowforever.exe",
}

PROC = "/proc"
_COMM_LIMIT = 15   # Linux keeps only the first 15 characters of a process name


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


def base_name(arg):
    """'C:\\Games\\Wow.exe' or '/x/Wow.exe' -> 'wow.exe'."""
    return re.split(r"[\\/]", arg.strip())[-1].lower()


def _read_small(path, limit):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _comm_matches(comm):
    """comm may be cut to 15 characters ('wowclassict.exe' fits, longer ones don't)."""
    if not comm:
        return False
    if comm in GAME_EXES:
        return True
    return len(comm) >= _COMM_LIMIT and any(e.startswith(comm) for e in GAME_EXES)


def linux_game_running(proc=PROC):
    """True if a process is named like the game (base names only)."""
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return False
    for pid in pids:
        comm = _read_small(os.path.join(proc, pid, "comm"), 64).decode(
            "utf-8", "replace").strip().lower()
        if _comm_matches(comm):
            return True
        if comm and not comm.endswith(".exe") and "wine" not in comm and "preloader" not in comm:
            continue   # plain Linux programs: skip their command line
        # Wine may name the process "wine64-preloader": take only argv[0]'s base name.
        first = _read_small(os.path.join(proc, pid, "cmdline"), 4096).split(b"\0", 1)[0]
        if base_name(first.decode("utf-8", "replace")) in GAME_EXES:
            return True
    return False


def game_running(run=None):
    """True if a game process name is running. False if unsure."""
    try:
        if run is None:
            if os.name != "nt":
                return linux_game_running()
            run = _run_tasklist
        return bool(process_names(run()) & GAME_EXES)
    except Exception:
        log.exception("Could not check running programs")
        return False
