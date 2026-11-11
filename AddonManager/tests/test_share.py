"""Send a backup with a code (am\\share.py) against the local fake relay (tests\\fake_relay.py).
Fake WoW folders and backup folders in temp folders only; nothing goes online."""

import json
import os
import queue
import secrets
import shutil
import threading
import time
import urllib.parse
import unittest
import zipfile
from unittest import mock

from am import backup, download, share, share_later
from am.share import ShareError
from am.share_words import WORDS
from tests import fixtures
from tests.fake_kiwi import FakeKiwi
from tests.fake_relay import FakeRelay
from tests.test_backup import make_pc, raw_backup


def same_entries(received, original):
    """The received file = the original plus only the received mark."""
    with zipfile.ZipFile(received) as a, zipfile.ZipFile(original) as b:
        names = [n for n in a.namelist() if n != backup.RECEIVED_NAME]
        return names == b.namelist() and all(a.read(n) == b.read(n) for n in names)


def fast(test):
    """Short waits so the timeout tests take seconds, not minutes."""
    for name, value in (("IDLE", 3), ("POLL_WAIT", 1)):
        p = mock.patch.object(share, name, value)
        p.start()
        test.addCleanup(p.stop)


class Sender:
    """share.send in a thread; .code once it's shown, .result / .error at the end."""

    def __init__(self, path, url, cancel=None, clock=None, **kw):
        self.codes = queue.Queue()
        self.result = self.error = None
        self.progress = []
        self.relay = share.Relay(url, allow_local=True, sleep=lambda s: None)
        args = dict(progress=lambda d, t: self.progress.append((d, t)), cancel=cancel)
        if clock:
            args["clock"] = clock
        self.thread = threading.Thread(target=self._run, args=(path, args), daemon=True)
        self.thread.start()

    def _run(self, path, args):
        try:
            self.result = share.send(path, self.relay, lambda c, t: self.codes.put((c, t)),
                                     **args)
        except BaseException as e:   # noqa: BLE001 (handed to the test)
            self.error = e
        finally:
            self.codes.put(None)

    def code(self):
        got = self.codes.get(timeout=20)
        if got is None:
            raise AssertionError(f"no code: {self.error!r}")
        return got[0]

    def join(self):
        self.thread.join(30)
        assert not self.thread.is_alive(), "sender still running"


class ShareTests(unittest.TestCase):
    def setUp(self):
        fast(self)
        self.base = fixtures.temp_dir("p2p_share_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root, self.addons, self.client = make_pc(os.path.join(self.base, "pc1"))
        self.backups = os.path.join(self.base, "backups")
        self.path, _size, _c = backup.make(self.addons, self.backups)
        self.inbox = os.path.join(self.base, "inbox")
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)

    def receiver(self):
        return share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)

    def assertSameBackup(self, received, original):
        self.assertTrue(same_entries(received, original))

    def receive(self, code, **kw):
        return share.receive(code, self.receiver(), self.inbox, **kw)

    def inbox_files(self):
        return sorted(os.listdir(self.inbox)) if os.path.isdir(self.inbox) else []

    # ---------- the normal way ----------
    def test_round_trip_then_restore(self):
        s = Sender(self.path, self.relay.url)
        got = []
        path = self.receive(s.code(), progress=lambda d, t: got.append((d, t)))
        s.join()
        self.assertIsNone(s.error)
        self.assertSameBackup(path, self.path)
        self.assertTrue(backup.has_received_mark(path))
        self.assertTrue(backup.is_received(path))
        self.assertEqual(self.inbox_files(), [os.path.basename(path)])   # no .part left
        self.assertEqual(got[-1][0], got[-1][1])
        self.assertEqual(self.relay.stored(), {})                       # relay keeps nothing
        info = backup.read(path)
        self.assertTrue(info.received)
        # The normal restore on another PC, settings onto the player's own account.
        _r2, addons2, client2 = make_pc(os.path.join(self.base, "pc2"), account="MINE",
                                        tag="new")
        result = backup.restore(path, addons2, os.path.join(self.base, "safety"),
                                account_map={"ACCOUNT1": "MINE"})
        self.assertTrue(result.safety and os.path.exists(result.safety))
        self.assertFalse(os.path.exists(os.path.join(client2, "WTF", "Account", "ACCOUNT1")))

    def test_bigger_file_many_chunks_and_retries(self):
        big = os.path.join(self.base, "big", "AddonManager backup 2026-11-10.zip")
        os.makedirs(os.path.dirname(big))
        with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as z:
            z.writestr("backup.json", json.dumps({"app": "AddonManager", "format": 1}))
            z.writestr("AddOns/Big/Big_Forever.toc", "## Version: 1\n")
            z.writestr("AddOns/Big/data.bin", os.urandom(3 * share.CHUNK + 1234))
        self.relay.buffer = 1
        self.relay.fail_next = 2   # two 500s: tried again
        s = Sender(big, self.relay.url)
        path = self.receive(s.code())
        s.join()
        self.assertIsNone(s.error)
        self.assertSameBackup(path, big)

    # ---------- wrong, used, expired ----------
    def test_wrong_code_kills_the_code(self):
        s = Sender(self.path, self.relay.url)
        ch, words = share.parse_code(s.code())
        other = [w for w in WORDS if w != words[0]][0]
        with self.assertRaises(ShareError) as cm:
            self.receive(share.format_code(ch, [other] + words[1:]))
        self.assertEqual(cm.exception.kind, "wrong_code")
        s.join()
        self.assertEqual(s.error.kind, "wrong_code")
        with self.assertRaises(ShareError) as cm:   # the right code is dead now
            self.receive(share.format_code(ch, words))
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual(self.relay.stored(), {})

    def test_second_join_is_used(self):
        s = Sender(self.path, self.relay.url)
        ch, _w = share.parse_code(s.code())
        first = self.receiver()
        first.join(ch)
        with self.assertRaises(ShareError) as cm:
            self.receiver().join(ch)
        self.assertEqual(cm.exception.kind, "used")
        first.close("cancel")
        s.join()
        self.assertEqual(s.error.kind, "ended")

    def test_expired_code(self):
        clock = lambda: self.relay.now()   # noqa: E731 (the sender sees the moved clock)
        s = Sender(self.path, self.relay.url, clock=clock)
        code = s.code()
        self.relay.advance(601)
        with self.assertRaises(ShareError) as cm:
            self.receive(code)
        self.assertEqual(cm.exception.kind, "not_found")
        s.join()
        self.assertIn(s.error.kind, ("expired", "ended"))

    def test_unknown_code(self):
        with self.assertRaises(ShareError) as cm:
            self.receive("37-amber-lamp-river")
        self.assertEqual(cm.exception.kind, "not_found")

    # ---------- a relay that cheats ----------
    def run_bad(self, hook=None, msg_hook=None):
        self.relay.hook_chunk, self.relay.hook_msg = hook, msg_hook
        before = self.inbox_files()
        s = Sender(self.path, self.relay.url)
        with self.assertRaises(ShareError) as cm:
            self.receive(s.code())
        s.join()
        self.assertIsNotNone(s.error)
        self.assertEqual(self.inbox_files(), before)    # nothing kept, no .part
        self.assertEqual(self.relay.stored(), {})
        return cm.exception.kind

    def test_tampered_chunk(self):
        def flip(i, box):
            return box[:10] + bytes([box[10] ^ 1]) + box[11:]
        self.assertEqual(self.run_bad(flip), "broken")

    def test_tampered_header(self):
        def flip(slot, data):
            return data[:-1] + bytes([data[-1] ^ 1]) if slot == "header" else data
        self.assertEqual(self.run_bad(msg_hook=flip), "broken")

    def test_cut_off(self):
        self.assertEqual(self.run_bad(lambda i, box: None), "ended")

    def test_replayed_chunk_from_another_transfer(self):
        saved = {}
        self.relay.hook_chunk = lambda i, box: saved.setdefault(i, box)
        s = Sender(self.path, self.relay.url)
        self.receive(s.code())
        s.join()
        self.assertEqual(self.run_bad(lambda i, box: saved.get(i, box)), "broken")

    def test_bad_confirmation_from_relay(self):
        def bad(slot, data):
            return bytes(32) if slot == "confA" else data
        self.assertEqual(self.run_bad(msg_hook=bad), "wrong_code")

    def test_cipher_rules(self):
        c = share.Cipher(b"k" * 32, 37)
        box = c.seal(2, b"data", final=False)
        self.assertEqual(c.open(2, box), b"data")
        for bad in (lambda: c.open(3, box),                      # moved
                    lambda: c.open(2, box, final=True),          # made the last one
                    lambda: share.Cipher(b"k" * 32, 38).open(2, box),   # other channel
                    lambda: share.Cipher(b"x" * 32, 37).open(2, box),   # other key
                    lambda: c.open(2, box[:-1])):                # cut
            with self.assertRaises(ShareError) as cm:
                bad()
            self.assertEqual(cm.exception.kind, "broken")

    def test_header_not_an_object(self):   # tester L1
        c = share.Cipher(b"k" * 32, 1)
        for text in (b"[1, 2]", b"7", b'"x"', b"null"):
            with self.assertRaises(ShareError) as cm:
                share._header(c, c.seal(0, text))
            self.assertEqual(cm.exception.kind, "broken")

    def test_header_checks(self):
        c = share.Cipher(b"k" * 32, 1)

        def header(**h):
            base = {"v": 1, "size": 10, "chunks": 1, "sha256": "a" * 64}
            base.update(h)
            return share._header(c, c.seal(0, json.dumps(base).encode()))
        self.assertEqual(header(), (10, 1, "a" * 64))
        with self.assertRaises(ShareError) as cm:
            header(size=share.MAX_SIZE + 1, chunks=501)
        self.assertEqual(cm.exception.kind, "too_big")
        for bad in ({"chunks": 2}, {"size": 0}, {"sha256": "x"}, {"v": 2}, {"size": "10"},
                    {"v": 2, "store": "r2", "per": 4}):
            with self.assertRaises(ShareError) as cm:
                header(**bad)
            self.assertEqual(cm.exception.kind, "broken")

    # ---------- the player's side ----------
    def test_cancel_while_waiting(self):
        cancel = threading.Event()
        s = Sender(self.path, self.relay.url, cancel=cancel)
        code = s.code()
        cancel.set()
        s.join()
        self.assertEqual(s.error.kind, "cancelled")
        with self.assertRaises(ShareError) as cm:
            self.receive(code)
        self.assertEqual(cm.exception.kind, "not_found")

    def test_receiver_cancel_removes_part(self):
        cancel = threading.Event()

        def slow(i, box):
            cancel.set()
            return None
        self.relay.hook_chunk = slow
        s = Sender(self.path, self.relay.url)
        with self.assertRaises(ShareError) as cm:
            self.receive(s.code(), cancel=cancel)
        self.assertEqual(cm.exception.kind, "cancelled")
        s.join()
        self.assertEqual(s.error.kind, "ended")
        self.assertEqual(self.inbox_files(), [])

    def test_disk_full(self):
        s = Sender(self.path, self.relay.url)
        code = s.code()
        with mock.patch.object(share.shutil, "disk_usage", return_value=mock.Mock(free=10)):
            with self.assertRaises(ShareError) as cm:
                self.receive(code)
        self.assertEqual(cm.exception.kind, "disk_full")
        s.join()

    def test_busy_offline_and_rate(self):
        self.relay.channels = 0
        with self.assertRaises(ShareError) as cm:
            share.Relay(self.relay.url, allow_local=True).open()
        self.assertEqual(cm.exception.kind, "busy")
        self.relay.channels, self.relay.rate = 499, 0
        with self.assertRaises(ShareError) as cm:
            share.Relay(self.relay.url, allow_local=True).open()
        self.assertEqual(cm.exception.kind, "busy")
        with self.assertRaises(ShareError) as cm:
            share.Relay("http://127.0.0.1:9", allow_local=True, sleep=lambda s: None).open()
        self.assertEqual(cm.exception.kind, "offline")

    def test_only_real_backups_are_sent(self):
        junk = os.path.join(self.base, "junk.zip")
        fixtures.make_raw_zip(junk, [("hello.txt", "x")])
        safety = os.path.join(self.base, "Before restore 2026-11-10 2130.zip")
        raw_backup(safety, [("AddOns/A/A.toc", "## Version: 1")],
                   meta={"app": "AddonManager", "format": 1, "kind": "safety"})
        chat = os.path.join(self.base, "AddonManager backup 2026-11-09.zip")
        raw_backup(chat, [("WTF/Account/A/SavedVariables/ChatHistory.lua", "x")])
        for path in (junk, safety, chat):
            with self.assertRaises(ShareError) as cm:
                share.send(path, share.Relay(self.relay.url, allow_local=True),
                           lambda c, t: None)
            self.assertEqual(cm.exception.kind, "not_backup", path)
        self.assertEqual(self.relay.calls, [])   # refused before going online
        with mock.patch.object(share, "LATER_MAX_SIZE", 10):
            with self.assertRaises(ShareError) as cm:
                share.send(self.path, share.Relay(self.relay.url, allow_local=True),
                           lambda c, t: None)
        self.assertEqual(cm.exception.kind, "too_big")
        self.assertEqual(cm.exception.limit, 10)
        with mock.patch.object(share, "MAX_SIZE", 10):   # over 500 MB, no send-later tick
            with self.assertRaises(ShareError) as cm:
                share.send(self.path, share.Relay(self.relay.url, allow_local=True),
                           lambda c, t: None)
        self.assertEqual((cm.exception.kind, cm.exception.detail), ("too_big", "later_off"))
        self.assertEqual(self.relay.calls, [])   # refused before going online


def big_backup(folder, extra):
    """A real (stored, not compressed) backup with `extra` random bytes: many chunks."""
    path = os.path.join(folder, "AddonManager backup 2026-11-10.zip")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("backup.json", json.dumps({"app": "AddonManager", "format": 1}))
        z.writestr("AddOns/Big/Big_Forever.toc", "## Version: 1\n")
        z.writestr("AddOns/Big/data.bin", os.urandom(extra))
    return path


class OldReceiver(share.Relay):
    """An app from before send later: joins without "|caps=later"."""

    def join(self, channel):
        self.channel = channel
        _s, data = self._call("POST", f"/v1/join/{channel}", secrets.token_hex(16).encode(),
                              ok=(200,))
        self._take_token(self._json(data))


class PrecheckTests(unittest.TestCase):
    """Before zipping a new backup to send: refuse early when it surely can't go."""

    def setUp(self):
        self.base = fixtures.temp_dir("share_pre_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def free(self, n):
        return mock.Mock(return_value=mock.Mock(free=n))

    def test_precheck(self):
        gib = 1024 ** 3
        lots = self.free(100 * gib)
        share.precheck(100 * 1024 * 1024, False, self.base, lots)   # small: fine
        share.precheck(3 * gib, True, self.base, lots)               # big, send later: fine
        with self.assertRaises(ShareError) as cm:                    # surely over 5 GiB
            share.precheck(12 * gib, True, self.base, lots)
        self.assertEqual((cm.exception.kind, cm.exception.limit),
                         ("too_big", share.LATER_MAX_SIZE))
        with self.assertRaises(ShareError) as cm:                    # no tick: 500 MB
            share.precheck(3 * gib, False, self.base, lots)
        self.assertEqual((cm.exception.kind, cm.exception.limit, cm.exception.detail),
                         ("too_big", share.MAX_SIZE, "later_off"))
        with self.assertRaises(ShareError) as cm:                    # zip won't fit
            share.precheck(1 * gib, True, os.path.join(self.base, "new", "folder"),
                           self.free(500 * 1024 * 1024))
        self.assertEqual(cm.exception.kind, "disk_full")

    def test_texts_and_time_left(self):
        from am import errors, share_ui
        gib = 1024 ** 3
        self.assertIn("5 GB at most", errors.share_message(ShareError("too_big", limit=5 * gib)))
        self.assertIn("500 MB at most", errors.share_message(ShareError("too_big")))
        hint = errors.share_message(ShareError("too_big", "later_off", limit=500 * 1024 ** 2))
        self.assertIn("third-party host", hint)                      # tos: neutral hint
        self.assertIn("copy it yourself", hint)
        self.assertNotIn("recommend", hint.lower())
        self.assertIn("about 2 GB is needed",
                      errors.share_message(ShareError("disk_full", limit=2 * gib)))
        self.assertEqual(errors.time_left(30), "less than a minute left")
        self.assertEqual(errors.time_left(600), "about 10 min left")
        self.assertEqual(errors.time_left(2 * 3600 + 300), "about 2 h 5 min left")
        self.assertEqual(errors.sending(0, 10), "Sending 1 KB of 1 KB...")
        now = [0.0]
        speed = share_ui.Speed(clock=lambda: now[0])
        self.assertIsNone(speed.left(0, 1000))            # too early to tell
        now[0] = 10
        self.assertEqual(speed.left(100, 1000), 90)       # 10 bytes/s, 900 left
        self.assertIn("about 2 min left", errors.receiving(100, 1000, 90))


class LaterSender(Sender):
    """A sender with send later ticked; .states gets ("uploading"/"ready", value)."""

    def __init__(self, path, url, kiwi_url, hours=12, **kw):
        self.states = queue.Queue()
        self._later = {"hours": hours, "api": kiwi_url, "allow_local": True}
        super().__init__(path, url, **kw)

    def _run(self, path, args):
        args = dict(args, later=self._later,
                    on_later=lambda s, v: self.states.put((s, v)))
        super()._run(path, args)

    def wait_state(self, want, timeout=30):
        while True:
            s, v = self.states.get(timeout=timeout)
            if s == want:
                return v


class LaterTests(unittest.TestCase):
    """Send later through a fake file.kiwi (tests\\fake_kiwi.py): the code waits
    LATER_AFTER (1 s here), then the backup is uploaded and the code fetches it once.
    The file.kiwi client runs on a fake clock (self.clients): each of its waits moves that
    clock on, so hours of "file.kiwi is down" pass in no time."""

    def setUp(self):
        fast(self)
        for mod, name, value in ((share_later, "LATER_AFTER", 1), (share, "PING_EVERY", 1)):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.clients = clients = []

        class FakeTimeKiwi(share_later.FileKiwi):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.now, self.naps = 0.0, []
                self.clock = lambda: self.now
                inner = self.sleep

                def sleep(s):
                    self.naps.append(s)
                    self.now += s
                    inner(s)
                self.sleep = sleep
                clients.append(self)
        p = mock.patch.object(share_later, "FileKiwi", FakeTimeKiwi)
        p.start()
        self.addCleanup(p.stop)
        self.base = fixtures.temp_dir("share_later_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = big_backup(os.path.join(self.base, "backups"), 2 * share.CHUNK + 999)
        self.inbox = os.path.join(self.base, "inbox")
        self.downloads = os.path.join(self.base, "Downloads")
        os.makedirs(self.downloads)
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)
        self.kiwi = FakeKiwi()
        self.addCleanup(self.kiwi.stop)

    def receiver(self, cls=share.Relay):
        return cls(self.relay.url, allow_local=True, sleep=lambda s: None)

    def send_later(self, **kw):
        s = LaterSender(self.path, self.relay.url, self.kiwi.url, **kw)
        code = s.code()
        return s, code

    def browser_download(self, ticket):
        """What the player does: the browser saves the file into Downloads."""
        name, data = self.kiwi.download(ticket.link)
        self.assertEqual(name, ticket.name)
        path = os.path.join(self.downloads, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_round_trip(self):
        s, code = self.send_later()
        left = s.wait_state("ready")
        s.join()
        self.assertIsNone(s.error)
        self.assertEqual(left, 12 * 3600)
        self.assertEqual(s.progress[-1][0], s.progress[-1][1])
        ticket = share.receive(code, self.receiver(), self.inbox)
        self.assertIsInstance(ticket, share.LaterTicket)
        self.assertRegex(ticket.name, r"^[0-9a-f]{8}\.amsend$")   # neutral, random name
        self.assertEqual(self.relay.stored(), {})                 # the record went with it
        got = []
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox,
                                progress=lambda d, t: got.append((d, t)))
        self.assertTrue(same_entries(path, self.path))
        self.assertTrue(backup.is_received(path))
        self.assertEqual(got[-1], (ticket.size, ticket.size))
        self.assertEqual([f for f in os.listdir(self.inbox)], [os.path.basename(path)])
        # file.kiwi's rules: one chunk at a time, our user agent, never extend the expiry.
        self.assertEqual(self.kiwi.max_parallel, 1)
        self.assertEqual(self.kiwi.agents, {download.USER_AGENT})
        self.assertTrue(self.kiwi.status_queries)
        self.assertFalse(any("uploadAuth" in q for q in self.kiwi.status_queries))
        with self.assertRaises(ShareError) as cm:                 # the code worked once
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "not_found")

    def test_receiver_comes_while_uploading(self):
        self.kiwi.delay = 0.3
        s, code = self.send_later()
        s.wait_state("uploading")
        seen = []
        ticket = share.receive(code, self.receiver(), self.inbox,
                               on_later=lambda state, p: seen.append((state, p)))
        s.join()
        self.assertIsNone(s.error)
        self.assertTrue(seen and all(state == "uploading" for state, _p in seen))
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox)
        self.assertTrue(same_entries(path, self.path))

    def test_wrong_code_burns_the_record(self):
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        ch, words = share.parse_code(code)
        other = [w for w in WORDS if w != words[0]][0]
        with self.assertRaises(ShareError) as cm:
            share.receive(share.format_code(ch, [other] + words[1:]), self.receiver(),
                          self.inbox)
        self.assertEqual(cm.exception.kind, "wrong_code")
        with self.assertRaises(ShareError) as cm:
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertEqual(self.relay.stored(), {})

    def test_one_guess_even_at_the_same_time(self):   # security #1
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        ch = share.parse_code(code)[0]
        first, second = self.receiver(), self.receiver()
        self.assertEqual(first.join(ch), "later")
        self.assertEqual(second.join(ch), "later")
        during = []

        def other_take():   # sent while the first /take is still on the record
            self.relay.hook_take = None
            try:
                second.take("1" * 64)
            except ShareError as e:
                during.append(e.kind)
        self.relay.hook_take = other_take
        with self.assertRaises(ShareError) as cm:
            first.take("0" * 64)
        self.assertEqual(cm.exception.kind, "wrong_code")
        self.assertEqual(during, ["used"])
        with self.assertRaises(ShareError):
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(self.relay.stored(), {})

    def test_bad_take_body_counts_as_the_try(self):
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        r = self.receiver()
        ch = share.parse_code(code)[0]
        r.join(ch)
        status, _d = r._once("POST", f"/{share.PROTOCOL}/{ch}/take", b"not json")
        self.assertEqual(status, 400)
        with self.assertRaises(ShareError) as cm:
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "not_found")

    def test_later_state_needs_the_join_token(self):   # security #7
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        ch = share.parse_code(code)[0]
        r = self.receiver()
        r.channel = ch                      # no join: no watch token
        status, _d = r._once("GET", f"/{share.PROTOCOL}/{ch}/later?wait=0")
        self.assertEqual(status, 403)
        r.token = "0" * 32
        status, _d = r._once("GET", f"/{share.PROTOCOL}/{ch}/later?wait=0")
        self.assertEqual(status, 403)
        self.assertEqual(r.join(ch), "later")
        self.assertEqual(r.later_state(0)["state"], "ready")
        ticket = share.receive(code, self.receiver(), self.inbox)   # still there
        self.assertIsInstance(ticket, share.LaterTicket)

    def test_no_wait_once_uploaded(self):
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        r = self.receiver()
        ch = share.parse_code(code)[0]
        self.assertEqual(r.join(ch), "later")
        t0 = time.monotonic()
        self.assertEqual(r.later_state(10)["state"], "ready")    # the relay answers at once
        self.assertLess(time.monotonic() - t0, 2)
        with mock.patch.object(share, "POLL_WAIT", 10):           # and the app asks without
            t0 = time.monotonic()                                 # waiting first
            share.receive(code, self.receiver(), self.inbox)
        self.assertLess(time.monotonic() - t0, 5)

    def test_code_ends_after_its_hours(self):
        s, code = self.send_later(hours=1)
        s.wait_state("ready")
        s.join()
        self.relay.advance(3601)
        with self.assertRaises(ShareError) as cm:
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertEqual(self.relay.stored(), {})

    def test_live_first_when_the_other_pc_is_there(self):
        share_later.LATER_AFTER = 20   # (patched back by setUp's cleanup)
        s, code = self.send_later()
        path = share.receive(code, self.receiver(), self.inbox)
        s.join()
        self.assertIsNone(s.error)
        self.assertTrue(same_entries(path, self.path))
        self.assertEqual(self.kiwi.calls, [])                     # nothing uploaded

    def test_backs_off_on_429_and_5xx(self):
        self.kiwi.fail_puts = [429, 500, 503]
        sleeps = []
        s = LaterSender(self.path, self.relay.url, self.kiwi.url)
        s.relay.sleep = sleeps.append
        code = s.code()
        s.wait_state("ready")
        s.join()
        self.assertIsNone(s.error)
        self.assertEqual(sleeps[:3], [2, 4, 8])
        ticket = share.receive(code, self.receiver(), self.inbox)
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox)
        self.assertTrue(same_entries(path, self.path))

    def test_big_backup_goes_up_at_once(self):
        with mock.patch.object(share, "MAX_SIZE", 1000), \
                mock.patch.object(share_later, "LATER_AFTER", 3600):   # no 5-minute wait
            s, code = self.send_later()
            self.assertEqual(s.wait_state("uploading", timeout=10), "big")
            s.wait_state("ready")
            s.join()
            self.assertIsNone(s.error)
            self.assertNotIn(("PUT", f"/v1/{share.parse_code(code)[0]}/msg/pakeA"),
                             self.relay.calls)                 # never a live send
            ticket = share.receive(code, self.receiver(), self.inbox)
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox)
        self.assertTrue(same_entries(path, self.path))

    def test_retry_cap(self):
        self.kiwi.fail_puts = [503] * 100
        with mock.patch.object(share_later, "RETRY_FOR", 10 ** 6):
            s = LaterSender(self.path, self.relay.url, self.kiwi.url)
            s.relay.sleep = lambda _s: None
            s.code()
            s.join()
        self.assertEqual(s.error.kind, "later_failed")
        # MAX_RETRIES quick ones, then one every 5 minutes for 2 hours (file.kiwi is down).
        slow = share_later.KIWI_DOWN_FOR // share_later.KIWI_DOWN_EVERY
        self.assertEqual(len([c for c in self.kiwi.calls if c[0] == "PUT"]),
                         share_later.MAX_RETRIES + 1 + slow)
        self.assertEqual(self.relay.stored(), {})

    def test_upload_fails(self):
        self.kiwi.fail_create = 500
        s, code = self.send_later()
        s.join()
        self.assertEqual((s.error.kind, s.error.detail), ("later_failed", "down"))
        self.assertEqual(self.relay.stored(), {})                 # code closed
        with self.assertRaises(ShareError) as cm:
            share.receive(code, self.receiver(), self.inbox)
        self.assertEqual(cm.exception.kind, "not_found")

    # ---------- file.kiwi is down: try again every 5 minutes for up to 2 hours ----------
    @staticmethod
    def all_states(s):
        out = []
        while not s.states.empty():
            out.append(s.states.get())
        return out

    def kiwi_now(self):
        return self.clients[-1].now if self.clients else 0

    def test_kiwi_down_then_back(self):
        self.kiwi.fail_create = lambda: 503 if self.kiwi_now() < 40 * 60 else None
        s, code = self.send_later()
        s.join()
        self.assertIsNone(s.error)
        states = [st for st, _v in self.all_states(s)]
        self.assertEqual(states, ["uploading", "kiwi_down", "uploading", "ready"])
        self.assertIn(share_later.KIWI_DOWN_EVERY, self.clients[-1].naps)
        ticket = share.receive(code, self.receiver(), self.inbox)    # the code still works
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox)
        self.assertTrue(same_entries(path, self.path))

    def test_kiwi_down_gives_up_after_2_hours(self):
        self.kiwi.fail_create = 503
        s, code = self.send_later()
        s.join()
        self.assertEqual((s.error.kind, s.error.detail), ("later_failed", "down"))
        naps = self.clients[-1].naps
        self.assertEqual(naps.count(share_later.KIWI_DOWN_EVERY),
                         share_later.KIWI_DOWN_FOR // share_later.KIWI_DOWN_EVERY)
        downs = [v for st, v in self.all_states(s) if st == "kiwi_down"]
        self.assertEqual(downs, [share_later.KIWI_DOWN_FOR])        # told once, until-time
        self.assertEqual(self.relay.stored(), {})                   # code closed
        from am import errors
        self.assertIn("2 hours", errors.share_message(s.error))
        self.assertIn("until 21:30", errors.later_kiwi_down("21:30"))
        self.assertIn("waiting for file.kiwi", errors.later_waiting(0))
        self.assertNotIn("file.kiwi", errors.later_waiting(500))

    def test_kiwi_refuses_fails_at_once(self):
        self.kiwi.fail_create = 403
        s, code = self.send_later()
        s.join()
        self.assertEqual(s.error.kind, "later_failed")
        self.assertNotEqual(s.error.detail, "down")
        self.assertEqual(len([c for c in self.kiwi.calls if c[0] == "POST"]), 1)
        self.assertEqual(self.clients[-1].naps, [])

    def test_429_backs_off_to_5_minutes(self):
        self.kiwi.fail_create = lambda: 429 if self.kiwi_now() < 1500 else None
        s, code = self.send_later()
        s.join()
        self.assertIsNone(s.error)
        naps = self.clients[-1].naps
        self.assertEqual(naps[:6], [2, 4, 8, 16, 32, 60])
        self.assertEqual(max(naps), share_later.KIWI_DOWN_EVERY)
        self.assertTrue(all(n >= 2 for n in naps))

    def test_chunk_outage_resumes(self):
        self.kiwi.fail_put_when = lambda n: 503 if n == 2 and self.kiwi_now() < 30 * 60 else None
        s, code = self.send_later()
        s.join()
        self.assertIsNone(s.error)
        puts = [c[1] for c in self.kiwi.calls if c[0] == "PUT"]
        self.assertEqual(sum(p.endswith("/00001") for p in puts), 1)   # not sent again
        self.assertGreater(sum(p.endswith("/00002") for p in puts), 1)
        self.assertIn("kiwi_down", [st for st, _v in self.all_states(s)])
        ticket = share.receive(code, self.receiver(), self.inbox)
        path = share.open_later(ticket, self.browser_download(ticket), self.inbox)
        self.assertTrue(same_entries(path, self.path))

    def real_wait_sender(self, cancel=None):
        """file.kiwi always down, and the app's own stop-aware 5-minute wait (not a fake one)."""
        self.kiwi.fail_create = 503
        s = LaterSender(self.path, self.relay.url, self.kiwi.url, cancel=cancel)
        s.relay.sleep = time.sleep          # share.py then waits on its stop event
        s.code()
        s.wait_state("kiwi_down")
        return s

    def test_cancel_while_kiwi_down(self):
        cancel = threading.Event()
        with mock.patch.object(share_later, "RETRY_FOR", -1):   # straight to the slow waits
            s = self.real_wait_sender(cancel)
            posts = len(self.kiwi.calls)
            t0 = time.monotonic()
            cancel.set()
            s.join()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(s.error.kind, "cancelled")
        time.sleep(0.3)
        self.assertEqual(len(self.kiwi.calls), posts)         # nothing more sent to file.kiwi
        self.assertEqual(self.relay.stored(), {})

    def test_relay_gone_while_kiwi_down(self):
        """Our relay can't be reached either (the player's internet): the closed mailbox
        stops the send, not the 2-hour wait."""
        with mock.patch.object(share_later, "RETRY_FOR", -1):
            s = self.real_wait_sender()
            t0 = time.monotonic()
            self.relay.advance(self.relay.later_idle + 60)
            s.join()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIsNotNone(s.error)
        self.assertNotEqual(s.error.kind, "later_failed")
        self.assertEqual(self.relay.stored(), {})

    def test_sender_cancel_while_uploading(self):
        self.kiwi.delay = 0.5
        cancel = threading.Event()
        s = LaterSender(self.path, self.relay.url, self.kiwi.url, cancel=cancel)
        s.code()
        s.wait_state("uploading")
        cancel.set()
        s.join()
        self.assertEqual(s.error.kind, "cancelled")
        self.assertEqual(self.relay.stored(), {})

    def test_old_app_gets_no_later_send(self):
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        with self.assertRaises(ShareError):
            self.receiver(OldReceiver).join(share.parse_code(code)[0])

    def test_wrong_or_damaged_download(self):
        s, code = self.send_later()
        s.wait_state("ready")
        s.join()
        ticket = share.receive(code, self.receiver(), self.inbox)
        good = self.browser_download(ticket)
        with open(good, "rb") as f:
            data = f.read()
        bad = os.path.join(self.downloads, "bad.amsend")
        for changed, kind in ((data[:-1], "later_file"),                     # cut off
                              (data[:200] + bytes([data[200] ^ 1]) + data[201:], "broken"),
                              (b"x" * len(data), "later_file")):            # not ours
            with open(bad, "wb") as f:
                f.write(changed)
            with self.assertRaises(ShareError) as cm:
                share.open_later(ticket, bad, self.inbox)
            self.assertEqual(cm.exception.kind, kind)
            self.assertFalse(os.path.isdir(self.inbox) and os.listdir(self.inbox))

    def test_find_download(self):
        name = "0a1b2c3d.amsend"
        self.assertIsNone(share_later.find_download(name, 10, [self.downloads]))
        for n, size in (("0a1b2c3d (1).amsend", 10), ("0a1b2c3d.amsend.crdownload", 10),
                        ("other.amsend", 10), (name, 9)):
            with open(os.path.join(self.downloads, n), "wb") as f:
                f.write(b"x" * size)
        self.assertEqual(share_later.find_download(name, 10, [self.downloads]),
                         os.path.join(self.downloads, "0a1b2c3d (1).amsend"))


class LaterCryptoTests(unittest.TestCase):
    def test_ece_round_trip_and_sizes(self):
        key = os.urandom(16)
        for n in (0, 1, share_later.ECE_DATA, share_later.ECE_DATA + 1, 300000):
            data = os.urandom(n)
            box = share_later.ece_encrypt(key, data)
            self.assertEqual(len(box), share_later.ece_size(n))
            self.assertEqual(share_later.ece_decrypt(key, box), data)
        with self.assertRaises(Exception):
            share_later.ece_decrypt(os.urandom(16), share_later.ece_encrypt(key, b"abc"))

    def test_record(self):
        rkey, verifier = share_later.record_keys(b"AM1:37:amber-lamp-river", b"s" * 16)
        rkey2, verifier2 = share_later.record_keys(b"AM1:37:amber-lamp-rive", b"s" * 16)
        self.assertNotEqual(verifier, verifier2)
        rec = {"v": 1, "link": "https://file.kiwi/abc123#" + "k" * 22, "key": "a" * 64,
               "size": 5, "sha256": "b" * 64, "blob": 70, "name": "0a1b2c3d.amsend"}
        sealed = share_later.seal_record(rkey, 37, rec)
        self.assertEqual(share_later.open_record(rkey, 37, sealed), rec)
        for bad in (lambda: share_later.open_record(rkey2, 37, sealed),
                    lambda: share_later.open_record(rkey, 38, sealed),
                    lambda: share_later.open_record(
                        rkey, 37, share_later.seal_record(rkey, 37, dict(rec, link="https://evil.example/x#k"))),
                    lambda: share_later.open_record(
                        rkey, 37, share_later.seal_record(rkey, 37, dict(rec, name="Player.amsend")))):
            with self.assertRaises(ValueError):
                bad()

    def test_link_rules(self):
        ok = "https://file.kiwi/abcd1234#" + "A" * 22
        self.assertTrue(share_later.link_ok(ok))
        for bad in ("http://file.kiwi/abcd1234#" + "A" * 22, "https://file.kiwi/abcd1234",
                    "https://file.kiwi.evil.com/a#" + "A" * 22,
                    "https://file.kiwi/a?x=1#" + "A" * 22, "https://u@file.kiwi/a#" + "A" * 22):
            self.assertFalse(share_later.link_ok(bad), bad)


WORMHOLE_DIR = os.environ.get("ADDONMANAGER_WORMHOLE_DIR", "")


@unittest.skipUnless(WORMHOLE_DIR and os.path.isdir(os.path.join(WORMHOLE_DIR, "node_modules",
                                                                 "wormhole-crypto")),
                     "set ADDONMANAGER_WORMHOLE_DIR to a folder with wormhole-crypto installed")
class WormholeMatchTests(unittest.TestCase):
    """Our RFC 8188 against the real wormhole-crypto (what file.kiwi's page uses): node
    decrypts ours, we decrypt node's. Needs node + `npm install wormhole-crypto@0.3.1` in
    a temp folder (never in the project)."""

    def test_match(self):
        import subprocess
        base = fixtures.temp_dir("share_match_")
        self.addCleanup(shutil.rmtree, base, True)
        key = os.urandom(16)
        with open(os.path.join(base, "key.txt"), "w") as f:
            f.write(share_later.b64url(key))
        sizes = {"empty": 0, "one": 1, "rec": share_later.ECE_DATA, "big": 3 * 1024 * 1024 + 7}
        for tag, n in sizes.items():
            plain = os.urandom(n)
            with open(os.path.join(base, f"plain_{tag}.bin"), "wb") as f:
                f.write(plain)
            with open(os.path.join(base, f"py_{tag}.bin"), "wb") as f:
                f.write(share_later.ece_encrypt(key, plain))
        script = os.path.join(WORMHOLE_DIR, "match.mjs")
        out = subprocess.run(["node", script, base], capture_output=True, text=True,
                             cwd=WORMHOLE_DIR, timeout=120)
        self.assertIn("NODE_OK", out.stdout, out.stderr)
        for tag in sizes:
            with open(os.path.join(base, f"plain_{tag}.bin"), "rb") as f, \
                    open(os.path.join(base, f"node_{tag}.bin"), "rb") as g:
                self.assertEqual(share_later.ece_decrypt(key, g.read()), f.read())


LIVE_URL = os.environ.get("ADDONMANAGER_SHARE_TEST_URL", "")


@unittest.skipUnless(LIVE_URL.startswith(("http://127.0.0.1:", "http://localhost:")),
                     "set ADDONMANAGER_SHARE_TEST_URL to a local `wrangler dev` address")
class LocalWorkerTests(ShareTests):
    """The same client against the real worker.js under `wrangler dev` (local only):
    the tests that don't need the fake relay's cheating hooks or clock."""

    def setUp(self):
        fast(self)
        self.base = fixtures.temp_dir("p2p_worker_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root, self.addons, self.client = make_pc(os.path.join(self.base, "pc1"))
        self.path, _s, _c = backup.make(self.addons, os.path.join(self.base, "backups"))
        self.inbox = os.path.join(self.base, "inbox")
        self.relay = mock.Mock(url=LIVE_URL, stored=lambda: {})

    def test_bigger_file_many_chunks_and_retries(self):
        big = os.path.join(self.base, "big", "AddonManager backup 2026-11-10.zip")
        os.makedirs(os.path.dirname(big))
        with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as z:
            z.writestr("backup.json", json.dumps({"app": "AddonManager", "format": 1}))
            z.writestr("AddOns/Big/Big_Forever.toc", "## Version: 1\n")
            z.writestr("AddOns/Big/data.bin", os.urandom(6 * share.CHUNK + 1234))
        s = Sender(big, LIVE_URL)
        path = self.receive(s.code())
        s.join()
        self.assertIsNone(s.error)
        self.assertSameBackup(path, big)

    def test_bad_token_and_paths(self):
        r = share.Relay(LIVE_URL, allow_local=True)
        ch, _ttl = r.open()
        r.token = "0" * 32
        with self.assertRaises(ShareError):
            r.put_msg("pakeA", b"x")
        for path in ("/v1/0/msg/pakeA", "/v1/500/msg/pakeA", "/v2/open", "/v1/x/close"):
            status, _d = r._once("GET", path)
            self.assertEqual(status, 404, path)

    # Need the fake relay's hooks or clock: not run against the real Worker.
    def _skip(self):
        self.skipTest("fake relay only")
    test_expired_code = test_tampered_chunk = test_tampered_header = _skip
    test_cut_off = test_replayed_chunk_from_another_transfer = _skip
    test_bad_confirmation_from_relay = test_receiver_cancel_removes_part = _skip
    test_busy_offline_and_rate = test_only_real_backups_are_sent = _skip


@unittest.skipUnless(LIVE_URL.startswith(("http://127.0.0.1:", "http://localhost:")),
                     "set ADDONMANAGER_SHARE_TEST_URL to a local `wrangler dev` address")
class LocalLaterWorkerTests(unittest.TestCase):
    """The send-later mailbox rules of the real worker.js under `wrangler dev --local`."""

    def later_box(self):
        s = share.Relay(LIVE_URL, allow_local=True)
        ch, _ttl = s.open()
        self.assertTrue(s.start_later(1, secrets.token_hex(16), "a" * 64))
        return s, ch

    def test_state_needs_the_join_token(self):
        s, ch = self.later_box()
        r = share.Relay(LIVE_URL, allow_local=True)
        r.channel = ch
        status, _d = r._once("GET", f"/v1/{ch}/later?wait=0")
        self.assertEqual(status, 403)
        self.assertEqual(r.join(ch), "later")
        self.assertEqual(r.later_state(0)["state"], "uploading")
        s.close("cancel")

    def test_no_wait_once_uploaded(self):
        s, ch = self.later_box()
        s.put_record(os.urandom(64))
        r = share.Relay(LIVE_URL, allow_local=True)
        self.assertEqual(r.join(ch), "later")
        t0 = time.monotonic()
        self.assertEqual(r.later_state(10)["state"], "ready")
        self.assertLess(time.monotonic() - t0, 3)
        s.close("cancel")

    def test_one_guess_even_at_the_same_time(self):
        s, ch = self.later_box()
        s.put_record(os.urandom(64))
        takers = [share.Relay(LIVE_URL, allow_local=True) for _ in range(6)]
        for r in takers:
            self.assertEqual(r.join(ch), "later")
        got = []

        def take(r, v):
            got.append(r._once("POST", f"/v1/{ch}/take",
                               json.dumps({"verifier": v}).encode())[0])
        threads = [threading.Thread(target=take, args=(r, f"{i:x}" * 64))
                   for i, r in enumerate(takers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([g for g in got if g in (200, 400, 403)]), 1, got)

    def test_body_without_length_is_capped(self):
        import http.client
        s = share.Relay(LIVE_URL, allow_local=True)
        ch, _ttl = s.open()
        host = urllib.parse.urlsplit(LIVE_URL)
        c = http.client.HTTPConnection(host.hostname, host.port, timeout=10)
        c.putrequest("PUT", f"/v1/{ch}/msg/pakeA")
        c.putheader("X-Token", s.token)
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        for _ in range(10):
            c.send(b"3e8\r\n" + b"x" * 1000 + b"\r\n")
        c.send(b"0\r\n\r\n")
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        s.close("cancel")


class CodeTests(unittest.TestCase):
    def test_word_list(self):
        self.assertEqual(len(WORDS), 256)
        self.assertEqual(len(set(WORDS)), 256)
        self.assertEqual(len({w[:3] for w in WORDS}), 256)
        banned = ("wow", "war", "warcraft", "blizzard", "orc", "horde", "alliance")
        for w in WORDS:
            self.assertRegex(w, r"^[a-z]{3,6}$")
            self.assertNotIn(w, banned)

    def test_parse_code(self):
        want = (37, ["amber", "lamp", "river"])
        for text in ("37-amber-lamp-river", " 37 Amber LAMP river ", "37,amb,lam,riv",
                     "37-ambre-lampp-rivr", "37.amber.lamp.river"):
            self.assertEqual(share.parse_code(text), want, text)
        for text in ("", "37-amber-lamp", "amber-lamp-river-37", "0-amber-lamp-river",
                     "500-amber-lamp-river", "37-qqq-lamp-river", "37-am-lamp-river",
                     "37-amber-lamp-river-dog", "0037-amber-lamp-river"):
            with self.assertRaises(ShareError, msg=text):
                share.parse_code(text)

    def test_new_code_round_trips(self):
        for _ in range(50):
            words = share.new_words()
            self.assertEqual(share.parse_code(share.format_code(12, words)), (12, words))

    def test_relay_address(self):
        share.Relay("https://guild-share.example.workers.dev")
        for url in ("http://guild-share.example.workers.dev", "ftp://x", "",
                    "https://user:pw@x.dev", "http://127.0.0.1:5000"):
            with self.assertRaises(ShareError, msg=url):
                share.Relay(url)
        share.Relay("http://127.0.0.1:5000", allow_local=True)


class ReceivedFileTests(unittest.TestCase):
    """What backup.read refuses in a file that came from another PC."""

    def setUp(self):
        self.base = fixtures.temp_dir("p2p_received_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def zip(self, name, entries):
        return raw_backup(os.path.join(self.base, name), entries)

    def test_names(self):
        p = backup.received_path(self.base)
        self.assertTrue(p.endswith(" received.zip"))
        self.assertTrue(backup.is_received(p))
        self.assertTrue(backup.is_received("AddonManager backup 2026-11-10 received (2).zip"))
        self.assertFalse(backup.is_received("AddonManager backup 2026-11-10.zip"))
        self.assertFalse(backup.is_received("Before restore 2026-11-10 2130.zip"))

    def test_chat_logs_refused_when_received(self):
        for name in ("ChatLog.lua", "chat-cache.txt", "Prat_ChatHistory.lua"):
            entries = [("AddOns/A/A.toc", "## Version: 1"),
                       (f"WTF/Account/ACC/SavedVariables/{name}", "x")]
            p = self.zip(f"AddonManager backup 2026-11-10 received {name}.zip", entries)
            with self.assertRaises(backup.BackupError, msg=name):
                backup.read(p, received=True)
        # This PC's own older backup: a chat-history file is just left out.
        p = self.zip("AddonManager backup 2026-11-01.zip",
                     [("AddOns/A/A.toc", "## Version: 1"),
                      ("WTF/Account/ACC/SavedVariables/ChatHistory.lua", "x"),
                      ("WTF/Account/ACC/SavedVariables/A.lua", "x")])
        info = backup.read(p)
        self.assertFalse(info.received)
        self.assertEqual(info.settings_files, 1)

    def test_outside_addons_and_account_refused(self):
        for entry in ("WTF/Config.wtf", "Logs/WoWChatLog.txt", "WTF/Account/ACC/../x.lua",
                      "Interface/AddOns/A/A.toc", "Wow.exe"):
            p = self.zip("AddonManager backup 2026-11-10 received.zip",
                         [("AddOns/A/A.toc", "## Version: 1"), (entry, "x")])
            with self.assertRaises(backup.BackupError, msg=entry):
                backup.read(p)

    def test_config_wtf_at_any_depth(self):   # tester L2
        for entry in ("WTF/Account/ACC/Config.wtf", "WTF/Account/ACC/R/C/config.wtf",
                      "WTF/Account/ACC/SavedVariables/CONFIG.WTF"):
            entries = [("AddOns/A/A.toc", "## Version: 1"), (entry, "x"),
                       ("WTF/Account/ACC/SavedVariables/A.lua", "x")]
            p = self.zip("AddonManager backup 2026-11-10 received.zip", entries)
            with self.assertRaises(backup.BackupError, msg=entry):
                backup.read(p)
            own = self.zip("AddonManager backup 2026-11-01.zip", entries)
            info = backup.read(own)          # own older backup: left out, not restored
            self.assertEqual(info.settings_files, 1, entry)
            self.assertNotIn(entry, [zi.filename for zi, _p, _k in info.entries])

    def test_chat_history_addons(self):
        refused = ["WTF/Account/ACC/SavedVariables/Elephant.lua",
                   "WTF/Account/ACC/SavedVariables/WIM.lua",
                   "WTF/Account/ACC/R/C/SavedVariables/Prat-3.0.lua",
                   "WTF/Account/ACC/R/C/SavedVariables/Elephant.lua"]
        for entry in refused:
            p = self.zip("AddonManager backup 2026-11-10 received.zip",
                         [("AddOns/A/A.toc", "## Version: 1"), (entry, "x")])
            with self.assertRaises(backup.BackupError, msg=entry):
                backup.read(p)
        p = self.zip("AddonManager backup 2026-11-10 received.zip",   # Prat's real settings
                     [("AddOns/A/A.toc", "## Version: 1"),
                      ("WTF/Account/ACC/SavedVariables/Prat-3.0.lua", "x")])
        self.assertEqual(backup.read(p).settings_files, 1)

    def test_received_mark_inside(self):
        p = self.zip("AddonManager backup 2026-11-10 received.zip",
                     [("AddOns/A/A.toc", "## Version: 1")])
        backup.mark_received(p)
        backup.mark_received(p)              # twice: still one mark
        renamed = os.path.join(self.base, "my old stuff.zip")
        os.rename(p, renamed)
        self.assertTrue(backup.has_received_mark(renamed))
        info = backup.read(renamed)
        self.assertTrue(info.received)
        with zipfile.ZipFile(renamed) as z:
            self.assertEqual(z.namelist().count(backup.RECEIVED_NAME), 1)
        own = self.zip("AddonManager backup 2026-11-02.zip", [("AddOns/A/A.toc", "x")])
        self.assertFalse(backup.read(own).received)
        self.assertFalse(backup.has_received_mark(own))

    def test_newest_own_skips_received(self):   # tester L5
        from am import share_ui
        marked = self.zip("AddonManager backup 2026-11-09.zip", [("AddOns/A/A.toc", "x")])
        backup.mark_received(marked)
        own = self.zip("AddonManager backup 2026-11-01.zip", [("AddOns/A/A.toc", "x")])

        def f(path, safety=False, online=False):
            return backup.FoundBackup(path, "Documents", 1, 0, safety, online)
        found = [f(os.path.join(self.base, "AddonManager backup 2026-11-10 received.zip")),
                 f(marked), f(os.path.join(self.base, "Before restore 2026.zip"), safety=True),
                 f(os.path.join(self.base, "AddonManager backup 2026-11-05.zip"), online=True),
                 f(own)]
        self.assertEqual(share_ui.newest_own(found).path, own)
        self.assertIsNone(share_ui.newest_own(found[:4]))

    def test_clean_partials(self):   # tester L6
        import queue as queue_mod
        import time
        old = os.path.join(self.base, share.PART_PREFIX + "aaaa" + share.PART_EXT)
        new = os.path.join(self.base, share.PART_PREFIX + "bbbb" + share.PART_EXT)
        other = os.path.join(self.base, "keep.part")
        for path in (old, new, other):
            with open(path, "wb") as fh:
                fh.write(b"x")
        past = time.time() - share.PART_MAX_AGE - 10
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.assertEqual(share.clean_partials([self.base, self.base, None,
                                               os.path.join(self.base, "missing")]), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new) and os.path.exists(other))
        # The Backups found search cleans its folders too.
        from am import backup_ui
        os.utime(new, (past, past))
        with mock.patch.object(backup, "cloud_folders", lambda: []):
            backup_ui._find_worker(queue_mod.Queue(), self.base)
        self.assertFalse(os.path.exists(new))

    def test_make_leaves_chat_logs_out(self):
        _root, addons, client = make_pc(os.path.join(self.base, "pc"))
        acc = os.path.join(client, "WTF", "Account", "ACCOUNT1", "SavedVariables")
        for name in ("ChatLogger.lua", "Elephant.lua", "Config.wtf"):
            with open(os.path.join(acc, name), "w") as f:
                f.write("private")
        path, _s, _c = backup.make(addons, os.path.join(self.base, "out"))
        with zipfile.ZipFile(path) as z:
            names = " ".join(z.namelist()).lower()
        self.assertNotIn("chat", names)
        self.assertNotIn("config.wtf", names)
        self.assertNotIn("elephant", names)

    def test_addon_versions(self):
        p = self.zip("AddonManager backup 2026-11-10 received.zip",
                     [("AddOns/TaxiTime/TaxiTime_Forever.toc", "## Version: 2.0.1\n"),
                      ("AddOns/Other/Other.toc", "## Version: 3\n")])
        info = backup.read(p)
        self.assertEqual(backup.addon_versions(info), {"TaxiTime": "2.0.1", "Other": None})


class WindowShareTests(unittest.TestCase):
    """The hidden window (never shown): Send and Receive windows against the fake relay."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("p2p_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        import tkinter as tk
        from am import config
        fast(self)
        self.tk = tk
        self.work = fixtures.temp_dir("p2p_ui_")
        self.addCleanup(fixtures.remove, self.work)
        self.root_dir, self.addons, self.client = make_pc(os.path.join(self.work, "pc"))
        self.asked, self.copied = [], []
        # Its own Documents folder: other tests' backup folders stay as they were.
        docs = mock.patch.dict(os.environ, {backup.DOCUMENTS_ENV: os.path.join(self.work, "docs")})
        docs.start()
        self.addCleanup(docs.stop)
        config.save_settings({})
        p = mock.patch.object(backup, "cloud_folders", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        import gc
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass
        self.root = None
        gc.collect()

    def confirm(self, title, text, ok, cancel):
        self.asked.append(text)
        return True

    def make_app(self, share_url=None):
        from am import ui
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     share_url=self.relay.url if share_url is None else share_url)
        app.clipboard = self.copied.append
        self.pump_until(app, lambda: not app.busy)
        return app

    def pump_until(self, app, cond, timeout=30):
        import time
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if cond():
                for _ in range(3):
                    self.root.update()
                return True
            time.sleep(0.02)
        raise AssertionError("timed out")

    def texts(self, widget):
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except self.tk.TclError:
                pass
            out.extend(self.texts(w))
        return out

    def test_buttons_only_with_a_relay(self):
        from am import errors
        app = self.make_app(share_url="")
        hub = app.open_backup_window(show=False)
        self.assertNotIn(errors.SEND_BUTTON, self.texts(hub.win))
        self.assertIsNone(app.open_send(show=False))
        app.close_backup_window()
        app.share_url = self.relay.url
        hub = app.open_backup_window(show=False)
        texts = self.texts(hub.win)
        self.assertIn(errors.SEND_BUTTON, texts)
        self.assertIn(errors.RECEIVE_BUTTON, texts)
        self.assertIn(errors.BACKUP_OTHER_PC, texts)
        self.assertNotIn(errors.SEND_BUTTON, self.texts(app.open_settings(show=False)))
        app.close_settings()
        hub.win.update_idletasks()
        self.assertLessEqual(hub.win.winfo_reqwidth(), 420)
        hub.share_buttons[0].invoke()               # Send: over the Backup window
        self.assertEqual(type(app.backup_dialog).__name__, "SendDialog")
        self.assertIs(app.backup_dialog.parent, hub.win)
        app.backup_dialog.close()
        dlg = app.open_restore(show=False)          # Backups found: Use a code...
        self.assertIsNotNone(dlg.code_button)
        rec = dlg.use_code()
        self.assertIs(app.backup_dialog, rec)
        self.assertEqual(type(rec).__name__, "ReceiveDialog")

    def test_send_window(self):
        from am import errors
        backup.make(self.addons, backup.default_folder())   # the newest backup to offer
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_send(show=False)
        texts = self.texts(dlg.win)
        self.assertIn(errors.SEND_PRIVATE, texts)            # the warning before any code
        self.assertIn("other players", errors.SEND_PRIVATE)
        self.assertNotIn("sharing your account", " ".join(texts).lower())
        # The choices use the dark theme's readable colours (not the light default), in
        # every state.
        from tkinter import ttk
        from am import theme
        style = ttk.Style(self.root)
        for w in (dlg.new_radio, dlg.newest_radio, dlg.other_radio):
            self.assertEqual(w.winfo_class(), "TRadiobutton")
        for state, fg in (([], theme.TEXT), (["selected"], theme.TEXT),
                          (["active"], theme.TEXT), (["disabled"], theme.DISABLED)):
            self.assertEqual(style.lookup("TRadiobutton", "foreground", state), fg, state)
            self.assertEqual(style.lookup("TRadiobutton", "background", state), theme.BG, state)
        self.assertEqual(style.lookup("TRadiobutton", "indicatorbackground", ["selected"]),
                         theme.ACCENT)
        self.pump_until(app, lambda: dlg.newest is not None)
        self.assertTrue(dlg.newest_radio.cget("text").startswith(errors.SEND_NEWEST))
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        self.assertTrue(dlg.start())                         # a new backup now
        self.pump_until(app, lambda: dlg.code is not None)
        self.assertFalse(app.busy)
        self.assertIn(dlg.code, self.texts(dlg.win))
        self.assertTrue(dlg.ends_var.get().startswith("Works once. Ends in "))
        dlg.copy()
        self.assertEqual(self.copied, [dlg.code])
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        # The other PC (a thread here) receives it.
        out = {}
        inbox = os.path.join(self.work, "inbox")
        t = threading.Thread(target=lambda: out.setdefault("path", share.receive(
            dlg.code, share.Relay(self.relay.url, allow_local=True), inbox)))
        t.start()
        self.pump_until(app, lambda: not dlg.active)
        t.join(20)
        self.assertEqual(dlg.status_var.get(), errors.SEND_SENT)
        self.assertTrue(backup.is_received(out["path"]))
        dlg.close()
        self.assertIsNone(app.backup_dialog)

    def test_send_asks_about_spoken(self):
        from am import config, errors
        for name in ("Spoken", "SpokenQuests"):
            fixtures.make_addon_folder(self.addons, name, "1.0")
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_send(show=False)
        self.assertTrue(dlg.start())
        self.pump_until(app, lambda: dlg.spoken_ask is not None)
        self.assertIn("Spoken (AI voice-over addon)", " ".join(self.texts(dlg.spoken_ask.win)))
        self.assertFalse(dlg.active)                     # nothing made before the answer
        dlg.spoken_ask.answer("skip")
        self.assertEqual(config.load_settings()["backup_spoken"], "skip")
        self.assertIn(errors.SPOKEN_LEFT_OUT, dlg.status_var.get())
        self.pump_until(app, lambda: dlg.code is not None)
        out = {}
        inbox = os.path.join(self.work, "inbox")
        t = threading.Thread(target=lambda: out.setdefault("path", share.receive(
            dlg.code, share.Relay(self.relay.url, allow_local=True), inbox)))
        t.start()
        self.pump_until(app, lambda: not dlg.active)
        t.join(20)
        info = backup.read(out["path"])
        self.assertEqual(info.left_out, ["Spoken", "SpokenQuests"])
        self.assertNotIn("Spoken", info.addons)
        dlg.close()

    def test_hours_setting_checked(self):   # tester L3
        from am import config
        app = self.make_app()
        app.open_backup_window(show=False)
        for bad in (True, 48, 3, "12", 0):
            config.save_settings({"send_later_hours": bad})
            if bad is True:   # a bool isn't an int setting: dropped on load
                self.assertNotIn("send_later_hours", config.load_settings())
            dlg = app.open_send(show=False)
            self.assertEqual(dlg.hours_var.get(), share_later.DEFAULT_HOURS, bad)
            dlg.close()
        config.save_settings({"send_later_hours": 4})
        dlg = app.open_send(show=False)
        self.assertEqual(dlg.hours_var.get(), 4)
        dlg.close()

    def test_damaged_download_opened_once(self):   # tester M1
        from am import errors, share_ui
        kiwi = FakeKiwi()
        self.addCleanup(kiwi.stop)
        for mod, name, value in ((share_later, "LATER_AFTER", 1), (share, "PING_EVERY", 1)):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        downloads = os.path.join(self.work, "Downloads")
        os.makedirs(downloads)
        env = mock.patch.dict(os.environ, {share_later.DOWNLOADS_ENV: downloads})
        env.start()
        self.addCleanup(env.stop)
        backup.make(self.addons, backup.default_folder())
        app = self.make_app()
        app.later_api = kiwi.url
        app.open_link = lambda u: None
        app.open_backup_window(show=False)
        dlg = app.open_send(show=False)
        dlg.toggle_later()
        dlg.start()
        self.pump_until(app, lambda: dlg.later_done, timeout=60)
        code = dlg.code
        self.pump_until(app, lambda: not dlg.active)
        dlg.close()
        rec = app.open_receive(show=False)
        rec.code_var.set(code)
        rec.start()
        self.pump_until(app, lambda: rec.ticket is not None, timeout=60)
        name, data = kiwi.download(rec.ticket.link)
        bad = bytearray(data)
        bad[-40] ^= 0xFF                                  # same size, one byte changed
        path = os.path.join(downloads, name)
        with open(path, "wb") as f:
            f.write(bytes(bad))
        opens = []
        real = share.open_later

        def counting(*a, **k):
            opens.append(1)
            return real(*a, **k)
        with mock.patch.object(share, "open_later", counting),                 mock.patch.object(share_ui, "DOWNLOAD_POLL_MS", 50):
            end = time.time() + 3
            while time.time() < end:
                self.root.update()
                time.sleep(0.02)
            self.assertEqual(len(opens), 1)                # tried once, not in a loop
            self.assertIn(rec.status_var.get(), {                 # the error stays on screen
                errors.share_message(ShareError(k)) for k in ("later_file", "broken")})
            self.assertFalse(rec.open_button.instate(["disabled"]))
            with open(path, "wb") as f:                     # a new, good download: opened
                f.write(data)
            os.utime(path, ns=(time.time_ns(), time.time_ns() + 10 ** 9))
            self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog",
                            timeout=30)
        self.assertEqual(len(opens), 2)

    def test_send_later_window(self):
        """Send later: off by default and remembered, honest text, then the whole way:
        upload (fake file.kiwi), Receive -> Download -> found in Downloads -> Restore."""
        from am import config, errors
        kiwi = FakeKiwi()
        self.addCleanup(kiwi.stop)
        for mod, name, value in ((share_later, "LATER_AFTER", 1), (share, "PING_EVERY", 1)):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        downloads = os.path.join(self.work, "Downloads")
        os.makedirs(downloads)
        env = mock.patch.dict(os.environ, {share_later.DOWNLOADS_ENV: downloads})
        env.start()
        self.addCleanup(env.stop)
        backup.make(self.addons, backup.default_folder())
        app = self.make_app()
        app.later_api = kiwi.url
        links = []
        app.open_link = links.append
        app.open_backup_window(show=False)
        dlg = app.open_send(show=False)
        texts = self.texts(dlg.win)
        for text in (errors.LATER_TICK, errors.LATER_NOTE, errors.LATER_TERMS,
                     errors.LATER_PRIVACY):
            self.assertIn(text, texts)
        for word in ("US", "IP address", "size", "encrypted", "can't be deleted early",
                     "a few days"):
            self.assertIn(word, errors.LATER_NOTE)
        self.assertNotIn("only the two", " ".join(texts).lower())
        self.assertNotIn("premium", " ".join(texts).lower())       # no paid-tier pointers
        self.assertFalse(dlg.later_var.get())                      # off by default
        self.assertIn("disabled", dlg.hours_button.state())
        self.assertTrue(all(link.bind("<Button-1>") for link in dlg.later_links))
        dlg.toggle_later()
        settings = config.load_settings()
        self.assertEqual((settings["send_later"], settings["send_later_hours"],
                          settings["send_later_v"]), (True, 12, share_later.CONSENT_VERSION))
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        self.assertTrue(dlg.start())
        self.pump_until(app, lambda: dlg.code is not None)
        self.assertTrue(dlg.ends_var.get().startswith("Works once. Not here in "))
        self.pump_until(app, lambda: dlg.later_done, timeout=60)
        self.assertTrue(dlg.status_var.get().startswith("Uploaded."))
        code = dlg.code
        self.pump_until(app, lambda: not dlg.active)
        dlg.close()
        again = app.open_send(show=False)                          # the tick is remembered
        self.assertTrue(again.later_var.get())
        again.close()
        old = config.load_settings()                               # ticked under the old
        old.pop("send_later_v")                                    # wording: counts as off
        config.save_settings(old)
        again = app.open_send(show=False)
        self.assertFalse(again.later_var.get())
        again.close()
        rec = app.open_receive(show=False)
        rec.code_var.set(code)
        self.assertTrue(rec.start())
        self.pump_until(app, lambda: rec.ticket is not None, timeout=60)
        self.assertIn(errors.LATER_GOT, self.texts(rec.win))
        rec.download()
        self.assertEqual(links, [rec.ticket.link])
        name, data = kiwi.download(rec.ticket.link)               # the browser saves it
        with open(os.path.join(downloads, name), "wb") as f:
            f.write(data)
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog",
                        timeout=60)
        self.assertTrue(app.backup_dialog.info.received)

    def test_send_cancel(self):
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_send(show=False)
        dlg.start()
        self.pump_until(app, lambda: dlg.code is not None)
        dlg.close()                                    # Cancel while waiting
        self.assertIsNone(app.backup_dialog)
        with self.assertRaises(ShareError):
            for _ in range(100):
                share.Relay(self.relay.url, allow_local=True).join(
                    share.parse_code(dlg.code)[0])
                self.root.after(50)

    def test_receive_then_restore_safely(self):
        from am import errors
        # The other PC: same account name, an addon that isn't the guild's.
        _r, other_addons, _c = make_pc(os.path.join(self.work, "other"), tag="new")
        fixtures.make_addon_folder(other_addons, "NotGuild", "1.0")
        sent, _s, _n = backup.make(other_addons, os.path.join(self.work, "sent"))
        s = Sender(sent, self.relay.url)
        code = s.code()
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_receive(show=False)
        self.assertIn(errors.RECEIVE_NOTE, self.texts(dlg.win))
        dlg.code_var.set("not a code")
        self.assertFalse(dlg.start())
        self.assertEqual(dlg.status_var.get(), errors.share_message(ShareError("bad_format")))
        dlg.code_var.set(code.upper().replace("-", " "))
        self.assertTrue(dlg.start())
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog")
        s.join()
        self.assertIsNone(s.error)
        rd = app.backup_dialog
        self.assertTrue(rd.info.received)
        self.assertFalse(rd.addons_var.get())                # addons off by default
        texts = " ".join(self.texts(rd.win))
        self.assertIn(errors.RECEIVED_ADDONS_NOTE, texts)
        self.assertIn("Not on the guild list: ", texts)
        self.assertIn("NotGuild", texts)
        self.assertIn("Not the guild's version: TaxiTime 1.2.0", texts)
        # Same account name here, but the player still picks it.
        self.assertEqual(rd.account_vars["ACCOUNT1"].get(), errors.RESTORE_CHOOSE)
        self.assertFalse(rd.start())
        self.assertEqual(rd.status_var.get(), errors.RESTORE_CHOOSE_FIRST)
        self.assertEqual(self.asked, [])
        rd.account_vars["ACCOUNT1"].set("ACCOUNT1")
        self.assertTrue(rd.start())
        self.pump_until(app, lambda: rd.result is not None or not rd.running)
        self.assertIsNotNone(rd.result)
        self.assertNotIn("NotGuild", os.listdir(self.addons))  # addons were left alone
        rd.win.update_idletasks()
        self.assertLessEqual(rd.win.winfo_reqwidth(), 420)

    def test_restore_long_account_names(self):   # tester L3
        from am import errors
        long_name = "VERYLONGACCOUNTNAME12345#1WWWWWWWW"
        os.rename(os.path.join(self.client, "WTF", "Account", "ACCOUNT1"),
                  os.path.join(self.client, "WTF", "Account", long_name))
        _r, other, _oc = make_pc(os.path.join(self.work, "other"),
                                 account="WWWWWWWWWWWWWWWWWWWWWWWW")
        p, _s, _c = backup.make(other, os.path.join(self.work, "sent"))
        received = os.path.join(self.work, "AddonManager backup 2026-11-10 received.zip")
        shutil.copy(p, received)
        app = self.make_app()
        app.open_backup_window(show=False)
        rd = app.open_restore(show=False, path=received)
        for var in rd.account_vars.values():
            var.set(long_name)
        rd.win.update_idletasks()
        self.assertLessEqual(rd.win.winfo_reqwidth(), 420)
        self.assertIn(errors.RECEIVED_ADDONS_NOTE, self.texts(rd.win))

    def test_licences_window(self):
        from am import backup_ui, errors
        app = self.make_app(share_url="")
        win = app.open_settings(show=False)
        self.assertEqual(app.licences_link.cget("text"), errors.LICENCES_LINK)
        dlg = app.open_licences(show=False)
        text = dlg.text.get("1.0", "end")
        self.assertIn("THIRD-PARTY NOTICES", text)
        self.assertIn("cryptography", text)
        self.assertEqual(str(dlg.text.cget("state")), "disabled")   # read-only
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        dlg.close()
        self.assertIsNone(app.backup_dialog)
        with mock.patch.object(backup_ui, "notices_path", lambda: os.path.join(self.work, "x")):
            self.assertEqual(backup_ui.read_notices(), errors.LICENCES_MISSING)
        win.update_idletasks()
        self.assertLessEqual(win.winfo_reqwidth(), 420)

    def test_receive_wrong_code(self):
        from am import errors
        s = Sender(self.path_for_send(), self.relay.url)
        ch, words = share.parse_code(s.code())
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_receive(show=False)
        folder = backup.default_folder()
        before = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        dlg.code_var.set(share.format_code(ch, [w for w in WORDS if w != words[0]][:1]
                                           + words[1:]))
        dlg.start()
        self.pump_until(app, lambda: not dlg.active)
        self.assertEqual(dlg.status_var.get(), errors.share_message(ShareError("wrong_code")))
        self.assertEqual(sorted(os.listdir(folder)) if os.path.isdir(folder) else [], before)
        s.join()

    def path_for_send(self):
        path, _s, _c = backup.make(self.addons, os.path.join(self.work, "send"))
        return path


if __name__ == "__main__":
    unittest.main()
