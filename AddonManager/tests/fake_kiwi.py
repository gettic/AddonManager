"""A local fake of file.kiwi's documented v2 upload API, for tests only (nothing goes online).

POST /v2/folders -> a folder with presigned-style chunk URLs on this same server; PUT of each
chunk (signature checked); GET upload-status. `download(link)` does what their download page
does in the browser: it undoes their RFC 8188 layer with the key after # (the test then
checks the app's own layer separately). Hooks: `fail_puts` (list of statuses answered to the
next chunk uploads, e.g. [429, 500]), `fail_create` (status for every folder create, or a
function giving the status or None), `fail_put_when` (function(chunk number) giving a status
or None), `delay` (seconds per chunk upload). It records what the app did: `max_parallel`,
`agents`, `status_queries` (each with its query string: uploadAuth must never be there)."""

import base64
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from am import share_later


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FakeKiwi:
    def __init__(self, chunk_size=256 * 1024):
        self.chunk_size = chunk_size
        self.folders = {}            # id -> {"ske", "files": {fid: {...}}}
        self.fail_puts = []
        self.fail_create = None
        self.fail_put_when = None
        self.delay = 0.0
        self.parallel = 0
        self.max_parallel = 0
        self.agents = set()
        self.status_queries = []
        self.calls = []
        self.lock = threading.Lock()
        kiwi = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _reply(self, status, body=b""):
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):
                body = self._body()
                kiwi.agents.add(self.headers.get("User-Agent"))
                self._reply(*kiwi.create(self.path, body))

            def do_PUT(self):
                body = self._body()
                kiwi.agents.add(self.headers.get("User-Agent"))
                self._reply(*kiwi.put(self.path, body, self.headers))

            def do_GET(self):
                kiwi.agents.add(self.headers.get("User-Agent"))
                self._reply(*kiwi.status(self.path))

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ---------- the API ----------
    def create(self, path, body):
        with self.lock:
            self.calls.append(("POST", path))
            if path != "/v2/folders":
                return 404, {"error": "not_found"}
            fail = self.fail_create() if callable(self.fail_create) else self.fail_create
            if fail:
                return fail, {"error": "oops"}
            try:
                req = json.loads(body)
                ske = base64.b64decode(req["encryption"]["ske"], validate=True)
                files = req["files"]
                assert req.get("mode", "send") == "send" and len(files) == 1
                name = base64.b64decode(files[0]["filename"], validate=True)
                size = files[0]["filesize"]
                assert isinstance(size, int) and size > 0
            except Exception:
                return 400, {"error": "bad_request"}
            fid_folder = secrets.token_hex(4)
            fid = secrets.token_hex(6)
            chunks = -(-size // self.chunk_size)
            sigs = [secrets.token_hex(8) for _ in range(chunks)]
            self.folders[fid_folder] = {"ske": ske, "files": {fid: {
                "name": name, "size": size, "chunks": chunks, "sigs": sigs, "data": {}}}}
            return 200, {
                "folderId": fid_folder, "folderUrl": f"{self.url}/{fid_folder}",
                "mode": "send", "uploadAuth": secrets.token_hex(8),
                "files": [{"filename": files[0]["filename"], "fileId": fid,
                           "chunkSize": self.chunk_size, "chunks": chunks,
                           "freeDownloadHours": 24,
                           "uploadUrls": {"head": self.url, "path": f"/up/{fid_folder}/{fid}",
                                          "tail": "X-Amz-Algorithm=FAKE",
                                          "signatures": sigs,
                                          "headers": {"x-amz-meta-fake": "1"}}}]}

    def put(self, path, body, headers):
        url = urllib.parse.urlsplit(path)
        m = re.fullmatch(r"/up/([0-9a-f]+)/([0-9a-f]+)/([0-9]{5})", url.path)
        q = urllib.parse.parse_qs(url.query)
        with self.lock:
            self.calls.append(("PUT", url.path))
            self.parallel += 1
            self.max_parallel = max(self.max_parallel, self.parallel)
            fail = self.fail_puts.pop(0) if self.fail_puts else None
            if fail is None and self.fail_put_when and m:
                fail = self.fail_put_when(int(m.group(3)))
        try:
            if self.delay:
                time.sleep(self.delay)
            if fail:
                return fail, {"error": "fake"}
            if not m or headers.get("x-amz-meta-fake") != "1":
                return 403, {"error": "signature"}
            f = self.folders.get(m.group(1), {}).get("files", {}).get(m.group(2))
            n = int(m.group(3))
            if (f is None or not 1 <= n <= f["chunks"]
                    or q.get("X-Amz-Signature") != [f["sigs"][n - 1]]
                    or q.get("X-Amz-Algorithm") != ["FAKE"]):
                return 403, {"error": "signature"}
            with self.lock:
                f["data"][n] = bytes(body)
            return 200, b""
        finally:
            with self.lock:
                self.parallel -= 1

    def status(self, path):
        url = urllib.parse.urlsplit(path)
        m = re.fullmatch(r"/v2/folders/([0-9a-f]+)/files/([0-9a-f]+)/upload-status", url.path)
        with self.lock:
            self.calls.append(("GET", url.path))
            self.status_queries.append(url.query)
            f = self.folders.get(m.group(1), {}).get("files", {}).get(m.group(2)) if m else None
            if f is None:
                return 404, {"error": "not_found"}
            missing = [n for n in range(1, f["chunks"] + 1) if n not in f["data"]]
            return 200, {"complete": not missing, "missing": missing}

    # ---------- what the browser does ----------
    def download(self, link):
        """(file name, plain bytes) from a link, like their download page."""
        p = urllib.parse.urlsplit(link)
        folder = self.folders[p.path.strip("/")]
        key = share_later.b64url_decode(p.fragment)
        assert share_later.ece_decrypt(key, folder["ske"]) == p.fragment.encode()
        (f,) = folder["files"].values()
        name = share_later.ece_decrypt(key, f["name"]).decode()
        data = b"".join(share_later.ece_decrypt(key, f["data"][n])
                        for n in range(1, f["chunks"] + 1))
        assert len(data) == f["size"]
        return name, data
