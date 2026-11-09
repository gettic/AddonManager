"""Anonymous daily usage count (on unless the player unticked "Share anonymous counts"
in the Settings window; the switch and its checks live in ui.py).

Once per day the app sends one small POST to the stats server (config.STATS_URL) after
its update check:

    {"v": 1, "first_this_week": true,
     "addons": [{"a": "GuildHub", "ver": "0.1.2", "s": "latest"}, ...]}

Only the guild's addons that are installed, plus the Addon Manager itself: folder name,
installed version, and latest / outdated / beta. "first_this_week" is true on the first
successful ping of each ISO week, so the server can keep a weekly total that counts each
person once without any ID. No name, account, character, ID, path, time or system
details. Nothing is sent while STATS_URL is empty. Failures are silent (log only) and
never affect the app; the day/week is only marked as sent after a 2xx reply.
"""

import datetime
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from am import download, versions

log = logging.getLogger(__name__)

DAY_SETTING = "stats_sent_day"     # settings key: the date ("2026-11-09") last sent
WEEK_SETTING = "stats_sent_week"   # settings key: the ISO week ("2026-W46") last sent
APP_ENTRY = "AddonManager"    # how the app itself is counted (also in the Worker's ADDONS)
TIMEOUT = 5
MAX_ENTRIES = 20
# Same rules as the Worker: entries that don't match are left out, never sent.
NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}", re.ASCII)
VERSION_RE = re.compile(r"\d+(\.\d+){0,3}(-[A-Za-z0-9.]{1,20})?", re.ASCII)
STATES = ("latest", "outdated", "beta")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def week(today=None):
    """The ISO week of a date (default: today) as "YYYY-Www", e.g. "2026-W46"."""
    year, wk, _ = (today or datetime.date.today()).isocalendar()
    return f"{year}-W{wk:02d}"


def day(today=None):
    """A date (default: today) as "YYYY-MM-DD"."""
    return (today or datetime.date.today()).isoformat()


def due(settings, today=None):
    """True if nothing was sent yet today."""
    return settings.get(DAY_SETTING) != day(today)


def first_this_week(settings, today=None):
    """True if nothing was sent yet this ISO week."""
    return settings.get(WEEK_SETTING) != week(today)


def state(installed, stable, beta_version=""):
    """latest / outdated / beta for an installed version against the newest normal
    release (stable) and the newest pre-release (beta_version). None if unclear."""
    if not installed:
        return None
    is_beta = versions.is_prerelease(installed) or (
        bool(beta_version) and versions.same(installed, beta_version))
    c = versions.compare(installed, stable) if stable else None
    if is_beta:
        return "outdated" if c == -1 else "beta"
    if not stable:
        return None
    if c is None:
        return None
    return "outdated" if c == -1 else "latest"


def entry(name, installed, stable, beta_version=""):
    """One payload entry, or None if it can't be sent cleanly."""
    s = state(installed, stable, beta_version)
    ver = versions.plain(installed or "")
    if s is None or not NAME_RE.fullmatch(name or "") or not VERSION_RE.fullmatch(ver):
        return None
    return {"a": name, "ver": ver, "s": s}


def build_payload(addons, app_version, app_newer, first_week):
    """addons: (folder, installed version, newest normal version, newest beta version) for
    each guild addon that is installed. app_newer: True if a newer Addon Manager exists.
    first_week: the first ping of this ISO week (counted in the weekly totals too)."""
    entries, seen = [], set()
    for folder, installed, stable, beta_v in addons:
        e = entry(folder, installed, stable, beta_v)
        if e and e["a"] not in seen and e["a"] != APP_ENTRY:
            entries.append(e)
            seen.add(e["a"])
    ver = versions.plain(app_version)
    if VERSION_RE.fullmatch(ver):
        entries.append({"a": APP_ENTRY, "ver": ver, "s": "outdated" if app_newer else "latest"})
    if len(entries) > MAX_ENTRIES:   # keep the app's own entry (it is last)
        entries = entries[-MAX_ENTRIES:]
    return {"v": 1, "first_this_week": bool(first_week), "addons": entries}


def url_ok(url, allow_local=False):
    """https only (http://127.0.0.1 etc. only in dev/tests); no user name in the URL."""
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
        return None   # a redirect is a failure: the ping goes to this one address only


def send(url, payload, allow_local=False, timeout=TIMEOUT):
    """POST the payload once. True on a 2xx reply; never raises."""
    if not url_ok(url, allow_local):
        return False
    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": download.USER_AGENT})
        opener = urllib.request.build_opener(_NoRedirects)
        with opener.open(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
        if not ok:
            log.info("Stats ping failed: HTTP %s", resp.status)
        return ok
    except urllib.error.HTTPError as e:
        log.info("Stats ping failed: HTTP %s", e.code)
    except Exception as e:   # offline, timeout, anything: silent
        log.info("Stats ping failed: %s", type(e).__name__)
    return False


def worker(q, url, payload, allow_local, today, this_week):
    """Thread: send once; on success tell the window ("stats_sent", day, week) so it
    saves them (only the window writes settings)."""
    if send(url, payload, allow_local):
        q.put(("stats_sent", today, this_week))
