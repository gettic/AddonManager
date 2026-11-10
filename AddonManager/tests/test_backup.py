"""Backup and restore (am\\backup.py, am\\backup_ui.py). Fake WoW folders, a fake
Documents folder and fake cloud folders in temp folders only: the real WoW folder,
WTF, Documents and %APPDATA% are never used."""

import datetime
import gc
import json
import os
import time
import tkinter as tk
import unittest
import zipfile
from unittest import mock

from am import backup, backup_ui, config, errors, ui
from am.backup import BackupError
from tests import fixtures

NOW = datetime.datetime(2026, 11, 10, 21, 30)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_pc(base, account="ACCOUNT1", tag="old"):
    """A fake PC: WoW Forever with addons and a full WTF. Returns (root, addons, client)."""
    root, addons = fixtures.make_fake_wow(base)
    client = os.path.dirname(os.path.dirname(addons))
    wtf = os.path.join(client, "WTF")
    acc = os.path.join(wtf, "Account", account)
    os.rename(os.path.join(wtf, "Account", "X"), acc)
    write(os.path.join(acc, "SavedVariables", "TaxiTime.lua"), f"TaxiTimeDB = '{tag}'\n")
    write(os.path.join(acc, "SavedVariables", "TaxiTime.lua.bak"), "old\n")
    write(os.path.join(acc, "bindings-cache.wtf"), f"bind {tag}\n")
    write(os.path.join(acc, "macros-cache.txt"), f"macro {tag}\n")
    write(os.path.join(acc, "config-cache.wtf"), f"cfg {tag}\n")
    write(os.path.join(acc, "Testrealm", "Testchar", "SavedVariables", "TaxiTime.lua"),
          f"char {tag}\n")
    write(os.path.join(acc, "Testrealm", "Testchar", "AddOns.txt"), f"TaxiTime: enabled {tag}\n")
    write(os.path.join(acc, "Testrealm", "Testchar", "chat-cache.txt"), "private chat\n")
    write(os.path.join(wtf, "Config.wtf"), "SET accountName \"secret\"\n")
    fixtures.make_addon_folder(addons, "Blizzard_Fake", "1.0")
    os.makedirs(os.path.join(addons, "NoToc"))
    write(os.path.join(addons, "NoToc", "readme.txt"), "x")
    os.makedirs(os.path.join(addons, ".addonmanager-staging-x"))
    return root, addons, client


def names(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return sorted(z.namelist())


def raw_backup(path, entries, meta=None):
    meta = meta if meta is not None else {"app": "AddonManager", "format": 1,
                                          "created": "2026-11-10 21:30"}
    items = [("backup.json", json.dumps(meta))] if meta is not False else []
    return fixtures.make_raw_zip(path, items + list(entries))


class BackupCoreTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.temp_dir("backup_restore_core_")
        self.root, self.addons, self.client = make_pc(os.path.join(self.base, "pc1"))
        self.wtf = os.path.join(self.client, "WTF")
        self.out = os.path.join(self.base, "out")

    def tearDown(self):
        fixtures.remove(self.base)

    def snap(self, client=None):
        client = client or self.client
        return (fixtures.tree_snapshot(os.path.join(client, "Interface")),
                fixtures.tree_snapshot(os.path.join(client, "WTF")))

    def test_make_contents_and_name(self):
        before = self.snap()
        path, size, counts = backup.make(self.addons, self.out, now=NOW)
        self.assertEqual(os.path.basename(path), "AddonManager backup 2026-11-10.zip")
        self.assertEqual(size, os.path.getsize(path))
        self.assertEqual(counts, {"addons": 2, "settings_files": 6})
        got = names(path)
        self.assertIn("backup.json", got)
        self.assertIn("AddOns/TaxiTime/TaxiTime_Forever.toc", got)
        self.assertIn("AddOns/OtherAddon/OtherAddon.toc", got)
        self.assertIn("WTF/Account/ACCOUNT1/SavedVariables/TaxiTime.lua", got)
        self.assertIn("WTF/Account/ACCOUNT1/bindings-cache.wtf", got)
        self.assertIn("WTF/Account/ACCOUNT1/Testrealm/Testchar/SavedVariables/TaxiTime.lua", got)
        for left_out in ("Blizzard_Fake", "NoToc", ".addonmanager", "chat-cache", "Config.wtf",
                         ".bak"):
            self.assertFalse([n for n in got if left_out in n], left_out)
        self.assertEqual(self.snap(), before)   # making a backup changes nothing
        self.assertEqual(os.listdir(self.out), [os.path.basename(path)])   # no .part left
        again = backup.make(self.addons, self.out, now=NOW)[0]
        self.assertEqual(os.path.basename(again), "AddonManager backup 2026-11-10 (2).zip")

    def test_make_parts(self):
        only_addons = names(backup.make(self.addons, self.out, settings=False, now=NOW)[0])
        self.assertFalse([n for n in only_addons if n.startswith("WTF/")])
        only_settings = names(backup.make(self.addons, self.out, addons=False, now=NOW)[0])
        self.assertFalse([n for n in only_settings if n.startswith("AddOns/")])
        with self.assertRaises(BackupError) as c:
            backup.make(self.addons, self.out, addons=False, settings=False)
        self.assertEqual(c.exception.kind, "nothing")

    def test_make_refuses_folder_inside_game(self):
        for inside in (os.path.join(self.addons, "TaxiTime"), os.path.join(self.wtf, "x")):
            with self.assertRaises(BackupError) as c:
                backup.make(self.addons, inside)
            self.assertEqual(c.exception.kind, "inside_game")
        self.assertFalse(os.path.exists(os.path.join(self.wtf, "x")))

    def test_read_counts(self):
        info = backup.read(backup.make(self.addons, self.out, now=NOW)[0])
        self.assertEqual(sorted(info.addons), ["OtherAddon", "TaxiTime"])
        self.assertEqual(list(info.accounts), ["ACCOUNT1"])
        self.assertEqual(info.characters, 1)
        self.assertEqual((info.settings_files, info.binding_files), (4, 2))
        self.assertIn("2 addons, settings for 1 account, 1 character", errors.backup_from(info))

    def test_read_refuses_bad_files(self):
        good = ("AddOns/A/A.toc", "## Version: 1\n")
        cases = {
            "not_backup": [raw_backup(os.path.join(self.base, "nometa.zip"), [good], meta=False),
                           raw_backup(os.path.join(self.base, "othermeta.zip"), [good],
                                      meta={"app": "Other", "format": 1})],
            "newer_format": [raw_backup(os.path.join(self.base, "new.zip"), [good],
                                        meta={"app": "AddonManager", "format": 99})],
        }
        # (A backslash name is turned into "/" by zipfile on Windows, so not listed here.)
        evil = ["../evil.lua", "AddOns/A/../../evil.lua", "/abs.lua", "C:/x.lua",
                "WTF/Config.wtf", "Interface/AddOns/A/x.lua",
                "AddOns/Blizzard_X/x.lua", "WTF/Account/A/evil.exe", "WTF/Account/A/chat-cache.txt",
                "WTF/Account/.hidden/x.lua", "AddOns/A/con.lua", "AddOns/A/x.lua ", "other.txt",
                "WTF/Account/A/a/b/c/d/e.lua"]
        for i, name in enumerate(evil):
            cases.setdefault("unsafe", []).append(
                raw_backup(os.path.join(self.base, f"evil{i}.zip"), [good, (name, "x")]))
        link = zipfile.ZipInfo("AddOns/A/link.lua")
        link.external_attr = (0o120777 << 16)
        cases["unsafe"].append(raw_backup(os.path.join(self.base, "link.zip"), [good, (link, "x")]))
        cases["unsafe"].append(raw_backup(os.path.join(self.base, "dup.zip"),
                                          [good, ("AddOns/A/x.lua", "1"), ("AddOns/A/X.lua", "2")]))
        notzip = os.path.join(self.base, "notzip.zip")
        write(notzip, "hello")
        cases["not_backup"].append(notzip)
        for kind, paths in cases.items():
            for path in paths:
                with self.assertRaises(BackupError, msg=path) as c:
                    backup.read(path)
                self.assertEqual(c.exception.kind, kind, path)

    def test_folder_without_toc_is_skipped(self):
        path = raw_backup(os.path.join(self.base, "b.zip"),
                          [("AddOns/A/A.toc", "x"), ("AddOns/B/b.lua", "x")])
        info = backup.read(path)
        self.assertEqual((info.addons, info.skipped), (["A"], ["B"]))

    def other_pc(self, account="ACCOUNT2"):
        root, addons, client = make_pc(os.path.join(self.base, "pc2"), account=account, tag="new")
        write(os.path.join(addons, "OnlyHere", "OnlyHere.toc"), "## Version: 1\n")
        write(os.path.join(client, "WTF", "Account", account, "SavedVariables", "OnlyHere.lua"), "x")
        return root, addons, client

    def test_restore_on_another_pc_merges_with_safety_copy(self):
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc()
        acc2 = os.path.join(client2, "WTF", "Account", "ACCOUNT2")
        write(os.path.join(addons2, "TaxiTime", "only_on_pc2.lua"), "x")
        before = self.snap(client2)
        info = backup.read(path)
        amap = backup.default_map(info.accounts, backup.local_accounts(addons2))
        self.assertEqual(amap, {"ACCOUNT1": "ACCOUNT2"})   # the only account here
        safety_dir = os.path.join(self.base, "safety")
        result = backup.restore(path, addons2, safety_dir, account_map=amap, now=NOW)
        self.assertEqual(sorted(result.addons), ["OtherAddon", "TaxiTime"])
        self.assertEqual(result.files, 6)
        # Restored: the backup's addon folders (whole folder) and settings files.
        self.assertFalse(os.path.exists(os.path.join(addons2, "TaxiTime", "only_on_pc2.lua")))
        self.assertTrue(os.path.isfile(os.path.join(addons2, "TaxiTime", "old_only.lua")))
        self.assertEqual(read_text(os.path.join(acc2, "SavedVariables", "TaxiTime.lua")),
                         "TaxiTimeDB = 'old'\n")
        self.assertEqual(read_text(os.path.join(acc2, "bindings-cache.wtf")), "bind old\n")
        # Merge: everything else here stays.
        self.assertTrue(os.path.isfile(os.path.join(addons2, "OnlyHere", "OnlyHere.toc")))
        self.assertTrue(os.path.isfile(os.path.join(acc2, "SavedVariables", "OnlyHere.lua")))
        self.assertEqual(read_text(os.path.join(acc2, "Testrealm", "Testchar",
                                                "chat-cache.txt")), "private chat\n")
        self.assertTrue(os.path.isdir(os.path.join(addons2, "Blizzard_Fake")))
        self.assertFalse([n for n in os.listdir(addons2) if n.startswith(".addonmanager-")
                          and n != ".addonmanager-staging-x"])
        self.assertFalse([n for n in os.listdir(os.path.join(client2, "WTF"))
                          if n.startswith(backup.RESTORE_PREFIX)])
        # The safety copy holds what was replaced, and restoring it puts it back.
        self.assertEqual(os.path.basename(result.safety), "Before restore 2026-11-10 2130.zip")
        sinfo = backup.read(result.safety)
        self.assertEqual(sinfo.kind, "safety")
        self.assertEqual(sorted(sinfo.addons), ["OtherAddon", "TaxiTime"])
        self.assertEqual(list(sinfo.accounts), ["ACCOUNT2"])
        backup.restore(result.safety, addons2, safety_dir, account_map={"ACCOUNT2": "ACCOUNT2"})
        self.assertEqual(self.snap(client2), before)

    def test_restore_parts(self):
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc(account="ACCOUNT1")
        acc2 = os.path.join(client2, "WTF", "Account", "ACCOUNT1")
        amap = {"ACCOUNT1": "ACCOUNT1"}
        wtf_before = fixtures.tree_snapshot(os.path.join(client2, "WTF"))
        backup.restore(path, addons2, self.out, settings=False, bindings=False, account_map=amap)
        self.assertEqual(fixtures.tree_snapshot(os.path.join(client2, "WTF")), wtf_before)
        addons_before = fixtures.tree_snapshot(addons2)
        backup.restore(path, addons2, self.out, addons=False, bindings=False, account_map=amap)
        self.assertEqual(fixtures.tree_snapshot(addons2), addons_before)
        self.assertEqual(read_text(os.path.join(acc2, "SavedVariables", "TaxiTime.lua")),
                         "TaxiTimeDB = 'old'\n")
        self.assertEqual(read_text(os.path.join(acc2, "bindings-cache.wtf")), "bind new\n")
        backup.restore(path, addons2, self.out, addons=False, settings=False, account_map=amap)
        self.assertEqual(read_text(os.path.join(acc2, "bindings-cache.wtf")), "bind old\n")
        # Skipped account (no match picked): nothing at all to do.
        with self.assertRaises(BackupError) as c:
            backup.restore(path, addons2, self.out, addons=False, account_map={"ACCOUNT1": None})
        self.assertEqual(c.exception.kind, "nothing")

    def test_default_map(self):
        self.assertEqual(backup.default_map(["A"], ["A", "B"]), {"A": "A"})
        self.assertEqual(backup.default_map(["A"], ["B"]), {"A": "B"})
        self.assertEqual(backup.default_map(["A"], ["B", "C"]), {"A": None})
        self.assertEqual(backup.default_map(["A"], []), {"A": None})

    def test_no_account_here_skips_settings(self):
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc()
        fixtures.remove(os.path.join(client2, "WTF", "Account"))
        self.assertEqual(backup.local_accounts(addons2), [])
        info = backup.read(path)
        folders, files = backup.plan(info, addons2, account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertEqual((len(folders), files), (2, []))

    def test_failure_half_way_changes_nothing(self):
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc(account="ACCOUNT1")
        before = self.snap(client2)
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            # Fail when the 3rd settings file is put in place (after addons + 2 files).
            if os.path.basename(os.path.dirname(src)) == "new":
                calls["n"] += 1
                if calls["n"] == 3:
                    raise PermissionError(13, "test lock", src)
            return real(src, dst)
        with mock.patch.object(backup.os, "replace", flaky):
            with self.assertRaises(BackupError) as c:
                backup.restore(path, addons2, os.path.join(self.base, "safety"),
                               account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertEqual(c.exception.kind, "permission")
        self.assertEqual(self.snap(client2), before)   # all swapped files were put back

    def test_stale_wtf_stage_removed_without_journal(self):
        stage = os.path.join(self.wtf, backup.RESTORE_PREFIX + "abc")
        os.makedirs(os.path.join(stage, "new"))
        self.assertIn(("removed", backup.RESTORE_PREFIX + "abc"), backup.recover_restore(self.addons))
        self.assertFalse(os.path.exists(stage))

    # ---- tester fixes (plans/backup-restore/test-report.md) ----
    def bang_addon(self, addons):
        fixtures.make_addon_folder(addons, "!BugGrabber", "1.0")
        fixtures.make_addon_folder(addons, "Taxi Time Extra", "1.0")

    def test_h1_recover_puts_back_any_backup_name(self):
        """H1: .addonmanager-backup-<name> for '!BugGrabber' / names with spaces."""
        from am import installer
        self.bang_addon(self.addons)
        for name in ("!BugGrabber", "Taxi Time Extra"):
            os.replace(os.path.join(self.addons, name),
                       os.path.join(self.addons, installer.BACKUP_PREFIX + name))
        actions = installer.recover(self.addons)
        self.assertIn(("restored", "!BugGrabber"), actions)
        self.assertIn(("restored", "Taxi Time Extra"), actions)
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "!BugGrabber")))
        for bad in ("..", "", "CON", "a:b"):
            self.assertFalse(installer._leftover_folder_ok(bad), bad)

    def test_m1_killed_restore_is_undone_at_next_start(self):
        """M1 + H1: the app killed after any rename: the next start puts everything back."""
        from am import installer
        self.bang_addon(self.addons)
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc(account="ACCOUNT1")
        self.bang_addon(addons2)
        write(os.path.join(addons2, "!BugGrabber", "mine.lua"), "pc2 only\n")
        before = self.snap(client2)
        safety = os.path.join(self.base, "safety")

        class Killed(BaseException):
            pass
        real_replace, real_journal = os.replace, backup._write_journal
        kill_at = 1
        while True:
            state = {"on": False, "n": 0}

            def journal(p, data):
                real_journal(p, data)
                state["on"] = True

            def replace(src, dst):
                if state["on"]:
                    state["n"] += 1
                    if state["n"] == kill_at:
                        raise Killed()
                return real_replace(src, dst)
            with mock.patch.object(backup, "_write_journal", journal), \
                    mock.patch.object(backup.os, "replace", replace), \
                    mock.patch.object(backup, "_undo", lambda p, j: False):   # no clean-up: a kill
                try:   # past the last rename: the restore simply finishes
                    backup.restore(path, addons2, safety, account_map={"ACCOUNT1": "ACCOUNT1"})
                    break
                except Killed:
                    pass
            self.assertTrue(os.path.exists(os.path.join(addons2, installer.RESTORE_JOURNAL)))
            self.assertIn(("undone", "restore"), backup.recover_restore(addons2))
            installer.recover(addons2)
            self.assertEqual(self.snap(client2), before, f"killed at rename {kill_at}")
            self.assertFalse([n for n in os.listdir(addons2) if n.startswith(".addonmanager-")])
            self.assertFalse([n for n in os.listdir(os.path.join(client2, "WTF"))
                              if n.startswith(backup.RESTORE_PREFIX)])
            kill_at += 1
        self.assertGreater(kill_at, 10)   # every rename point was tried
        # Finally a full restore works (no leftover blocks it).
        backup.restore(path, addons2, safety, account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertFalse(os.path.exists(os.path.join(addons2, "!BugGrabber", "mine.lua")))

    def test_m2_read_only_file_in_replaced_folder(self):
        import stat
        from am import installer
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, _client2 = self.other_pc(account="ACCOUNT1")
        ro = os.path.join(addons2, "TaxiTime", "TaxiTime.lua")
        os.chmod(ro, stat.S_IREAD)
        backup.restore(path, addons2, os.path.join(self.base, "safety"),
                       account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertFalse([n for n in os.listdir(addons2) if n.startswith(installer.BACKUP_PREFIX)])
        backup.restore(path, addons2, os.path.join(self.base, "safety"),   # not blocked
                       account_map={"ACCOUNT1": "ACCOUNT1"})

    def test_m3_two_accounts_onto_one(self):
        two = os.path.join(self.wtf, "Account", "TWO")
        write(os.path.join(two, "SavedVariables", "TaxiTime.lua"), "TaxiTimeDB = 'from TWO'\n")
        path = backup.make(self.addons, self.out, now=NOW)[0]
        info = backup.read(path)
        self.assertEqual(backup.default_map(sorted(info.accounts), ["MINE"]),
                         {"ACCOUNT1": None, "TWO": None})   # the player picks
        self.assertEqual(backup.default_map(["ACCOUNT1", "TWO"], ["ACCOUNT1"]),
                         {"ACCOUNT1": "ACCOUNT1", "TWO": None})
        _root, addons2, client2 = self.other_pc(account="MINE")
        before = self.snap(client2)
        safety = os.path.join(self.base, "safety")
        with self.assertRaises(BackupError) as c:
            backup.restore(path, addons2, safety, account_map={"ACCOUNT1": "MINE", "TWO": "MINE"})
        self.assertEqual(c.exception.kind, "same_account")
        self.assertIn("same account", errors.backup_message(c.exception))
        self.assertEqual(self.snap(client2), before)
        self.assertFalse(os.path.exists(safety))
        result = backup.restore(path, addons2, safety, account_map={"ACCOUNT1": None, "TWO": "MINE"})
        backup.read(result.safety)   # the safety copy can be read (no duplicates)
        acc = os.path.join(client2, "WTF", "Account", "MINE", "SavedVariables", "TaxiTime.lua")
        self.assertEqual(read_text(acc), "TaxiTimeDB = 'from TWO'\n")

    def test_m4_no_backup_folder_inside_the_game(self):
        client = self.client
        for inside in (os.path.join(client, "Backups"), os.path.join(client, "Interface", "Backups"),
                       os.path.join(self.root, "Backups")):
            with self.assertRaises(BackupError, msg=inside) as c:
                backup.make(self.addons, inside)
            self.assertEqual(c.exception.kind, "inside_game")
            self.assertFalse(os.path.exists(inside))
        path = backup.make(self.addons, self.out, now=NOW)[0]
        with self.assertRaises(BackupError) as c:
            backup.restore(path, self.addons, os.path.join(self.root, "Backups"),
                           account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertEqual(c.exception.kind, "inside_game")

    def test_l1_damaged_data_says_damaged_and_drops_safety_copy(self):
        write(os.path.join(self.addons, "TaxiTime", "big.lua"),
              "".join(f"local v{i} = {i * 7919 % 1000}\n" for i in range(20000)))
        path = backup.make(self.addons, self.out, now=NOW)[0]
        with zipfile.ZipFile(path) as z:
            zi = z.getinfo("AddOns/TaxiTime/big.lua")
        start = zi.header_offset + 30 + len(zi.filename.encode()) + len(zi.extra) + 40
        with open(path, "r+b") as f:
            f.seek(start)
            f.write(b"\xff" * 64)
        backup.read(path)   # the list of files still looks fine
        _root, addons2, client2 = self.other_pc(account="ACCOUNT1")
        before = self.snap(client2)
        safety = os.path.join(self.base, "safety")
        with self.assertRaises(BackupError) as c:
            backup.restore(path, addons2, safety, account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertEqual(c.exception.kind, "bad_zip")
        self.assertIn("damaged", errors.backup_message(c.exception))
        self.assertEqual(self.snap(client2), before)
        self.assertEqual(os.listdir(safety), [])   # the unused safety copy is removed

    def test_l2_folder_without_toc_comes_back_from_safety_copy(self):
        path = backup.make(self.addons, self.out, now=NOW)[0]
        _root, addons2, client2 = self.other_pc(account="ACCOUNT1")
        for name in os.listdir(os.path.join(addons2, "TaxiTime")):
            if name.endswith(".toc"):
                os.remove(os.path.join(addons2, "TaxiTime", name))
        before = self.snap(client2)
        result = backup.restore(path, addons2, os.path.join(self.base, "safety"),
                                account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertIn("TaxiTime", backup.read(result.safety).addons)
        backup.restore(result.safety, addons2, os.path.join(self.base, "safety"),
                       account_map={"ACCOUNT1": "ACCOUNT1"})
        self.assertEqual(self.snap(client2), before)

    def test_safe_part(self):
        for ok in ("TaxiTime", "!BugGrabber", "Test realm", "ACCOUNT2#1", "Éowyn", "a.lua"):
            self.assertTrue(backup.safe_part(ok), ok)
        for bad in ("", ".", "..", "a/b", "a\\b", "c:", "x ", "x.", "CON", "nul.txt", "a\x00",
                    "a*b", "x" * 256):
            self.assertFalse(backup.safe_part(bad), bad)

    def test_places_and_names(self):
        self.assertTrue(backup.default_folder().startswith(os.environ[backup.DOCUMENTS_ENV]))
        self.assertTrue(backup.default_folder().endswith("AddonManager backups"))
        home = os.path.join(self.base, "home")
        os.makedirs(os.path.join(home, "Dropbox"))
        one = os.path.join(self.base, "OneDrive - Guild")
        os.makedirs(one)
        found = backup.cloud_folders(home=home, env={"OneDrive": one,
                                                     "OneDriveConsumer": os.path.join(home, "nope")})
        labels = [label for label, _p in found]
        self.assertIn("Dropbox", labels)
        self.assertEqual("OneDrive" in labels, os.name == "nt")
        self.assertEqual(backup.cloud_folders(home=os.path.join(self.base, "empty"), env={}), [])
        self.assertTrue(backup.safety_path(self.out, NOW).endswith("Before restore 2026-11-10 2130.zip"))

    def test_linux_documents_from_user_dirs(self):
        home = os.path.join(self.base, "home")
        write(os.path.join(home, ".config", "user-dirs.dirs"),
              'XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        self.assertEqual(backup._linux_documents(home), home + "/Dokumente")
        self.assertEqual(backup._linux_documents(os.path.join(self.base, "x")),
                         os.path.join(self.base, "x", "Documents"))


class WindowBackupTests(unittest.TestCase):
    """The hidden window: Settings section, Make a backup and Restore windows."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("backup_restore_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir("backup_restore_ui_")
        self.root_dir, self.addons, self.client = make_pc(os.path.join(self.work, "pc"))
        self.asked, self.answer, self.game = [], True, False
        self.opened = []
        config.save_settings({})
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        fixtures.remove(self.work)

    def confirm(self, title, text, ok, cancel):
        self.asked.append((title, text, ok, cancel))
        return self.answer

    def pump(self, app, timeout=20):
        end = time.time() + timeout
        self.root.update()
        while (app.busy or (app.backup_dialog and app.backup_dialog.running)) and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        for _ in range(5):
            self.root.update()
            time.sleep(0.02)

    def make_app(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: self.game, detect=lambda: [], confirm=self.confirm)
        app.open_folder = self.opened.append
        app.ask_backup_folder = lambda start: os.path.join(self.work, "picked")
        self.pump(app)
        return app

    def texts(self, widget):
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
            out.extend(self.texts(w))
        return out

    def test_settings_section(self):
        app = self.make_app()
        win = app.open_settings(show=False)
        texts = self.texts(win)
        for t in (errors.BACKUP_BUTTON, errors.RESTORE_BUTTON, errors.BACKUP_NOTE):
            self.assertIn(t, texts)
        self.assertFalse(app.backup_button.instate(["disabled"]))
        win.update_idletasks()
        self.assertLessEqual(win.winfo_reqwidth(), 420)

    def test_make_backup(self):
        app = self.make_app()
        app.open_settings(show=False)
        dlg = app.open_backup(show=False)
        texts = self.texts(dlg.win)
        self.assertIn(errors.BACKUP_PRIVATE, texts)   # the don't-share line
        self.assertEqual(dlg.folder, backup.default_folder())   # Documents by default
        self.assertTrue(dlg.settings_var.get() and dlg.addons_var.get())
        self.pump(app)
        self.assertTrue(dlg.addons_check.cget("text").startswith(errors.BACKUP_ADDONS + " (about"))
        self.assertTrue(dlg.start())
        self.pump(app)
        self.assertTrue(dlg.path and os.path.isfile(dlg.path))
        self.assertEqual(os.path.dirname(dlg.path), backup.default_folder())
        self.assertTrue(dlg.status_var.get().startswith("Backup saved: AddonManager backup "))
        dlg.open_result()
        self.assertEqual(self.opened, [backup.default_folder()])
        self.assertFalse(app.busy)
        dlg.close()
        self.assertIsNone(app.backup_dialog)

    def test_make_backup_options_and_folder(self):
        app = self.make_app()
        dlg = app.open_backup(show=False)
        dlg.settings_var.set(False)
        dlg.addons_var.set(False)
        self.assertFalse(dlg.start())
        self.assertEqual(dlg.status_var.get(), errors.BACKUP_PICK_ONE)
        dlg.addons_var.set(True)
        dlg.change_folder()
        picked = os.path.join(self.work, "picked")
        self.assertEqual(config.load_settings()["backup_folder"], picked)
        self.assertTrue(dlg.start())
        self.pump(app)
        self.assertEqual(os.path.dirname(dlg.path), picked)
        self.assertFalse([n for n in names(dlg.path) if n.startswith("WTF/")])
        self.assertIs(config.load_settings()["backup_settings"], False)
        dlg.close()
        again = app.open_backup(show=False)   # remembers the ticks and the folder
        self.assertFalse(again.settings_var.get())
        self.assertEqual(again.folder, picked)

    def test_game_running_blocks(self):
        app = self.make_app()
        self.game = True
        dlg = app.open_backup(show=False)
        dlg.start()
        self.pump(app)
        self.assertEqual(dlg.status_var.get(), errors.GAME_OPEN)
        self.assertFalse(os.path.exists(backup.default_folder())
                         and os.listdir(backup.default_folder()))

    def test_restore_window(self):
        path = backup.make(self.addons, os.path.join(self.work, "b"), now=NOW)[0]
        acc = os.path.join(self.client, "WTF", "Account", "ACCOUNT1")
        write(os.path.join(acc, "SavedVariables", "TaxiTime.lua"), "changed\n")
        os.rename(acc, os.path.join(self.client, "WTF", "Account", "ACCOUNT2"))
        os.makedirs(os.path.join(self.client, "WTF", "Account", "SECOND"))
        app = self.make_app()
        app.open_settings(show=False)
        dlg = app.open_restore(show=False, path=path)
        self.assertIn("From: 2026-11-10", " ".join(self.texts(dlg.win)))
        self.assertIn("ACCOUNT1", dlg.account_vars)   # two accounts here: the player picks
        self.assertEqual(dlg.account_vars["ACCOUNT1"].get(), errors.RESTORE_SKIP)
        dlg.account_vars["ACCOUNT1"].set("ACCOUNT2")
        self.answer = False
        self.assertFalse(dlg.start())   # said no: nothing changes
        self.assertEqual(read_text(os.path.join(self.client, "WTF", "Account", "ACCOUNT2",
                                                "SavedVariables", "TaxiTime.lua")), "changed\n")
        self.answer = True
        self.assertTrue(dlg.start())
        self.assertIn("safety copy", self.asked[-1][1])
        self.pump(app)
        self.assertIsNotNone(dlg.result)
        self.assertEqual(read_text(os.path.join(self.client, "WTF", "Account", "ACCOUNT2",
                                                "SavedVariables", "TaxiTime.lua")),
                         "TaxiTimeDB = 'old'\n")
        self.assertTrue(dlg.status_var.get().startswith("Restored. Safety copy: "))
        self.assertTrue(os.path.isfile(dlg.result.safety))
        self.assertTrue(app.message_var.get().startswith("Restored."))
        self.assertFalse(app.busy)

    def test_restore_refuses_bad_file_and_game(self):
        app = self.make_app()
        app.open_settings(show=False)
        bad = os.path.join(self.work, "bad.zip")
        write(bad, "no zip")
        self.assertIsNone(app.open_restore(show=False, path=bad))
        self.assertIn("isn't an Addon Manager backup", app.backup_status.get())
        app.ask_backup_file = lambda: ""
        self.assertIsNone(app.open_restore(show=False))   # picker cancelled
        path = backup.make(self.addons, os.path.join(self.work, "b"), now=NOW)[0]
        before = fixtures.tree_snapshot(self.client)
        dlg = app.open_restore(show=False, path=path)
        self.game = True
        dlg.start()
        self.pump(app)
        self.assertEqual(dlg.status_var.get(), errors.GAME_OPEN)
        self.assertEqual(fixtures.tree_snapshot(self.client), before)

    def test_no_account_note(self):
        path = backup.make(self.addons, os.path.join(self.work, "b"), now=NOW)[0]
        fixtures.remove(os.path.join(self.client, "WTF", "Account"))
        app = self.make_app()
        dlg = app.open_restore(show=False, path=path)
        self.assertIn(errors.NO_ACCOUNT, self.texts(dlg.win))
        self.assertTrue(dlg.checks["settings"].instate(["disabled"]))
        self.assertFalse(dlg.settings_var.get())
        self.assertTrue(dlg.addons_var.get())

    def test_backup_folder_setting(self):
        self.assertEqual(backup_ui.backup_folder({}), backup.default_folder())
        self.assertEqual(backup_ui.backup_folder({"backup_folder": "relative"}),
                         backup.default_folder())
        here = os.path.abspath(self.work)
        self.assertEqual(backup_ui.backup_folder({"backup_folder": here}), here)


if __name__ == "__main__":
    unittest.main()
