"""Send a backup to another PC with a short code ("37-amber-lamp-river").

Both PCs only make outgoing HTTPS calls to a small relay (share_worker\\worker.js, a
Cloudflare Worker + Durable Object). The relay never sees the file or the code's words:
- SPAKE2 turns the code into a shared key; the relay (or anyone) gets one guess per code,
  then the mailbox is gone (key confirmation with HMAC both ways).
- The file goes in 1 MiB chunks, each sealed with ChaCha20-Poly1305 (nonce and extra data
  hold the chunk number and a "last chunk" flag), then size + sha256 are checked.
- The receiver checks the result with backup.read(received=True) before keeping it; the
  normal Restore window (account pick, safety copy) does the rest.
- Send later (only when the player ticked it): if nobody joined after 5 minutes, or at once
  for a backup over MAX_SIZE (up to LATER_MAX_SIZE; the relay can't carry it), the backup is
  uploaded to file.kiwi (am\\share_later.py) and the same code fetches it later, once.
No tkinter here: the windows are in am\\share_ui.py. The two packages are imported only
when a transfer starts (app start stays fast)."""

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from am import backup, download, share_later
from am.share_words import WORDS

log = logging.getLogger(__name__)

PROTOCOL = "v1"
CHUNK = 1024 * 1024                  # plaintext bytes per chunk
TAG = 16                             # Poly1305 tag
MAX_SIZE = 500 * 1024 * 1024         # biggest file sent through the relay itself
LATER_MAX_SIZE = 5 * 1024 * 1024 * 1024   # biggest file sent later (file.kiwi; under its
                                          # 10 GB for a free 24 h download)
PING_EVERY = 15                      # seconds between "still here" calls while uploading
ZIP_GUESS_LOW, ZIP_GUESS_HIGH = 0.45, 0.9   # zip size / addon folder size (before zipping)
MAX_CHANNEL = 499
CODE_WORDS = 3
CODE_TTL = 600                       # seconds a code waits for the other PC
IDLE = 120                           # seconds without progress before giving up
POLL_WAIT = 10                       # seconds the relay holds a "nothing yet" poll
TIMEOUT = POLL_WAIT + 20             # socket timeout per call
MSG_MAX = 4096
RETRIES = 3                          # network blips during a transfer
SPARE_DISK = 20 * 1024 * 1024
PART_PREFIX = ".AddonManager receiving "   # an unfinished download (hidden .part)
PART_EXT = ".part"
PART_MAX_AGE = 2 * 3600                    # older than any transfer can run: left over
ID_A, ID_B = b"AddonManager-send", b"AddonManager-receive"
_PREFIX = {w[:3]: w for w in WORDS}


class ShareError(Exception):
    """kind: bad_format, offline, timeout, busy, not_found, used, wrong_code, expired,
    ended (the other PC stopped), cancelled, too_big, broken, not_backup, disk_full,
    file (couldn't read/write the file), server (an answer the app doesn't understand),
    later_failed (the upload
    to file.kiwi didn't work), later_file (the downloaded file isn't the right one)."""

    def __init__(self, kind, detail="", limit=None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.limit = limit   # too_big: the biggest size that can be sent (bytes); detail
        # "later_off": a bigger one works with the send-later tick


# ---------- the code ----------
def new_words():
    return [secrets.choice(WORDS) for _ in range(CODE_WORDS)]


def format_code(channel, words):
    return f"{channel}-" + "-".join(words)


def parse_code(text):
    """(channel, [words]) from what the player typed: any case, spaces, dashes or other
    marks between parts; each word by its first three letters (typos after that are fine).
    Raises ShareError("bad_format") before any network call."""
    parts = re.findall(r"[0-9]+|[a-z]+", str(text or "").lower())
    if len(parts) != CODE_WORDS + 1 or not parts[0].isdigit() or len(parts[0]) > 3:
        raise ShareError("bad_format")
    channel = int(parts[0])
    if not 1 <= channel <= MAX_CHANNEL:
        raise ShareError("bad_format")
    words = []
    for p in parts[1:]:
        if len(p) < 3 or p[:3] not in _PREFIX:
            raise ShareError("bad_format")
        words.append(_PREFIX[p[:3]])
    return channel, words


def _password(channel, words):
    return f"AM1:{channel}:{'-'.join(words)}".encode("ascii")


# ---------- crypto ----------
class _Keys:
    def __init__(self, shared, channel):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        out = HKDF(algorithm=hashes.SHA256(), length=96, salt=b"AM-share-v1",
                   info=f"keys {channel}".encode("ascii")).derive(shared)
        self.confirm_a, self.confirm_b, self.data = out[:32], out[32:64], out[64:]


def _mac(key, label, *parts):
    h = hmac.new(key, label, hashlib.sha256)
    for p in parts:
        h.update(len(p).to_bytes(4, "big"))
        h.update(p)
    return h.digest()


class Cipher:
    """ChaCha20-Poly1305 per chunk; index 0 is the header. The nonce is the chunk index,
    the extra data the channel, index and last-chunk flag: a chunk can't be moved, repeated,
    taken from another transfer (other key) or made the last one."""

    def __init__(self, key, channel):
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        self._aead = ChaCha20Poly1305(key)
        self._channel = channel

    def _parts(self, index, final):
        nonce = b"AMs1" + index.to_bytes(8, "big")
        aad = b"AM-share-v1|%d|%d|%d" % (self._channel, index, 1 if final else 0)
        return nonce, aad

    def seal(self, index, data, final=False):
        nonce, aad = self._parts(index, final)
        return self._aead.encrypt(nonce, data, aad)

    def open(self, index, box, final=False):
        from cryptography.exceptions import InvalidTag
        nonce, aad = self._parts(index, final)
        try:
            return self._aead.decrypt(nonce, box, aad)
        except (InvalidTag, ValueError, TypeError) as e:
            raise ShareError("broken", f"chunk {index}") from e


# ---------- the relay ----------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect becomes an HTTPError: never followed


_CLOSED = {"bad_code": "wrong_code", "cancel": "ended", "expired": "expired",
           "done": "ended", "idle": "ended"}


class Relay:
    """The relay's small HTTP API (urllib only). https only; http://127.0.0.1 or localhost
    only when allow_local (tests). Answers are size-capped; redirects never followed."""

    def __init__(self, url, allow_local=False, timeout=TIMEOUT, sleep=time.sleep):
        url = (url or "").strip().rstrip("/")
        p = urllib.parse.urlsplit(url)
        local = p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost")
        if not (p.scheme == "https" and p.hostname) and not (allow_local and local):
            raise ShareError("server", "relay address not allowed")
        if p.username or p.password or p.query or p.fragment:
            raise ShareError("server", "relay address not allowed")
        self.url = url
        self.local = bool(allow_local and local)   # a test relay: test addresses allowed
        self.timeout = timeout
        self.sleep = sleep
        self.token = None
        self.channel = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _once(self, method, path, body=None, max_bytes=MSG_MAX, timeout=None):
        headers = {"User-Agent": download.USER_AGENT}
        if self.token:
            headers["X-Token"] = self.token
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers=headers)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ShareError("server", "answer too big")
                return resp.status, data
        except ShareError:
            raise
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(1024)
                kind = json.loads(raw.decode("utf-8")).get("error", "")
            except Exception:
                kind = ""
            finally:
                e.close()
            return e.code, str(kind)
        except Exception as e:   # URLError, OSError, timeouts
            raise ShareError("timeout" if download._is_timeout(e) else "offline",
                             type(e).__name__) from e

    def _call(self, method, path, body=None, max_bytes=MSG_MAX, ok=(200, 204)):
        """(status, data). Network blips and 5xx (not 503) are tried RETRIES times."""
        delay = 1
        for attempt in range(RETRIES + 1):
            try:
                status, data = self._once(method, path, body, max_bytes)
            except ShareError as e:
                if e.kind not in ("offline", "timeout") or attempt == RETRIES:
                    raise
                self.sleep(delay)
                delay *= 2
                continue
            if status in ok:
                return status, data
            if status >= 500 and status != 503 and attempt < RETRIES:
                self.sleep(delay)
                delay *= 2
                continue
            raise self._error(status, data)
        raise ShareError("server")

    @staticmethod
    def _error(status, kind):
        if status == 404:
            return ShareError("not_found")
        if status == 409:
            return ShareError("used", kind)
        if status == 410:
            return ShareError(_CLOSED.get(kind, "ended"), kind)
        if status == 413:
            return ShareError("too_big")
        if status in (429, 503):
            return ShareError("busy", kind)
        return ShareError("server", f"HTTP {status}")

    def _json(self, data):
        try:
            out = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ShareError("server", "bad answer") from e
        if not isinstance(out, dict):
            raise ShareError("server", "bad answer")
        return out

    def _take_token(self, out):
        token = out.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ShareError("server", "bad token")
        self.token = token

    def open(self):
        """New mailbox for the sender: (channel, seconds the code stays valid)."""
        _s, data = self._call("POST", f"/{PROTOCOL}/open", b"", ok=(200,))
        out = self._json(data)
        ch, ttl = out.get("ch"), out.get("ttl")
        if not isinstance(ch, int) or not 1 <= ch <= MAX_CHANNEL:
            raise ShareError("server", "bad channel")
        self._take_token(out)
        self.channel = ch
        return ch, ttl if isinstance(ttl, int) and 0 < ttl <= CODE_TTL else CODE_TTL

    def join(self, channel):
        """The receiver takes the mailbox (only the first join ever counts; a repeat of
        this same join after a network blip gets the same answer)."""
        self.channel = channel
        # This app takes sends kept for later (file.kiwi).
        jid = secrets.token_hex(16) + "|caps=later"
        try:
            _s, data = self._call("POST", f"/{PROTOCOL}/join/{channel}", jid.encode("ascii"),
                                  ok=(200,))
        except ShareError as e:
            if e.kind in ("wrong_code", "ended", "expired"):   # a closed mailbox
                raise ShareError("not_found", e.detail) from e
            raise
        out = self._json(data)
        if out.get("later") is True:
            self._take_token(out)   # the watch token: GET /later needs it
            return "later"
        self._take_token(out)
        return None

    def put_msg(self, slot, data):
        self._call("PUT", f"/{PROTOCOL}/{self.channel}/msg/{slot}", data)

    def get_msg(self, slot, wait=POLL_WAIT):
        status, data = self._call("GET", f"/{PROTOCOL}/{self.channel}/msg/{slot}?wait={wait}")
        return data if status == 200 else None

    def put_chunk(self, index, box, wait=POLL_WAIT):
        """True when stored; False when the relay's buffer stayed full (try again)."""
        status, _d = self._call("PUT", f"/{PROTOCOL}/{self.channel}/chunk/{index}?wait={wait}",
                                box, ok=(200, 204, 429))
        if status == 429 and _d != "full":
            raise ShareError("busy", str(_d))
        return status != 429

    def get_chunk(self, index, wait=POLL_WAIT):
        status, data = self._call("GET", f"/{PROTOCOL}/{self.channel}/chunk/{index}?wait={wait}",
                                  max_bytes=CHUNK + TAG + 64)
        return data if status == 200 else None

    def ping(self, permille=None):
        """Still here (uploads for later, with their progress). A closed mailbox raises; a
        network blip doesn't (the upload's own retries decide)."""
        body = json.dumps({"p": permille}).encode("ascii") if permille is not None else b""
        try:
            self._call("POST", f"/{PROTOCOL}/{self.channel}/ping", body)
        except ShareError as e:
            if e.kind not in ("offline", "timeout"):
                raise

    # ---------- send later ----------
    def start_later(self, hours, salt_hex, verifier):
        """Turn the waiting code into a send-later one. False if the other PC joined just
        now (go on live)."""
        body = json.dumps({"hours": hours, "salt": salt_hex, "verifier": verifier})
        try:
            self._call("POST", f"/{PROTOCOL}/{self.channel}/later", body.encode("ascii"))
        except ShareError as e:
            if e.kind == "used" and e.detail == "joined":
                return False
            raise
        return True

    def put_record(self, sealed):
        """Store the sealed record; seconds the code works from now."""
        _s, data = self._call("PUT", f"/{PROTOCOL}/{self.channel}/record", sealed, ok=(200,))
        left = self._json(data).get("left")
        return left if isinstance(left, int) and left > 0 else 0

    def later_state(self, wait=POLL_WAIT):
        _s, data = self._call("GET", f"/{PROTOCOL}/{self.channel}/later?wait={wait}",
                              ok=(200,))
        out = self._json(data)
        if out.get("state") not in ("uploading", "ready") or not re.fullmatch(
                r"[0-9a-f]{32}", str(out.get("salt"))):
            raise ShareError("server", "bad later state")
        return out

    def take(self, verifier):
        """The sealed record, once. A wrong code deletes it (wrong_code)."""
        body = json.dumps({"verifier": verifier}).encode("ascii")
        status, data = self._once("POST", f"/{PROTOCOL}/{self.channel}/take", body)
        if status == 403:
            raise ShareError("wrong_code", "later")
        if status != 200:
            raise self._error(status, data)
        rec = self._json(data).get("record")
        try:
            return base64.b64decode(rec, validate=True)
        except (TypeError, ValueError) as e:
            raise ShareError("server", "bad record") from e

    def close(self, reason):
        """Tell the relay to delete the mailbox now. Never raises."""
        if not self.token or self.channel is None:
            return
        try:
            self._once("POST", f"/{PROTOCOL}/{self.channel}/close",
                       json.dumps({"reason": reason}).encode("ascii"), timeout=5)
        except Exception:
            pass


# ---------- size checks ----------
def size_check(size, later_on):
    """True when the file goes straight to send later (over MAX_SIZE, the tick on).
    Raises too_big (with the limit) when it can't be sent at all."""
    if size > LATER_MAX_SIZE:
        raise ShareError("too_big", limit=LATER_MAX_SIZE)
    if size > MAX_SIZE:
        if not later_on:
            raise ShareError("too_big", "later_off", limit=MAX_SIZE)
        return True
    return False


def precheck(unpacked, later_on, folder, disk_usage=shutil.disk_usage):
    """Before making a new backup to send: refuse early (no minutes of zipping) when the zip
    surely can't be sent, or the backup folder's drive can't hold it. unpacked = addon
    folder bytes (backup.addons_size)."""
    low, high = int(unpacked * ZIP_GUESS_LOW), int(unpacked * ZIP_GUESS_HIGH)
    if low > LATER_MAX_SIZE:
        raise ShareError("too_big", limit=LATER_MAX_SIZE)
    if low > MAX_SIZE and not later_on:
        raise ShareError("too_big", "later_off", limit=MAX_SIZE)
    try:
        probe = folder
        while probe and not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)
        free = disk_usage(probe).free
    except (OSError, ValueError):
        return
    if free < high + SPARE_DISK:
        raise ShareError("disk_full", limit=high + SPARE_DISK)


# ---------- waiting ----------
def _check(cancel, deadline, clock, late="expired"):
    if cancel is not None and cancel.is_set():
        raise ShareError("cancelled")
    if clock() > deadline:
        raise ShareError(late)


def _wait_msg(relay, slot, deadline, cancel, clock, late="expired"):
    while True:
        _check(cancel, deadline, clock, late)
        wait = max(1, min(POLL_WAIT, int(deadline - clock())))
        data = relay.get_msg(slot, wait)
        if data is not None:
            return data


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _close_for(relay, e):
    relay.close("bad_code" if isinstance(e, ShareError) and e.kind == "wrong_code"
                else "cancel")


# ---------- send ----------
def send(path, relay, on_code, progress=None, cancel=None, clock=time.monotonic, later=None,
         on_later=None):
    """Send a backup file. on_code(code, seconds) is called once the code is ready (show
    it); progress(done, total) while sending. Returns the size sent. Raises ShareError.
    later = {"hours", "api", "allow_local"} when the player ticked send later: if nobody
    joined after share_later.LATER_AFTER seconds, it goes to file.kiwi instead (a file
    over MAX_SIZE goes there at once, before the code is shown);
    on_later("uploading", None) / on_later("ready", seconds the code works); while
    file.kiwi is down on_later("kiwi_down", seconds it keeps trying), then "uploading" again."""
    from spake2 import SPAKE2_A
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ShareError("not_backup", type(e).__name__) from e
    big = size_check(size, bool(later))
    try:
        info = backup.read(path, received=True)   # the same check the other PC makes
    except backup.BackupError as e:
        raise ShareError("not_backup", e.kind) from e
    if info.kind != "backup":
        raise ShareError("not_backup", "safety copy")
    sha = _sha256_file(path)
    chunks = max(1, math.ceil(size / CHUNK))
    channel, ttl = relay.open()
    words = new_words()
    if big:   # too big for the relay: send later at once (nobody can join live first)
        try:
            _send_later(path, relay, channel, words, size, sha, later, progress, cancel, clock,
                        on_later, lambda: on_code(format_code(channel, words), ttl))
            return size
        except BaseException as e:
            _close_for(relay, e)
            if isinstance(e, OSError):
                raise _file_error(e) from e
            raise
    spake = SPAKE2_A(_password(channel, words), idA=ID_A, idB=ID_B)
    msg_a = spake.start()
    try:
        relay.put_msg("pakeA", msg_a)
        on_code(format_code(channel, words), ttl)
        code_end = clock() + ttl
        msg_b = None
        if later:
            try:
                msg_b = _wait_msg(relay, "pakeB", min(code_end, clock() + share_later.LATER_AFTER),
                                  cancel, clock)
            except ShareError as e:
                if e.kind != "expired" or clock() >= code_end:
                    raise
                if _send_later(path, relay, channel, words, size, sha, later, progress,
                               cancel, clock, on_later):
                    return size
        if msg_b is None:
            msg_b = _wait_msg(relay, "pakeB", code_end, cancel, clock)
        try:
            keys = _Keys(spake.finish(msg_b), channel)
        except ShareError:
            raise
        except Exception as e:
            raise ShareError("wrong_code", "pake") from e
        conf_b = _wait_msg(relay, "confB", clock() + IDLE, cancel, clock, "ended")
        if not hmac.compare_digest(conf_b, _mac(keys.confirm_b, b"confirm B", msg_a, msg_b)):
            raise ShareError("wrong_code")
        relay.put_msg("confA", _mac(keys.confirm_a, b"confirm A", msg_a, msg_b))
        cipher = Cipher(keys.data, channel)
        header = json.dumps({"v": 1, "size": size, "chunks": chunks, "sha256": sha})
        relay.put_msg("header", cipher.seal(0, header.encode("ascii")))
        if progress:
            progress(0, size)
        sent = 0
        with open(path, "rb") as f:
            for i in range(1, chunks + 1):
                data = f.read(CHUNK)
                if not data and size:
                    raise ShareError("not_backup", "file changed")
                box = cipher.seal(i, data, final=(i == chunks))
                deadline = clock() + IDLE
                while not relay.put_chunk(i, box):
                    _check(cancel, deadline, clock, "ended")
                sent += len(data)
                if progress:
                    progress(sent, size)
                _check(cancel, deadline, clock, "ended")
        if sent != size:
            raise ShareError("not_backup", "file changed")
        done = _wait_msg(relay, "done", clock() + IDLE, cancel, clock, "ended")
        if not hmac.compare_digest(done, _mac(keys.confirm_b, b"done", sha.encode("ascii"))):
            raise ShareError("broken", "done")
        relay.close("done")
        return size
    except BaseException as e:
        _close_for(relay, e)
        if isinstance(e, OSError):
            raise _file_error(e) from e
        raise


# ---------- send later ----------
def _later_error(e):
    if isinstance(e, ShareError):
        return e
    kind = getattr(e, "kind", "")
    if kind == "cancelled":
        return ShareError("cancelled")
    if kind == "file":
        return ShareError("not_backup", "file changed")
    if getattr(e, "detail", "") == "down":   # file.kiwi didn't answer for KIWI_DOWN_FOR
        return ShareError("later_failed", "down")
    return ShareError("later_failed", f"{type(e).__name__} {kind}")


def _send_later(path, relay, channel, words, size, sha, later, progress, cancel, clock,
                on_later, show_code=None):
    """Nobody came (or the file is too big for the relay): upload to file.kiwi and leave
    the sealed record for the code. False if the other PC joined at the last moment (the
    live send goes on). show_code() once the mailbox is a send-later one."""
    salt = os.urandom(16)
    rkey, verifier = share_later.record_keys(_password(channel, words), salt)
    if not relay.start_later(int(later["hours"]), salt.hex(), verifier):
        if show_code:   # nobody had the code yet: can't happen
            raise ShareError("server", "joined before the code was shown")
        return False
    if show_code:
        show_code()
    if on_later:
        on_later("uploading", "big" if show_code else None)
    file_key, kiwi_key, name = os.urandom(32), os.urandom(16), share_later.random_name()
    blob = share_later.Blob(path, file_key, size, sha)
    stop = threading.Event()
    # file.kiwi's waits (up to 5 min when it is down) end at once on cancel; tests pass
    # their own relay sleep.
    nap = stop.wait if relay.sleep is time.sleep else relay.sleep
    kiwi = share_later.FileKiwi(
        later.get("api") or share_later.API_URL, allow_local=bool(later.get("allow_local")),
        sleep=nap,
        on_wait=(lambda left: on_later("kiwi_down", int(left))) if on_later else None,
        on_ok=(lambda: on_later("uploading", None)) if on_later else None)
    result = {}
    seen = [0, blob.total]

    def moved(done, total):
        seen[0], seen[1] = done, total
        if progress:
            progress(min(size, done * size // max(1, total)), size)

    def run():
        try:
            result["link"] = kiwi.upload(blob, name, kiwi_key, stop.is_set, moved)
        except BaseException as e:   # noqa: BLE001 (handed to the main thread)
            result["error"] = e

    if progress:
        progress(0, size)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    last_ping = clock()
    try:
        while t.is_alive():
            if cancel is not None and cancel.is_set():
                raise ShareError("cancelled")
            if clock() - last_ping >= PING_EVERY:
                relay.ping(min(1000, seen[0] * 1000 // max(1, seen[1])))
                last_ping = clock()
            t.join(0.25)
    finally:
        stop.set()
        t.join(10)
    if "error" in result:
        raise _later_error(result["error"])
    record = {"v": 1, "link": result["link"], "key": file_key.hex(), "size": size,
              "sha256": sha, "blob": blob.total, "name": name}
    left = relay.put_record(share_later.seal_record(rkey, channel, record))
    if on_later:
        on_later("ready", left)
    return True


@dataclasses.dataclass
class LaterTicket:
    """What the code unlocked for a send-later: download `link` in the browser (it saves
    `name`), then open_later() with the file. Kept in memory only."""
    link: str
    key: bytes
    size: int
    sha256: str
    blob: int
    name: str


def _receive_later(channel, words, relay, cancel, clock, on_later):
    deadline = clock() + share_later.UPLOAD_WAIT
    wait = 0   # first a quick look: an upload that is done needs no waiting
    while True:
        _check(cancel, deadline, clock, "ended")
        st = relay.later_state(wait)
        wait = POLL_WAIT
        if st["state"] == "ready":
            break
        p = st.get("p")
        if on_later:
            on_later("uploading", p if isinstance(p, int) else 0)
    rkey, verifier = share_later.record_keys(_password(channel, words),
                                             bytes.fromhex(st["salt"]))
    sealed = relay.take(verifier)
    try:
        r = share_later.open_record(rkey, channel, sealed, relay.local)
    except ValueError as e:
        raise ShareError("broken", "record") from e
    if r["size"] > LATER_MAX_SIZE:
        raise ShareError("too_big", limit=LATER_MAX_SIZE)
    return LaterTicket(r["link"], bytes.fromhex(r["key"]), r["size"], r["sha256"], r["blob"],
                       r["name"])


def open_later(ticket, blob_path, dest_folder, progress=None, cancel=None):
    """Open the file downloaded from file.kiwi with the ticket's key: the backup goes to
    dest_folder ("... received.zip") after the same checks as a live send. Returns the
    path. The downloaded file itself is left alone."""
    try:
        if os.path.getsize(blob_path) != ticket.blob:
            raise ShareError("later_file", "size")
        _h, plain = share_later.open_blob(blob_path, ticket.key, ticket.size)
    except OSError as e:
        raise ShareError("later_file", type(e).__name__) from e
    except ValueError as e:
        raise ShareError("later_file", str(e)) from e
    os.makedirs(dest_folder, exist_ok=True)
    try:
        free = shutil.disk_usage(dest_folder).free
    except OSError:
        free = None
    if free is not None and free < ticket.size + SPARE_DISK:
        raise ShareError("disk_full", limit=ticket.size + SPARE_DISK)
    part = os.path.join(dest_folder, f"{PART_PREFIX}{secrets.token_hex(4)}{PART_EXT}")
    try:
        h, got = hashlib.sha256(), 0
        if progress:
            progress(0, ticket.size)
        with open(part, "xb") as f:
            try:
                for data in plain:
                    if cancel is not None and cancel.is_set():
                        raise ShareError("cancelled")
                    got += len(data)
                    h.update(data)
                    f.write(data)
                    if progress:
                        progress(got, ticket.size)
            except ValueError as e:   # cut off or not ours (a changed chunk = broken)
                raise ShareError("later_file", "damaged") from e
        if got != ticket.size or not hmac.compare_digest(h.hexdigest(), ticket.sha256):
            raise ShareError("broken", "checksum")
        try:
            info = backup.read(part, received=True)
        except backup.BackupError as e:
            raise ShareError("not_backup", e.kind) from e
        if info.kind != "backup":
            raise ShareError("not_backup", "safety copy")
        backup.mark_received(part)
        final = backup.received_path(dest_folder)
        os.rename(part, final)
        part = None
        return final
    except OSError as e:
        raise _file_error(e) from e
    finally:
        if part is not None:
            try:
                os.remove(part)
            except OSError:
                log.warning("Could not remove an unfinished file")


# ---------- receive ----------
def _header(cipher, box):
    try:
        h = json.loads(cipher.open(0, box).decode("ascii"))
    except ShareError:
        raise
    except Exception as e:
        raise ShareError("broken", "header") from e
    if not isinstance(h, dict):
        raise ShareError("broken", "header")
    size, chunks, sha = h.get("size"), h.get("chunks"), h.get("sha256")
    if (h.get("v") != 1 or not isinstance(size, int) or not isinstance(chunks, int)
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise ShareError("broken", "header")
    if size > MAX_SIZE:
        raise ShareError("too_big", limit=MAX_SIZE)
    if size < 1 or chunks != max(1, math.ceil(size / CHUNK)):
        raise ShareError("broken", "header")
    return size, chunks, sha


def receive(code, relay, dest_folder, progress=None, cancel=None, clock=time.monotonic,
            on_start=None, on_later=None):
    """Receive a backup into dest_folder ("AddonManager backup <date> received.zip").
    progress(done, total) while receiving; on_start() once the code matched. The file is
    written to a hidden .part first and kept only if every check passes, including
    backup.read(received=True). Returns the new file's path, or a LaterTicket when the
    other PC uploaded it for later (on_later("uploading", per mille) while it still
    uploads). Raises ShareError."""
    from spake2 import SPAKE2_B
    channel, words = parse_code(code)
    if relay.join(channel) == "later":
        return _receive_later(channel, words, relay, cancel, clock, on_later)
    spake = SPAKE2_B(_password(channel, words), idA=ID_A, idB=ID_B)
    msg_b = spake.start()
    part = None
    try:
        relay.put_msg("pakeB", msg_b)
        msg_a = _wait_msg(relay, "pakeA", clock() + IDLE, cancel, clock, "ended")
        try:
            keys = _Keys(spake.finish(msg_a), channel)
        except Exception as e:
            raise ShareError("wrong_code", "pake") from e
        relay.put_msg("confB", _mac(keys.confirm_b, b"confirm B", msg_a, msg_b))
        conf_a = _wait_msg(relay, "confA", clock() + IDLE, cancel, clock, "ended")
        if not hmac.compare_digest(conf_a, _mac(keys.confirm_a, b"confirm A", msg_a, msg_b)):
            raise ShareError("wrong_code")
        if on_start:
            on_start()
        cipher = Cipher(keys.data, channel)
        size, chunks, sha = _header(
            cipher, _wait_msg(relay, "header", clock() + IDLE, cancel, clock, "ended"))
        os.makedirs(dest_folder, exist_ok=True)
        try:
            free = shutil.disk_usage(dest_folder).free
        except OSError:
            free = None
        if free is not None and free < size + SPARE_DISK:
            raise ShareError("disk_full", limit=size + SPARE_DISK)
        part = os.path.join(dest_folder, f"{PART_PREFIX}{secrets.token_hex(4)}{PART_EXT}")
        h, got = hashlib.sha256(), 0
        if progress:
            progress(0, size)
        with open(part, "xb") as f:
            for i in range(1, chunks + 1):
                deadline = clock() + IDLE
                while True:
                    _check(cancel, deadline, clock, "ended")
                    box = relay.get_chunk(i)
                    if box is not None:
                        break
                data = cipher.open(i, box, final=(i == chunks))
                got += len(data)
                if got > size or (i < chunks and len(data) != CHUNK):
                    raise ShareError("broken", "size")
                h.update(data)
                f.write(data)
                if progress:
                    progress(got, size)
        if got != size or not hmac.compare_digest(h.hexdigest(), sha):
            raise ShareError("broken", "checksum")
        try:
            info = backup.read(part, received=True)
        except backup.BackupError as e:
            raise ShareError("not_backup", e.kind) from e
        if info.kind != "backup":
            raise ShareError("not_backup", "safety copy")
        backup.mark_received(part)   # still "received" if the file is renamed later
        final = backup.received_path(dest_folder)
        os.rename(part, final)
        part = None
    except BaseException as e:
        if part is not None:
            try:
                os.remove(part)   # our own unfinished download, never anything else
            except OSError:
                log.warning("Could not remove an unfinished download")
        _close_for(relay, e)
        if isinstance(e, OSError):
            raise _file_error(e) from e
        raise
    try:   # tell the sender it arrived (the file is already kept either way)
        relay.put_msg("done", _mac(keys.confirm_b, b"done", sha.encode("ascii")))
    except ShareError as e:
        log.info("Could not confirm to the sender (%s)", e.kind)
    return final


def clean_partials(folders, now=None, max_age=PART_MAX_AGE):
    """Remove leftover unfinished downloads (PART_PREFIX...PART_EXT, not changed for
    max_age seconds: the app was closed or the PC turned off while receiving) from these
    folders. Only our own file names, plain files, never links. Returns how many."""
    now = time.time() if now is None else now
    removed = 0
    for folder in dict.fromkeys(f for f in folders if f):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not (e.name.startswith(PART_PREFIX) and e.name.endswith(PART_EXT)):
                continue
            try:
                if (e.is_symlink() or not e.is_file(follow_symlinks=False)
                        or now - e.stat(follow_symlinks=False).st_mtime < max_age):
                    continue
                os.remove(e.path)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("Removed %d unfinished download(s)", removed)
    return removed


def _file_error(e):
    full = getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) in (39, 112)
    return ShareError("disk_full" if full else "file", type(e).__name__)
