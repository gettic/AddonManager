"""Send later through a Send server (am\\share_send.py) against the local fake
(tests\\fake_send.py); the relay is tests\\fake_relay.py, file.kiwi tests\\fake_kiwi.py.
Temp folders only; nothing goes online."""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import threading
import unittest
from unittest import mock

from am import errors, share, share_later, share_send
from am.share import ShareError
from am.share_send import SendError
from tests import REAL_SEND_URLS, fixtures
from tests.fake_kiwi import FakeKiwi
from tests.fake_relay import FakeRelay
from tests.fake_send import FakeSend
from tests.test_share import LaterSender, Sender, big_backup, fast, same_entries


class Blob:
    """Bytes standing in for share_later.Blob."""

    def __init__(self, data):
        self.data, self.total = data, len(data)

    def read(self, a, b):
        return self.data[a:b]


# ---------- the WebSocket client on its own ----------
class WebSocketTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return share_send.WebSocket(a), b

    @staticmethod
    def server_frame(opcode, payload, fin=True, rsv=0, masked=False):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | rsv | opcode])
        m = 0x80 if masked else 0
        if n < 126:
            head += bytes([m | n])
        elif n < 65536:
            head += bytes([m | 126]) + struct.pack(">H", n)
        else:
            head += bytes([m | 127]) + struct.pack(">Q", n)
        return head + (b"\0\0\0\0" if masked else b"") + payload

    def test_accept_key_rfc6455_example(self):
        self.assertEqual(share_send.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_masking_and_lengths(self):
        self.assertEqual(share_send.mask(b"Hello", bytes([0x37, 0xfa, 0x21, 0x3d])),
                         bytes([0x7f, 0x9f, 0x4d, 0x51, 0x58]))   # RFC 6455 5.7
        for n, marker, extra in ((5, 5, 0), (200, 126, 2), (70000, 127, 8)):
            data = os.urandom(n)
            f = share_send.frame(2, data, b"abcd")
            self.assertEqual(f[0], 0x82)
            self.assertEqual(f[1], 0x80 | marker)
            body = f[2 + extra + 4:]
            self.assertEqual(share_send.mask(body, b"abcd"), data)

    def test_ping_fragments_and_refusals(self):
        ws, srv = self.pair()
        srv.sendall(self.server_frame(9, b"hi") + self.server_frame(1, b'{"a"', fin=False)
                    + self.server_frame(0, b": 1}"))
        self.assertEqual(ws.recv_json(), {"a": 1})
        pong = srv.recv(100)
        self.assertEqual(pong[0], 0x8A)                       # answered the ping
        for bad in (self.server_frame(1, b"{}", masked=True), self.server_frame(1, b"{}", rsv=0x40),
                    self.server_frame(0, b"{}"), self.server_frame(1, b"[1]"),
                    self.server_frame(1, b"x" * 70000)):
            ws, srv = self.pair()
            srv.sendall(bad)
            with self.assertRaises(SendError) as cm:
                ws.recv_json()
            self.assertEqual(cm.exception.kind, "broken")
        ws, srv = self.pair()
        srv.sendall(self.server_frame(8, b""))
        with self.assertRaises(SendError) as cm:
            ws.recv_json()
        self.assertEqual(cm.exception.kind, "retry")

    def test_control_frames_and_deadline(self):           # security review #5
        for bad in (self.server_frame(9, b"x" * 126), self.server_frame(9, b"hi", fin=False),
                    self.server_frame(8, b"x" * 200)):
            ws, srv = self.pair()
            srv.sendall(bad)
            with self.assertRaises(SendError) as cm:
                ws.recv_json()
            self.assertEqual(cm.exception.kind, "broken")
        ws, srv = self.pair()                                 # only pings, forever
        stop_pinging = threading.Event()

        def pings():
            while not stop_pinging.is_set():
                try:
                    srv.sendall(self.server_frame(9, b"p"))
                    srv.recv(4096)
                except OSError:
                    return
        threading.Thread(target=pings, daemon=True).start()
        self.addCleanup(stop_pinging.set)
        t = __import__("time").monotonic()
        with self.assertRaises(SendError) as cm:
            ws.recv_json(timeout=1)
        self.assertEqual(cm.exception.kind, "retry")
        self.assertLess(__import__("time").monotonic() - t, 5)
        ws, srv = self.pair()                                 # silent: Cancel ends the wait
        with self.assertRaises(SendError) as cm:
            ws.recv_json(timeout=60, stop=lambda: True)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertEqual(str(SendError("retry", "a\nb" + "x" * 500)).count("\n"), 0)
        self.assertLessEqual(len(SendError("retry", "x" * 500).detail), share_send.DETAIL_MAX)
        with self.assertRaises(SendError) as cm:              # #4: never server text
            share_send.Send._answer_error({"error": "evil\ntext"})
        self.assertEqual(cm.exception.detail, "error ?")

    def test_handshake_checks(self):
        def answer(lines):
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            self.addCleanup(srv.close)

            def run():
                c, _a = srv.accept()
                with c:
                    req = c.recv(4096).decode()
                    key = [ln.split(": ")[1] for ln in req.split("\r\n")
                           if ln.startswith("Sec-WebSocket-Key")][0]
                    c.sendall(lines(key).encode())
            threading.Thread(target=run, daemon=True).start()
            return f"http://127.0.0.1:{srv.getsockname()[1]}/api/ws"

        good = lambda k: ("HTTP/1.1 101 Switching\r\nUpgrade: websocket\r\nConnection: Upgrade"
                          f"\r\nSec-WebSocket-Accept: {share_send.accept_key(k)}\r\n\r\n")
        share_send.WebSocket.connect(answer(good), 5, allow_local=True).close()
        for lines, kind in (
                (lambda k: "HTTP/1.1 101 x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Accept: wrong\r\n\r\n", "broken"),
                (lambda k: good(k)[:-2] + "Sec-WebSocket-Extensions: x\r\n\r\n", "broken"),
                (lambda k: "HTTP/1.1 429 x\r\nRetry-After: 7\r\n\r\n", "retry"),
                (lambda k: "HTTP/1.1 403 x\r\n\r\n", "refused")):
            with self.assertRaises(SendError) as cm:
                share_send.WebSocket.connect(answer(lines), 5, allow_local=True)
            self.assertEqual(cm.exception.kind, kind)
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(SendError):                    # plain http only for tests
            share_send.WebSocket.connect("http://example.org/api/ws")


# ---------- Send's layer and keychain ----------
class LayerTests(unittest.TestCase):
    def test_stream_matches_ece_and_reads_back(self):
        key, salt = os.urandom(16), os.urandom(16)
        for n in (1, 1000, share_send.DATA, share_send.DATA + 1, 5 * share_send.DATA + 77):
            data = os.urandom(n)
            frames = list(share_send.ece_frames(lambda a, b: data[a:b], n, key, salt,
                                                block_records=2))
            stream = b"".join(frames)
            self.assertEqual(stream, share_later.ece_encrypt(key, data, salt))
            self.assertEqual(len(stream), share_later.ece_size(n))
            self.assertTrue(all(len(f) > 1 for f in frames))   # never the end byte
            reader, out = share_send.EceReader(key, n), b""
            for i in range(0, len(stream), 7777):             # any piece sizes
                out += reader.feed(stream[i:i + 7777])
            reader.finish()
            self.assertEqual(out, data)

    def test_reader_refuses_damage(self):
        key, data = os.urandom(16), os.urandom(3 * share_send.DATA)
        stream = share_later.ece_encrypt(key, data)
        for why, bad, at_finish in (
                ("changed", stream[:500] + bytes([stream[500] ^ 1]) + stream[501:], False),
                ("cut", stream[:-10], True), ("longer", stream + b"x", False),
                ("wrong key", share_later.ece_encrypt(os.urandom(16), data), False)):
            with self.subTest(why):
                reader = share_send.EceReader(key, len(data))
                with self.assertRaises(ValueError):
                    reader.feed(bad)
                    if at_finish:
                        reader.finish()

    def test_keychain_and_metadata(self):
        secret = os.urandom(16)
        key = share_send.auth_key(secret)
        self.assertEqual(len(key), 64)
        nonce = base64.b64encode(b"n" * 16).decode()
        sig = share_send.b64any(share_send.auth_header(key, nonce).split(" ")[1])
        import hmac as h
        self.assertEqual(sig, h.new(key, b"n" * 16, hashlib.sha256).digest())
        meta = share_send.open_metadata(secret, share_send.seal_metadata(secret, "ab12cd34.amsend", 9))
        self.assertEqual((meta["name"], meta["size"], meta["type"]),
                         ("ab12cd34.amsend", 9, "application/octet-stream"))
        self.assertNotIn("=", share_send.seal_metadata(secret, "x", 1))     # base64url

    def test_record_checks(self):
        p = share_send.Part("0123456789abcdef", "a" * 20, b"k" * 16, 0, 50).record()
        q = dict(p, start=50, end=100)
        self.assertTrue(share_send.check_parts([p, q], 100))
        for bad, blob in (([], 100), ([p], 100), ([q], 100), ([p, q], 101),
                          ([dict(p, id="XYZ")], 50), ([dict(p, owner="a")], 50),
                          ([dict(p, secret="!" * 22)], 50), ([dict(p, url="x")], 50),
                          ([dict(p, start=True)], 50), ([p] * 9, 50),
                          ([dict(p, end=0)], 0)):
            self.assertFalse(share_send.check_parts(bad, blob), (bad, blob))
        # eight parts stay well under the relay's 4096-byte record cap
        parts = [dict(p, start=i * 10, end=i * 10 + 10) for i in range(8)]
        rec = {"v": 3, "host": "send", "site": "adminforge", "parts": parts, "key": "0" * 64,
               "size": 1, "sha256": "0" * 64, "blob": 80, "name": "ab12cd34.amsend"}
        key = os.urandom(32)
        sealed = share_later.seal_record(key, 5, rec)
        self.assertLess(len(sealed), 2500)
        self.assertEqual(share_later.open_record(key, 5, sealed), rec)
        for bad in (dict(rec, site="elsewhere"), dict(rec, host="kiwi"),
                    dict(rec, link="https://file.kiwi/x#y"), dict(rec, blob=81)):
            with self.assertRaises(ValueError):
                share_later.open_record(key, 5, share_later.seal_record(key, 5, bad))
        self.assertEqual(share_later.open_record(                    # send.vis.ee: 3 parts
            key, 5, share_later.seal_record(key, 5, dict(rec, site="visee")))["site"], "visee")
        for newer in ("elsewhere", "chapril"):        # a host this version doesn't know
            with self.assertRaises(ValueError) as e:
                share_later.open_record(key, 5, share_later.seal_record(
                    key, 5, dict(rec, site=newer)))
            self.assertEqual(str(e.exception), "site")
        for bad in ("Bad Site", "x" * 21, 3, None):    # not a host name: just damaged
            with self.assertRaises(ValueError) as e:
                share_later.open_record(key, 5, share_later.seal_record(key, 5, dict(rec, site=bad)))
            self.assertEqual(str(e.exception), "record")

    def test_sites(self):
        """adminForge, then send.vis.ee (Chapril gone); send.vis.ee's 2.5 GiB per file means
        3 parts for a 5 GiB backup, 2 a bit under it, 1 for small ones; its upload is capped."""
        self.assertEqual(list(share_send.SITES), ["adminforge", "visee"])
        self.assertEqual(share_send.ORDER, ("adminforge", "visee"))
        with mock.patch.dict(share_send.SITES, {
                k: share_send.Site(k, "https://x.example", k, 1) for k in share_send.ORDER}):
            self.assertEqual(share_send.sites_to_try(), ["adminforge", "visee"])
        self.assertEqual(share_send.sites_to_try(), [])     # tests: blank addresses
        self.assertEqual(REAL_SEND_URLS, {"adminforge": "https://send.adminforge.de",
                                          "visee": "https://send.vis.ee"})
        self.assertEqual(share_send.SITES["adminforge"].max_parts, 1)
        self.assertEqual(share_send.SITES["adminforge"].max_rate, 0)
        self.assertEqual(share_send.SITES["visee"].max_parts, 3)
        self.assertEqual(share_send.SITES["visee"].max_rate, 8 * 1000 * 1000)
        for name in ("CHAPRIL_ON", "CHAPRIL_PER_WEEK", "CHAPRIL_TERMS_URL", "chapril_ok"):
            self.assertFalse(hasattr(share_send, name))
        gib, mib = 1024 ** 3, 1024 ** 2
        limits = (int(2.5 * gib), 20, 3 * 24 * 3600)              # send.vis.ee's /config
        with mock.patch.object(share_send.SITES["visee"], "url", "https://x.example"):
            client = share_send.Send("visee")
        for plain, want in ((50 * mib, 1), (3 * gib, 2), (5 * gib - 4 * mib, 2),
                            (share.LATER_MAX_SIZE, 3)):
            total = share_later.Blob("unused", bytes(32), plain, "0" * 64).total
            ranges, time_limit = client.plan(total, 24, limits)
            self.assertEqual(len(ranges), want, plain)
            self.assertEqual(ranges[-1][1], total)
            self.assertLessEqual(time_limit, limits[2])         # 24 h + 8 h <= 3 days
        with self.assertRaises(share_send.SendError):              # over 3 parts: refused
            client.plan(8 * gib, 1, limits)

    def test_upload_rate_cap(self):
        """send.vis.ee's upload waits when it is ahead of max_rate (Cancel still ends it)."""
        with mock.patch.object(share_send.SITES["visee"], "url", "https://x.example"):
            client = share_send.Send("visee", sleep=lambda s: naps.append(s))
        naps, now = [], [100.0]
        client.clock = lambda: now[0]
        client._pace(8 * 1000 * 1000, 100.0, None)                 # 1 s of bytes, 0 s gone
        self.assertEqual(naps, [1.0])
        client._pace(4 * 1000 * 1000, 99.0, None)                  # behind: no wait
        client._pace(1000, 100.0, None)                            # a little ahead: no wait
        self.assertEqual(naps, [1.0])
        with self.assertRaises(SendError) as e:
            client._pace(8 * 1000 * 1000, 100.0, lambda: True)
        self.assertEqual(e.exception.kind, "cancelled")
        client.max_rate = 0                                        # adminForge: no cap
        client._pace(10 ** 12, 100.0, None)
        self.assertEqual(naps, [1.0])


# ---------- the client against the fake server ----------
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSend()
        self.addCleanup(self.fake.stop)
        self.naps = []
        self.client = share_send.Send("adminforge", allow_local=True, sleep=self.naps.append,
                                      url=self.fake.url)

    def round_trip(self, data, hours=12):
        parts = self.client.upload(Blob(data), hours, "ab12cd34.amsend")
        path = os.path.join(fixtures.temp_dir("send_dl_"), "blob")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w+b") as f:
            self.client.download(parts, f)
        with open(path, "rb") as f:
            return parts, f.read()

    def test_upload_download_delete(self):
        data = os.urandom(300 * 1024 + 5)
        parts, got = self.round_trip(data)
        self.assertEqual(got, data)
        self.assertEqual(len(parts), 1)
        self.assertEqual(self.fake.asked, [(12 * 3600 + 8 * 3600, 2)])   # 20 h, dlimit 2
        self.assertEqual(self.fake.agents, {share_send.download.USER_AGENT})
        self.assertEqual(self.fake.max_parallel, 1)
        statuses = [s for _i, s in self.fake.downloads]
        self.assertEqual(statuses, [401, 200])                # the nonce, then the file
        self.assertIn(parts[0].id, self.fake.stored())        # 1 of 2 downloads used
        self.client.delete_all(parts)
        self.assertEqual(self.fake.stored(), {})
        self.assertEqual(self.fake.deletes, [(parts[0].id, 200)])
        sid = parts[0].id
        meta = share_send.open_metadata(parts[0].secret, self.fake.metadata[0])
        self.assertEqual(meta["name"], "ab12cd34.amsend")     # random neutral name only
        self.assertEqual(self.naps, [])
        # A wrong owner token deletes nothing.
        parts, _got = self.round_trip(b"x" * 10)
        self.assertFalse(self.client.delete(share_send.Part(parts[0].id, "b" * 20, b"k" * 16, 0, 10)))
        self.assertIn(parts[0].id, self.fake.stored())
        self.assertNotEqual(sid, parts[0].id)

    def test_parts_when_a_server_allows_it(self):
        self.fake.limits["MAX_FILE_SIZE"] = share_send.PART_MARGIN + 100 * 1024
        data = os.urandom(250 * 1024)
        with self.assertRaises(SendError) as cm:              # max_parts 1: refused
            self.client.upload(Blob(data), 1, "ab12cd34.amsend")
        self.assertEqual(cm.exception.kind, "refused")
        self.assertEqual(self.fake.calls, [("GET", "/config")])   # nothing uploaded
        self.client.site = share_send.Site("adminforge", self.fake.url, "x", 3)
        parts, got = self.round_trip(data)
        self.assertEqual((len(parts), got), (3, data))
        self.assertEqual(self.fake.max_parallel, 1)           # one after another

    def test_refusals_from_limits(self):
        for what, value in (("MAX_EXPIRE_SECONDS", 3600), ("MAX_DOWNLOADS", 1)):
            with self.subTest(what):
                fake = FakeSend()
                self.addCleanup(fake.stop)
                fake.limits[what] = value
                c = share_send.Send("adminforge", allow_local=True, sleep=self.naps.append,
                                    url=fake.url)
                with self.assertRaises(SendError) as cm:
                    c.upload(Blob(b"x" * 100), 12, "ab12cd34.amsend")
                self.assertEqual(cm.exception.kind, "refused")
                self.assertEqual(fake.uploads, [])
        self.fake.config_status = 503
        with self.assertRaises(SendError) as cm:
            self.client.upload(Blob(b"x"), 1, "ab12cd34.amsend")
        self.assertEqual(cm.exception.kind, "down")

    def test_gentle_retries(self):
        self.fake.handshake = [(503, None)]                   # one blip: one retry after 30 s
        self.round_trip(b"y" * 1000)
        self.assertEqual(self.naps, [30])
        self.naps.clear()
        self.fake.handshake = [(429, 900)]                    # Retry-After capped at 5 min
        self.round_trip(b"y" * 1000)
        self.assertEqual(self.naps, [300])
        self.naps.clear()
        self.fake.handshake = [(503, None), (503, None), (503, None)]
        with self.assertRaises(SendError) as cm:              # then the next host, no loop
            self.client.upload(Blob(b"y"), 1, "ab12cd34.amsend")
        self.assertEqual(cm.exception.kind, "down")
        self.assertEqual(self.naps, [30])
        self.fake.handshake = []
        self.naps.clear()
        self.fake.ws_error = [413]                            # refused: no retry at all
        with self.assertRaises(SendError) as cm:
            self.client.upload(Blob(b"y"), 1, "ab12cd34.amsend")
        self.assertEqual((cm.exception.kind, self.naps), ("refused", []))

    def test_dropped_upload_tries_once_more_and_leaves_nothing(self):
        self.fake.drop_after = 2
        data = os.urandom(5 * share_send.DATA)
        parts, got = self.round_trip(data)
        self.assertEqual(got, data)
        self.assertEqual(len(self.fake.uploads), 1)           # the dropped one kept nothing
        self.client.delete_all(parts)
        self.assertEqual(self.fake.stored(), {})

    def test_download_cut_tampered_gone(self):
        data = os.urandom(3 * share_send.DATA)
        parts = self.client.upload(Blob(data), 1, "ab12cd34.amsend")
        out = os.path.join(fixtures.temp_dir("send_dl_"), "blob")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        self.fake.cut_download = 1000                         # cut: once more, from the start
        with open(out, "w+b") as f:
            self.client.download(parts, f)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(self.naps, [30])
        self.fake.tamper = True                               # changed: broken, no retry
        with open(out, "w+b") as f, self.assertRaises(SendError) as cm:
            self.client.download(parts, f)
        self.assertEqual(cm.exception.kind, "broken")
        self.fake.advance(10 ** 6)                            # its time is up
        with open(out, "w+b") as f, self.assertRaises(SendError) as cm:
            self.client.download(parts, f)
        self.assertEqual(cm.exception.kind, "gone")

    def test_cancel_mid_upload_deletes(self):
        stop = threading.Event()
        calls = [0]

        def moved(done):
            calls[0] += 1
            if calls[0] == 2:
                stop.set()
        with self.assertRaises(SendError) as cm:
            self.client.upload_part(Blob(os.urandom(4 * share_send.DATA)).read, 0,
                                    4 * share_send.DATA, 3600, "ab12cd34.amsend",
                                    stop.is_set, moved)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertEqual(self.fake.stored(), {})
        self.assertEqual(len(self.fake.deletes), 1)

    def test_cancel_while_waiting_for_ok_still_deletes(self):   # security review #1
        self.fake.ok_delay = 1.5
        stop = threading.Event()
        data = os.urandom(2 * share_send.DATA)

        def moved(done):
            if done >= len(data):
                stop.set()                                    # Cancel after the last byte
        with self.assertRaises(SendError) as cm:
            self.client.upload_part(Blob(data).read, 0, len(data), 3600, "ab12cd34.amsend",
                                    stop.is_set, moved)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertTrue(share_send.wait_cleanups(20))         # deleted in a clean-up thread
        self.assertEqual(self.fake.stored(), {})              # stored, then deleted
        self.assertEqual([s for _i, s in self.fake.deletes], [200])

    def test_quitting_right_after_cancel_still_deletes(self):   # tester's bug #2
        """Another Python process cancels after the last byte and leaves main at once (as
        the app does when the player quits): its exit waits for the clean-up thread."""
        import subprocess
        import sys
        self.fake.ok_delay = 2
        code = (
            "import os, sys, threading\n"
            f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
            "from am import share_send\n"
            f"c = share_send.Send('adminforge', allow_local=True, url={self.fake.url!r})\n"
            "data = os.urandom(70000)\n"
            "stop = threading.Event()\n"
            "def moved(done):\n"
            "    if done >= len(data): stop.set()\n"
            "try:\n"
            "    c.upload_part(lambda a, b: data[a:b], 0, len(data), 3600, 'ab12cd34.amsend',\n"
            "                  stop.is_set, moved)\n"
            "except share_send.SendError as e:\n"
            "    print(e.kind, flush=True)\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(out.stdout.strip(), "cancelled", out.stderr)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(self.fake.stored(), {})               # deleted before the exit
        self.assertEqual([s for _i, s in self.fake.deletes], [200])

    def test_only_its_own_address(self):                     # security review #3
        with self.assertRaises(SendError):
            share_send.Send("adminforge")                     # tests: the address is blank
        for url, local in (("http://example.org", True), ("https://example.org", True),
                           ("https://127.0.0.1:9", True), (self.fake.url, False)):
            with self.subTest(url), self.assertRaises(SendError):
                share_send.Send("adminforge", allow_local=local, url=url)
        self.assertNotIn("owner", repr(share_send.Part("a" * 16, "b" * 20, b"k" * 16, 0, 1)))
        self.assertNotIn("b" * 20, repr(share_send.Part("a" * 16, "b" * 20, b"k" * 16, 0, 1)))
        with self.assertRaises(KeyError):
            share_send.Send("elsewhere", url="https://x.example")


# ---------- the whole way: share.send / share.receive ----------
class SendLaterSender(LaterSender):
    def __init__(self, path, url, kiwi_url, sites, **kw):
        self.states = __import__("queue").Queue()
        self.hosts = []
        self._later = {"hours": 12, "api": kiwi_url, "allow_local": True, "send": sites}
        Sender.__init__(self, path, url, **kw)

    def _run(self, path, args):
        def on_later(s, v):
            if s == "host":
                self.hosts.append(v)
            self.states.put((s, v))
        Sender._run(self, path, dict(args, later=self._later, on_later=on_later))


class FlowTests(unittest.TestCase):
    def setUp(self):
        fast(self)
        for mod, name, value in ((share_later, "LATER_AFTER", 1), (share, "PING_EVERY", 1)):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.base = fixtures.temp_dir("share_send_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = big_backup(os.path.join(self.base, "backups"), 2 * share.CHUNK + 999)
        self.inbox = os.path.join(self.base, "inbox")
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)
        self.kiwi = FakeKiwi()
        self.addCleanup(self.kiwi.stop)
        self.fake = FakeSend()
        self.addCleanup(self.fake.stop)
        self.spare = FakeSend()
        self.addCleanup(self.spare.stop)
        for key, srv in (("adminforge", self.fake), ("visee", self.spare)):
            p = mock.patch.object(share_send.SITES[key], "url", srv.url)
            p.start()
            self.addCleanup(p.stop)
    def send(self, sites=("adminforge",), cancel=None):
        s = SendLaterSender(self.path, self.relay.url, self.kiwi.url, list(sites),
                            cancel=cancel)
        return s, s.code()

    def receiver(self):
        return share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)

    @staticmethod
    def hosts(s):
        return list(s.hosts)

    def test_round_trip_through_send(self):
        s, code = self.send()
        s.wait_state("ready")
        s.join()
        self.assertIsNone(s.error)
        self.assertEqual(s.progress[-1][0], s.progress[-1][1])
        self.assertEqual(len(self.fake.stored()), 1)
        got, said = [], []
        path = share.receive(code, self.receiver(), self.inbox,
                             progress=lambda d, t: got.append((d, t)),
                             on_later=lambda k, v: said.append((k, v)))
        self.assertTrue(same_entries(path, self.path))
        self.assertEqual(os.listdir(self.inbox), [os.path.basename(path)])   # no .part left
        self.assertIn(("store", "send"), said)                # Cancel asks first
        self.assertEqual(got[-1][0], got[-1][1])
        self.assertEqual(self.fake.stored(), {})              # deleted after the download
        self.assertEqual([st for _i, st in self.fake.deletes], [200])
        self.assertEqual(self.kiwi.calls, [])
        self.assertEqual(self.spare.calls, [])
        names = json.dumps(self.fake.calls) + json.dumps(sorted(self.fake.agents))
        for word in ("Big", "AddOns", "backup", "WoW"):        # nothing personal or game-ish
            self.assertNotIn(word, names)

    def test_fallbacks_in_order(self):
        self.fake.config_status = 503                         # adminForge down -> send.vis.ee
        s, code = self.send(("adminforge", "visee"))
        s.wait_state("ready")
        s.join()
        self.assertEqual(self.hosts(s), ["adminforge", "visee"])
        path = share.receive(code, self.receiver(), self.inbox)
        self.assertTrue(same_entries(path, self.path))
        self.assertEqual(self.spare.stored(), {})
        os.remove(path)
        self.spare.config_status = 503                        # both down -> file.kiwi
        s, code = self.send(("adminforge", "visee"))
        s.wait_state("ready")
        s.join()
        self.assertIsNone(s.error)
        self.assertEqual(self.hosts(s), ["adminforge", "visee", "kiwi"])
        ticket = share.receive(code, self.receiver(), self.inbox)
        self.assertIsNotNone(ticket.link)
        self.spare.config_status = 200
        self.fake.config_status = 200
        self.fake.ws_error = [413]                            # too big for it -> next at once
        s, code = self.send(("adminforge",))
        s.wait_state("ready")
        s.join()
        self.assertEqual(self.hosts(s), ["adminforge", "kiwi"])

    def damaged(self, hook):
        s, code = self.send()
        s.wait_state("ready")
        s.join()
        hook()
        with self.assertRaises(ShareError) as cm:
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(self.fake.stored(), {})              # deleted all the same
        self.assertEqual(os.listdir(self.inbox) if os.path.isdir(self.inbox) else [], [])
        return cm.exception

    def test_failed_downloads_delete_and_ask_for_a_new_send(self):
        e = self.damaged(lambda: setattr(self.fake, "tamper", True))
        self.assertEqual(e.kind, "broken")
        self.assertIn("Make a new code", errors.share_message(e))
        self.assertNotIn("Try again.", errors.share_message(e))
        e = self.damaged(lambda: self.fake.files.clear())    # gone (the operator deleted it)
        self.assertEqual(e.kind, "store_failed")
        self.assertEqual(errors.share_message(e),
                         "The download stopped." + errors.STORE_NEW_CODE)
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)):
            e = self.damaged(lambda: None)
        self.assertEqual((e.kind, e.detail), ("disk_full", "store"))

    def test_receiver_cancel_deletes(self):
        s, code = self.send()
        s.wait_state("ready")
        s.join()
        cancel = threading.Event()
        cancel.set()
        ch, words = share.parse_code(code)
        r = self.receiver()
        self.assertEqual(r.join(ch), "later")
        ticket = share._receive_later(ch, words, r, None, __import__("time").monotonic, None)
        with self.assertRaises(ShareError) as cm:
            share._fetch_send(ticket, r, self.inbox, None, cancel)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertEqual(self.fake.stored(), {})

    def test_sender_cancel_leaves_nothing(self):
        cancel = threading.Event()
        self.fake.drop_after = 10 ** 9
        gate = threading.Event()
        real = share_send.Send.upload_part

        def slow(client, *a, **kw):
            gate.set()
            cancel.wait(10)
            __import__("time").sleep(1)      # the sender sees Cancel and sets the stop flag
            return real(client, *a, **kw)
        with mock.patch.object(share_send.Send, "upload_part", slow):
            s, _code = self.send(cancel=cancel)
            gate.wait(20)
            cancel.set()
            s.join()
        self.assertEqual(s.error.kind, "cancelled")
        self.assertEqual(self.fake.stored(), {})
        self.assertEqual(self.kiwi.calls, [])

    def test_upload_done_after_cancel_is_deleted(self):      # security review #1
        cancel = threading.Event()
        gate = threading.Event()
        real = share_send.Send.upload

        def late(client, blob, hours, name, stop=None, progress=None):
            gate.set()
            cancel.wait(10)
            __import__("time").sleep(0.5)                    # the sender has given up
            return real(client, blob, hours, name, None, progress)   # ignores Cancel
        with mock.patch.object(share_send.Send, "upload", late):
            s, _code = self.send(cancel=cancel)
            gate.wait(20)
            cancel.set()
            s.join()
            self.assertEqual(s.error.kind, "cancelled")
            end = __import__("time").time() + 20             # the upload thread goes on
            while (not self.fake.uploads or self.fake.stored()) and __import__("time").time() < end:
                __import__("time").sleep(0.1)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(self.fake.stored(), {})
        self.assertEqual([st for _i, st in self.fake.deletes], [200])

    def test_too_big_record_deletes_it(self):                 # security review #2
        s, code = self.send()
        s.wait_state("ready")
        s.join()
        self.assertEqual(len(self.fake.stored()), 1)
        with mock.patch.object(share, "LATER_MAX_SIZE", 10):
            with self.assertRaises(ShareError) as cm:
                share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "too_big")
        self.assertEqual(self.fake.stored(), {})

    def test_record_put_fails_deletes_the_upload(self):
        with mock.patch.object(share.Relay, "put_record",
                               side_effect=ShareError("offline")):
            s, _code = self.send()
            s.join()
        self.assertEqual(s.error.kind, "offline")
        self.assertEqual(self.fake.stored(), {})

    def test_unknown_host_asks_for_update(self):
        """A send kept on a Send server this version doesn't know (a newer app's host):
        "update this app", not "damaged"."""
        s, code = self.send(("visee",))
        s.wait_state("ready")
        s.join()
        self.assertIsNone(s.error)
        with mock.patch.dict(share_send.SITES, clear=False) as sites:
            del sites["visee"]
            with self.assertRaises(ShareError) as e:
                share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(e.exception.kind, "later_newer")
        text = errors.share_message(e.exception)
        self.assertIn("newer Addon Manager", text)
        self.assertNotIn("damaged", text)

    def test_couldnt_send_names_all_hosts(self):
        for detail in ("", "down"):
            text = errors.share_message(ShareError("later_failed", detail))
            self.assertIn("None of the 3 hosts", text)
            self.assertIn("new code", text)

    def test_older_record_versions_still_open(self):
        self.assertTrue(share_later.CONSENT_VERSION >= 6)
        self.assertIn("adminForge", errors.later_uploading_text("now", "adminforge"))
        self.assertIn("send.vis.ee", errors.later_uploading(1, 2, None, "visee"))
        self.assertIn("file.kiwi", errors.later_uploading_text(None, "kiwi"))


if __name__ == "__main__":
    unittest.main()
