"""Latest versions and downloads from each addon's public GitHub repo (Releases API).

No login and no token: GitHub allows 60 API calls an hour per PC, so results are
cached for a few minutes in cache.json. Only a repo's own release download links
(https://github.com/<repo>/releases/download/...) are accepted. Nothing about the
player or the game is sent: the request is the repo name and a neutral User-Agent.
"""

import dataclasses
import json
import logging
import os
import re
import time
import urllib.parse

from am import config, download, versions
from am import manifest as manifest_mod

log = logging.getLogger(__name__)

API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
CACHE_SECONDS = 10 * 60
_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})", re.IGNORECASE)   # used with fullmatch


class ReleaseError(Exception):
    """kind: no_release, no_asset, rate_limited, offline, timeout, bad.
    wait: seconds until GitHub's limit resets (rate_limited)."""

    def __init__(self, kind, detail="", wait=0):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.wait = wait


def api_base(override=None, allow_local=False):
    """The real GitHub API; a local fake (path or localhost URL) only in test/dev mode."""
    if override and allow_local:
        url = manifest_mod.to_url(override)
        if manifest_mod.url_allowed(url, True):
            return url.rstrip("/")
    return config.GITHUB_API


def _trim(release):
    """Keep only what we need from a release (also what goes into the cache)."""
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise ReleaseError("bad", "no tag_name")
    raw_assets = release.get("assets")
    assets = []
    for a in raw_assets if isinstance(raw_assets, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str):
            url, digest, size, state = (a.get("browser_download_url"), a.get("digest"),
                                        a.get("size"), a.get("state"))
            assets.append({"name": a["name"], "url": url if isinstance(url, str) else "",
                           "digest": digest if isinstance(digest, str) else "",
                           "size": size if isinstance(size, int) and size > 0 else 0,
                           "state": state if isinstance(state, str) else "uploaded"})
    return {"tag": release["tag_name"], "prerelease": release.get("prerelease") is True,
            "assets": assets}


def valid_release(release):
    """True if a (cached) release has exactly the shape _trim makes
    ("prerelease" may be missing in older cache entries: then it is a normal release)."""
    if not isinstance(release, dict) or not isinstance(release.get("tag"), str):
        return False
    if not isinstance(release.get("prerelease", False), bool):
        return False
    assets = release.get("assets")
    if not isinstance(assets, list):
        return False
    types = {"name": str, "url": str, "digest": str, "size": int, "state": str}
    return all(isinstance(a, dict) and all(isinstance(a.get(k), t) for k, t in types.items())
               for a in assets)


def _cached(cache, key):
    """A cache entry {"time": number, "release": valid} or None."""
    entry = cache.get(key)
    if (isinstance(entry, dict) and isinstance(entry.get("time"), (int, float))
            and not isinstance(entry.get("time"), bool) and valid_release(entry.get("release"))):
        return entry
    return None


def cache_key(base, repo):
    """Cache key of a repo's newest normal release + newest pre-release."""
    return f"{base}|{repo}|releases"


def _cached_releases(cache, key):
    """A cache entry {"time", "release": stable or None, "beta": pre-release or None},
    or None if missing or damaged."""
    entry = cache.get(key)
    if not (isinstance(entry, dict) and isinstance(entry.get("time"), (int, float))
            and not isinstance(entry.get("time"), bool)):
        return None
    stable, beta = entry.get("release"), entry.get("beta")
    if (stable is not None and not valid_release(stable)) or \
            (beta is not None and not valid_release(beta)):
        return None
    return {"time": entry["time"], "release": stable, "beta": beta}


def _get_json(url, repo, timeout):
    """GET a GitHub API URL -> parsed JSON, errors as ReleaseError."""
    try:
        data = download.fetch_bytes(url, timeout=timeout, headers=API_HEADERS)
    except download.DownloadError as e:
        if e.kind == "rate_limited":
            raise ReleaseError("rate_limited", e.detail, wait=e.wait) from e
        if e.kind == "http" and e.code == 404:
            raise ReleaseError("no_release", repo) from e
        if e.kind in ("offline", "timeout"):
            # A missing local fake file (file://) also lands here: treat as "no release".
            if url.startswith("file:") and e.kind == "offline":
                raise ReleaseError("no_release", repo) from e
            raise ReleaseError(e.kind, e.detail) from e
        raise ReleaseError("bad", e.detail) from e
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ReleaseError("bad", f"not JSON: {e}") from e


def newest_releases(repo, base, timeout=20):
    """GET <base>/repos/<repo>/releases (newest first, up to 100 in one call) ->
    (newest normal release or None, newest pre-release published after it or None).
    A beta is what GitHub flags as a pre-release (whatever its tag). Drafts are skipped."""
    if not manifest_mod.is_safe_repo(repo):
        raise ReleaseError("bad", f"repo {repo!r}")
    url = f"{base}/repos/{repo}/releases"
    if not url.startswith("file:"):   # the file-based fake API (dev only) has no query
        url += "?per_page=100"
    data = _get_json(url, repo, timeout)
    if not isinstance(data, list):
        raise ReleaseError("bad", "release list is not a list")
    stable = beta = None
    for raw in data:
        if not isinstance(raw, dict) or raw.get("draft"):
            continue
        try:
            release = _trim(raw)
        except ReleaseError:
            log.warning("Unreadable release in %s skipped", repo)
            continue
        if release["prerelease"]:
            beta = beta or release
        else:
            stable = release
            break   # pre-releases older than the newest normal release don't matter
    return stable, beta


def choose(stable, beta, show_betas):
    """(release, is_beta) to offer. The pre-release (published after the newest normal
    release) only with show_betas, and not if its version is the same or lower; a tag
    that can't be compared (e.g. v1.1.0beta) counts as newer, as it was published later."""
    if show_betas and beta is not None and (
            stable is None
            or versions.compare(plain_tag(beta["tag"]), plain_tag(stable["tag"])) not in (0, -1)):
        return beta, True
    if stable is None:
        raise ReleaseError("no_release", "only pre-releases" if beta else "no releases")
    return stable, False


def latest_release(repo, base, timeout=20):
    """GET <base>/repos/<repo>/releases/latest (never drafts or pre-releases).
    Used by the app's own update, which stays on normal releases."""
    if not manifest_mod.is_safe_repo(repo):
        raise ReleaseError("bad", f"repo {repo!r}")
    return _trim(_get_json(f"{base}/repos/{repo}/releases/latest", repo, timeout))


def parse_digest(value):
    """GitHub's asset digest "sha256:<hex>" -> "<hex>" (lower case), else "".
    A digest in another form is logged: the install then relies on the zip/.toc checks."""
    m = _DIGEST_RE.fullmatch(value) if isinstance(value, str) else None
    if m:
        return m.group(1).lower()
    if value:
        log.warning("Release digest not understood (no hash check): %r", str(value)[:80])
    return ""


def plain_tag(tag):
    """Tag "v1.3.0" or "1.3.0" -> "1.3.0"."""
    tag = (tag or "").strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag


def pick_asset(release, folder):
    """The zip to install: <folder>-<version>.zip or <folder>-v<version>.zip,
    else the only .zip in the release. None if there's no usable one."""
    version = plain_tag(release["tag"])
    wanted = {f"{folder}-{version}.zip".lower(), f"{folder}-v{version}.zip".lower()}
    assets = [a for a in release["assets"] if a.get("state", "uploaded") == "uploaded"]
    for a in assets:
        if a["name"].lower() in wanted:
            return a
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    return zips[0] if len(zips) == 1 else None


def download_allowed(url, repo, allow_local):
    """Only this repo's own release downloads: https://github.com/<repo>/releases/download/...
    with no port, no user name, no query and no '..' or %-escapes in the path."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    if (parts.scheme == "https" and parts.netloc.lower() == "github.com" and port is None
            and not parts.query and not parts.fragment
            and ".." not in parts.path and "%" not in parts.path and "\\" not in parts.path
            and parts.path.lower().startswith(f"/{repo.lower()}/releases/download/")):
        return True
    if allow_local and parts.scheme in ("file", "http"):
        return manifest_mod.url_allowed(url, True)
    return False


def apply_release(addon, release, allow_local, beta=False, beta_version=""):
    """A copy of addon with version, url, sha256 and size from the release
    (beta: it is a pre-release; beta_version: the newest pre-release's version, offered
    or not). Outside dev mode the copy has strict_hosts set: every redirect of its
    download must stay on https GitHub hosts (see download.github_host_ok)."""
    version = plain_tag(release["tag"])
    if not version or not manifest_mod._VERSION_RE.fullmatch(version):
        raise ReleaseError("bad", f"tag {release['tag']!r}")
    asset = pick_asset(release, addon.folder)
    if asset is None:
        raise ReleaseError("no_asset", f"{addon.repo} {release['tag']}")
    if not download_allowed(asset["url"], addon.repo, allow_local):
        raise ReleaseError("bad", f"download not allowed: {asset['url']}")
    size = asset["size"] if isinstance(asset["size"], int) and asset["size"] > 0 else 0
    return dataclasses.replace(addon, version=version, url=asset["url"],
                               sha256=parse_digest(asset["digest"]), size=size,
                               strict_hosts=not allow_local, beta=bool(beta),
                               beta_version=beta_version or "")

def load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        log.warning("Could not save the GitHub cache %s", path)


def resolve_all(addons, base=None, allow_local=False, cache_file=None, use_fresh_cache=True,
                now=None, show_betas=False):
    """Fill in repo-based addons from GitHub. Returns (addons, problems) where
    problems maps folder -> ReleaseError. Addons without a repo pass through.
    show_betas: offer a pre-release when it is newer than the newest normal release.
    The cache (if cache_file) is used when fresh (and use_fresh_cache), and whenever
    GitHub is rate-limiting this PC. Damaged cache entries are dropped and fetched again;
    a problem with one addon never stops the others."""
    base = base or config.GITHUB_API
    now = time.time() if now is None else now
    cache = load_cache(cache_file) if cache_file else {}
    changed = False
    result, problems = [], {}
    for addon in addons:
        if not addon.repo:
            result.append(addon)
            continue
        # An entry from v0.2.0 (latest normal release only): kept as a stale fallback until
        # the first successful fetch replaces it.
        old_key = f"{base}|{addon.repo}"
        old = _cached(cache, old_key)
        if old is None and old_key in cache:
            cache.pop(old_key, None)
            changed = True
        key = cache_key(base, addon.repo)
        cached = _cached_releases(cache, key)
        if cached is None and key in cache:
            log.warning("Damaged cache entry for %s dropped", addon.repo)
            cache.pop(key, None)
            changed = True
        fallback = cached   # used when GitHub is rate-limiting this PC
        if fallback is None and old is not None:
            fallback = {"time": old["time"],
                        "release": dict(old["release"], prerelease=False), "beta": None}
        try:
            pair = None
            if cached and use_fresh_cache and 0 <= now - cached["time"] < CACHE_SECONDS:
                pair = cached["release"], cached["beta"]
            if pair is None:
                try:
                    pair = newest_releases(addon.repo, base)
                    cache[key] = {"time": now, "release": pair[0], "beta": pair[1]}
                    if cache.pop(old_key, None) is not None:
                        log.info("Old cache entry for %s replaced", addon.repo)
                    changed = True
                except ReleaseError as e:
                    if e.kind == "rate_limited" and fallback:
                        log.info("GitHub limit reached; using cached %s", addon.repo)
                        pair = fallback["release"], fallback["beta"]
                    else:
                        raise
            release, is_beta = choose(pair[0], pair[1], show_betas)
            # The beta's version, if it's one the box would offer (so an installed normal
            # version is never mistaken for it).
            beta_version = ""
            if pair[1] is not None and choose(pair[0], pair[1], True)[1]:
                beta_version = plain_tag(pair[1]["tag"])
            result.append(apply_release(addon, release, allow_local, is_beta, beta_version))
        except ReleaseError as e:
            log.warning("Release of %s: %s", addon.repo, e)
            problems[addon.folder] = e
            result.append(addon)
        except Exception as e:  # last resort: never let one addon blank the whole list
            log.exception("Release of %s unreadable", addon.repo)
            if cache.pop(key, None) is not None:
                changed = True
            problems[addon.folder] = ReleaseError("bad", repr(e))
            result.append(addon)
    if cache_file and changed:
        save_cache(cache_file, cache)
    return result, problems


def load_list(location, allow_local=False):
    """The guild's addon list. If the real list can't be read, or has no usable
    addon in it, use the built-in copy."""
    builtin = bool(location) and location == config.DEFAULT_MANIFEST_URL
    try:
        m = manifest_mod.load(location, allow_local=allow_local)
    except manifest_mod.ManifestError as e:
        if builtin and e.kind != "too_new":
            log.warning("Addon list failed (%s); using the built-in list", e)
            return manifest_mod.parse(json.dumps(config.BUILTIN_LIST), location)
        raise
    if builtin and not m.addons:
        log.warning("Addon list has no usable addons; using the built-in list")
        return manifest_mod.parse(json.dumps(config.BUILTIN_LIST), location)
    return m