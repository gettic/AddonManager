"""Send later through public Send servers (timvisee/send): adminForge's send.adminforge.de,
and Chapril's drop.chapril.org as a spare (switched off until its operator says yes).

Tried after the guild's own server and before file.kiwi, only when the player ticked send
later. The sealed .amsend blob (share_later.Blob, our ChaCha20 layer with key K) goes up as
one normal Send file: Send's own RFC 8188 layer with a random 16-byte secret, a key-derived
upload key and encrypted metadata with a random neutral name, exactly as Send's browser
client makes them (app/keychain.js, app/ece.js). The receiving app downloads it by itself
(no browser), takes off Send's layer while it downloads, then opens the blob like any other
send-later file, and deletes the Send file with its owner token (also after a failure or
Cancel). The secret and the owner token are only ever inside the sealed code record.

Being gentle with volunteer servers (tos): one upload at a time, one retry per part after a
pause (Retry-After honoured up to 5 min), then the next host; a download limit of 2; delete
as soon as it isn't needed; the app's usual neutral User-Agent. The app only talks to the
fixed addresses in SITES (never one from a record or a server answer).

Upload uses a WebSocket (wss://<host>/api/ws, RFC 6455 client side, standard library only);
the rest is plain HTTPS. No tkinter here."""

import base64
import dataclasses
import hashlib
import hmac
import http.client
import json
import logging
import math
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse

from am import download, share_later

log = logging.getLogger(__name__)


@dataclasses.dataclass
class Site:
    key: str
    url: str           # "" = never used (tests blank these)
    label: str
    max_parts: int     # parts one send may be split into on this server


SITES = {
    "adminforge": Site("adminforge", "https://send.adminforge.de", "adminForge", 1),
    "chapril": Site("chapril", "https://drop.chapril.org", "Chapril", 1),
}
ORDER = ("adminforge", "chapril")
CHAPRIL_ON = False          # off until Chapril's operator says app uploads are fine
CHAPRIL_PER_WEEK = 2        # at most this many sends per PC in 7 days go to Chapril
ADMINFORGE_TERMS_URL = "https://adminforge.de/nutzungsbestimmungen/"
ADMINFORGE_PRIVACY_URL = "https://adminforge.de/datenschutz/"
CHAPRIL_TERMS_URL = "https://www.chapril.org/cgu.html"   # terms and privacy in one page

DLIMIT = 2                  # downloads allowed: one more if a download breaks near its end
UPLOAD_WINDOW = 6 * 3600    # the relay's LATER_UPLOAD_SECONDS: the code starts after this
TIME_MARGIN = 2 * 3600      # time left for the download after the code ends
TIMEOUT = 60
END_WAIT = 300              # after the last byte: wait this long for the server's "ok"
STOP_GRACE = 30             # Cancel after the last byte: wait this long for "ok", then delete
CONFIG_MAX = 64 * 1024
ANSWER_MAX = 64 * 1024
PART_MARGIN = 1024 * 1024   # stay this far under the server's file size limit
UPLOAD_TRIES = 2            # per part, then the next host
BACKOFF = (30, 120)         # seconds before a retry
RETRY_AFTER_MAX = 300
MAX_PARTS = 8               # record size stays well under the relay's 4096 bytes
MAX_PART_SIZE = 8 * 1024 ** 3
READ_BLOCK = 256 * 1024
SECRET_LEN = 16
DEFAULT_NONCE = "yRCdyQ1EMSA3mo4rqSkuNQ=="   # Send's keychain default (first try gets a 401)
ID_RE = "[0-9a-f]{10,16}"
OWNER_RE = "[0-9a-f]{20,64}"
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HEADER_MAX = 16 * 1024
CONTROL_MAX = 125           # RFC 6455: ping/pong/close payloads, never fragmented
WAIT_STEP = 1.0             # seconds per look while waiting for an answer (Cancel checks)
DETAIL_MAX = 120
LOCAL_HOSTS = ("127.0.0.1", "localhost")

_ONE_AT_A_TIME = threading.Lock()
_CLEANUPS = []


def cleanup_later(work):
    """Run a delete (at most STOP_GRACE + a few requests) in a thread that is NOT a daemon:
    Python waits for it before the app's process ends, so quitting right after Cancel still
    deletes the Send file. Never raises."""
    def run():
        try:
            work()
        except Exception:   # noqa: BLE001 (a delete never raises; belt and braces)
            log.info("A Send clean-up didn't finish")
    t = threading.Thread(target=run, name="send-cleanup", daemon=False)
    _CLEANUPS[:] = [c for c in _CLEANUPS if c.is_alive()] + [t]
    t.start()
    return t


def wait_cleanups(timeout=None):
    """Wait for the clean-ups started so far (tests). True when all are done."""
    end = None if timeout is None else time.monotonic() + timeout
    for t in list(_CLEANUPS):
        t.join(None if end is None else max(0, end - time.monotonic()))
    return not any(t.is_alive() for t in _CLEANUPS)


class SendError(Exception):
    """kind: refused (this server won't take it: limits, 4xx; next host at once), down (no
    answer after the tries), gone (the file isn't there any more), broken (damaged, a wrong
    answer), cancelled, certificate. "retry" is internal. status/wait: an HTTP answer and
    its Retry-After."""

    def __init__(self, kind, detail="", status=0, wait=None):
        # Short and on one line: details can come from a server and end up in the log.
        detail = " ".join(str(detail).split())[:DETAIL_MAX]
        super().__init__(f"{kind} {detail}".strip())
        self.kind = kind
        self.detail = detail
        self.status = status
        self.wait = wait


def sites_to_try(chapril_ok=False):
    """The Send servers to try, in order (keys). Chapril only when switched on and the
    player's PC is under its weekly cap."""
    out = []
    for key in ORDER:
        if not SITES[key].url:
            continue
        if key == "chapril" and not (CHAPRIL_ON and chapril_ok):
            continue
        out.append(key)
    return out


def chapril_ok(dates, today):
    """True when fewer than CHAPRIL_PER_WEEK of the saved dates (YYYY-MM-DD) are in the last
    7 days."""
    import datetime
    try:
        now = datetime.date.fromisoformat(today)
    except (TypeError, ValueError):
        return False
    recent = 0
    for d in dates or ():
        try:
            if 0 <= (now - datetime.date.fromisoformat(d)).days < 7:
                recent += 1
        except (TypeError, ValueError):
            continue
    return recent < CHAPRIL_PER_WEEK


# ---------- Send's keychain (app/keychain.js) ----------
def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64any(text):
    """Standard or URL-safe base64, with or without padding."""
    text = text.replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _hkdf(secret, info, n):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=None, info=info).derive(secret)


def auth_key(secret):
    """The upload's HMAC key (WebCrypto's default HMAC-SHA256 length: 64 bytes)."""
    return _hkdf(secret, b"authentication", 64)


def meta_key(secret):
    return _hkdf(secret, b"metadata", 16)


def auth_header(key, nonce_b64):
    sig = hmac.new(key, b64any(nonce_b64), hashlib.sha256).digest()
    return "send-v1 " + b64url(sig)


def seal_metadata(secret, name, size):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kind = "application/octet-stream"
    meta = {"name": name, "size": size, "type": kind,
            "manifest": {"files": [{"name": name, "size": size, "type": kind}]}}
    data = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return b64url(AESGCM(meta_key(secret)).encrypt(bytes(12), data, None))


def open_metadata(secret, sealed_b64):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return json.loads(AESGCM(meta_key(secret)).decrypt(bytes(12), b64any(sealed_b64), None))


# ---------- Send's RFC 8188 layer as a stream (app/ece.js = share_later.ece_*) ----------
RS = share_later.ECE_RS
DATA = share_later.ECE_DATA
HEAD = share_later.ECE_HEADER


def ece_frames(read, size, secret, salt=None, block_records=16):
    """The Send file for `size` plain bytes from read(start, end), one item per WebSocket
    message: the 21-byte header, then each sealed record (never 1 byte long, so never taken
    for the end byte). Same bytes as share_later.ece_encrypt."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt if salt is not None else os.urandom(16)
    cek, base = share_later._ece_keys(secret, salt)
    aead = AESGCM(cek)
    yield salt + struct.pack(">I", RS) + b"\x00"
    count = math.ceil(size / DATA)
    block, block_start = b"", 0
    for seq in range(count):
        start, end = seq * DATA, min((seq + 1) * DATA, size)
        if end > block_start + len(block):
            block_start = start
            block = read(start, min(size, start + DATA * block_records))
            if len(block) < end - start:
                raise share_later.LaterError("file", "short read")
        record = block[start - block_start:end - block_start]
        padded = record + (b"\x02" if seq == count - 1 else b"\x01")
        yield aead.encrypt(share_later._nonce(base, seq), padded, None)


class EceReader:
    """Takes Send's layer off while downloading: feed(bytes) -> plain bytes. Knows the plain
    size, so a cut-off, longer or changed file raises ValueError."""

    def __init__(self, secret, size):
        self.secret, self.size = secret, size
        self.count = math.ceil(size / DATA)
        self.buf = bytearray()
        self.aead = self.base = None
        self.seq = 0

    def feed(self, data):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.buf += data
        if self.aead is None:
            if len(self.buf) < HEAD:
                return b""
            head = bytes(self.buf[:HEAD])
            if struct.unpack(">I", head[16:20])[0] != RS or head[20] != 0:
                raise ValueError("header")
            cek, self.base = share_later._ece_keys(self.secret, head[:16])
            self.aead = AESGCM(cek)
            del self.buf[:HEAD]
        out = []
        while self.seq < self.count:
            last = self.seq == self.count - 1
            plain_len = (self.size - self.seq * DATA) if last else DATA
            need = plain_len + 1 + share_later.ECE_TAG
            if len(self.buf) < need:
                break
            rec = bytes(self.buf[:need])
            del self.buf[:need]
            try:
                plain = self.aead.decrypt(share_later._nonce(self.base, self.seq), rec, None)
            except InvalidTag as e:
                raise ValueError("changed") from e
            if len(plain) != plain_len + 1 or plain[-1] != (2 if last else 1):
                raise ValueError("padding")
            out.append(plain[:-1])
            self.seq += 1
        if self.seq == self.count and self.buf:
            raise ValueError("too long")
        return b"".join(out)

    def finish(self):
        if self.aead is None or self.seq != self.count or self.buf:
            raise ValueError("cut off")


# ---------- WebSocket client (RFC 6455) ----------
def accept_key(key_b64):
    return base64.b64encode(hashlib.sha1(key_b64.encode("ascii") + WS_GUID).digest()).decode()


def mask(data, key4):
    n = len(data)
    if not n:
        return b""
    m = (key4 * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(m, "little")).to_bytes(n, "little")


def frame(opcode, payload, key4=None):
    """One masked client frame (FIN set)."""
    key4 = key4 if key4 is not None else os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + key4 + mask(payload, key4)


def _net_error(e):
    if isinstance(e, SendError):
        return e
    if download.is_cert_error(e):
        return SendError("certificate", download.error_text(e))
    return SendError("retry", download.error_text(e))


def url_ok(url, allow_local=False):
    p = urllib.parse.urlsplit(url)
    if p.scheme == "https" and p.hostname and not p.username:
        return True
    return bool(allow_local and p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"))


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self.rbuf = bytearray()

    @classmethod
    def connect(cls, url, timeout=TIMEOUT, allow_local=False):
        """Open and upgrade. A non-101 answer raises SendError("retry"/"refused") with its
        status and Retry-After."""
        if not url_ok(url, allow_local):
            raise SendError("refused", "address not allowed")
        p = urllib.parse.urlsplit(url)
        tls = p.scheme == "https"
        port = p.port or (443 if tls else 80)
        sock = None
        try:
            sock = socket.create_connection((p.hostname, port), timeout=timeout)
            if tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
            ws = cls(sock)
            host = p.hostname if p.port is None else f"{p.hostname}:{p.port}"
            ws._handshake(host, p.path or "/")
            return ws
        except BaseException as e:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if isinstance(e, (OSError, ValueError)):
                raise _net_error(e) from e
            raise

    def _handshake(self, host, path):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: {download.USER_AGENT}\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) > HEADER_MAX:
                raise SendError("broken", "answer too long")
            got = self.sock.recv(4096)
            if not got:
                raise SendError("retry", "closed during handshake")
            data += got
        head, rest = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        m = re.match(r"HTTP/1\.[01] (\d{3})", lines[0])
        if not m:
            raise SendError("broken", "not http")
        status = int(m.group(1))
        headers = {}
        for line in lines[1:]:
            k, _s, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if status != 101:
            wait = None
            try:
                wait = int(headers.get("retry-after", ""))
            except ValueError:
                pass
            kind = "retry" if status == 429 or status >= 500 else "refused"
            raise SendError(kind, f"HTTP {status}", status=status, wait=wait)
        if (headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in headers.get("connection", "").lower()
                or headers.get("sec-websocket-accept") != accept_key(key)
                or headers.get("sec-websocket-extensions")
                or headers.get("sec-websocket-protocol")):
            raise SendError("broken", "bad upgrade")
        self.rbuf += rest

    def _exact(self, n):
        while len(self.rbuf) < n:
            got = self.sock.recv(max(4096, n - len(self.rbuf)))
            if not got:
                raise SendError("retry", "closed")
            self.rbuf += got
        out = bytes(self.rbuf[:n])
        del self.rbuf[:n]
        return out

    def _recv_frame(self, max_bytes):
        b0, b1 = self._exact(2)
        if b0 & 0x70:
            raise SendError("broken", "reserved bits")
        if b1 & 0x80:
            raise SendError("broken", "masked server frame")
        fin, op, n = bool(b0 & 0x80), b0 & 0x0F, b1 & 0x7F
        if op >= 8 and (not fin or n > CONTROL_MAX):
            raise SendError("broken", "bad control frame")
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        if n > max_bytes:
            raise SendError("broken", "answer too long")
        return fin, op, self._exact(n)

    def _wait_data(self, stop, deadline):
        """Wait (WAIT_STEP at a time) until the server sends something, so a frame is never
        cut by a timeout; Cancel and the deadline are checked in between."""
        if self.rbuf:
            return
        old = self.sock.gettimeout()
        try:
            while not self.rbuf:
                if stop is not None and stop():
                    raise SendError("cancelled")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SendError("retry", "no answer")
                self.sock.settimeout(min(WAIT_STEP, left))
                try:
                    got = self.sock.recv(4096)
                except (socket.timeout, ssl.SSLWantReadError):
                    continue
                if not got:
                    raise SendError("retry", "closed")
                self.rbuf += got
        finally:
            self.sock.settimeout(old)

    def send_text(self, text):
        self.sock.sendall(frame(1, text.encode("utf-8")))

    def send_binary(self, data):
        self.sock.sendall(frame(2, data))

    def recv_json(self, max_bytes=ANSWER_MAX, timeout=TIMEOUT, stop=None):
        """The next text message as a dict (pings answered on the way), within `timeout`
        seconds in all; stop() is checked while waiting."""
        deadline = time.monotonic() + timeout
        parts, total = [], 0
        while True:
            if time.monotonic() > deadline:   # e.g. a server that only ever sends pings
                raise SendError("retry", "no answer")
            self._wait_data(stop, deadline)
            fin, op, payload = self._recv_frame(max_bytes)
            if op == 9:                                   # ping
                self.sock.sendall(frame(10, payload))
                continue
            if op == 10:                                  # pong
                continue
            if op == 8:
                raise SendError("retry", "closed by the server")
            if op not in (0, 1, 2) or (op == 0) != bool(parts):   # continuation only after a start
                raise SendError("broken", f"opcode {op}")
            total += len(payload)
            if total > max_bytes:
                raise SendError("broken", "answer too long")
            parts.append(payload)
            if fin:
                break
        try:
            out = json.loads(b"".join(parts).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise SendError("broken", "not json") from e
        if not isinstance(out, dict):
            raise SendError("broken", "bad answer")
        return out

    def close(self):
        try:
            self.sock.sendall(frame(8, struct.pack(">H", 1000)))
        except (OSError, ValueError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- the Send server's API ----------
@dataclasses.dataclass
class Part:
    """One Send file: its id and owner token (from the server), our secret, and which bytes
    of the blob it holds."""
    id: str
    owner: str = dataclasses.field(repr=False)      # never in a log or repr
    secret: bytes = dataclasses.field(repr=False)
    start: int = 0
    end: int = 0

    def record(self):
        return {"id": self.id, "owner": self.owner, "secret": b64url(self.secret),
                "start": self.start, "end": self.end}

    @classmethod
    def from_record(cls, r):
        return cls(r["id"], r["owner"], b64any(r["secret"]), r["start"], r["end"])


def check_parts(parts, blob):
    """True when a record's parts list is one we accept: 1..MAX_PARTS, in order, every byte
    of the blob once."""
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        return False
    at = 0
    for p in parts:
        if not isinstance(p, dict) or set(p) != {"id", "owner", "secret", "start", "end"}:
            return False
        sid, owner, sec, start, end = (p["id"], p["owner"], p["secret"], p["start"], p["end"])
        if (not isinstance(sid, str) or not re.fullmatch(ID_RE, sid)
                or not isinstance(owner, str) or not re.fullmatch(OWNER_RE, owner)
                or not isinstance(sec, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", sec)
                or type(start) is not int or type(end) is not int
                or start != at or not 0 < end - start <= MAX_PART_SIZE):
            return False
        at = end
    return isinstance(blob, int) and at == blob


class Send:
    """One Send server (a SITES key). sleep(seconds) should wake up early on cancel."""

    def __init__(self, site, allow_local=False, sleep=time.sleep, url=None):
        self.site = SITES[site]
        if url is not None:   # tests only: a local fake server, never another host
            p = urllib.parse.urlsplit(url)
            if not (allow_local and p.scheme == "http" and p.hostname in LOCAL_HOSTS):
                raise SendError("refused", "address not allowed")
        self.url = (url or self.site.url).rstrip("/")
        if not self.url or not url_ok(self.url, allow_local):
            raise SendError("refused", "address not allowed")
        self.allow_local = allow_local
        self.sleep = sleep

    # plain HTTPS
    def _open(self, method, path, body=None, headers=None, timeout=TIMEOUT):
        p = urllib.parse.urlsplit(self.url)
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(p.hostname, p.port, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.putheader("User-Agent", download.USER_AGENT)
            hdrs = dict(headers or {})
            if body is not None:
                hdrs["Content-Length"] = str(len(body))
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders(body)
            return conn, conn.getresponse()
        except BaseException as e:
            conn.close()
            if isinstance(e, (OSError, http.client.HTTPException)):
                raise _net_error(e) from e
            raise

    def _small(self, method, path, body=None, headers=None, timeout=TIMEOUT):
        conn, resp = self._open(method, path, body, headers, timeout)
        try:
            return resp.status, resp.read(ANSWER_MAX + 1)[:ANSWER_MAX], resp
        except (OSError, http.client.HTTPException) as e:
            raise _net_error(e) from e
        finally:
            conn.close()

    def limits(self):
        """(max file size, max downloads, max expire seconds) from GET /config."""
        status, data, _r = self._small("GET", "/config", timeout=20)
        if status == 429 or status >= 500:
            raise SendError("down", f"config HTTP {status}")
        if status != 200:
            raise SendError("refused", f"config HTTP {status}")
        try:
            lim = json.loads(data.decode("utf-8"))["LIMITS"]
            out = (lim["MAX_FILE_SIZE"], lim["MAX_DOWNLOADS"], lim["MAX_EXPIRE_SECONDS"])
        except (ValueError, UnicodeDecodeError, KeyError, TypeError) as e:
            raise SendError("refused", "config not understood") from e
        if not all(type(x) is int and x > 0 for x in out):
            raise SendError("refused", "config not understood")
        return out

    def plan(self, total, hours, limits):
        """([(start, end)], time limit) for a blob of `total` bytes, or SendError("refused")
        when this server can't take it."""
        max_file, max_downloads, max_expire = limits
        time_limit = int(hours) * 3600 + UPLOAD_WINDOW + TIME_MARGIN
        if time_limit > max_expire:
            raise SendError("refused", "keeps files too short")
        if DLIMIT > max_downloads:
            raise SendError("refused", "download limit")
        part = min(max_file - PART_MARGIN, MAX_PART_SIZE)
        if part <= 0:
            raise SendError("refused", "too small")
        n = math.ceil(total / part)
        if not 1 <= n <= min(self.site.max_parts, MAX_PARTS):
            raise SendError("refused", "too big")
        return [(i * part, min(total, (i + 1) * part)) for i in range(n)], time_limit

    def upload_part(self, read, start, end, time_limit, name, stop=None, moved=None):
        """One Send file over the WebSocket. moved(plain bytes of this part done)."""
        secret = os.urandom(SECRET_LEN)
        size = end - start
        ws = WebSocket.connect(self.url + "/api/ws", TIMEOUT, self.allow_local)
        made = None
        handed = False   # the socket and the delete went to a cleanup thread
        try:
            try:
                ws.send_text(json.dumps({
                    "fileMetadata": seal_metadata(secret, name, size),
                    "authorization": "send-v1 " + b64url(auth_key(secret)),
                    "timeLimit": time_limit, "dlimit": DLIMIT}))
                ans = ws.recv_json(stop=stop)
                self._answer_error(ans)
                sid, owner = ans.get("id"), ans.get("ownerToken")
                if (not isinstance(sid, str) or not re.fullmatch(ID_RE, sid)
                        or not isinstance(owner, str) or not re.fullmatch(OWNER_RE, owner)):
                    raise SendError("broken", "bad answer")
                made = Part(sid, owner, secret, start, end)
                done = 0
                for item in ece_frames(lambda a, b: read(start + a, start + b), size, secret):
                    if stop is not None and stop():
                        raise SendError("cancelled")
                    ws.send_binary(item)
                    if len(item) > HEAD:
                        done = min(size, done + len(item) - 1 - share_later.ECE_TAG)
                        if moved:
                            moved(done)
                ws.send_binary(b"\x00")
                try:
                    ans = ws.recv_json(timeout=END_WAIT, stop=stop)
                except SendError as e:
                    if e.kind == "cancelled":   # all sent: the server stores it anyway, so
                        part = made             # wait a little for that, then delete it, in a
                        handed = True           # thread that the app's exit waits for

                        def finish():
                            try:
                                ws.recv_json(timeout=STOP_GRACE)
                            except (SendError, OSError, ValueError):
                                pass
                            finally:
                                ws.close()
                            self.delete(part)
                        cleanup_later(finish)
                    raise
                self._answer_error(ans)
                if ans.get("ok") is not True:
                    raise SendError("broken", "no ok")
                return made
            except (OSError, ValueError) as e:
                raise _net_error(e) from e
        except BaseException:
            if made is not None and not handed:   # the server drops a half upload; delete
                self.delete(made)                 # in case it kept it
            raise
        finally:
            if not handed:
                ws.close()

    @staticmethod
    def _answer_error(ans):
        if "error" in ans:
            code = ans.get("error")
            text = str(code) if type(code) is int and 0 <= code < 1000 else "?"   # never server text
            if code in (400, 401, 413):
                raise SendError("refused", f"error {text}")
            raise SendError("retry", f"error {text}")

    def _tries(self, call, stop):
        """call() up to UPLOAD_TRIES times; only blips, 429 and 5xx are tried again, after
        BACKOFF (or Retry-After, at most RETRY_AFTER_MAX). Then SendError("down")."""
        for i in range(UPLOAD_TRIES):
            if stop is not None and stop():
                raise SendError("cancelled")
            try:
                return call()
            except SendError as e:
                if e.kind != "retry":
                    raise
                if i == UPLOAD_TRIES - 1:
                    raise SendError("down", e.detail) from e
                pause = BACKOFF[min(i, len(BACKOFF) - 1)]
                if e.wait is not None:
                    pause = max(0, min(RETRY_AFTER_MAX, e.wait))
                log.info("Send server didn't take it (%s); trying again in %s s", e.detail, pause)
                self.sleep(pause)
        raise SendError("down")

    def upload(self, blob, hours, name, stop=None, progress=None):
        """The whole blob (share_later.Blob) as one or more Send files, one after another.
        Returns [Part]. On any failure every uploaded part is deleted again."""
        with _ONE_AT_A_TIME:
            if stop is not None and stop():
                raise SendError("cancelled")
            try:
                limits = self.limits()
            except SendError as e:
                if e.kind == "retry":
                    raise SendError("down", e.detail) from e
                raise
            ranges, time_limit = self.plan(blob.total, hours, limits)
            parts = []
            if progress:
                progress(0, blob.total)
            try:
                for start, end in ranges:
                    def moved(done, start=start):
                        if progress:
                            progress(start + done, blob.total)
                    parts.append(self._tries(lambda: self.upload_part(
                        blob.read, start, end, time_limit, name, stop, moved), stop))
                return parts
            except BaseException:
                self.delete_all(parts)
                raise

    def download_part(self, part, out, stop=None, moved=None):
        """One Send file into the open file `out` (plain blob bytes), checked."""
        akey = auth_key(part.secret)
        nonce = DEFAULT_NONCE
        size = part.end - part.start
        for _auth in range(3):
            conn, resp = self._open("GET", f"/api/download/{part.id}",
                                    headers={"Authorization": auth_header(akey, nonce)})
            try:
                if resp.status == 401:
                    resp.read(ANSWER_MAX)
                    m = re.fullmatch(r"send-v1 ([A-Za-z0-9+/=_-]{8,64})",
                                     resp.getheader("WWW-Authenticate", "") or "")
                    if not m or m.group(1) == nonce:
                        raise SendError("broken", "auth")
                    nonce = m.group(1)
                    continue
                if resp.status == 404:
                    raise SendError("gone", "HTTP 404")
                if resp.status == 429 or resp.status >= 500:
                    raise SendError("retry", f"HTTP {resp.status}")
                if resp.status != 200:
                    raise SendError("broken", f"HTTP {resp.status}")
                try:
                    length = int(resp.getheader("Content-Length", ""))
                except ValueError:
                    length = -1
                if length != share_later.ece_size(size):
                    raise SendError("broken", "size")
                reader, done = EceReader(part.secret, size), 0
                try:
                    while True:
                        if stop is not None and stop():
                            raise SendError("cancelled")
                        data = resp.read(READ_BLOCK)
                        if not data:
                            break
                        plain = reader.feed(data)
                        if plain:
                            out.write(plain)
                            done += len(plain)
                            if moved:
                                moved(done)
                    reader.finish()
                except ValueError as e:
                    if str(e) == "cut off":
                        raise SendError("retry", "cut off") from e
                    raise SendError("broken", str(e)) from e
                except (OSError, http.client.HTTPException) as e:
                    if isinstance(e, OSError) and not isinstance(e, (socket.timeout,
                                                                     ConnectionError, ssl.SSLError)):
                        raise   # our own disk: not the server's fault
                    raise _net_error(e) from e
                return
            finally:
                conn.close()
        raise SendError("broken", "auth")

    def download(self, parts, out, stop=None, progress=None):
        """Every part into `out`, in order; a part whose download breaks gets one more try
        (DLIMIT is 2), from its start again."""
        total = parts[-1].end if parts else 0
        if progress:
            progress(0, total)
        for part in parts:
            at = out.tell()

            def moved(done, part=part):
                if progress:
                    progress(part.start + done, total)

            def once(part=part, at=at, moved=moved):
                out.seek(at)
                out.truncate()
                self.download_part(part, out, stop, moved)
            self._tries(once, stop)

    def delete(self, part):
        """Delete one Send file with its owner token. Never raises; True if the server said
        200 (404 = already gone)."""
        body = json.dumps({"owner_token": part.owner}).encode("ascii")
        try:
            status, _d, _r = self._small("POST", f"/api/delete/{part.id}", body,
                                         {"Content-Type": "application/json"}, timeout=10)
            return status == 200
        except Exception:   # Send deletes it by itself when its time is up
            log.info("Could not delete a Send file; the server removes it by itself")
            return False

    def delete_all(self, parts):
        for p in parts:
            self.delete(p)
