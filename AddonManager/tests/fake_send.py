"""A local fake of a Send server (timvisee/send), for tests only (nothing goes online).

Same rules as Send's source (server/routes/ws.js, middleware/auth.js, routes/download.js,
routes/delete.js, clientConstants.js): GET /config (LIMITS); the WebSocket upload on
/api/ws (first text message = JSON with fileMetadata, authorization, timeLimit, dlimit;
answers {url, ownerToken, id}; then binary messages until one of exactly b"\\x00"; then
{ok: true}; a close before the end keeps nothing; over encryptedSize(MAX_FILE_SIZE) =
{error: 413}); GET /api/download/<id> with HMAC auth over a nonce that changes after each
good request (401 + WWW-Authenticate otherwise), counted when the whole file was written,
deleted at dlimit; POST /api/delete/<id> with owner_token; files end after timeLimit on
`now` (move it with `advance`).

Hooks: `handshake` (list of (status, retry_after) answered to the next upgrades),
`ws_error` (list of error codes sent instead of {ok:true}), `drop_after` (close the socket
after this many binary messages, once), `cut_download` (bytes, once), `tamper` (flip one
byte of the next download), `limits` (dict), `ok_delay` (seconds before storing + "ok"). Records: `uploads`, `deletes`, `downloads`,
`agents`, `max_parallel`, `asked` (timeLimit, dlimit of each upload), `calls`."""

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from am import share_later, share_send


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def encrypted_size(n):
    return share_later.ece_size(n)


class FakeSend:
    def __init__(self, max_file=8 * 1024 ** 3, max_downloads=100, max_expire=7 * 86400):
        self.limits = {"MAX_FILE_SIZE": max_file, "MAX_DOWNLOADS": max_downloads,
                       "MAX_EXPIRE_SECONDS": max_expire, "MAX_FILES_PER_ARCHIVE": 64,
                       "MAX_ARCHIVES_PER_USER": 16}
        self.files = {}          # id -> {owner, metadata, auth, nonce, dlimit, dl, data, end}
        self.now = 0.0
        self.handshake = []
        self.ws_error = []
        self.drop_after = None
        self.cut_download = None
        self.tamper = False
        self.config_status = 200
        self.ok_delay = 0.0
        self.uploads = []        # ids of finished uploads
        self.deletes = []        # (id, status)
        self.downloads = []      # (id, status)
        self.agents = set()
        self.asked = []          # (timeLimit, dlimit)
        self.metadata = []       # fileMetadata strings as sent
        self.calls = []
        self.parallel = 0
        self.max_parallel = 0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _reply(self, status, body=b"", headers=None):
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.agents.add(self.headers.get("User-Agent"))
                fake.calls.append(("GET", self.path))
                if self.path == "/config":
                    return self._reply(fake.config_status, {"LIMITS": fake.limits,
                                                           "DEFAULTS": {"DOWNLOADS": 1}})
                if self.path == "/api/ws":
                    return fake._ws(self)
                m = re.fullmatch(r"/api/download/([0-9a-f]{10,16})", self.path)
                if m:
                    return fake._download(self, m.group(1))
                self._reply(404)

            def do_POST(self):
                fake.agents.add(self.headers.get("User-Agent"))
                fake.calls.append(("POST", self.path))
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                m = re.fullmatch(r"/api/delete/([0-9a-f]{10,16})", self.path)
                if not m:
                    return self._reply(404)
                fid = m.group(1)
                try:
                    token = json.loads(body)["owner_token"]
                except (ValueError, KeyError, TypeError):
                    token = None
                with fake.lock:
                    f = fake._file(fid)
                    if f is None:
                        status = 404
                    elif not isinstance(token, str) or not hmac.compare_digest(
                            token.encode(), f["owner"].encode()):
                        status = 401
                    else:
                        del fake.files[fid]
                        status = 200
                    fake.deletes.append((fid, status))
                self._reply(status, b"OK" if status == 200 else b"")

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def advance(self, seconds):
        self.now += seconds

    def _file(self, fid):
        f = self.files.get(fid)
        if f is not None and self.now >= f["end"]:
            del self.files[fid]
            return None
        return f

    def stored(self):
        with self.lock:
            return {k: v for k, v in self.files.items() if self.now < v["end"]}

    # ---------- the WebSocket upload ----------
    def _ws(self, h):
        if self.handshake:
            status, wait = self.handshake.pop(0)
            return h._reply(status, b"busy", {"Retry-After": str(wait)} if wait is not None
                            else None)
        key = h.headers.get("Sec-WebSocket-Key", "")
        if (h.headers.get("Upgrade", "").lower() != "websocket"
                or h.headers.get("Sec-WebSocket-Version") != "13" or not key):
            return h._reply(400)
        accept = base64.b64encode(hashlib.sha1(key.encode() + share_send.WS_GUID).digest())
        h.send_response(101)
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", accept.decode())
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        with self.lock:
            self.parallel += 1
            self.max_parallel = max(self.max_parallel, self.parallel)
        try:
            self._ws_session(h.rfile, h.connection)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            with self.lock:
                self.parallel -= 1

    @staticmethod
    def _read(rfile, n):
        data = rfile.read(n)
        if len(data) != n:
            raise ConnectionError("closed")
        return data

    def _frame_in(self, rfile):
        b0, b1 = self._read(rfile, 2)
        if not b1 & 0x80:
            raise ValueError("client frames must be masked")
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(rfile, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(rfile, 8))[0]
        key = self._read(rfile, 4)
        data = self._read(rfile, n)
        return b0 & 0x0F, share_send.mask(data, key)

    @staticmethod
    def _send(conn, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        conn.sendall(head + payload)

    def _json_out(self, conn, obj):
        self._send(conn, 1, json.dumps(obj).encode())

    def _ws_session(self, rfile, conn):
        op, msg = self._frame_in(rfile)
        if op != 1:
            return
        info = json.loads(msg)
        time_limit = info.get("timeLimit") or 86400
        dlimit = info.get("dlimit") or 1
        meta, auth = info.get("fileMetadata"), info.get("authorization")
        self.asked.append((time_limit, dlimit))
        self.metadata.append(meta)
        if (not meta or not auth or time_limit <= 0
                or time_limit > self.limits["MAX_EXPIRE_SECONDS"]
                or dlimit > self.limits["MAX_DOWNLOADS"]):
            self._json_out(conn, {"error": 400})
            self._send(conn, 8, b"")
            return
        fid, owner = os.urandom(8).hex(), os.urandom(10).hex()
        self._json_out(conn, {"url": f"{self.url}/download/{fid}/", "ownerToken": owner,
                              "id": fid})
        data, count = bytearray(), 0
        cap = encrypted_size(self.limits["MAX_FILE_SIZE"])
        while True:
            op, msg = self._frame_in(rfile)
            if op == 8:
                return                                   # closed early: nothing kept
            if op != 2:
                continue
            if len(msg) == 1 and msg[0] == 0:
                break
            count += 1
            if self.drop_after is not None and count > self.drop_after:
                self.drop_after = None
                conn.shutdown(socket.SHUT_RDWR)
                return
            data += msg
            if len(data) > cap:
                self._json_out(conn, {"error": 413})
                return
        if self.ws_error:
            self._json_out(conn, {"error": self.ws_error.pop(0)})
            return
        if self.ok_delay:   # storing takes a while (Send keeps it even if the client left)
            import time
            time.sleep(self.ok_delay)
        with self.lock:
            self.files[fid] = {"owner": owner, "metadata": meta, "auth": auth.split(" ")[1],
                               "nonce": base64.b64encode(os.urandom(16)).decode(),
                               "dlimit": dlimit, "dl": 0, "data": bytes(data),
                               "end": self.now + time_limit}
            self.uploads.append(fid)
        self._json_out(conn, {"ok": True})
        self._send(conn, 8, struct.pack(">H", 1000))

    # ---------- download ----------
    def _download(self, h, fid):
        with self.lock:
            f = self._file(fid)
            if f is None:
                self.downloads.append((fid, 404))
                return h._reply(404)
            token = (h.headers.get("Authorization") or " ").split(" ")[1]
            want = hmac.new(share_send.b64any(f["auth"]), share_send.b64any(f["nonce"]),
                            hashlib.sha256).digest()
            try:
                ok = hmac.compare_digest(want, share_send.b64any(token))
            except ValueError:
                ok = False
            if not ok:
                self.downloads.append((fid, 401))
                return h._reply(401, b"", {"WWW-Authenticate": f"send-v1 {f['nonce']}"})
            f["nonce"] = base64.b64encode(os.urandom(16)).decode()
            data = f["data"]
            self.downloads.append((fid, 200))
        if self.tamper:
            self.tamper = False
            data = data[:100] + bytes([data[100] ^ 1]) + data[101:]
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("WWW-Authenticate", f"send-v1 {f['nonce']}")
        h.end_headers()
        if self.cut_download is not None:
            cut, self.cut_download = self.cut_download, None
            h.wfile.write(data[:cut])
            h.wfile.flush()
            h.close_connection = True
            h.connection.shutdown(socket.SHUT_RDWR)
            return                                      # aborted: not counted
        h.wfile.write(data)
        with self.lock:
            f["dl"] += 1
            if f["dl"] >= f["dlimit"]:
                self.files.pop(fid, None)
