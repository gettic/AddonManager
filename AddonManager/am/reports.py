"""Guide reports: sends the "Report a mistake" lines a player saved with Send in the
All-in-One Guide addon, so they don't need a GitHub account.

In the game, Send only writes the report line into the addon's own saved file (the addon
never sends anything). WoW writes that file when the player logs out or types /reload:

    WTF\\Account\\<account>\\SavedVariables\\AllinOneGuide.lua
        AllinOneGuideDB = { ["modules"] = { ["reports"] = { ["outbox"] = { "AOGR2 rid=...;ck=...", ... } } } }

This module only READS that file as text (a small scanner and regular expressions; the Lua
is never run) and never writes it (WoW rewrites it at logout and could race with us). It
skips everything while the game runs (the file isn't final then). Each line must pass the
same checks as the server (report_worker\\): the "AOGR2 " start, the allowed characters, at
most 1,500 characters and a matching Adler-32 checksum. A report is sent once: the app keeps
the ids of sent reports (settings "reports_sent"); the server also refuses an id twice.

What is sent (POST to config.REPORT_URL), nothing else:

    {"v": 1, "a": "AllinOneGuide", "r": "AOGR2 rid=k3x9qa;v=...;ck=1a2b3c4d"}

The line holds the step, the addon and client version, the language, level/class/race and,
unless the player ticked "Leave out my location" in the game, the map position. Never a name,
account, path or the account folder name. The server turns it into a public GitHub issue.

Asked once (settings "send_reports": missing = not asked yet, True = send automatically,
False = never), and only when there is something to send. Closing the question (X or Esc)
saves nothing: asked again next time. Settings has the same switch.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib

from am import download

log = logging.getLogger(__name__)

ON_SETTING = "send_reports"      # True = send automatically, False = never, missing = ask once
SENT_SETTING = "reports_sent"    # ids of reports already sent (newest last)
SENT_KEEP = 500                  # ids kept (the addon keeps 20 reports at most)
TIMEOUT = 8
MAX_LEN = 1500
MAX_FILE = 8 * 1024 * 1024       # a saved file bigger than this is not read
MAX_PER_RUN = 20
FINAL_CODES = (400, 409)         # the only final answers (bad line / already filed)

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


def _read(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            log.info("Saved file too big to read for reports")
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def pending(accounts_dir, sent=()):
    """[(addon folder, line)] not sent yet, from every account, each report id once."""
    done = set(sent or ())
    out = []
    for addon, stem in ADDONS.items():
        for path in saved_files(accounts_dir, stem):
            for line in outbox_lines(_read(path)):
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


TYPES = {"coords": "Wrong place on the map", "order": "Wrong order",
         "missing": "Quest isn't there", "notick": "Step doesn't tick",
         "text": "Text is wrong", "danger": "Dangerous spot", "other": "Something else"}


def summary(line):
    """One short line for the question: type, step, note, location or not
    ('Wrong order, step 12: "note" (no location)'). No raw guide ids."""
    f = fields(line)
    what = TYPES.get(f.get("ab", ""), "Something else")
    note = " ".join(f.get("note", "").split())
    if len(note) > 60:
        note = note[:57] + "..."
    text = what
    step = " ".join(f.get("sid", "").split())[:20]
    if step:
        text += f", step {step}"
    if note:
        text += f': "{note}"'
    text += " (no location)" if f.get("noloc") == "1" else " (with your map position)"
    return text


def question(lines):
    """Title and text of the one-time question."""
    n = len(lines)
    shown = [f"- {summary(line)}" for _a, line in lines[:3]]
    if n > 3:
        shown.append(f"- and {n - 3} more")
    head = ("1 guide report you made in the game is waiting:" if n == 1 else
            f"{n} guide reports you made in the game are waiting:")
    text = "\n".join([head] + shown + [
        "",
        "Send guide reports automatically from now on?",
        "",
        "Anyone can read them on the guide's GitHub page, so keep names out of your note. "
        "A report holds the step, your note, level, class, race, language, game and addon "
        "version, and map position (unless you ticked \"Leave out my location\"). Never your "
        "name or account.",
        "You can change this later in Settings."])
    return "Guide reports", text


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


def find_worker(q, accounts_dir, sent, check_game):
    """Thread: ("reports_found", [(addon, line)]) unless the game runs (then nothing)."""
    try:
        if check_game():
            return
        found = pending(accounts_dir, sent)
    except Exception:   # noqa: BLE001 (never bother the player)
        log.exception("Reading guide reports failed")
        return
    if found:
        q.put(("reports_found", found))


def send_worker(q, url, lines, allow_local, check_game=None):
    """Thread: send each; ("reports_sent", [ids done], number sent). Done = sent or refused
    for good (never tried again)."""
    done, n = [], 0
    for addon, line in lines:
        how = send_one(url, addon, line, allow_local)
        if how in ("sent", "refused"):
            done.append(rid(line))
            n += how == "sent"
        else:
            break   # offline or the server is busy: the rest waits for the next run
    q.put(("reports_sent", done, n))
