"""Guild storage for send later: the sealed .amsend blob kept on the guild's own relay
(share_worker\\worker.js StoreLedger / StoreSlice: Durable Object storage on Workers Free).

Tried before file.kiwi, only when the player ticked send later. The receiving app downloads
it by itself (no browser), then deletes it there. Always the relay's own address (never one
from a record); the blob is already encrypted (share_later.Blob), the server only keeps bytes.
Two random tokens per send, made here: the put token (only the sender has it) and the get
token (inside the sealed record). The server only stores their sha256 hashes; the token itself
goes with each piece request (HTTPS) and is hashed there, never kept.
urllib only (through share.Relay._once); no tkinter."""

import dataclasses
import hashlib
import logging
import math
import secrets
import time

log = logging.getLogger(__name__)

STORE_MAX = 4_000_000_000        # biggest blob kept on the guild's server (bigger -> file.kiwi)
PIECE_MAX = 8 * 1024 * 1024      # a server answer asking for bigger pieces is refused
SLICE_MAX = 1024 * 1024 * 1024
MAX_SLICES = 10
PIECE_TIMEOUT = 120              # seconds per piece (4 MiB on a slow line)
RETRY_FOR = 600                  # a piece may keep failing this long ...
MAX_TRIES = 30                   # ... or this many times, then the server is "down"
ID_RE = "[0-9a-f]{32}"


class StoreError(Exception):
    """kind: unavailable (not used: busy, too big, older server), down (stopped answering),
    gone (the stored send isn't there any more), broken (a piece of the wrong size),
    cancelled, certificate."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind} {detail}".strip())
        self.kind = kind
        self.detail = detail


def _hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_tokens():
    """(put, get): two different random tokens."""
    return secrets.token_hex(16), secrets.token_hex(16)


@dataclasses.dataclass
class StoreInfo:
    """One stored send: its id and layout (from the server), the tokens (made here)."""
    id: str
    piece: int
    slice: int
    put: str = ""
    get: str = ""


def check_layout(total, piece, slice_size):
    """True when the layout is one we accept (sizes from the server or a record)."""
    return (isinstance(total, int) and isinstance(piece, int) and isinstance(slice_size, int)
            and not isinstance(piece, bool) and not isinstance(slice_size, bool)
            and 0 < total <= STORE_MAX and 0 < piece <= PIECE_MAX
            and 0 < slice_size <= SLICE_MAX and slice_size % piece == 0
            and math.ceil(total / slice_size) <= MAX_SLICES)


def layout(total, piece, slice_size):
    """[(slice, n, start, end)] for a blob of `total` bytes: every byte once, in order."""
    out = []
    for k in range(math.ceil(total / slice_size)):
        base = k * slice_size
        size = min(slice_size, total - base)
        for n in range(math.ceil(size / piece)):
            start = base + n * piece
            out.append((k, n, start, min(start + piece, base + size)))
    return out


class Store:
    """The guild storage API on the relay (share.Relay: same address, opener and checks)."""

    def __init__(self, relay, sleep=None, clock=time.monotonic):
        self.relay = relay
        self.sleep = sleep or relay.sleep
        self.clock = clock

    def _call(self, method, path, token, body=None, max_bytes=1024):
        from am.share import ShareError
        try:
            return self.relay._once(method, path, body, max_bytes, timeout=PIECE_TIMEOUT,
                                    headers={"X-Store-Token": token})
        except ShareError as e:
            if e.kind == "certificate":
                raise StoreError("certificate", e.detail) from e
            if e.kind == "server":   # an answer bigger than a piece
                raise StoreError("broken", e.detail) from e
            raise StoreError("retry", e.kind) from e

    def _retrying(self, call, stop):
        """call() until it works; blips and 5xx/429 wait 2-60 s. Gives up after RETRY_FOR
        seconds or MAX_TRIES tries ("down")."""
        first, delay, tries = None, 2, 0
        while True:
            if stop and stop():
                raise StoreError("cancelled")
            try:
                return call()
            except StoreError as e:
                if e.kind != "retry":
                    raise
                tries += 1
                now = self.clock()
                first = now if first is None else first
                if tries >= MAX_TRIES or now - first >= RETRY_FOR:
                    raise StoreError("down", e.detail) from e
                log.info("Guild server didn't answer (%s); trying again", e.detail)
                self.sleep(delay)
                delay = min(60, delay * 2)

    @staticmethod
    def _status(status, data):
        if status >= 500 or status == 429:
            raise StoreError("retry", f"HTTP {status}")
        raise StoreError("gone", f"HTTP {status} {data!s:.40}")

    def _path(self, info, k=None, n=None):
        from am.share import PROTOCOL
        tail = "" if k is None else f"/{k}/{n}"
        return f"/{PROTOCOL}/s/{info.id}{tail}"

    def upload(self, info, blob, stop=None, progress=None):
        """Every piece of the blob (share_later.Blob), one at a time, in order."""
        def put(path, data):
            status, got = self._call("PUT", path, info.put, data)
            if status not in (200, 204):
                self._status(status, got)
        parts = layout(blob.total, info.piece, info.slice)
        if progress:
            progress(0, blob.total)
        for k, n, start, end in parts:
            if stop and stop():
                raise StoreError("cancelled")
            data = blob.read(start, end)
            self._retrying(lambda: put(self._path(info, k, n), data), stop)
            if progress:
                progress(end, blob.total)

    def download(self, info, total, out, stop=None, progress=None):
        """Every piece into the open file `out`; each must have exactly its size."""
        def get(path, want):
            status, data = self._call("GET", path, info.get, max_bytes=want)
            if status != 200:
                self._status(status, data)
            if len(data) != want:
                raise StoreError("broken", "size")
            return data
        if progress:
            progress(0, total)
        for k, n, start, end in layout(total, info.piece, info.slice):
            out.write(self._retrying(lambda: get(self._path(info, k, n), end - start), stop))
            if progress:
                progress(end, total)

    def delete(self, info, token):
        """Delete the stored send now (either token). Never raises."""
        try:
            self.relay._once("DELETE", self._path(info), None, timeout=10,
                             headers={"X-Store-Token": token})
        except Exception:   # the server deletes it by itself within hours anyway
            log.info("Could not delete the stored send; the server removes it by itself")
