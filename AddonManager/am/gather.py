"""Game data from All-in-One Guide: sends the sealed "AOGD1" pages the addon keeps on this PC,
so the guides can be fixed and sped up with real game facts.

In the game the addon only records into its own saved file (it never sends anything):

    WTF\\Account\\<account>\\SavedVariables\\AllinOneGuide.lua
        AllinOneGuideDB = { ["modules"] = { ["gather"] = { ["pages"] = { "AOGD1 rid=...", ... } } } }

Rules (the guide's am-spec.md and our tos check, plans\\guide-data-gathering):
- Read ONLY while WoW is closed (gamecheck before anything is opened); read-only, shared open
  (reports._open_shared), the Lua is never run: a quoted-string scan inside the
  modules -> gather -> pages table only. The live part and anything else is never read or kept.
- Each page must match PAGE_RE exactly and carry a matching Adler-32 (the addon's
  Format.Adler32). Anything else is skipped: not shown, not sent.
- On unless the player switches it off in Settings (settings "share_game_data": missing or
  True = on, False = off). Its own setting, separate from the guide reports' "send_reports".
- A random sender code (sid, 12 a-z0-9 from `secrets`) per WoW account folder, made before the
  first send and kept in settings "gather_accounts" with the page ids already sent. Switching
  off removes every code; switching on again makes new ones. The account folder name is never
  stored or sent: entries are keyed by an HMAC with a random salt made on this PC.
- POST config.DATA_URL {"v": 1, "sid": "...", "pages": [...]} (at most 30 pages, 100 KB).
  Answer 200 {"took", "dup", "bad", "over"}: took/dup/bad are done for good; "over" (the daily
  limit) and a plain 429 wait for the next UTC day. Everything else: try again next look.
Nothing is logged but counts.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request

from am import download, reports

log = logging.getLogger(__name__)

ON_SETTING = "share_game_data"      # missing or True = send, False = never
ACCOUNTS_SETTING = "gather_accounts"
SALT_SETTING = "gather_salt"
STEM = "AllinOneGuide"              # the saved file (SavedVariables\AllinOneGuide.lua)
MAX_PAGE = 3000
MAX_PAGES = 30                      # per request (and the addon's ring size)
MAX_BODY = 100 * 1024               # per request
SENT_KEEP = 200                     # page ids remembered per account
SID_LEN = 12
SID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
TIMEOUT = 15

# The spec's page rule (am-spec.md 1), as the addon's G.WIRE.
PAGE_RE = re.compile(
    r"AOGD1 rid=(?P<rid>[a-z0-9]+);h=1;[A-Za-z0-9 .,:_/+()'!?\-%=;]+ AOGD1 ck=(?P<ck>[0-9a-f]{8})",
    re.ASCII)
SID_RE = re.compile(r"[a-z0-9]{%d}" % SID_LEN, re.ASCII)
_CK_TAIL = len(" AOGD1 ck=") + 8
_TOP_RE = re.compile(r"^AllinOneGuideDB\s*=\s*\{", re.MULTILINE)
_KEY_AT = re.compile(r'\[\s*"((?:[^"\\\r\n]|\\.){1,64})"\s*\]\s*=\s*\{')
_STRING_RE = re.compile(r'"((?:[^"\\\r\n]|\\.){0,%d})"' % (MAX_PAGE + 100))
_UNESCAPE_RE = re.compile(r'\\(["\\])')


# ---------------------------------------------------------------- pages
def valid_page(page):
    """True for a whole, unchanged page (rule + length + checksum)."""
    if not isinstance(page, str) or len(page) > MAX_PAGE:
        return False
    m = PAGE_RE.fullmatch(page)
    return bool(m) and reports.checksum(page[:-_CK_TAIL]) == m.group("ck")


def page_rid(page):
    m = PAGE_RE.fullmatch(page or "")
    return m.group("rid") if m else None


def _child(text, start, end, key):
    """The index just after the "{" of ["key"] = { lying directly inside text[start:end]
    (not inside a deeper table), or -1. Strings and "--" comments are skipped."""
    depth, i = 0, start
    while i < end:
        c = text[i]
        if c == "[" and depth == 0:
            m = _KEY_AT.match(text, i, end)
            if m:
                if m.group(1) == key:
                    return m.end()
                i = m.end() - 1   # step onto its "{": depth goes up there
                continue
        if c == '"':
            i += 1
            while i < end and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return -1
        elif c == "-" and text.startswith("--", i):
            nl = text.find("\n", i, end)
            i = end if nl < 0 else nl
            continue
        i += 1
    return -1


def pages_in(text):
    """The valid pages of AllinOneGuideDB.modules.gather.pages in a saved file's text, in
    file order, without repeats. Nothing outside that one table is looked at."""
    top = _TOP_RE.search(text or "")
    if not top:
        return []
    start, end = top.end(), reports._block_end(text, top.end())
    for key in ("modules", "gather", "pages"):
        if end < 0:
            return []
        start = _child(text, start, end, key)
        if start < 0:
            return []
        end = reports._block_end(text, start)
    if end < 0:
        return []
    out, seen = [], set()
    block = text[start:end]
    for m in _STRING_RE.finditer(block):
        page = _UNESCAPE_RE.sub(r"\1", m.group(1))
        if valid_page(page) and page not in seen:
            seen.add(page)
            out.append(page)
    return out


def account_files(accounts_dir):
    """[(account folder name, saved file path)] (the name stays on this PC)."""
    out = []
    for path in reports.saved_files(accounts_dir, STEM):
        out.append((os.path.basename(os.path.dirname(os.path.dirname(path))), path))
    return out


def stamp(accounts_dir):
    """{path: (size, mtime ns)}: os.stat only."""
    out = {}
    for _name, path in account_files(accounts_dir):
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[path] = (st.st_size, st.st_mtime_ns)
    return out


# ---------------------------------------------------------------- settings
def is_on(settings):
    return settings.get(ON_SETTING) is not False


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def salt(settings):
    """The random salt for account keys (made and put into settings once)."""
    s = settings.get(SALT_SETTING)
    if not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{32}", s):
        s = secrets.token_hex(16)
        settings[SALT_SETTING] = s
    return s


def account_key(accounts_dir, name, salt_hex):
    """A stable key for one WoW account folder that doesn't show its name."""
    msg = (os.path.normcase(os.path.abspath(accounts_dir)) + "\0" + name.lower()).encode("utf-8")
    return hmac.new(bytes.fromhex(salt_hex), msg, hashlib.sha256).hexdigest()[:20]


def new_sid():
    return "".join(secrets.choice(SID_CHARS) for _ in range(SID_LEN))


def accounts(settings):
    """settings["gather_accounts"] cleaned: {key: {"sid"?, "sent": [...], "wait"?}}."""
    raw = settings.get(ACCOUNTS_SETTING)
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, e in raw.items():
        if not isinstance(key, str) or not isinstance(e, dict):
            continue
        clean = {"sent": [r for r in e.get("sent") or [] if isinstance(r, str)][-SENT_KEEP:]}
        if isinstance(e.get("sid"), str) and SID_RE.fullmatch(e["sid"]):
            clean["sid"] = e["sid"]
        if isinstance(e.get("wait"), str):
            clean["wait"] = e["wait"]
        out[key] = clean
    return out


def give_sids(settings, keys):
    """A sender code for each key that has none (before its first send). Returns {key: sid}."""
    accs = accounts(settings)
    for key in keys:
        e = accs.setdefault(key, {"sent": []})
        if "sid" not in e:
            e["sid"] = new_sid()
    settings[ACCOUNTS_SETTING] = accs
    return {k: accs[k]["sid"] for k in keys}


def forget_sids(settings):
    """Switched off: every sender code goes at once (sent ids are kept)."""
    accs = accounts(settings)
    for e in accs.values():
        e.pop("sid", None)
        e.pop("wait", None)
    if accs:
        settings[ACCOUNTS_SETTING] = accs


def record(settings, results, day=None):
    """Store a send's results: [(key, done ids, over ids)]. Over = wait until the next UTC day."""
    day = day or today()
    accs = accounts(settings)
    for key, done, over in results:
        e = accs.get(key)
        if e is None or "sid" not in e:
            continue   # switched off meanwhile: nothing kept for it
        e["sent"] = (e["sent"] + [r for r in done if r not in e["sent"]])[-SENT_KEEP:]
        if over:
            e["wait"] = day
        else:
            e.pop("wait", None)
    settings[ACCOUNTS_SETTING] = accs


# ---------------------------------------------------------------- reading
def read_all(accounts_dir, unfinished=None):
    """[(account name, [pages])] for every account with a saved file. A file WoW hasn't
    finished writing is skipped (its path goes into unfinished)."""
    out = []
    for name, path in account_files(accounts_dir):
        text = reports._read(path)
        if text and not reports.complete(text):
            if unfinished is not None:
                unfinished.append(path)
            continue
        out.append((name, pages_in(text)))
    return out


def batch(pages, sent=()):
    """The pages not sent yet, at most MAX_PAGES and MAX_BODY bytes (oldest first)."""
    done, out, size = set(sent), [], 64
    for p in pages:
        rid = page_rid(p)
        if not rid or rid in done:
            continue
        if len(out) >= MAX_PAGES or size + len(p) + 4 > MAX_BODY:
            break
        out.append(p)
        size += len(p) + 4
        done.add(rid)
    return out


def pending(accounts_dir, settings, day=None, unfinished=None):
    """[(key, [pages])] to send now: accounts not waiting for tomorrow, unsent pages only.
    unfinished gets the paths of files WoW is still writing and the keys of waiting
    accounts (either way the files are read again at the next look)."""
    day = day or today()
    salt_hex = settings.get(SALT_SETTING)
    if not isinstance(salt_hex, str):
        return []
    accs = accounts(settings)
    out = []
    for name, pages in read_all(accounts_dir, unfinished):
        key = account_key(accounts_dir, name, salt_hex)
        e = accs.get(key, {})
        if e.get("wait") == day:
            if unfinished is not None:
                unfinished.append(key)   # waits for tomorrow: read again then, changed or not
            continue
        b = batch(pages, e.get("sent", ()))
        if b:
            out.append((key, b))
    return out


_UNFINISHED = {}   # path -> (size, mtime ns) of a file that didn't balance at the last look


def find_worker(q, accounts_dir, settings, check_game, last=None, settle_s=reports.SETTLE_S,
                sleep=time.sleep, day=None):
    """Thread: always ("gather_found", [(key, pages)], stamp to keep, game running).
    WoW running = nothing is looked at or read. Read only when the files changed since last
    (None = read) and then stayed the same for settle_s."""
    found, keep, game = [], last, False
    try:
        game = bool(check_game())
        if not game:
            now = stamp(accounts_dir)
            if now and now != last:
                if settle_s:
                    sleep(settle_s)
                if stamp(accounts_dir) == now and not check_game():
                    unfinished = []
                    found = pending(accounts_dir, settings, day, unfinished)
                    # a file that doesn't balance and has the same size+time as at the last
                    # look is stuck, not being written: not read again until it changes
                    rest = [u for u in unfinished if not (u in now and _UNFINISHED.get(u) == now[u])]
                    for u in unfinished:
                        if u in now:
                            _UNFINISHED[u] = now[u]
                    if not rest:
                        keep = now
            elif not now:
                keep = now
    except Exception:   # noqa: BLE001 (never bother the player)
        log.exception("Reading game data failed")
    q.put(("gather_found", found, keep, game))


def preview(accounts_dir, check_game):
    """For the Settings preview: None while WoW runs, else [(label, [pages])] with neutral
    labels ("WoW account 1", ...) in folder order. Every valid page, sent or not."""
    if check_game():
        return None
    return [(f"WoW account {i}", pages)
            for i, (_name, pages) in enumerate(read_all(accounts_dir), 1)]


def preview_worker(q, accounts_dir, check_game):
    try:
        result = preview(accounts_dir, check_game)
    except Exception:   # noqa: BLE001
        log.exception("Game data preview failed")
        result = []
    q.put(("gather_preview", result))


# ---------------------------------------------------------------- sending
def _ids(data, name):
    v = data.get(name) if isinstance(data, dict) else None
    return [r for r in v if isinstance(r, str)] if isinstance(v, list) else []


def send_batch(url, sid, pages, allow_local=False, timeout=TIMEOUT):
    """POST one batch. -> ("ok", done ids, over ids), ("over", [], all ids) for a plain
    429, or ("later", [], []) for offline / any other answer. Never raises."""
    ids = [page_rid(p) for p in pages]
    if not reports.url_ok(url, allow_local) or not SID_RE.fullmatch(sid or "") or not pages:
        return ("later", [], [])
    try:
        body = json.dumps({"v": 1, "sid": sid, "pages": pages},
                          separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": download.USER_AGENT})
        opener = urllib.request.build_opener(reports._NoRedirects)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return ("later", [], [])
            data = json.loads(resp.read(64 * 1024).decode("utf-8"))
        if not isinstance(data, dict):
            return ("later", [], [])
        mine = set(ids)
        done = [r for r in _ids(data, "took") + _ids(data, "dup") + _ids(data, "bad") if r in mine]
        over = [r for r in _ids(data, "over") if r in mine and r not in done]
        return ("ok", done, over)
    except urllib.error.HTTPError as e:
        log.info("Game data not sent: HTTP %s", e.code)
        return ("over", [], ids) if e.code == 429 else ("later", [], [])
    except Exception as e:   # offline, timeout, bad answer: next look
        log.info("Game data not sent: %s", type(e).__name__)
    return ("later", [], [])


def send_worker(q, url, batches, allow_local, still_on):
    """Thread: [(key, sid, pages)] -> ("gather_sent", [(key, done, over)], pages taken, all
    done). still_on() is asked before each request (switched off = stop at once)."""
    results, n, all_done = [], 0, True
    for key, sid, pages in batches:
        if not still_on():
            all_done = False
            break
        how, done, over = send_batch(url, sid, pages, allow_local)
        if how == "later":
            all_done = False
            break
        results.append((key, done, over))
        n += len(done)
        if over:
            all_done = False
    q.put(("gather_sent", results, n, all_done))
