"""Locked kept copies of received backups (am\\backup_lock.py), the encrypted codes file
(am\\codes.py), the relay's sender /status route (tests\\fake_relay.py = worker.js), and the
hidden windows: Restore with Save zip..., Backups found with locked rows, Unlock backup, Your
codes. Fake WoW folders, backup folders and the codes file in temp folders only."""

import json
import os
import shutil
import threading
import time
import unittest
import zipfile
from unittest import mock

from am import backup, backup_delete, backup_lock, codes, config, errors, share
from am.backup_lock import LockError
from am.share_words import WORDS
from tests import fixtures
from tests.fake_relay import FakeRelay
from tests.test_backup import make_pc, raw_backup

CODE = share.format_code(12, WORDS[:3])
OTHER = share.format_code(12, [WORDS[0], WORDS[1], WORDS[5]])


def clear_codes(test):
    """Every test starts and ends with no codes file (it lives in the tests' temp home)."""
    def rm():
        for p in (codes.store_path(), codes.store_path() + ".old"):
            if os.path.exists(p):
                os.remove(p)
    rm()
    test.addCleanup(rm)


def received_zip(base, addons):
    """A received backup as share.receive leaves it: checked, marked, "... received.zip"."""
    made, _s, _c = backup.make(addons, os.path.join(base, "sent"))
    folder = os.path.join(base, "backups")
    os.makedirs(folder, exist_ok=True)
    backup.mark_received(made)
    path = backup.received_path(folder)
    shutil.move(made, path)
    return path


class FakeTrash:
    def __init__(self, bin_dir, where="bin"):
        self.bin_dir, self.where, self.sent = bin_dir, where, []
        os.makedirs(bin_dir, exist_ok=True)

    def available(self, _path):
        return self.where

    def send(self, path):
        self.sent.append(os.path.basename(path))
        shutil.move(path, os.path.join(self.bin_dir, os.path.basename(path)))


class LockTests(unittest.TestCase):
    def setUp(self):
        clear_codes(self)
        self.base = fixtures.temp_dir("lock_core_")
        self.addCleanup(fixtures.remove, self.base)
        self.root, self.addons, self.client = make_pc(os.path.join(self.base, "pc"))
        self.zip = received_zip(self.base, self.addons)
        self.folder = os.path.dirname(self.zip)
        self.out = os.path.join(self.base, "out")

    def locked(self, code=CODE):
        dest = os.path.join(self.folder, "AddonManager backup 2026-11-14 received.amlocked")
        fid = backup_lock.lock(self.zip, dest, code)
        return dest, fid

    def test_round_trip_any_code_spelling(self):
        dest, fid = self.locked()
        self.assertEqual(backup_lock.file_id(dest), fid)
        with open(dest, "rb") as f:
            raw = f.read()
        with open(self.zip, "rb") as f:
            plain = f.read()
        self.assertNotIn(b"backup.json", raw)              # nothing readable inside
        self.assertNotIn(CODE.encode(), raw)
        typed = CODE.upper().replace("-", "  ")
        self.assertTrue(backup_lock.check_code(dest, typed))
        self.assertFalse(backup_lock.check_code(dest, OTHER))
        part = backup_lock.unlock(dest, typed, self.out)
        self.assertTrue(os.path.basename(part).startswith(backup_lock.UNLOCKED_PREFIX))
        with open(part, "rb") as f:
            self.assertEqual(f.read(), plain)

    def test_big_file_many_pieces(self):
        big = os.path.join(self.base, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(backup_lock.CHUNK * 2 + 123))
        dest = os.path.join(self.base, "big" + backup_lock.EXT)
        backup_lock.lock(big, dest, CODE)
        seen = []
        part = backup_lock.unlock(dest, CODE, self.out, progress=lambda d, t: seen.append(d))
        with open(big, "rb") as a, open(part, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(seen[-1], backup_lock.CHUNK * 2 + 123)

    def test_wrong_code_writes_nothing(self):
        dest, _fid = self.locked()
        with self.assertRaises(LockError) as cm:
            backup_lock.unlock(dest, OTHER, self.out)
        self.assertEqual(cm.exception.kind, "wrong_code")
        self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))
        with self.assertRaises(LockError) as cm:
            backup_lock.unlock(dest, "not a code", self.out)
        self.assertEqual(cm.exception.kind, "wrong_code")

    def test_damaged_cut_changed_header_newer(self):
        dest, _fid = self.locked()
        with open(dest, "rb") as f:
            raw = bytearray(f.read())
        bad = os.path.join(self.base, "bad" + backup_lock.EXT)
        flipped = bytearray(raw)
        flipped[-5] ^= 0xFF
        with open(bad, "wb") as f:
            f.write(flipped)
        with self.assertRaises(LockError) as cm:
            backup_lock.unlock(bad, CODE, self.out)
        self.assertEqual(cm.exception.kind, "damaged")
        self.assertEqual([n for n in os.listdir(self.out)], [])   # no plain part left
        with open(bad, "wb") as f:
            f.write(raw[:-10])
        self.assertEqual(self._kind(bad), "damaged")
        # a header changed by hand: the key is bound to it, so it opens like a wrong code
        n = int.from_bytes(raw[8:12], "big")
        head = json.loads(raw[12:12 + n])
        head["id"] = "0" * 32
        new = json.dumps(head, separators=(",", ":")).encode()
        with open(bad, "wb") as f:
            f.write(raw[:8] + len(new).to_bytes(4, "big") + new + raw[12 + n:])
        self.assertEqual(self._kind(bad), "wrong_code")
        with open(bad, "wb") as f:
            f.write(b"AMLOCK9\n" + raw[8:])
        self.assertEqual(self._kind(bad), "newer")
        self.assertEqual(self._kind(self.zip), "not_locked")

    def _kind(self, path):
        try:
            backup_lock.unlock(path, CODE, self.out)
        except LockError as e:
            return e.kind
        return None

    def test_hand_made_locked_file_with_config_wtf_is_refused(self):
        """tos: the unlock path runs backup.read(received=True) before any restore."""
        evil = raw_backup(os.path.join(self.base, "evil.zip"), [
            ("WTF/Account/ACCOUNT1/SavedVariables/x.lua", "x"),
            ("WTF/Config.wtf", "SET accountName x")])
        dest = os.path.join(self.base, "evil" + backup_lock.EXT)
        backup_lock.lock(evil, dest, CODE)
        with self.assertRaises(backup.BackupError):
            backup_lock.open_locked(dest, CODE, self.out)
        self.assertEqual(os.listdir(self.out), [])          # its plain copy was removed
        plain, info = backup_lock.open_locked(self.locked()[0], CODE, self.out)
        self.assertTrue(info.received)
        self.assertEqual(info.path, plain)

    def test_lock_refuses_without_room_and_cancel_leaves_nothing(self):
        dest = os.path.join(self.folder, "x" + backup_lock.EXT)
        full = mock.Mock(return_value=mock.Mock(free=10))
        with self.assertRaises(LockError) as cm:
            backup_lock.lock(self.zip, dest, CODE, disk_usage=full)
        self.assertEqual(cm.exception.kind, "disk_full")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(LockError) as cm:
            backup_lock.lock(self.zip, dest, CODE, cancel=stop)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertEqual(sorted(os.listdir(self.folder)), [os.path.basename(self.zip)])

    def test_keep_locks_hides_and_remembers(self):
        kept = backup_lock.keep(self.zip, CODE.upper(), remember=codes.remember_lock)
        self.assertIsNone(kept.lock_error)
        self.assertTrue(kept.locked.endswith(" received.amlocked"))
        self.assertFalse(os.path.exists(self.zip))              # no plain zip in sight
        self.assertTrue(os.path.basename(kept.plain).startswith(backup_lock.UNLOCKED_PREFIX))
        self.assertEqual(kept.code, CODE)
        self.assertEqual(codes.lock_code(kept.file_id), CODE)    # written the normal way
        self.assertTrue(codes.known_lock(os.path.basename(kept.locked),
                                         os.path.getsize(kept.locked)))
        self.assertNotIn(CODE, repr(kept))
        kept.discard()
        self.assertEqual(os.listdir(self.folder), [os.path.basename(kept.locked)])
        found = backup.find_backups([("Documents", self.folder)])
        self.assertEqual([(f.locked, f.safety) for f in found], [(True, False)])
        self.assertTrue(backup_delete.own_name(kept.locked))

    def test_keep_without_room_still_gives_the_plain_copy(self):
        with mock.patch.object(backup_lock.shutil, "disk_usage",
                               return_value=mock.Mock(free=10)):
            kept = backup_lock.keep(self.zip, CODE, remember=codes.remember_lock)
        self.assertIsNone(kept.locked)
        self.assertEqual(kept.lock_error.kind, "disk_full")
        self.assertTrue(os.path.exists(kept.plain))
        self.assertEqual(codes.locked(), {})
        self.assertIn("Couldn't keep a copy here", errors.kept_failed("disk_full"))

    def test_save_copy_checks(self):
        target = os.path.join(self.base, "copy", "WoW backup.zip")
        os.makedirs(os.path.dirname(target))
        with open(target, "wb") as f:
            f.write(b"old file")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(LockError) as cm:
            backup_lock.save_copy(self.zip, target, cancel=stop)
        self.assertEqual(cm.exception.kind, "cancelled")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"old file")             # untouched on a stop
        self.assertEqual(os.listdir(os.path.dirname(target)), ["WoW backup.zip"])
        backup_lock.save_copy(self.zip, target)
        with open(target, "rb") as a, open(self.zip, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(backup.read(target).received)            # still a received backup
        for kwargs, kind in (
                (dict(target=self.zip), "same_file"),
                (dict(target=target, game_check=lambda folder: True), "inside_game"),
                (dict(target=target, disk_usage=lambda f: mock.Mock(free=5)), "disk_full"),
                (dict(target=target, fat32=True), "too_big_for_drive")):
            with mock.patch.object(backup_lock, "FAT32_MAX", 10):
                with self.assertRaises(LockError) as cm:
                    backup_lock.save_copy(self.zip, **kwargs)
            self.assertEqual(cm.exception.kind, kind)
            self.assertTrue(errors.save_zip_message(cm.exception))

    def test_received_file_hidden_until_locked(self):
        """B4: the app receives into a hidden part (never a visible plain zip, even if it
        stops before locking); keep() names the locked file like a received backup."""
        from tests.test_share import Sender
        relay = FakeRelay()
        self.addCleanup(relay.stop)
        made, _s, _c = backup.make(self.addons, os.path.join(self.base, "send"))
        s = Sender(made, relay.url)
        inbox = os.path.join(self.base, "inbox")
        path = share.receive(s.code(), share.Relay(relay.url, allow_local=True,
                                                   sleep=lambda x: None), inbox, hidden=True)
        s.join()
        self.assertTrue(os.path.basename(path).startswith(backup_lock.UNLOCKED_PREFIX))
        self.assertEqual(os.listdir(inbox), [os.path.basename(path)])   # nothing visible
        self.assertTrue(backup.read(path, received=True).received)
        kept = backup_lock.keep(path, CODE)
        self.assertEqual(kept.plain, path)
        self.assertRegex(os.path.basename(kept.locked),
                         r"^AddonManager backup \d{4}-\d{2}-\d{2} received\.amlocked$")
        kept.discard()
        self.assertEqual(os.listdir(inbox), [os.path.basename(kept.locked)])
        self.assertEqual(backup_lock.clean_parts([inbox]), 0)

    def test_clean_parts_own_names_only(self):
        for name in (backup_lock.UNLOCKED_PREFIX + "ab.part", backup_lock.LOCKING_PREFIX + "cd.part",
                     "notes.part", "AddonManager backup 2026-11-14 received.amlocked"):
            with open(os.path.join(self.folder, name), "wb") as f:
                f.write(b"x")
        saving = os.path.join(self.base, "usb")
        os.makedirs(saving)
        for name in (backup_lock.SAVING_PREFIX + "ef.part", backup_lock.UNLOCKED_PREFIX + "x.part"):
            with open(os.path.join(saving, name), "wb") as f:
                f.write(b"x")
        self.assertEqual(backup_lock.clean_parts([saving], prefixes=(backup_lock.SAVING_PREFIX,)), 1)
        self.assertEqual(os.listdir(saving), [backup_lock.UNLOCKED_PREFIX + "x.part"])
        self.assertEqual(backup_lock.clean_parts([self.folder, None]), 2)
        self.assertEqual(sorted(os.listdir(self.folder)), sorted([
            "notes.part", "AddonManager backup 2026-11-14 received.amlocked",
            os.path.basename(self.zip)]))


class CodesTests(unittest.TestCase):
    def setUp(self):
        clear_codes(self)
        self.base = fixtures.temp_dir("codes_store_")
        self.addCleanup(fixtures.remove, self.base)

    def raw(self):
        with open(codes.store_path(), "rb") as f:
            return f.read()

    def test_round_trip_never_plain_and_never_in_settings(self):
        codes.remember_lock("a" * 32, "x.amlocked", 10, CODE)
        eid = codes.add_sent(OTHER, 12, "f" * 32, "adminforge", time.time() + 3600, 1,
                             [{"id": "abc", "owner": "own", "secret": "s"}])
        self.assertNotIn(CODE.encode(), self.raw())
        self.assertNotIn(b"f" * 32, self.raw())
        self.assertNotIn(WORDS[0].encode(), self.raw())
        self.assertEqual(codes.lock_code("a" * 32), CODE)
        e = codes.sent()[0]
        self.assertEqual((e["id"], e["code"], e["parts"]), (eid, OTHER, [{"id": "abc", "owner": "own"}]))
        self.assertNotIn(CODE, json.dumps(config.load_settings()))
        codes.update_sent(eid, state="taken", state_at=1.0)
        self.assertEqual(codes.sent()[0]["state"], "taken")
        codes.remove_sent(eid)
        self.assertEqual(codes.sent(), [])
        codes.forget_named("x.amlocked", 10)
        self.assertEqual(codes.locked(), {})

    def test_expired_sent_codes_go(self):
        codes.add_sent(CODE, 1, "a" * 32, "kiwi", time.time() - 1, 1)
        codes.add_sent(OTHER, 2, "b" * 32, "kiwi", time.time() + 60, 1)
        self.assertEqual([e["code"] for e in codes.sent()], [OTHER])

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows only")
    def test_windows_uses_dpapi(self):
        codes.remember_lock("b" * 32, "y.amlocked", 1, CODE)
        self.assertIn(b'"kind":"dpapi"', self.raw())

    def test_linux_key_from_machine_id_and_user(self):
        mid = os.path.join(self.base, "machine-id")
        with open(mid, "w") as f:
            f.write("0123456789abcdef0123456789abcdef\n")
        keys = codes.Keys("machine", machine_paths=[mid], key_folder=self.base)
        home = os.path.join(self.base, "home")
        codes.remember_lock("c" * 32, "z.amlocked", 1, CODE, home=home, keys=keys)
        self.assertEqual(codes.lock_code("c" * 32, home=home, keys=keys), CODE)
        with open(codes.store_path(home), "rb") as f:
            self.assertIn(b'"kind":"machine"', f.read())
        if os.name != "nt":
            self.assertEqual(os.stat(codes.store_path(home)).st_mode & 0o777, 0o600)
        # copied to another PC (another machine id): useless there, set aside, starts empty
        with open(mid, "w") as f:
            f.write("ffffffffffffffffffffffffffffffff\n")
        self.assertIsNone(codes.lock_code("c" * 32, home=home, keys=keys))
        self.assertTrue(os.path.exists(codes.store_path(home) + ".old"))

    def test_no_machine_id_uses_a_key_file_elsewhere(self):
        keys = codes.Keys("machine", machine_paths=[os.path.join(self.base, "none")],
                          key_folder=os.path.join(self.base, "data"))
        home = os.path.join(self.base, "home")
        codes.remember_lock("d" * 32, "w.amlocked", 1, CODE, home=home, keys=keys)
        self.assertTrue(os.path.exists(os.path.join(self.base, "data", codes.KEY_FILE_NAME)))
        self.assertEqual(codes.lock_code("d" * 32, home=home, keys=keys), CODE)
        os.remove(os.path.join(self.base, "data", codes.KEY_FILE_NAME))
        self.assertIsNone(codes.lock_code("d" * 32, home=home, keys=keys))

    def test_read_error_never_saves_over_the_codes(self):
        """A file in use / without permission for a moment: nothing is changed or set
        aside, every remembered code is still there afterwards."""
        codes.remember_lock("a" * 32, "x.amlocked", 10, CODE)
        eid = codes.add_sent(OTHER, 12, "f" * 32, "kiwi", time.time() + 600, 1)
        before = self.raw()
        with mock.patch.object(codes, "_read_raw", side_effect=PermissionError(13, "denied")), \
                mock.patch.object(codes.time, "sleep"):
            self.assertIsNone(codes.add_sent(CODE, 5, "e" * 32, "kiwi", time.time() + 60, 1))
            codes.remember_lock("b" * 32, "y.amlocked", 1, CODE)
            codes.forget_lock("a" * 32)
            self.assertIsNone(codes.update_sent(eid, state="taken"))
            self.assertEqual(codes.sent(), [])
            self.assertIsNone(codes.lock_code("a" * 32))      # shown as unknown, not lost
        self.assertEqual(self.raw(), before)                  # never written over
        self.assertFalse(os.path.exists(codes.store_path() + ".old"))
        self.assertEqual(codes.lock_code("a" * 32), CODE)
        self.assertEqual([e["state"] for e in codes.sent()], ["ready"])
        # a key file that is there but unreadable: the same (Linux without a machine id)
        keys = codes.Keys("machine", machine_paths=[os.path.join(self.base, "none")],
                          key_folder=os.path.join(self.base, "data"))
        home = os.path.join(self.base, "home")
        codes.remember_lock("c" * 32, "z.amlocked", 1, CODE, home=home, keys=keys)
        with open(codes.store_path(home), "rb") as f:
            kept = f.read()
        real_open = open

        def no_key(path, *a, **k):
            if str(path).endswith(codes.KEY_FILE_NAME):
                raise PermissionError(13, "denied")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", no_key):
            codes.forget_lock("c" * 32, home=home, keys=keys)
        with open(codes.store_path(home), "rb") as f:
            self.assertEqual(f.read(), kept)
        self.assertEqual(codes.lock_code("c" * 32, home=home, keys=keys), CODE)

    def test_damaged_file_is_set_aside(self):
        os.makedirs(os.path.dirname(codes.store_path()), exist_ok=True)
        with open(codes.store_path(), "wb") as f:
            f.write(b"junk")
        self.assertEqual(codes.sent(), [])
        self.assertTrue(os.path.exists(codes.store_path() + ".old"))

    def test_delete_old_backups_forgets_the_code(self):
        folder = os.path.join(self.base, "b")
        os.makedirs(folder)
        path = os.path.join(folder, "AddonManager backup 2026-11-14 received.amlocked")
        with open(path, "wb") as f:
            f.write(b"x" * 20)
        codes.remember_lock("e" * 32, os.path.basename(path), 20, CODE)
        found = backup.find_backups([("Documents", folder)])
        trash = FakeTrash(os.path.join(self.base, "bin"))
        with mock.patch.object(backup_delete, "check", return_value=None):
            out = backup_delete.delete(found, [("Documents", folder)], None, trash)
        self.assertEqual(out[0][1], backup_delete.MOVED)
        self.assertEqual(codes.locked(), {})


class StatusRouteTests(unittest.TestCase):
    """The sender's GET /v1/<ch>/status on the fake relay (the same rules as worker.js)."""

    def setUp(self):
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)

    def later_box(self):
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        ch, _ttl = sender.open()
        self.assertTrue(sender.start_later(1, "0" * 32, "1" * 64))
        return sender, ch

    def test_states_and_same_answer_for_strangers(self):
        sender, ch = self.later_box()
        self.assertEqual(sender.status()["state"], "uploading")
        sender.put_record(b"r" * 40)
        st = sender.status()
        self.assertEqual(st["state"], "ready")
        self.assertGreater(st["left"], 3500)
        stranger = share.Relay.for_sender(self.relay.url, ch, "e" * 32, allow_local=True)
        nobody = share.Relay.for_sender(self.relay.url, 498 if ch != 498 else 497, "e" * 32,
                                        allow_local=True)
        self.assertIsNone(stranger.status())                    # another token: not known
        self.assertIsNone(nobody.status())                      # no such code: the same
        recv = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        self.assertEqual(recv.join(ch), "later")
        self.assertIsNone(share.Relay.for_sender(self.relay.url, ch, recv.token,
                                                 allow_local=True).status())  # watch token
        recv.take("1" * 64)
        self.assertEqual(sender.status(), {"state": "taken", "left": 0})
        self.assertEqual(self.relay.stored(), {})               # nothing of the backup kept
        self.relay.advance(3000)
        self.assertEqual(sender.status()["state"], "taken")     # kept until the code's end
        self.relay.advance(700)
        self.assertIsNone(sender.status())                      # then deleted
        self.assertNotIn(ch, self.relay.boxes)

    def test_wrong_code_stop_expired(self):
        sender, ch = self.later_box()
        sender.put_record(b"r" * 40)
        recv = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        recv.join(ch)
        with self.assertRaises(share.ShareError):
            recv.take("2" * 64)
        self.assertEqual(sender.status()["state"], "burned")
        sender2, _ch2 = self.later_box()
        sender2.put_record(b"r" * 40)
        self.assertTrue(sender2.stop())
        self.assertEqual(sender2.status()["state"], "stopped")
        self.assertTrue(sender2.stop())                         # already closed: fine
        sender3, _ch3 = self.later_box()
        sender3.put_record(b"r" * 40)
        self.relay.advance(3601)
        self.assertEqual(sender3.status()["state"], "ended")
        self.relay.advance(301)
        self.assertIsNone(sender3.status())                     # then it's gone

    def test_live_box_and_old_relay(self):
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        sender.open()
        self.assertIsNone(sender.status())                      # not a send-later: not known

    def test_rate_limited(self):
        sender, _ch = self.later_box()
        self.relay.status_rate = 1
        sender.status()
        with self.assertRaises(share.ShareError) as cm:
            sender.status()
        self.assertEqual(cm.exception.kind, "busy")


LIVE_URL = os.environ.get("ADDONMANAGER_SHARE_TEST_URL", "")


@unittest.skipUnless(LIVE_URL.startswith(("http://127.0.0.1:", "http://localhost:")),
                     "set ADDONMANAGER_SHARE_TEST_URL to a local `wrangler dev` address")
class LocalStatusWorkerTests(unittest.TestCase):
    """The sender's /status route of the real worker.js under `wrangler dev --local`
    (no fake clock there: the end-of-code delete is checked on the fake relay)."""

    def later_box(self):
        s = share.Relay(LIVE_URL, allow_local=True)
        ch, _ttl = s.open()
        self.assertTrue(s.start_later(1, "0" * 32, "1" * 64))
        return s, ch

    def test_states_and_same_answer_for_strangers(self):
        s, ch = self.later_box()
        self.assertEqual(s.status()["state"], "uploading")
        s.put_record(b"r" * 40)
        st = s.status()
        self.assertEqual(st["state"], "ready")
        self.assertGreater(st["left"], 3000)
        self.assertIsNone(share.Relay.for_sender(LIVE_URL, ch, "e" * 32, True).status())
        free = next(c for c in range(499, 1, -1) if c != ch)
        self.assertIsNone(share.Relay.for_sender(LIVE_URL, free, "e" * 32, True).status())
        r = share.Relay(LIVE_URL, allow_local=True)
        self.assertEqual(r.join(ch), "later")
        self.assertIsNone(share.Relay.for_sender(LIVE_URL, ch, r.token, True).status())
        r.take("1" * 64)
        self.assertEqual(s.status(), {"state": "taken", "left": 0})

    def test_wrong_code_and_stop(self):
        s, ch = self.later_box()
        s.put_record(b"r" * 40)
        r = share.Relay(LIVE_URL, allow_local=True)
        r.join(ch)
        with self.assertRaises(share.ShareError):
            r.take("2" * 64)
        self.assertEqual(s.status()["state"], "burned")
        s2, _ch2 = self.later_box()
        s2.put_record(b"r" * 40)
        self.assertTrue(s2.stop())
        self.assertEqual(s2.status()["state"], "stopped")

    def test_live_box_not_known(self):
        s = share.Relay(LIVE_URL, allow_local=True)
        s.open()
        self.assertIsNone(s.status())
        s.close("cancel")


class LaterHookTests(unittest.TestCase):
    def setUp(self):
        clear_codes(self)

    def test_kept_goes_to_the_codes_file_never_the_queue(self):
        import queue
        from am import share_ui
        q = queue.Queue()
        hook = share_ui._later_hook(q, "t")
        hook("kept", {"code": CODE, "channel": 12, "token": "a" * 32, "left": 3600,
                      "hours": 1, "host": "visee", "parts": [{"id": "x", "owner": "o"}]})
        hook("ready", 3600)
        self.assertEqual(q.get_nowait(), ("share_later", "t", "ready", 3600))
        self.assertTrue(q.empty())
        e = codes.sent()[0]
        self.assertEqual((e["code"], e["ch"], e["host"]), (CODE, 12, "visee"))


class LockWindowTests(unittest.TestCase):
    """Hidden windows (never shown)."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("lock_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        import tkinter as tk
        from am import backup_ui
        clear_codes(self)
        self.tk = tk
        self.work = fixtures.temp_dir("lock_ui_")
        self.addCleanup(fixtures.remove, self.work)
        self.root_dir, self.addons, self.client = make_pc(os.path.join(self.work, "pc"))
        docs = mock.patch.dict(os.environ, {backup.DOCUMENTS_ENV: os.path.join(self.work, "docs")})
        docs.start()
        self.addCleanup(docs.stop)
        self.backups = os.path.join(self.work, "backups")
        config.save_settings({"backup_folder": self.backups})
        for target, name in ((backup, "cloud_folders"),):
            p = mock.patch.object(target, name, lambda: [])
            p.start()
            self.addCleanup(p.stop)
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)
        self.asked = []
        self.root = tk.Tk()
        self.root.withdraw()
        self.backup_ui = backup_ui

    def tearDown(self):
        import gc
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass
        self.root = None
        gc.collect()

    def make_app(self):
        from am import ui
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [],
                     confirm=lambda title, text, ok, cancel: self.asked.append(title) or True,
                     share_url=self.relay.url,
                     trash_api=FakeTrash(os.path.join(self.work, "bin")))
        self.pump_until(app, lambda: not app.busy)
        return app

    def pump_until(self, app, cond, timeout=30):
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

    def kept_copy(self):
        z = received_zip(self.work, self.addons)
        os.makedirs(self.backups, exist_ok=True)
        dest = os.path.join(self.backups, os.path.basename(z))
        shutil.move(z, dest)
        return backup_lock.keep(dest, CODE, remember=codes.remember_lock)

    def test_receive_keeps_it_locked_then_save_zip(self):
        sent, _s, _n = backup.make(self.addons, os.path.join(self.work, "sent"))
        s = threading.Thread(target=lambda: self._send(sent), daemon=True)
        self.codes_q = __import__("queue").Queue()
        s.start()
        code = self.codes_q.get(timeout=20)
        app = self.make_app()
        app.open_backup_window(show=False)
        rec = app.open_receive(show=False)
        rec.code_var.set(code)
        self.assertTrue(rec.start())
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog")
        rd = app.backup_dialog
        kept = rd.kept
        locked_name = os.path.basename(kept.locked)
        self.assertTrue(kept.locked and os.path.exists(kept.locked))
        self.assertEqual(rd.info.path, kept.plain)
        names = os.listdir(self.backups)
        self.assertNotIn(True, [n.endswith("received.zip") for n in names])   # only locked
        self.assertIn(errors.KEPT_LOCKED, self.texts(rd.win))
        self.assertEqual(codes.lock_code(kept.file_id), share.format_code(*share.parse_code(code)))
        rd.win.update_idletasks()
        self.assertLessEqual(rd.win.winfo_reqwidth(), 420)
        # Save zip...: a normal zip where the player picked; the locked copy goes after
        target = os.path.join(self.work, "usb", "WoW backup.zip")
        os.makedirs(os.path.dirname(target))
        asked_names = []
        app.ask_save_zip = lambda name: asked_names.append(name) or target
        self.assertTrue(rd.save_zip())
        self.assertIn("disabled", rd.restore_button.state())       # not while saving
        self.pump_until(app, lambda: not rd.saving)
        self.assertEqual(asked_names, [os.path.splitext(locked_name)[0] + ".zip"])
        self.assertTrue(zipfile.is_zipfile(target))
        self.assertTrue(backup.read(target).received)
        self.assertFalse(os.path.exists(os.path.join(self.backups, locked_name)))
        self.assertIsNone(kept.locked)
        self.assertEqual(len(app.trash.sent), 1)                     # to the Recycle Bin
        self.assertEqual(codes.locked(), {})
        self.assertEqual(rd.status_var.get(), errors.zip_saved("WoW backup.zip", True))
        self.assertNotIn("disabled", rd.restore_button.state())
        plain = kept.plain
        rd.close()
        self.assertFalse(os.path.exists(plain))                      # hidden copy removed
        self.assertEqual([n for n in os.listdir(self.backups)
                          if n.startswith(backup_lock.UNLOCKED_PREFIX)], [])

    def _send(self, path):
        relay = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        try:
            share.send(path, relay, lambda c, t: self.codes_q.put(c))
        except Exception:
            self.codes_q.put(None)

    def test_save_zip_failure_keeps_the_locked_copy(self):
        kept = self.kept_copy()
        app = self.make_app()
        app.open_backup_window(show=False)
        rd = app.backup_dialog = self.backup_ui.RestoreDialog(
            app, backup.read(kept.plain, received=True), show=False, kept=kept)
        gone = os.path.join(self.work, "unplugged", "WoW backup.zip")   # a folder gone
        app.ask_save_zip = lambda name: gone
        rd.save_zip()
        self.pump_until(app, lambda: not rd.saving)
        self.assertTrue(os.path.exists(kept.locked))                  # the locked copy stays
        self.assertEqual(codes.lock_code(kept.file_id), CODE)
        self.assertEqual(rd.status_var.get(), errors.save_zip_message(LockError("file")))
        app.ask_save_zip = lambda name: ""          # the save dialog cancelled
        self.assertFalse(rd.save_zip())
        rd.close()

    def slow_save(self, started):
        """save_copy stand-in: writes its hidden part, waits for Cancel, removes it."""
        real = backup_lock.save_copy

        def slow(plain, target, progress=None, cancel=None, **kw):
            part = os.path.join(os.path.dirname(target),
                                backup_lock.SAVING_PREFIX + "test" + backup_lock.PART_EXT)
            with open(part, "wb") as f:
                f.write(b"x")
            started.set()
            try:
                if not cancel.wait(20):
                    return real(plain, target, progress, cancel, **kw)
                raise LockError("cancelled")
            finally:
                os.remove(part)
        return mock.patch.object(backup_lock, "save_copy", slow)

    def saving_dialog(self, app, kept, started):
        target = os.path.join(self.work, "usb", "WoW backup.zip")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        rd = app.backup_dialog = self.backup_ui.RestoreDialog(
            app, backup.read(kept.plain, received=True), show=False, kept=kept)
        app.ask_save_zip = lambda name: target
        self.assertTrue(rd.save_zip())
        self.pump_until(app, started.is_set)
        return rd, os.path.dirname(target)

    def test_closing_the_backup_window_while_saving(self):
        """B1: the Backup window (X/Close) can't close under a running Save zip...: the copy
        stops, both windows stay, Restore can be used again after."""
        kept = self.kept_copy()
        plain = kept.plain
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        started = threading.Event()
        with self.slow_save(started):
            rd, usb = self.saving_dialog(app, kept, started)
            app.close_backup_window()
            self.assertIs(app.backup_win, hub)
            self.assertIs(app.backup_dialog, rd)
            self.assertEqual(rd.status_var.get(), errors.STOPPING)
            self.pump_until(app, lambda: not rd.saving)
        self.assertEqual(rd.status_var.get(), errors.SAVE_ZIP_STOPPED)
        self.assertEqual(os.listdir(usb), [])                       # no part left
        self.assertTrue(os.path.exists(kept.locked))
        app.close_backup_window()                                    # now it closes
        self.assertIsNone(app.backup_dialog)
        self.assertIsNone(app.backup_win)
        self.assertFalse(os.path.exists(plain))
        app.open_backup_window(show=False)
        self.assertIsNotNone(app.open_restore(show=False))

    def test_closing_the_app_while_saving(self):
        """B2: closing the app stops the copy, removes its part, then the app closes."""
        kept = self.kept_copy()
        plain = kept.plain
        app = self.make_app()
        app.open_backup_window(show=False)
        started = threading.Event()
        with self.slow_save(started):
            rd, usb = self.saving_dialog(app, kept, started)
            app.close()
            self.assertFalse(app.closed)
            self.pump_until(app, lambda: app.closed)
        self.assertEqual(os.listdir(usb), [])
        self.assertFalse(os.path.exists(plain))                # hidden copy gone too
        self.assertTrue(os.path.exists(kept.locked))

    def test_backups_found_locked_row_known_code(self):
        kept = self.kept_copy()
        kept.discard()
        app = self.make_app()
        app.open_backup_window(show=False)
        lst = app.open_restore(show=False)
        self.pump_until(app, lambda: lst.found is not None)
        texts = [str(b.cget("text")) for b in lst.row_buttons]
        self.assertTrue(any(t.endswith(" · " + errors.FOUND_LOCKED) for t in texts), texts)
        lst.pick(kept.locked)
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog")
        rd = app.backup_dialog
        self.assertTrue(rd.info.received)
        self.assertFalse(rd.addons_var.get())
        self.assertEqual(rd.kept.locked, kept.locked)
        self.assertIsNotNone(rd.back_button)
        plain = rd.kept.plain
        rd.close()
        self.assertFalse(os.path.exists(plain))

    def test_unlock_window_on_another_pc(self):
        kept = self.kept_copy()
        kept.discard()
        codes.forget_lock(kept.file_id)              # as on another PC
        app = self.make_app()
        app.open_backup_window(show=False)
        lst = app.open_restore(show=False)
        self.pump_until(app, lambda: lst.found is not None)
        lst.pick(kept.locked)
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "UnlockDialog")
        u = app.backup_dialog
        u.win.update_idletasks()
        self.assertLessEqual(u.win.winfo_reqwidth(), 420)
        u.code_var.set(OTHER)
        self.assertTrue(u.start())
        self.pump_until(app, lambda: u.cancel_event is None)
        self.assertEqual(u.status_var.get(), errors.UNLOCK_WRONG)
        self.assertNotIn("disabled", u.go_button.state())
        u.code_var.set(CODE.upper().replace("-", " "))
        self.assertTrue(u.start())
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "RestoreDialog")
        self.assertEqual(codes.lock_code(kept.file_id), CODE)   # remembered now
        app.backup_dialog.close()
        # Back from the Unlock window: the list again
        codes.forget_lock(kept.file_id)
        app.open_restore(show=False, path=kept.locked)
        self.pump_until(app, lambda: type(app.backup_dialog).__name__ == "UnlockDialog")
        self.assertIsNotNone(app.backup_dialog.back())
        self.assertEqual(type(app.backup_dialog).__name__, "BackupListDialog")

    def test_codes_window_status_stop_show(self):
        from am import codes_ui
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        ch, _ttl = sender.open()
        sender.start_later(2, "0" * 32, "1" * 64)
        left = sender.put_record(b"r" * 40)
        codes.add_sent(OTHER, ch, sender.token, "kiwi", time.time() + left, 2)
        kept = self.kept_copy()
        kept.discard()
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        self.assertTrue(hub.codes_row.winfo_manager())
        self.assertTrue(hub.codes_var.get().startswith("1 code still works ("))
        self.assertEqual(hub.codes_button.winfo_reqwidth(), hub.make_button.winfo_reqwidth())
        d = hub.open_codes()
        self.assertIsInstance(d, codes_ui.CodesDialog)
        self.pump_until(app, lambda: d.live)
        texts = self.texts(d.win)
        self.assertIn(OTHER, texts)
        self.assertNotIn(CODE, texts)                     # hidden until Show code
        self.assertTrue(any("not picked up yet" in t for t in texts), texts)
        show = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_SHOW][0]
        show.invoke()
        self.assertIn(CODE, self.texts(d.win))
        forget = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_FORGET_BUTTON]
        self.assertEqual(len(forget), 1)
        hide = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_HIDE][0]
        self.assertEqual(forget[0].winfo_reqwidth(), hide.winfo_reqwidth())
        stop = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_STOP][0]
        stop.invoke()
        self.assertIn(errors.CODES_STOP_TITLE, self.asked)
        self.pump_until(app, lambda: d.status_var.get() == errors.CODES_STOPPED)
        self.assertEqual(sender.status()["state"], "stopped")
        self.assertEqual(codes.sent()[0]["state"], "stopped")
        stop = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_STOP][0]
        self.assertIn("disabled", stop.state())
        d.win.update_idletasks()
        self.assertLessEqual(d.win.winfo_reqwidth(), 420)
        forget = [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_FORGET_BUTTON]
        forget[0].invoke()                               # asks first, then forgets the code
        self.assertIn(errors.CODES_FORGET_TITLE, self.asked)
        self.assertEqual(codes.locked(), {})
        self.assertFalse([b for b in d.row_buttons
                          if str(b.cget("text")) == errors.CODES_FORGET_BUTTON])
        d.close()
        self.assertIsNone(d.poll_job)

    def test_codes_window_picked_up(self):
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        ch, _ttl = sender.open()
        sender.start_later(1, "0" * 32, "1" * 64)
        left = sender.put_record(b"r" * 40)
        eid = codes.add_sent(OTHER, ch, sender.token, "adminforge", time.time() + left, 1)
        recv = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        recv.join(ch)
        recv.take("1" * 64)
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        d = hub.open_codes()
        self.pump_until(app, lambda: d.live)
        self.assertEqual(d.live[eid][0], "taken")
        self.assertTrue(any(t.startswith("adminForge: Picked up") for t in self.texts(d.win)))
        self.assertEqual(codes.sent()[0]["state"], "taken")
        d.close()
        hub.refresh_codes()
        self.assertFalse(hub.codes_row.winfo_manager())   # nothing waiting any more


if __name__ == "__main__":
    unittest.main()
