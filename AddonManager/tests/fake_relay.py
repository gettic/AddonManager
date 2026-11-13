"""A local fake of the send-code relay (share_worker\\worker.js), for tests only.

Pure Python (ThreadingHTTPServer on 127.0.0.1, a free port, in a daemon thread). It keeps
the same rules as the Worker: one join per code (the same join id gets the same answer),
write-once slots owned by one side, at most BUFFER chunks waiting, chunks in order, size cap,
code expiry / idle limit with a clock tests can move, close -> everything deleted and a short
"closed" note left. Tests can make it misbehave through `hook_chunk` (change or drop a chunk
on its way to the receiver) and `hook_msg` (change a message).

Send later (worker.js "Send later"): later / ping progress / record / later state / take, with
the same one-try rule; `later_idle`, `later_upload` and `later_max_hours` as the Worker's vars.

Guild storage (worker.js StoreLedger / StoreSlice): /v1/<ch>/store, /v1/s/<id>/<slice>/<n>,
DELETE /v1/s/<id>, with the same caps, token hashes, order and deadlines (`store_*` as the
Worker's STORE_* vars; `store=False` = an older Worker without it, `store="off"` = STORE_ON 0). `stored_store()` = what it
still holds; hooks `fail_store_put` / `fail_store_get` (that many 500s) and `hook_store_get`."""

import base64
import hashlib
import json
import random
import re
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
        self.last = self.send_last = now
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
        self.later = None       # send later: hours, salt, verifier, state, started, until, record
        self.later_p = 0        # upload progress (per mille), memory only
        self.store = None       # guild storage id (worker.js m.store)
        self.store_end = None

    def later_watch(self):
        return self.later["watch"] if self.later else None


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # a PC that hangs up mid-transfer (Cancel tests) is normal here


class FakeRelay:
    def __init__(self, ttl=600, idle=120, max_time=3600, buffer=4, max_bytes=None,
                 channels=499, rate=1000, chunk=1024 * 1024, later_idle=300,
                 later_upload=21600, later_max_hours=24, store=True, store_piece=64 * 1024,
                 store_slice=256 * 1024, store_max_send=4_000_000_000,
                 store_live_cap=4_400_000_000, store_day_cap=20_000_000_000,
                 store_max_seconds=86400, store_download_seconds=21600, store_rate=1000,
                 store_start_seconds=300):
        self.store_start_seconds = store_start_seconds
        self.store_on = store
        self.store_piece, self.store_slice = store_piece, store_slice
        self.store_max_send, self.store_live_cap = store_max_send, store_live_cap
        self.store_day_cap, self.store_max_seconds = store_day_cap, store_max_seconds
        self.store_download_seconds, self.store_rate = store_download_seconds, store_rate
        self.ledger = {}                # id -> entry (worker.js StoreLedger + its slices)
        self.store_day = 0              # bytes reserved "today" (the fake has one day)
        self.fail_store_put = 0         # this many piece PUTs answered 500 first
        self.fail_store_get = 0         # this many piece GETs answered 500 first
        self.hook_store_get = None      # f(slice, n, data) -> bytes
        self.ttl, self.idle, self.max_time, self.buffer = ttl, idle, max_time, buffer
        self.max_bytes = max_bytes if max_bytes is not None else 520 * 1024 * 1024
        self.channels, self.rate, self.chunk = channels, rate, chunk
        self.later_idle, self.later_upload = later_idle, later_upload
        self.later_max_hours = later_max_hours
        self.offset = 0.0               # tests move the clock with advance()
        self.boxes = {}
        self.calls = []                 # (method, path) of every request
        self.rate_hits = {}
        self.hook_chunk = None          # f(index, box) -> bytes or None (= never arrives)
        self.hook_msg = None            # f(slot, data) -> bytes
        self.hook_take = None           # f() called (lock let go) after a /take claims the record
        self.fail_next = 0              # this many requests answered 500 first
        self.cond = threading.Condition()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # keep-alive, like Cloudflare

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
                out = relay.handle(method, self.path, body, self.headers.get("X-Token"),
                                   self.headers.get("X-Store-Token"))
                self._reply(out[0], out[1])

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
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
    def _later_deadline(self, box):
        if box.later["state"] == "ready":
            return box.later["until"]
        return min(box.send_last + self.later_idle, box.later["started"] + self.later_upload)

    def _deadline(self, box):
        return min(box.last + self.idle, box.joined_at + self.max_time)

    def _expire(self, box, now):
        if box.closed:
            if now - box.closed_at > 300:
                self.boxes.pop(box.ch, None)
            return
        if box.later:
            if now > self._later_deadline(box):
                self._close(box, "expired", now)
        elif box.recv_token is None:
            if now - box.created > box.ttl:
                self._close(box, "expired", now)
        elif now > self._deadline(box):
            self._close(box, "expired", now)

    def _close(self, box, reason, now):
        store = None if box.closed else box.store
        taken = bool(store and reason == "done" and box.later
                     and box.later.get("store") == store)
        box.store = None
        if store:
            entry = self.ledger.get(store)
            if entry and taken:
                entry["deadline"] = min(entry["deadline"], now + self.store_download_seconds)
            elif entry:
                self._drop_store(store)
        box.closed, box.closed_at = reason, now
        box.later = None
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
                    if b.slots or b.chunks or b.later}

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

    def handle(self, method, path, body, token, store_token=None):
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
            self._sweep(now)
            m = re.fullmatch(r"/v1/s/([0-9a-f]{32})(?:/([0-9])/([0-9]{1,4}))?", url.path)
            if m:
                return self._store_route(method, m, body, store_token or "", now)
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
                if box.later:
                    caps = _caps(body)
                    if b"later" not in caps or (box.later.get("store") and b"store" not in caps):
                        return 410, {"error": "later"}
                    return 200, {"later": True, "token": box.later["watch"]}
                jid = hashlib.sha256(body).hexdigest()
                if box.recv_token is not None:
                    if box.jid == jid:
                        return 200, {"token": box.recv_token}
                    return 409, {"error": "used"}
                box.recv_token, box.jid, box.joined_at, box.last = (
                    secrets.token_hex(16), jid, now, now)
                box.send_last = now
                return 200, {"token": box.recv_token}
            if len(parts) < 3:
                return 404, {"error": "not_found"}
            box = self._box(_int(parts[1]))
            if box is None:
                return 404, {"error": "not_found"}
            if box.closed:
                return 410, {"error": box.closed}
            if parts[2:] == ["later"] and method == "GET":
                if token != box.later_watch():
                    return 403, {"error": "token"}
                return self._later_state(box, wait)
            if parts[2:] == ["take"] and method == "POST":
                if not self._rate("join"):
                    return 429, {"error": "rate"}
                return self._take(box, body, now)
            side = ("send" if token == box.send_token else
                    "recv" if token and token == box.recv_token else None)
            if side is None:
                return 403, {"error": "token"}
            box.last = now
            if side == "send":
                box.send_last = now
            if parts[2] == "close" and method == "POST":
                try:
                    reason = json.loads(body.decode()).get("reason")
                except Exception:
                    reason = None
                self._close(box, reason if reason in REASONS else "cancel", now)
                return 204, b""
            if parts[2:] == ["ping"] and method == "POST":
                if box.later and side == "send":
                    try:
                        p = json.loads(body.decode()[:100]).get("p")
                        if isinstance(p, int) and 0 <= p <= 1000:
                            box.later_p = p
                    except Exception:
                        pass
                    self.cond.notify_all()
                return 204, b""
            if parts[2:] == ["later"] and method == "POST":
                return self._start_later(box, side, body, now)
            if parts[2:] == ["store"] and method in ("POST", "DELETE") and self.store_on:
                return self._store_open(box, side, method, body, now)
            if parts[2:] == ["record"] and method == "PUT":
                return self._put_record(box, side, body, now)
            if len(parts) == 4 and parts[2] == "msg":
                return self._msg(box, side, method, parts[3], body, wait)
            if len(parts) == 4 and parts[2] == "chunk":
                return self._chunk(box, side, method, _int(parts[3]), body, wait)
            return 404, {"error": "not_found"}

    def _rate(self, kind, most=None):
        n = self.rate_hits.get(kind, 0) + 1
        self.rate_hits[kind] = n
        return n <= (self.rate if most is None else most)

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

    # ---------- send later ----------
    def _start_later(self, box, side, body, now):
        if side != "send":
            return 403, {"error": "side"}
        if box.recv_token is not None:
            return 409, {"error": "joined"}
        try:
            req = json.loads(body.decode()[:400])
            hours, salt, verifier = req["hours"], req["salt"], req["verifier"]
            store = req.get("store") or None
        except Exception:
            return 400, {"error": "bad"}
        if (not isinstance(hours, int) or isinstance(hours, bool)
                or not 1 <= hours <= self.later_max_hours
                or not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{32}", salt)
                or not isinstance(verifier, str) or not re.fullmatch(r"[0-9a-f]{64}", verifier)
                or (store is not None and store != box.store)):
            return 400, {"error": "bad"}
        if box.later:
            same = box.later["salt"] == salt and box.later["verifier"] == verifier
            return (204, b"") if same else (409, {"error": "taken"})
        box.later = {"hours": hours, "salt": salt, "verifier": verifier, "state": "uploading",
                     "started": now, "until": None, "record": None,
                     "watch": secrets.token_hex(16), "store": store}
        box.later_p = 0
        self.cond.notify_all()
        return 204, b""

    def _put_record(self, box, side, body, now):
        if side != "send":
            return 403, {"error": "side"}
        if not box.later:
            return 409, {"error": "order"}
        if not 28 <= len(body) <= 4096:
            return 413, {"error": "too_big"}
        rec = base64.b64encode(body).decode()
        if box.later["state"] == "ready":
            if box.later["record"] != rec:
                return 409, {"error": "taken"}
            return 200, {"left": round(box.later["until"] - now)}
        until = now + box.later["hours"] * 3600
        if box.later.get("store") and box.store_end:   # as worker.js: STORE_MARGIN
            until = min(until, box.store_end - 7200)
        if until <= now:
            self._close(box, "expired", now)
            return 410, {"error": "expired"}
        box.later.update(state="ready", record=rec, until=until)
        self.cond.notify_all()
        return 200, {"left": round(until - now)}

    def _later_state(self, box, wait):
        if not box.later:
            return 404, {"error": "not_found"}
        start, p = box.later["state"], box.later_p

        def check():
            if box.closed or not box.later or box.later["state"] != start or box.later_p != p:
                return True
            return None
        if start == "uploading":   # uploaded already: answer at once (as worker.js)
            self._wait(check, wait)
        if box.closed or not box.later:
            return 410, {"error": box.closed or "cancel"}
        until = box.later["until"]
        state = "ready" if box.later["state"] == "taking" else box.later["state"]
        return 200, {"state": state, "salt": box.later["salt"], "p": box.later_p,
                     "left": max(0, round(until - self.now())) if until else None}

    def _take(self, box, body, now):
        if not box.later:
            return 404, {"error": "not_found"}
        if box.later["state"] == "taking":
            return 409, {"error": "taking"}
        if box.later["state"] != "ready":
            return 409, {"error": "uploading"}
        box.later["state"] = "taking"   # claimed first, as the Worker: one guess per record
        if self.hook_take:
            self.cond.release()
            try:
                self.hook_take()
            finally:
                self.cond.acquire()
        try:
            verifier = json.loads(body.decode()[:200])["verifier"]
        except Exception:
            verifier = None
        if not isinstance(verifier, str) or not re.fullmatch(r"[0-9a-f]{64}", verifier):
            self._close(box, "bad_code", now)
            return 400, {"error": "bad"}
        if verifier != box.later["verifier"]:   # one try per code
            self._close(box, "bad_code", now)
            return 403, {"error": "bad_code"}
        record = box.later["record"]
        self._close(box, "done", now)
        return 200, {"record": record}

    # ---------- guild storage (worker.js StoreLedger / StoreSlice) ----------
    def stored_store(self):
        """(sends held, bytes held) in the guild storage."""
        with self.cond:
            return (len(self.ledger),
                    sum(len(p) for e in self.ledger.values() for p in e["pieces"].values()))

    def _sweep(self, now):
        """The alarms: mailboxes and stored sends past their end go."""
        for box in list(self.boxes.values()):
            self._expire(box, now)
        for sid, e in list(self.ledger.items()):
            if e["deadline"] <= now or (
                    not e["started"] and e["opened"] + self.store_start_seconds <= now):
                self._drop_store(sid)

    def _drop_store(self, sid):
        """Gone; today's count keeps only what was really stored (as worker.js)."""
        e = self.ledger.pop(sid, None)
        if e:
            stored = sum(len(p) for p in e["pieces"].values())
            self.store_day = max(0, self.store_day - (e["size"] - stored))

    def _store_open(self, box, side, method, body, now):
        if side != "send":
            return 403, {"error": "side"}
        if method == "DELETE":
            sid, box.store = box.store, None
            if box.later:
                box.later["store"] = None
            if sid:
                self._drop_store(sid)
            return 204, b""
        if not self._rate("store", self.store_rate):
            return 429, {"error": "rate"}
        if box.recv_token is not None:
            return 409, {"error": "joined"}
        if box.later:
            return 409, {"error": "order"}
        try:
            req = json.loads(body.decode()[:400])
            size, put, get = req["blob"], req["put"], req["get"]
        except Exception:
            return 400, {"error": "bad"}
        if (not isinstance(size, int) or isinstance(size, bool) or size < 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(put))
                or not re.fullmatch(r"[0-9a-f]{64}", str(get)) or put == get):
            return 400, {"error": "bad"}
        if box.store:   # a repeat: the first one goes
            self._drop_store(box.store)
            box.store = None
        if self.store_on == "off":
            return 503, {"error": "busy"}
        piece = self.store_piece
        slice_ = max(piece, self.store_slice // piece * piece)
        slices = -(-size // slice_)
        if size > self.store_max_send or slices > 10:
            return 413, {"error": "too_big"}
        live = sum(e["size"] for e in self.ledger.values())
        if live + size > self.store_live_cap or self.store_day + size > self.store_day_cap:
            return 503, {"error": "busy"}
        sid = secrets.token_hex(16)
        end = now + self.store_max_seconds
        self.ledger[sid] = {"size": size, "piece": piece, "slice": slice_, "put": put,
                            "get": get, "deadline": end, "pieces": {}, "next": {},
                            "opened": now, "started": False}
        self.store_day += size
        box.store, box.store_end = sid, end
        return 200, {"id": sid, "piece": piece, "slice": slice_}

    def _store_route(self, method, m, body, token, now):
        if not self.store_on:
            return 404, {"error": "not_found"}
        sid, k, n = m.group(1), m.group(2), m.group(3)
        entry = self.ledger.get(sid)
        h = hashlib.sha256(token.encode()).hexdigest()
        if k is None:
            if method != "DELETE" or entry is None:
                return 404, {"error": "not_found"}
            if h not in (entry["put"], entry["get"]):
                return 403, {"error": "token"}
            self._drop_store(sid)
            return 204, b""
        if len(body) > self.store_piece + 64:
            return 413, {"error": "too_big"}
        if entry is None:
            return 404, {"error": "not_found"}
        k, n = int(k), int(n)
        size, piece, slice_ = entry["size"], entry["piece"], entry["slice"]
        slices = -(-size // slice_)
        if k >= slices:
            return 404, {"error": "not_found"}
        sbytes = min(slice_, size - k * slice_)
        pieces = -(-sbytes // piece)
        if n >= pieces:
            return 404, {"error": "not_found"}
        nxt = entry["next"].get(k, 0)
        if method == "PUT":
            if h != entry["put"]:
                return 403, {"error": "token"}
            if self.store_on == "off":   # STORE_ON 0: no more uploads
                return 410, {"error": "off"}
            if self.fail_store_put > 0:
                self.fail_store_put -= 1
                return 500, {"error": "oops"}
            want = piece if n < pieces - 1 else sbytes - (pieces - 1) * piece
            if len(body) != want:
                return 400, {"error": "length"}
            if n < nxt:
                return 204, b""
            if n != nxt:
                return 409, {"error": "order"}
            entry["pieces"][(k, n)] = body
            entry["next"][k] = n + 1
            entry["started"] = True
            return 204, b""
        if method == "GET":
            if h != entry["get"]:
                return 403, {"error": "token"}
            if self.fail_store_get > 0:
                self.fail_store_get -= 1
                return 500, {"error": "oops"}
            if n >= nxt:
                return 409, {"error": "order"}
            data = entry["pieces"][(k, n)]
            if self.hook_store_get:
                data = self.hook_store_get(k, n, data)
            return 200, data
        return 404, {"error": "not_found"}


def _caps(body):
    """What the receiving app can do, from its join body (as worker.js joinCaps)."""
    m = re.search(rb"\|caps=([a-z0-9,]{1,30})$", body)
    return m.group(1).split(b",") if m else []


def _int(text):
    try:
        return int(text)
    except ValueError:
        return -1
