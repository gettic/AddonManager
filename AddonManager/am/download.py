"""Downloads with urllib: timeouts, size caps, progress and sha256 checks."""

import hashlib
import socket
import urllib.error
import urllib.request

from am import APP_NAME, APP_VERSION

USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION}"
CHUNK = 64 * 1024


class DownloadError(Exception):
    """kind: offline, timeout, http, too_big, checksum."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _is_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def open_url(url, timeout):
    """urlopen with our User-Agent; maps errors to DownloadError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        raise DownloadError("http", f"{e.code} {url}") from e
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


def fetch_bytes(url, max_bytes=2 * 1024 * 1024, timeout=20):
    """Download a small file (the addon list) into memory."""
    with open_url(url, timeout) as resp:
        return b"".join(_read_chunks(resp, max_bytes, url))


def download_file(url, dest_path, max_bytes, expected_sha256=None, progress=None, timeout=30):
    """Download url to dest_path. progress(done, total) is called as it goes.
    Returns the sha256 hex digest; raises DownloadError on any problem."""
    digest = hashlib.sha256()
    with open_url(url, timeout) as resp:
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
