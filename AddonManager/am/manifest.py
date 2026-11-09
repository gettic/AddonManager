"""The guild's addon list (manifest.json): fetch, check and resolve links."""

import json
import logging
import os
import pathlib
import re
import urllib.parse
from dataclasses import dataclass

from am import download

log = logging.getLogger(__name__)

SUPPORTED_FORMAT = 1
# These are used with fullmatch ("$" would also accept a trailing newline).
_FOLDER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_VERSION_RE = re.compile(r"[vV]?[0-9A-Za-z.\-+]+")
_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{0,38}/[A-Za-z0-9_.\-]{1,100}")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class ManifestError(Exception):
    """kind: not_set, unreachable, timeout, bad, too_new."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class Addon:
    folder: str
    name: str
    version: str
    url: str
    description: str = ""
    sha256: str = ""
    size: int = 0
    repo: str = ""      # "owner/name": version and url come from its latest GitHub release
    strict_hosts: bool = False   # download and every redirect must stay on https GitHub hosts
    beta: bool = False  # version/url come from a GitHub pre-release (Show beta versions)
    beta_version: str = ""  # newest GitHub pre-release's version, offered or not (spots it installed)
    stable_version: str = ""  # newest normal GitHub release's version (even when a beta is offered)


@dataclass
class Manifest:
    title: str
    addons: list
    app_version: str = ""
    app_url: str = ""


def is_safe_folder(name):
    """A plain AddOns folder name: no slashes, drive, '..' or leading dot."""
    return (isinstance(name, str) and 0 < len(name) <= 100
            and bool(_FOLDER_RE.fullmatch(name)) and not name.endswith("."))


def to_url(location):
    """A plain file path becomes a file:// URL; URLs stay as they are."""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", location or ""):
        return location
    return pathlib.Path(os.path.abspath(location)).as_uri()


def is_safe_repo(repo):
    """A plain GitHub "owner/name" (no '..', no extra slashes)."""
    return (isinstance(repo, str) and bool(_REPO_RE.fullmatch(repo))
            and repo.split("/")[1] not in (".", ".."))


def url_allowed(url, allow_local):
    """https always; file:// and http://localhost only in test/dev mode."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return True
    if allow_local:
        if parts.scheme == "file":
            return True
        if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
            return True
    return False


def parse(data, base_url, allow_local=False):
    """Build a Manifest from JSON bytes. Bad addon entries are skipped."""
    try:
        doc = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError) as e:
        raise ManifestError("bad", f"not JSON: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("addons"), list):
        raise ManifestError("bad", "no addons list")
    fmt = doc.get("format", 1)
    if not isinstance(fmt, int) or fmt < 1:
        raise ManifestError("bad", f"format {fmt!r}")
    if fmt > SUPPORTED_FORMAT:
        raise ManifestError("too_new", f"format {fmt}")

    addons, seen = [], set()
    for entry in doc["addons"]:
        addon = _parse_addon(entry, base_url, allow_local)
        if addon is None:
            continue
        if addon.folder.lower() in seen:
            log.warning("Duplicate addon %s skipped", addon.folder)
            continue
        seen.add(addon.folder.lower())
        addons.append(addon)

    app = doc.get("app") if isinstance(doc.get("app"), dict) else {}
    return Manifest(title=str(doc.get("title") or ""), addons=addons,
                    app_version=str(app.get("version") or ""), app_url=str(app.get("url") or ""))


def _parse_addon(entry, base_url, allow_local):
    if not isinstance(entry, dict):
        log.warning("Addon entry skipped: not an object")
        return None
    folder = entry.get("folder")
    version = entry.get("version")
    url = entry.get("url")
    if not is_safe_folder(folder):
        log.warning("Addon entry skipped: unsafe folder %r", folder)
        return None
    repo = entry.get("repo")
    if repo is not None:
        # Version and download are filled in later from GitHub (am.github.resolve).
        if not is_safe_repo(repo):
            log.warning("Addon %s skipped: bad repo %r", folder, repo)
            return None
        return Addon(folder=folder, name=str(entry.get("name") or folder), version="", url="",
                     description=str(entry.get("description") or ""), repo=repo)
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version.strip()):
        log.warning("Addon %s skipped: bad version %r", folder, version)
        return None
    if not isinstance(url, str) or not url.strip():
        log.warning("Addon %s skipped: no url", folder)
        return None
    full_url = urllib.parse.urljoin(base_url, url.strip())
    if not url_allowed(full_url, allow_local):
        log.warning("Addon %s skipped: url not allowed %s", folder, full_url)
        return None
    sha = entry.get("sha256") or ""
    if sha and (not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
        log.warning("Addon %s skipped: bad sha256", folder)
        return None
    size = entry.get("size") or 0
    if not isinstance(size, int) or size < 0:
        size = 0
    return Addon(folder=folder, name=str(entry.get("name") or folder), version=version.strip(),
                 url=full_url, description=str(entry.get("description") or ""),
                 sha256=sha.lower(), size=size)


def load(location, allow_local=False, timeout=20):
    """Fetch and parse the addon list from a URL (or a path in test/dev mode)."""
    if not location:
        raise ManifestError("not_set")
    url = to_url(location) if allow_local else location
    if not url_allowed(url, allow_local):
        raise ManifestError("bad", f"address not allowed: {url}")
    try:
        data = download.fetch_bytes(url, timeout=timeout)
    except download.DownloadError as e:
        kind = {"timeout": "timeout", "offline": "unreachable"}.get(e.kind, "bad")
        raise ManifestError(kind, e.detail) from e
    return parse(data, url, allow_local)
