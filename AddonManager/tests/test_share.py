"""Send a backup with a code (am\\share.py) against the local fake relay (tests\\fake_relay.py).
Fake WoW folders and backup folders in temp folders only; nothing goes online."""

import json
import os
import queue
import shutil
import threading
import unittest
import zipfile
from unittest import mock

from am import backup, share
from am.share import ShareError
from am.share_words import WORDS
from tests import fixtures
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
        for bad in ({"chunks": 2}, {"size": 0}, {"sha256": "x"}, {"v": 2}, {"size": "10"}):
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
        with mock.patch.object(share, "MAX_SIZE", 10):
            with self.assertRaises(ShareError) as cm:
                share.send(self.path, share.Relay(self.relay.url, allow_local=True),
                           lambda c, t: None)
        self.assertEqual(cm.exception.kind, "too_big")


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
        win = app.open_settings(show=False)
        self.assertNotIn(errors.SEND_BUTTON, self.texts(win))
        self.assertIsNone(app.open_send(show=False))
        app.close_settings()
        app.share_url = self.relay.url
        win = app.open_settings(show=False)
        texts = self.texts(win)
        self.assertIn(errors.SEND_BUTTON, texts)
        self.assertIn(errors.RECEIVE_BUTTON, texts)
        win.update_idletasks()
        self.assertLessEqual(win.winfo_reqwidth(), 420)
        dlg = app.open_restore(show=False)          # Backups found: Use a code...
        self.assertIsNotNone(dlg.code_button)
        rec = dlg.use_code()
        self.assertIs(app.backup_dialog, rec)
        self.assertEqual(type(rec).__name__, "ReceiveDialog")

    def test_send_window(self):
        from am import errors
        backup.make(self.addons, backup.default_folder())   # the newest backup to offer
        app = self.make_app()
        app.open_settings(show=False)
        dlg = app.open_send(show=False)
        texts = self.texts(dlg.win)
        self.assertIn(errors.SEND_PRIVATE, texts)            # the warning before any code
        self.assertIn("other players", errors.SEND_PRIVATE)
        self.assertNotIn("sharing your account", " ".join(texts).lower())
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

    def test_send_cancel(self):
        app = self.make_app()
        app.open_settings(show=False)
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
        app.open_settings(show=False)
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
        app.open_settings(show=False)
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
        app.open_settings(show=False)
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
