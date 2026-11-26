"""Guide reports: sends the "Report a mistake" lines a player saved with Send in the
All-in-One Guide addon, so they don't need a GitHub account.

In the game, Send only writes the report line into the addon's own saved file (the addon
never sends anything). WoW writes that file when the player logs out or types /reload:

    WTF\\Account\\<account>\\SavedVariables\\AllinOneGuide.lua
        AllinOneGuideDB = { ["modules"] = { ["reports"] = { ["outbox"] = { "AOGR2 rid=...;ck=...", ... } } } }

This module only READS that file as text (a small scanner and regular expressions; the Lua
is never run) and never writes it. It is opened read-only and shared for reading, writing
and deleting (`_open_shared`), so WoW can always rewrite or rename it, even mid-read. The
file is read at app start and, while the app is open, when its size or modified time
changed (looked at every WATCH_S with os.stat only) and then stayed the same for SETTLE_S;
a file whose blocks don't all close (WoW still writing) is skipped and looked at again at
the next check. This works while the game runs too (WoW writes the file at /reload, logout
and exit); nothing but this one file is read. Each line must pass the
same checks as the server (report_worker\\): the "AOGR2 " start, the allowed characters, at
most 1,500 characters and a matching Adler-32 checksum. A report is sent once: the app keeps
the ids of sent reports (settings "reports_sent"); the server also refuses an id twice.

What is sent (POST to config.REPORT_URL), nothing else:

    {"v": 1, "a": "AllinOneGuide", "r": "AOGR2 rid=k3x9qa;v=...;ck=1a2b3c4d"}

The line holds the step, the addon and client version, the language, level/class/race and,
unless the player ticked "Leave out my location" in the game, the map position. Never a name,
account, path or the account folder name. The server turns it into a public GitHub issue.

On by default, like the anonymous counts: sent quietly with no question (settings
"send_reports": missing or True = send, False = never). The player switches it off with the
"Send guide reports" switch in Settings; a False saved earlier ("Never send") stays off.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from am import download

log = logging.getLogger(__name__)

ON_SETTING = "send_reports"      # missing or True = send (on by default), False = never
SENT_SETTING = "reports_sent"    # ids of reports already sent (newest last)
SENT_KEEP = 500                  # ids kept (the addon keeps 20 reports at most)
TIMEOUT = 8
MAX_LEN = 1500
MAX_FILE = 8 * 1024 * 1024       # a saved file bigger than this is not read
MAX_PER_RUN = 20
FINAL_CODES = (400, 409)         # the only final answers (bad line / already filed)
WATCH_S = 600                    # while the app is open: look at the file every 10 minutes
SETTLE_S = 5                     # a changed file must stay the same this long before reading

# Addon folder -> its saved file (the stem in SavedVariables). Kept general so other
# guild addons can use it later with the same line format.
ADDONS = {"AllinOneGuide": "AllinOneGuide"}

# The report line (same as the addon's Report.WIRE and the server's LINE_RE).
LINE_RE = re.compile(
    r"AOGR2 rid=(?P<rid>[a-z0-9]{1,16});[A-Za-z0-9 .,:_/+()'!?\-%=;]*;ck=(?P<ck>[0-9a-f]{8})",
    re.ASCII)
_OUTBOX_RE = re.compile(r'\[\s*"outbox"\s*\]\s*=\s*\{')
_REPORTS_RE = re.compile(r'\[\s*"reports"\s*\]\s*=\s*\{')
_STRING_RE = re.compile(r'"((?:[^"\\\r\n]|\\.){0,2000})"')
_FIELD_RE = re.compile(r"([a-z0-9]{1,8})=([^;]*)")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# complete(): strings (escapes and newlines allowed), "--" comments, braces; a lone " is a
# string that never closes.
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|--[^\n]*|[{}"]', re.DOTALL)


def checksum(body):
    """Adler-32 as 8 lower-case hex digits (the addon's Format.Adler32)."""
    return "%08x" % (zlib.adler32(body.encode("ascii")) & 0xFFFFFFFF)


def valid(line):
    """True for a whole, unchanged report line."""
    if not isinstance(line, str) or len(line) > MAX_LEN:
        return False
    m = LINE_RE.fullmatch(line)
    if not m:
        return False
    body = line[: -len(";ck=") - 8]
    return checksum(body) == m.group("ck")


def rid(line):
    m = LINE_RE.fullmatch(line or "")
    return m.group("rid") if m else None


def _block_end(text, start):
    """text[start] is just after a "{": the index of its matching "}" (strings skipped),
    or -1."""
    depth, i, n = 1, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        elif c == "-" and text.startswith("--", i):
            nl = text.find("\n", i)   # a "-- [1]" comment WoW writes after each entry
            i = n if nl < 0 else nl
            continue
        i += 1
    return -1


def complete(text):
    """True when every { in a saved file's text closes and no string is left open (WoW
    has finished writing it). Strings and "--" comments are skipped, as in _block_end."""
    depth = 0
    for m in _TOKEN_RE.finditer(text):
        tok = m.group()
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
            if depth < 0:
                return False
        elif tok == '"':
            return False
    return depth == 0


def outbox_lines(text):
    """The valid report lines in every ["reports"] = { ... ["outbox"] = { ... } } block of
    a saved file's text, in file order (newest first), without repeats. Stops at the first
    block that never closes (nothing after it can close either), so a damaged file with
    many open blocks can't take long."""
    out, seen = [], set()
    for rm in _REPORTS_RE.finditer(text):
        r_end = _block_end(text, rm.end())
        if r_end < 0:
            break
        reports = text[rm.end():r_end]
        for om in _OUTBOX_RE.finditer(reports):
            o_end = _block_end(reports, om.end())
            if o_end < 0:
                break
            for sm in _STRING_RE.finditer(reports[om.end():o_end]):
                line = sm.group(1)
                if valid(line) and line not in seen:
                    seen.add(line)
                    out.append(line)
    return out


def saved_files(accounts_dir, stem):
    """<accounts>\\<account>\\SavedVariables\\<stem>.lua for every account folder (case
    of the names as on disk; links and odd names skipped)."""
    files = []
    try:
        with os.scandir(accounts_dir) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
    except OSError:
        return files
    want = (stem + ".lua").lower()
    for acc in entries:
        if (not acc.is_dir(follow_symlinks=False) or acc.name.startswith(".")
                or acc.name == "SavedVariables"):
            continue
        try:
            with os.scandir(acc.path) as it:
                sv = next((e.path for e in it if e.name.lower() == "savedvariables"
                           and e.is_dir(follow_symlinks=False)), None)
            if sv is None:
                continue
            with os.scandir(sv) as it:
                path = next((e.path for e in it if e.name.lower() == want
                             and e.is_file(follow_symlinks=False)), None)
            if path:
                files.append(path)
        except OSError:
            continue
    return files


def _open_shared(path):
    """The file opened read-only (binary). On Windows through CreateFileW with
    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, so WoW can still write, replace
    or rename it while it is open (Python's own open() doesn't share delete/rename).
    Elsewhere a plain open (Linux has no such locks)."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, normal = 0x80000000, 0x1 | 0x2 | 0x4, 3, 0x80
    handle = create(path, generic_read, share_all, None, open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _read(path):
    """The file's text ("" when missing, too big or unreadable), read in one go."""
    try:
        with _open_shared(path) as f:
            data = f.read(MAX_FILE + 1)
    except OSError:
        return ""
    if len(data) > MAX_FILE:
        log.info("Saved file too big to read for reports")
        return ""
    return data.decode("utf-8", errors="replace")


def stamp(accounts_dir):
    """{path: (size, modified ns)} of every saved file reports come from: os.stat only, the
    files aren't opened. Compared between checks to see whether WoW wrote them."""
    out = {}
    for stem in ADDONS.values():
        for path in saved_files(accounts_dir, stem):
            try:
                st = os.stat(path)
            except OSError:
                continue
            out[path] = (st.st_size, st.st_mtime_ns)
    return out


def pending(accounts_dir, sent=(), unfinished=None):
    """[(addon folder, line)] not sent yet, from every account, each report id once.
    A file WoW hasn't finished writing (complete() False) is skipped; its path goes into
    the list unfinished when one is given."""
    done = set(sent or ())
    out = []
    for addon, stem in ADDONS.items():
        for path in saved_files(accounts_dir, stem):
            text = _read(path)
            if text and not complete(text):
                log.info("Saved file not complete yet; reports looked at next time")
                if unfinished is not None:
                    unfinished.append(path)
                continue
            for line in outbox_lines(text):
                r = rid(line)
                if r and r not in done:
                    done.add(r)
                    out.append((addon, line))
    return out[:MAX_PER_RUN]


def fields(line):
    """The line's fields as plain text ({"ab": "coords", "note": "...", ...})."""
    body = line[len("AOGR2 "):]
    out = {}
    for m in _FIELD_RE.finditer(body):
        try:
            out[m.group(1)] = urllib.parse.unquote(m.group(2), errors="replace")
        except Exception:   # noqa: BLE001
            out[m.group(1)] = m.group(2)
    return out


def url_ok(url, allow_local=False):
    """https only (http://127.0.0.1 etc. only in tests); no user name in the URL."""
    if not isinstance(url, str) or not url:
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return allow_local and parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is a failure: reports go to this one address only


def send_one(url, addon, line, allow_local=False, timeout=TIMEOUT):
    """POST one report. "sent" (2xx), "refused" (only 409 = already filed and 400 = a bad
    line: don't try again) or "later" (offline and every other answer, 404/401/403/5xx...:
    the address may not be live yet or blocked for a while). Never raises."""
    if not url_ok(url, allow_local) or not valid(line):
        return "refused" if url_ok(url, allow_local) else "later"
    try:
        body = json.dumps({"v": 1, "a": addon, "r": line}, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": download.USER_AGENT})
        opener = urllib.request.build_opener(_NoRedirects)
        with opener.open(req, timeout=timeout) as resp:
            return "sent" if 200 <= resp.status < 300 else "later"
    except urllib.error.HTTPError as e:
        log.info("Guide report not sent: HTTP %s", e.code)
        return "refused" if e.code in FINAL_CODES else "later"
    except Exception as e:   # offline, timeout, anything: try again next run
        log.info("Guide report not sent: %s", type(e).__name__)
    return "later"


def remember(settings, rids):
    """Add sent ids to the settings (newest last, at most SENT_KEEP). Returns the list."""
    old = [r for r in settings.get(SENT_SETTING) or [] if isinstance(r, str)]
    new = old + [r for r in rids if r not in old]
    settings[SENT_SETTING] = new[-SENT_KEEP:]
    return settings[SENT_SETTING]


def find_worker(q, accounts_dir, sent, last=None, settle_s=SETTLE_S, sleep=time.sleep):
    """Thread: always ("reports_found", [(addon, line)], stamp), so the caller knows the
    look is over. The files are read only when their stamp differs from last (None = the
    first look) and then stays the same for settle_s. The stamp passed back is the one to
    compare with next time: last again when they were still changing or a file wasn't
    complete (looked at again at the next check)."""
    found, keep = [], last
    try:
        now = stamp(accounts_dir)
        if now and now != last:
            if settle_s:
                sleep(settle_s)
            if stamp(accounts_dir) == now:
                unfinished = []
                found = pending(accounts_dir, sent, unfinished)
                if not unfinished:
                    keep = now
        elif not now:
            keep = now
    except Exception:   # noqa: BLE001 (never bother the player)
        log.exception("Reading guide reports failed")
    q.put(("reports_found", found, keep))


def send_worker(q, url, lines, allow_local):
    """Thread: send each; ("reports_sent", [ids done], number sent, all done). Done = sent
    or refused for good (never tried again)."""
    done, n = [], 0
    for addon, line in lines:
        how = send_one(url, addon, line, allow_local)
        if how in ("sent", "refused"):
            done.append(rid(line))
            n += how == "sent"
        else:
            break   # offline or the server is busy: the rest waits for the next run
    q.put(("reports_sent", done, n, len(done) == len(lines)))
