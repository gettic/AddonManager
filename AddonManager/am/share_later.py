"""Send later: when the other PC isn't ready, the backup goes to file.kiwi (a third-party
file host) and the same 3-word code fetches it later. Only when the player ticked it (off by
default); am\\share.py runs the flow, am\\share_ui.py the windows.

Two layers of encryption:
- Ours: the zip is sealed with a random 256-bit file key K in the send code's chunk format
  (ChaCha20-Poly1305), as one "blob" made on the fly (no temp copy).
- file.kiwi's: RFC 8188 (aes128gcm) exactly as their uploader does it (wormhole-crypto 0.3.1),
  so their download page in the browser can undo it. Their key is only in the link's #part.
The link and K go in a small record on our relay, sealed with a key made from the code
(scrypt); the relay hands it out once, to the first right code (a wrong one deletes it).

file.kiwi rules we keep (tos conditions): their documented v2 API only, one chunk at a time,
back off on 429 and 5xx, never ask for a longer expiry, the app's usual User-Agent, and random
neutral names. When file.kiwi is down, the upload waits and tries again every 5 minutes for
up to 2 hours (KIWI_DOWN_*), as long as the app stays open. No tkinter here."""

import base64
import hashlib
import http.client
import json
import logging
import math
import os
import re
import secrets
import ssl
import struct
import time
import urllib.parse

from am import download

log = logging.getLogger(__name__)

API_URL = "https://api.file.kiwi"
LINK_PREFIX = "https://file.kiwi/"
TERMS_URL = "https://file.kiwi/legal/terms"
PRIVACY_URL = "https://file.kiwi/legal/privacy"
LATER_AFTER = 300                    # seconds the code waits for the other PC first
CONSENT_VERSION = 2                  # the tick's wording (2: also big backups at once); a
                                     # tick given under an older wording counts as off
HOURS = (1, 2, 4, 8, 12, 24)         # how long the code works for a later send
DEFAULT_HOURS = 12
EXT = ".amsend"
MAGIC = b"AMSEND1\n"
TIMEOUT = 60
UPLOAD_WAIT = 6 * 3600 + 600         # the receiver waits at most this long for an upload
RETRY_FOR = 600                      # seconds a chunk may keep failing before giving up
MAX_RETRIES = 30                     # quick retries per send in all (then file.kiwi is "down")
KIWI_DOWN_FOR = 2 * 3600             # file.kiwi down: keep trying this long after it first
KIWI_DOWN_EVERY = 300                # went down, one try every this many seconds
MAX_ANSWER = 64 * 1024
BLOCK = 256 * 1024
CHUNK_MIN, CHUNK_MAX = 64 * 1024, 64 * 1024 * 1024   # file.kiwi chunk sizes we accept
# scrypt for the code record: ~64 MiB, well under a second on a normal PC.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1
SCRYPT_MAXMEM = 128 * 1024 * 1024


class LaterError(Exception):
    """kind: offline, timeout, certificate, busy, server, cancelled, file."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# ---------- RFC 8188 (aes128gcm), as wormhole-crypto 0.3.1 ----------
ECE_RS = 64 * 1024
ECE_KEY = 16
ECE_TAG = 16
ECE_HEADER = ECE_KEY + 4 + 1         # salt + record size + idlen (0)
ECE_DATA = ECE_RS - ECE_TAG - 1      # plaintext bytes per record


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _ece_keys(key, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(key)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(key)
    return cek, nonce


def _nonce(base, seq):
    if seq > 0xFFFFFFFF:
        raise ValueError("too many records")
    tail = struct.unpack(">I", base[8:])[0] ^ seq
    return base[:8] + struct.pack(">I", tail)


def ece_encrypt(key, data, salt=None):
    """One aes128gcm stream of `data` (bytes): header + records. Empty data = header only
    (as wormhole-crypto)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt if salt is not None else os.urandom(ECE_KEY)
    cek, base = _ece_keys(key, salt)
    aead = AESGCM(cek)
    out = [salt + struct.pack(">I", ECE_RS) + b"\x00"]
    count = math.ceil(len(data) / ECE_DATA)
    view = memoryview(data)
    for seq in range(count):
        record = bytes(view[seq * ECE_DATA:(seq + 1) * ECE_DATA])
        if seq == count - 1:
            padded = record + b"\x02"
        else:
            padded = record + b"\x01" + bytes(ECE_RS - len(record) - ECE_TAG - 1)
        out.append(aead.encrypt(_nonce(base, seq), padded, None))
    return b"".join(out)


def ece_decrypt(key, data):
    """Undo ece_encrypt (what file.kiwi's download page does; used by tests)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(data) < ECE_HEADER or data[ECE_HEADER - 1] != 0:
        raise ValueError("bad header")
    salt, rs = data[:ECE_KEY], struct.unpack(">I", data[ECE_KEY:ECE_KEY + 4])[0]
    if rs != ECE_RS:
        raise ValueError("record size")
    cek, base = _ece_keys(key, salt)
    aead = AESGCM(cek)
    body = data[ECE_HEADER:]
    records = [body[i:i + rs] for i in range(0, len(body), rs)]
    out = []
    for seq, rec in enumerate(records):
        plain = aead.decrypt(_nonce(base, seq), rec, None).rstrip(b"\x00")
        last = seq == len(records) - 1
        if not plain or plain[-1] != (2 if last else 1):
            raise ValueError("padding")
        out.append(plain[:-1])
    return b"".join(out)


def ece_size(n):
    """Encrypted size of n plaintext bytes (wormhole-crypto encryptedSize)."""
    return ECE_HEADER + n + (ECE_TAG + 1) * math.ceil(n / ECE_DATA)


# ---------- our blob: the backup sealed with K ----------
class Blob:
    """The sealed backup as file.kiwi gets it (before their layer), made on the fly:
    MAGIC, header length (4 bytes), sealed header (index 0), then the 1 MiB chunks sealed
    like a live send (share.Cipher with channel 0)."""

    def __init__(self, path, key, size, sha256):
        from am import share
        self.path, self.key, self.size = path, key, size
        self.chunk = share.CHUNK
        self.chunks = max(1, math.ceil(size / share.CHUNK))
        self.cipher = share.Cipher(key, 0)
        header = json.dumps({"v": 1, "size": size, "chunks": self.chunks,
                             "sha256": sha256}).encode("ascii")
        sealed = self.cipher.seal(0, header)
        self.head = MAGIC + struct.pack(">I", len(sealed)) + sealed
        self.sealed = share.CHUNK + share.TAG
        self.total = len(self.head) + size + share.TAG * self.chunks
        self._cache = (None, b"")

    def _chunk(self, f, i):
        if self._cache[0] == i:
            return self._cache[1]
        f.seek((i - 1) * self.chunk)
        want = self.chunk if i < self.chunks else self.size - (self.chunks - 1) * self.chunk
        data = f.read(want)
        if len(data) != want:
            raise LaterError("file", "file changed")
        box = self.cipher.seal(i, data, final=(i == self.chunks))
        self._cache = (i, box)
        return box

    def read(self, start, end):
        """Bytes start..end (end not included) of the blob."""
        out = []
        if start < len(self.head):
            out.append(self.head[start:min(end, len(self.head))])
            start = len(self.head)
        with open(self.path, "rb") as f:
            while start < end:
                rel = start - len(self.head)
                i = rel // self.sealed + 1
                box = self._chunk(f, i)
                off = rel - (i - 1) * self.sealed
                piece = box[off:off + (end - start)]
                if not piece:
                    raise LaterError("file", "past the end")
                out.append(piece)
                start += len(piece)
        return b"".join(out)


def open_blob(path, key, expect_size):
    """Check a downloaded blob and yield its plain chunks: (header dict, generator).
    Raises ValueError when it is not ours, cut off or changed."""
    from am import share
    with open(path, "rb") as f:
        head = f.read(len(MAGIC) + 4)
        if len(head) != len(MAGIC) + 4 or head[:len(MAGIC)] != MAGIC:
            raise ValueError("not a send-later file")
        n = struct.unpack(">I", head[len(MAGIC):])[0]
        if not 16 < n <= 4096:
            raise ValueError("bad header")
        cipher = share.Cipher(key, 0)
        try:
            h = json.loads(cipher.open(0, f.read(n)).decode("ascii"))
        except share.ShareError:
            raise ValueError("bad header")
    if not isinstance(h, dict) or h.get("v") != 1:
        raise ValueError("bad header")
    size, chunks, sha = h.get("size"), h.get("chunks"), h.get("sha256")
    if (not isinstance(size, int) or size != expect_size or not isinstance(sha, str)
            or chunks != max(1, math.ceil(size / share.CHUNK))):
        raise ValueError("bad header")
    total = len(MAGIC) + 4 + n + size + share.TAG * chunks
    if os.path.getsize(path) != total:
        raise ValueError("cut off")

    def chunks_of():
        with open(path, "rb") as f:
            f.seek(len(MAGIC) + 4 + n)
            for i in range(1, chunks + 1):
                want = (share.CHUNK if i < chunks
                        else size - (chunks - 1) * share.CHUNK) + share.TAG
                box = f.read(want)
                if len(box) != want:
                    raise ValueError("cut off")
                yield cipher.open(i, box, final=(i == chunks))
    return h, chunks_of()


def random_name():
    """Neutral, random file name (no player, guild or game names)."""
    return secrets.token_hex(4) + EXT


# ---------- the code record ----------
def record_keys(password, salt):
    """(record key, verifier hex) from the code (share._password) and the record's salt."""
    out = hashlib.scrypt(password, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=SCRYPT_MAXMEM, dklen=64)
    return out[:32], hashlib.sha256(b"AM-later-verify|" + out[32:]).hexdigest()


def _aad(channel):
    return b"AM-later-v1|%d" % channel


def seal_record(key, channel, record):
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    nonce = os.urandom(12)
    data = json.dumps(record, separators=(",", ":")).encode("ascii")
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, _aad(channel))


def open_record(key, channel, sealed, allow_local=False):
    """The record dict, checked. Raises ValueError."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    try:
        data = ChaCha20Poly1305(key).decrypt(sealed[:12], sealed[12:], _aad(channel))
        r = json.loads(data.decode("ascii"))
    except (InvalidTag, ValueError, UnicodeDecodeError) as e:
        raise ValueError("record") from e
    if not isinstance(r, dict) or r.get("v") != 1:
        raise ValueError("record")
    link, k, name = r.get("link"), r.get("key"), r.get("name")
    if (not isinstance(link, str) or not link_ok(link, allow_local) or not isinstance(k, str)
            or not re.fullmatch(r"[0-9a-f]{64}", k) or not isinstance(r.get("size"), int)
            or not isinstance(r.get("blob"), int) or not isinstance(r.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) or not isinstance(name, str)
            or not re.fullmatch(r"[0-9a-f]{8}" + re.escape(EXT), name)):
        raise ValueError("record")
    return r


def link_ok(link, allow_local=False):
    """A file.kiwi folder link with its key after #, nothing else."""
    p = urllib.parse.urlsplit(link)
    if allow_local and p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"):
        return bool(p.fragment)
    return (link.startswith(LINK_PREFIX) and p.scheme == "https" and p.hostname == "file.kiwi"
            and bool(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", p.fragment or ""))
            and not p.query and not p.username)


# ---------- file.kiwi's upload API (v2) ----------
class FileKiwi:
    """Upload one blob: POST /v2/folders, then each chunk to its presigned URL, one at a time,
    then upload-status (without uploadAuth: never extends the expiry).
    sleep(seconds) should wake up early on cancel (share.py passes its stop event's wait);
    on_wait(seconds left) when file.kiwi starts counting as down, on_ok() when it answers
    again (both called on the upload's thread)."""

    def __init__(self, api_url=API_URL, allow_local=False, sleep=time.sleep,
                 clock=time.monotonic, on_wait=None, on_ok=None):
        p = urllib.parse.urlsplit(api_url)
        local = p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost")
        if not (p.scheme == "https" and p.hostname) and not (allow_local and local):
            raise LaterError("server", "address not allowed")
        self.api = api_url.rstrip("/")
        self.allow_local = allow_local
        self.sleep, self.clock = sleep, clock
        self.on_wait, self.on_ok = on_wait, on_ok
        self.retries = 0          # quick retries of this upload together (MAX_RETRIES)
        self.down_until = None    # file.kiwi went down: give up at this clock() time
        self.waiting = False      # on_wait was called and file.kiwi hasn't answered since

    def _url_ok(self, url):
        p = urllib.parse.urlsplit(url)
        if p.scheme == "https" and p.hostname:
            return True
        return self.allow_local and p.scheme == "http" and p.hostname in ("127.0.0.1",
                                                                         "localhost")

    def _request(self, method, url, body=None, headers=None, stop=None, on_bytes=None):
        """(status, answer bytes). One connection per call; no redirects."""
        if not self._url_ok(url):
            raise LaterError("server", "address not allowed")
        p = urllib.parse.urlsplit(url)
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(p.hostname, p.port, timeout=TIMEOUT,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(p.hostname, p.port, timeout=TIMEOUT)
        try:
            path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.putheader("User-Agent", download.USER_AGENT)
            hdrs = {"Content-Length": str(len(body) if body is not None else 0)}
            hdrs.update(headers or {})
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            if body:
                view = memoryview(body)
                for i in range(0, len(body), BLOCK):
                    if stop is not None and stop():
                        raise LaterError("cancelled")
                    conn.send(view[i:i + BLOCK])
                    if on_bytes:
                        on_bytes(min(BLOCK, len(body) - i))
            resp = conn.getresponse()
            data = resp.read(MAX_ANSWER + 1)
            return resp.status, data[:MAX_ANSWER]
        except LaterError:
            raise
        except (OSError, http.client.HTTPException) as e:
            if download.is_cert_error(e):   # not a blip: never retried
                raise LaterError("certificate", download.error_text(e)) from e
            raise LaterError("timeout" if download._is_timeout(e) else "offline",
                             download.error_text(e)) from e
        finally:
            conn.close()

    def _retrying(self, call, stop, parse=None):
        """call() until it isn't a network blip, 429, 5xx or (with parse(status, data)) an
        answer that isn't JSON (a maintenance page). Returns (status, data), or parse's
        result. First quick retries: 2 s .. 60 s for RETRY_FOR seconds, at most MAX_RETRIES
        per upload. Then file.kiwi counts as down: one try every KIWI_DOWN_EVERY seconds
        until KIWI_DOWN_FOR after it first went down, then LaterError(kind, "down")."""
        deadline = self.clock() + RETRY_FOR
        delay = 2
        while True:
            self._check(stop)
            kind = None
            try:
                status, data = call()
            except LaterError as e:
                if e.kind not in ("offline", "timeout"):
                    raise
                kind = e.kind
            else:
                if status == 429:
                    kind = "busy"
                elif status >= 500:
                    kind = "server"
                elif parse is not None:
                    try:
                        out = parse(status, data)
                    except LaterError as e:
                        if e.detail != "not json":
                            raise
                        kind = "server"
                else:
                    out = status, data
            if kind is None:
                if self.waiting:
                    self.waiting = False
                    if self.on_ok:
                        self.on_ok()
                return out
            now = self.clock()
            if now <= deadline and self.retries < MAX_RETRIES:
                self.retries += 1
                pause = delay
                delay = min(delay * 2, 60)
            else:
                if self.down_until is None:
                    self.down_until = now + KIWI_DOWN_FOR
                left = self.down_until - now
                if left <= 0:
                    raise LaterError(kind, "down")
                if not self.waiting:
                    self.waiting = True
                    if self.on_wait:
                        self.on_wait(left)
                pause = min(KIWI_DOWN_EVERY, left)
            self.sleep(pause)
            self._check(stop)

    @staticmethod
    def _check(stop):
        if stop is not None and stop():
            raise LaterError("cancelled")

    @staticmethod
    def _json(data):
        try:
            out = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise LaterError("server", "not json") from e
        if not isinstance(out, dict):
            raise LaterError("server", "bad answer")
        return out

    def create(self, blob_size, name, key, stop=None):
        """The folder for one file. Returns (folder dict, file dict)."""
        ske = base64.b64encode(ece_encrypt(key, b64url(key).encode("ascii"))).decode("ascii")
        enc_name = base64.b64encode(ece_encrypt(key, name.encode("utf-8"))).decode("ascii")
        body = json.dumps({"mode": "send", "encryption": {"ske": ske},
                           "files": [{"filename": enc_name, "filesize": blob_size,
                                      "mimetype": "application/octet-stream"}]}).encode("ascii")
        return self._retrying(lambda: self._request(
            "POST", self.api + "/v2/folders", body, {"Content-Type": "application/json"}),
            stop, lambda status, data: self._folder(status, data, blob_size))

    def _folder(self, status, data, blob_size):
        if status != 200:
            raise LaterError("server", f"HTTP {status}")
        folder = self._json(data)
        files = folder.get("files")
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise LaterError("server", "bad answer")
        f = files[0]
        urls = f.get("uploadUrls")
        cs, n = f.get("chunkSize"), f.get("chunks")
        if (not isinstance(urls, dict) or not isinstance(cs, int)
                or not CHUNK_MIN <= cs <= CHUNK_MAX or n != max(1, math.ceil(blob_size / cs))
                or not isinstance(urls.get("signatures"), list) or len(urls["signatures"]) != n
                or not all(isinstance(urls.get(k), str) for k in ("head", "path", "tail"))
                or not isinstance(urls.get("headers", {}), dict)
                or not isinstance(f.get("fileId"), str)
                or not isinstance(folder.get("folderId"), str)):
            raise LaterError("server", "bad answer")
        return folder, f

    def link(self, folder, key):
        base = folder.get("folderUrlBase") or folder.get("folderUrl") or ""
        base = base.split("#", 1)[0] if isinstance(base, str) else ""
        link = f"{base}#{b64url(key)}"
        if not link_ok(link, self.allow_local):
            raise LaterError("server", "bad link")
        return link

    def chunk_url(self, f, c):
        u = f["uploadUrls"]
        return (f"{u['head']}{u['path']}/{c + 1:05d}?{u['tail']}"
                f"&X-Amz-Signature={u['signatures'][c]}")

    def put_chunk(self, f, c, data, stop, on_bytes):
        headers = {k: str(v) for k, v in (f["uploadUrls"].get("headers") or {}).items()
                   if k.lower() not in ("content-length", "host")}
        sent = [0]

        def call():
            on_bytes(-sent[0])
            sent[0] = 0

            def moved(n):
                sent[0] += n
                on_bytes(n)
            return self._request("PUT", self.chunk_url(f, c), data, headers, stop, moved)
        status, _d = self._retrying(call, stop)
        if status not in (200, 201, 204):
            raise LaterError("server", f"chunk HTTP {status}")

    def missing(self, folder, f, stop=None):
        """Chunk numbers (1-based) file.kiwi doesn't have yet ([] = complete)."""
        url = (f"{self.api}/v2/folders/{urllib.parse.quote(folder['folderId'], safe='')}"
               f"/files/{urllib.parse.quote(f['fileId'], safe='')}/upload-status")

        def parse(status, data):
            if status != 200:
                raise LaterError("server", f"status HTTP {status}")
            return self._json(data)
        out = self._retrying(lambda: self._request("GET", url), stop, parse)
        if out.get("complete") is True:
            return []
        miss = out.get("missing")
        if not isinstance(miss, list) or not all(isinstance(m, int) for m in miss):
            raise LaterError("server", "bad answer")
        return miss

    def upload(self, blob, name, key, stop=None, progress=None):
        """Upload the blob; returns the link (with file.kiwi's key after #)."""
        folder, f = self.create(blob.total, name, key, stop)
        cs, n = f["chunkSize"], f["chunks"]
        done = [0]

        def on_bytes(k):
            done[0] += k
            if progress:   # the encrypted chunk is a little bigger than the blob part
                progress(min(blob.total, done[0] * blob.total // max(1, ece_size(blob.total))),
                         blob.total)

        todo = list(range(n))
        for _round in range(3):
            for c in todo:
                part = blob.read(c * cs, min((c + 1) * cs, blob.total))
                self.put_chunk(f, c, ece_encrypt(key, part), stop, on_bytes)
            todo = [m - 1 for m in self.missing(folder, f, stop) if 1 <= m <= n]
            if not todo:
                return self.link(folder, key)
        raise LaterError("server", "upload not complete")


# ---------- finding the browser's download ----------
DOWNLOADS_ENV = "ADDONMANAGER_DOWNLOADS"   # tests: a temp folder instead of Downloads


def _windows_downloads():
    """Downloads via the Known Folder API (follows a moved Downloads folder)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]
        folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                         (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                      ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        log.info("Downloads folder lookup failed", exc_info=True)
        return None


def _linux_downloads(home):
    """XDG_DOWNLOAD_DIR from ~/.config/user-dirs.dirs, else ~/Downloads."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", home)
                    if os.path.isabs(value) and os.path.normpath(value) != os.path.normpath(home):
                        return value
    except OSError:
        pass
    return os.path.join(home, "Downloads")


def downloads_dirs():
    """Where browsers save downloads (the usual folder first)."""
    if os.environ.get(DOWNLOADS_ENV):
        return [os.environ[DOWNLOADS_ENV]]
    home = os.path.expanduser("~")
    first = _windows_downloads() if os.name == "nt" else _linux_downloads(home)
    return list(dict.fromkeys(f for f in (first, os.path.join(home, "Downloads")) if f))


def find_download(name, size, folders):
    """The finished download of `name` (also "name (1)" etc., as browsers rename doubles)
    with exactly `size` bytes; the newest one. None if not there (yet)."""
    stem = name[:-len(EXT)] if name.endswith(EXT) else name
    pattern = re.compile(re.escape(stem) + r"( \(\d{1,3}\)|\(\d{1,3}\))?" + re.escape(EXT))
    best = None
    for folder in folders:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not pattern.fullmatch(e.name):
                continue
            try:
                if e.is_symlink() or not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size == size and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, e.path)
    return best[1] if best else None
