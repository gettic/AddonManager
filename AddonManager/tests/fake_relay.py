"""A local fake of the send-code relay (share_worker\\worker.js), for tests only.

Pure Python (ThreadingHTTPServer on 127.0.0.1, a free port, in a daemon thread). It keeps
the same rules as the Worker: one join per code (the same join id gets the same answer),
write-once slots owned by one side, at most BUFFER chunks waiting, chunks in order, size cap,
code expiry / idle limit with a clock tests can move, close -> everything deleted and a short
"closed" note left. Tests can make it misbehave through `hook_chunk` (change or drop a chunk
on its way to the receiver) and `hook_msg` (change a message)."""

import hashlib
import json
import random
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEND_SLOTS = {"pakeA", "confA", "header"}
RECV_SLOTS = {"pakeB", "confB", "done"}
REASONS = {"done", "cancel", "bad_code"}


class Box:
    def __init__(self, ch, now, ttl):
        self.ch = ch
        self.created = now
        self.ttl = ttl
        self.last = now
        self.send_token = secrets.token_hex(16)
        self.recv_token = None
        self.jid = None
        self.joined_at = None
        self.slots = {}
        self.chunks = {}
        self.next_put = 1
        self.acked = 0
        self.bytes = 0
        self.closed = None      # reason once closed
        self.closed_at = None


class FakeRelay:
    def __init__(self, ttl=600, idle=120, max_time=3600, buffer=4, max_bytes=None,
                 channels=499, rate=1000, chunk=1024 * 1024):
        self.ttl, self.idle, self.max_time, self.buffer = ttl, idle, max_time, buffer
        self.max_bytes = max_bytes if max_bytes is not None else 520 * 1024 * 1024
        self.channels, self.rate, self.chunk = channels, rate, chunk
        self.offset = 0.0               # tests move the clock with advance()
        self.boxes = {}
        self.calls = []                 # (method, path) of every request
        self.rate_hits = {}
        self.hook_chunk = None          # f(index, box) -> bytes or None (= never arrives)
        self.hook_msg = None            # f(slot, data) -> bytes
        self.fail_next = 0              # this many requests answered 500 first
        self.cond = threading.Condition()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _reply(self, status, body=b"", ctype="application/octet-stream"):
                if isinstance(body, dict):
                    body, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                status, out = relay.handle(method, self.path, body,
                                           self.headers.get("X-Token"))
                self._reply(status, out)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def now(self):
        return time.monotonic() + self.offset

    def advance(self, seconds):
        with self.cond:
            self.offset += seconds
            self.cond.notify_all()

    # ---------- rules ----------
    def _expire(self, box, now):
        if box.closed:
            if now - box.closed_at > 300:
                self.boxes.pop(box.ch, None)
            return
        if box.recv_token is None:
            if now - box.created > box.ttl:
                self._close(box, "expired", now)
        elif now - box.last > self.idle or now - box.joined_at > self.max_time:
            self._close(box, "expired", now)

    def _close(self, box, reason, now):
        box.closed, box.closed_at = reason, now
        box.slots.clear()
        box.chunks.clear()
        self.cond.notify_all()

    def _box(self, ch):
        box = self.boxes.get(ch)
        if box is not None:
            self._expire(box, self.now())
        return self.boxes.get(ch)

    def stored(self):
        """Everything the relay still holds (tests check it's empty after a transfer)."""
        with self.cond:
            return {ch: (len(b.slots), len(b.chunks)) for ch, b in self.boxes.items()
                    if b.slots or b.chunks}

    def _wait(self, check, wait):
        end = time.monotonic() + max(0, min(wait, 30))
        while True:
            value = check()
            if value is not None:
                return value
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.cond.wait(min(left, 0.2))

    def handle(self, method, path, body, token):
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        wait = float(q.get("wait", ["0"])[0])
        parts = url.path.strip("/").split("/")
        with self.cond:
            self.calls.append((method, url.path))
            if self.fail_next > 0:
                self.fail_next -= 1
                return 500, {"error": "oops"}
            if parts[:1] != ["v1"]:
                return 404, {"error": "not_found"}
            now = self.now()
            if parts[1:] == ["open"] and method == "POST":
                if not self._rate("open"):
                    return 429, {"error": "rate"}
                free = [c for c in range(1, self.channels + 1) if self._box(c) is None]
                if not free:
                    return 503, {"error": "busy"}
                box = Box(random.choice(free), now, self.ttl)
                self.boxes[box.ch] = box
                return 200, {"ch": box.ch, "token": box.send_token, "ttl": self.ttl}
            if len(parts) == 3 and parts[1] == "join" and method == "POST":
                if not self._rate("join"):
                    return 429, {"error": "rate"}
                box = self._box(_int(parts[2]))
                if box is None:
                    return 404, {"error": "not_found"}
                if box.closed:
                    return 410, {"error": box.closed}
                jid = hashlib.sha256(body).hexdigest()
                if box.recv_token is not None:
                    if box.jid == jid:
                        return 200, {"token": box.recv_token}
                    return 409, {"error": "used"}
                box.recv_token, box.jid, box.joined_at, box.last = (
                    secrets.token_hex(16), jid, now, now)
                return 200, {"token": box.recv_token}
            if len(parts) < 3:
                return 404, {"error": "not_found"}
            box = self._box(_int(parts[1]))
            if box is None:
                return 404, {"error": "not_found"}
            if box.closed:
                return 410, {"error": box.closed}
            side = ("send" if token == box.send_token else
                    "recv" if token and token == box.recv_token else None)
            if side is None:
                return 403, {"error": "token"}
            box.last = now
            if parts[2] == "close" and method == "POST":
                try:
                    reason = json.loads(body.decode()).get("reason")
                except Exception:
                    reason = None
                self._close(box, reason if reason in REASONS else "cancel", now)
                return 204, b""
            if len(parts) == 4 and parts[2] == "msg":
                return self._msg(box, side, method, parts[3], body, wait)
            if len(parts) == 4 and parts[2] == "chunk":
                return self._chunk(box, side, method, _int(parts[3]), body, wait)
            return 404, {"error": "not_found"}

    def _rate(self, kind):
        n = self.rate_hits.get(kind, 0) + 1
        self.rate_hits[kind] = n
        return n <= self.rate

    def _msg(self, box, side, method, slot, body, wait):
        own = SEND_SLOTS if side == "send" else RECV_SLOTS
        if slot not in SEND_SLOTS | RECV_SLOTS:
            return 404, {"error": "not_found"}
        if method == "PUT":
            if slot not in own:
                return 403, {"error": "slot"}
            if len(body) > 4096:
                return 413, {"error": "too_big"}
            if slot in box.slots and box.slots[slot] != body:
                return 409, {"error": "taken"}
            box.slots[slot] = body
            self.cond.notify_all()
            return 204, b""
        if slot in own:
            return 403, {"error": "slot"}

        def check():
            if box.closed:
                return ("closed",)
            return box.slots.get(slot)
        got = self._wait(check, wait)
        if got is None:
            return 204, b""
        if got == ("closed",):
            return 410, {"error": box.closed}
        if self.hook_msg:
            got = self.hook_msg(slot, got)
        return 200, got

    def _chunk(self, box, side, method, index, body, wait):
        if method == "PUT":
            if side != "send":
                return 403, {"error": "side"}
            if index < box.next_put:
                return 204, b""          # a repeat after a network blip
            if index != box.next_put:
                return 409, {"error": "order"}
            if len(body) > self.chunk + 64:
                return 413, {"error": "too_big"}
            if box.bytes + len(body) > self.max_bytes:
                self._close(box, "cancel", self.now())
                return 413, {"error": "too_big"}
            room = self._wait(lambda: True if box.closed or len(box.chunks) < self.buffer
                              else None, wait)
            if box.closed:
                return 410, {"error": box.closed}
            if room is None:
                return 429, {"error": "full"}
            box.chunks[index] = body
            box.bytes += len(body)
            box.next_put += 1
            self.cond.notify_all()
            return 204, b""
        if side != "recv":
            return 403, {"error": "side"}
        if index <= box.acked:
            return 409, {"error": "order"}
        for i in [i for i in box.chunks if i < index]:
            del box.chunks[i]
        box.acked = index - 1
        self.cond.notify_all()

        def check():
            if box.closed:
                return ("closed",)
            return box.chunks.get(index)
        got = self._wait(check, wait)
        if got is None:
            return 204, b""
        if got == ("closed",):
            return 410, {"error": box.closed}
        if self.hook_chunk:
            got = self.hook_chunk(index, got)
            if got is None:
                return 204, b""
        return 200, got


def _int(text):
    try:
        return int(text)
    except ValueError:
        return -1
