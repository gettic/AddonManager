"""Downloads with urllib: timeouts, size caps, progress and sha256 checks."""

import hashlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from am import APP_VERSION

# Neutral on purpose: no game, Blizzard or player details ever leave the PC.
USER_AGENT = f"AddonManager/{APP_VERSION}"
CHUNK = 64 * 1024


class DownloadError(Exception):
    """kind: offline, timeout, http, rate_limited, redirect, too_big, checksum.
    code: HTTP status (http/rate_limited); wait: seconds until a rate limit resets."""

    def __init__(self, kind, detail="", code=0, wait=0):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.code = code
        self.wait = wait


def _is_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def _rate_limit_wait(code, headers):
    """Seconds to wait if this HTTP error is a rate limit (GitHub: 403/429), else None."""
    if code not in (403, 429):
        return None
    headers = headers if headers is not None else {}
    retry_after = headers.get("retry-after")
    if code == 403 and headers.get("x-ratelimit-remaining") != "0" and not retry_after:
        return None
    try:
        if retry_after:
            return max(0, int(retry_after))
        reset = headers.get("x-ratelimit-reset")
        return max(0, int(reset) - int(time.time())) if reset else 60
    except ValueError:
        return 60


GITHUB_HOSTS = ("github.com",)
GITHUB_HOST_SUFFIX = ".githubusercontent.com"   # objects./release-assets.githubusercontent.com


def github_host_ok(url):
    """A release download hop: https, no port or user name, on github.com or GitHub's
    file hosts (*.githubusercontent.com)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    return (parts.scheme == "https" and bool(host) and host == (parts.hostname or "")
            and (host in GITHUB_HOSTS or host.endswith(GITHUB_HOST_SUFFIX)))


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but never from https down to http, and (with url_check)
    only to addresses url_check allows."""

    def __init__(self, url_check=None):
        super().__init__()
        self.url_check = url_check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url).scheme
        new = urllib.parse.urlsplit(newurl).scheme
        if (old == "https" and new != "https") or (self.url_check and not self.url_check(newurl)):
            raise DownloadError("redirect", f"refused redirect {req.full_url} -> {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, timeout, headers=None, url_check=None):
    """urlopen with our User-Agent and checked redirects; maps errors to DownloadError.
    url_check(url) -> bool, if given, must allow the first address and every redirect."""
    if url_check and not url_check(url):
        raise DownloadError("redirect", f"address not allowed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    opener = urllib.request.build_opener(_CheckedRedirects(url_check))
    try:
        return opener.open(req, timeout=timeout)
    except DownloadError:
        raise
    except urllib.error.HTTPError as e:
        wait = _rate_limit_wait(e.code, e.headers)
        e.close()
        if wait is not None:
            raise DownloadError("rate_limited", f"{e.code} {url}", code=e.code, wait=wait) from e
        raise DownloadError("http", f"{e.code} {url}", code=e.code) from e
    except Exception as e:  # URLError, OSError, ValueError
        if _is_timeout(e):
            raise DownloadError("timeout", url) from e
        raise DownloadError("offline", f"{e} ({url})") from e


def _read_chunks(resp, max_bytes, timeout_url):
    total = 0
    while True:
        try:
            chunk = resp.read(CHUNK)
        except Exception as e:
            if _is_timeout(e):
                raise DownloadError("timeout", timeout_url) from e
            raise DownloadError("offline", str(e)) from e
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise DownloadError("too_big", f"over {max_bytes} bytes")
        yield chunk


def fetch_bytes(url, max_bytes=2 * 1024 * 1024, timeout=20, headers=None):
    """Download a small file (the addon list, release info) into memory."""
    with open_url(url, timeout, headers) as resp:
        return b"".join(_read_chunks(resp, max_bytes, url))


def download_file(url, dest_path, max_bytes, expected_sha256=None, progress=None, timeout=30,
                  url_check=None):
    """Download url to dest_path. progress(done, total) is called as it goes.
    Returns the sha256 hex digest; raises DownloadError on any problem."""
    digest = hashlib.sha256()
    with open_url(url, timeout, url_check=url_check) as resp:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError, AttributeError):
            total = 0
        if total > max_bytes:
            raise DownloadError("too_big", f"{total} bytes")
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in _read_chunks(resp, max_bytes, url):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    got = digest.hexdigest()
    if expected_sha256 and got.lower() != expected_sha256.strip().lower():
        raise DownloadError("checksum", f"expected {expected_sha256}, got {got}")
    return got
