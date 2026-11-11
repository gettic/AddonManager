"""Backup and restore (am\\backup.py, am\\backup_ui.py). Fake WoW folders, a fake
Documents folder and fake cloud folders in temp folders only: the real WoW folder,
WTF, Documents and %APPDATA% are never used."""

import datetime
import gc
import json
import os
import shutil
import time
import tkinter as tk
from tkinter import ttk
import unittest
import zipfile
from unittest import mock

from am import backup, backup_ui, config, errors, ui
from am.backup import BackupError
from tests import fixtures

NOW = datetime.datetime(2026, 11, 10, 21, 30)
SID = "S-1-5-21-1000-2000-3000-1001"


class FakeReg:
    """A fake registry for backup.cloud_folders: {"HKCU\\path": {value name: text}}."""

    def __init__(self, keys):
        self.keys = keys
        self.read = []   # (key, value name) of every value read

    def value(self, key, name):
        self.read.append((key, name))
        return self.keys.get(key, {}).get(name)

    def subkeys(self, key):
        start = key.lower() + "\\"
        out = []
        for k in self.keys:
            if k.lower().startswith(start):
                part = k[len(start):].split("\\")[0]
                if part not in out:
                    out.append(part)
        return out

    def values(self, key):
        self.read.append((key, "*"))
        return list(self.keys.get(key, {}).items())


NO_CLOUD_PC = {"drives": [], "reg": FakeReg({}), "sid": SID}


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
                                                     "OneDriveConsumer": os.path.join(home, "nope")},
                                     **NO_CLOUD_PC)
        labels = [label for label, _p in found]
        self.assertIn("Dropbox", labels)
        self.assertEqual("OneDrive" in labels, os.name == "nt")
        self.assertEqual(backup.cloud_folders(home=os.path.join(self.base, "empty"), env={},
                                              **NO_CLOUD_PC), [])
        self.assertTrue(backup.safety_path(self.out, NOW).endswith("Before restore 2026-11-10 2130.zip"))

    @unittest.skipUnless(os.name == "nt", "Windows cloud apps")
    def test_cloud_folders_windows_apps(self):
        """Every app found from fake folders, drive labels, registry and config only."""
        home = os.path.join(self.base, "cloudhome")
        docs = os.path.join(self.base, "clouddocs")
        cfg = os.path.join(self.base, "cloudcfg")
        g_drive = os.path.join(self.base, "gdrive")      # stands in for G:\
        g_label = os.path.join(self.base, "gdrive2")     # a second, label-only Google mount
        p_drive = os.path.join(self.base, "pdrive")      # stands in for P:\ (pCloud)
        other = os.path.join(self.base, "otherdrive")
        nc = os.path.join(self.base, "Nextcloud sync")
        box_root = os.path.join(self.base, "BoxRoot")
        for d in (os.path.join(g_drive, "Meine Ablage"), os.path.join(g_drive, ".shortcut-targets-by-id"),
                  os.path.join(g_label, "My Drive"), os.path.join(g_label, "Shared drives"),
                  p_drive, other, nc, box_root, os.path.join(docs, "MEGA"),
                  os.path.join(home, "Box"), os.path.join(home, "Proton Drive", "acct", "My files"),
                  os.path.join(home, "Sync"), os.path.join(home, "iCloudDrive")):
            os.makedirs(d, exist_ok=True)
        write(os.path.join(cfg, "Nextcloud", "nextcloud.cfg"),
              "[Accounts]\n0\\dav_user=someone@example.com\n"
              f"0\\Folders\\1\\localPath={nc.replace(os.sep, '/')}/\n")
        prefs = {"per_account_preferences": [
            {"key": "123456789", "value": {"mount_point_path": g_drive}}]}
        roots = backup.SYNC_ROOTS_KEY
        reg = FakeReg({
            backup.DRIVEFS_KEY: {"PerAccountPreferences": json.dumps(prefs)},
            backup.UNINSTALL_KEYS[1] + "\\SyncApp": {"Publisher": "Sync.com Inc."},
            roots + f"\\Box!{SID}!acct\\UserSyncRoots": {SID: box_root},
            roots + f"\\Unknown!{SID}!x\\UserSyncRoots": {SID: other},
            roots + "\\Dropbox!S-1-5-21-9!other\\UserSyncRoots": {"S-1-5-21-9": other},
        })
        found = backup.cloud_folders(
            home=home, env={}, drives=[(g_label, "Google Drive"), (p_drive, "pCloud Drive"),
                                       (other, "Data")],
            reg=reg, cfg=cfg, docs=docs, sid=SID)
        got = {(label, os.path.normcase(os.path.normpath(p))) for label, p in found}
        want = {("Google Drive", os.path.join(g_drive, "Meine Ablage")),
                ("Google Drive", os.path.join(g_label, "My Drive")),
                ("iCloud Drive", os.path.join(home, "iCloudDrive")),
                ("MEGA", os.path.join(docs, "MEGA")),
                ("pCloud", p_drive),
                ("Box", os.path.join(home, "Box")),
                ("Proton Drive", os.path.join(home, "Proton Drive", "acct", "My files")),
                ("Nextcloud", nc),
                ("Sync.com", os.path.join(home, "Sync")),
                ("Box", box_root)}
        self.assertEqual(got, {(label, os.path.normcase(os.path.normpath(p))) for label, p in want})
        # Only mount points / paths are read: never an account value.
        self.assertNotIn("dav_user", repr(found))
        self.assertFalse([r for r in reg.read if "account" in r[1].lower() and r[1] != "PerAccountPreferences"])

    @unittest.skipUnless(os.name == "nt", "Windows cloud apps")
    def test_cloud_folders_need_proof(self):
        """A plain Sync folder without Sync.com installed, a Google mount with two unknown
        folders, another user's sync root: no buttons."""
        home = os.path.join(self.base, "proofhome")
        g = os.path.join(self.base, "proofg")
        for d in (os.path.join(home, "Sync"), os.path.join(g, "Geteilte Ablagen"),
                  os.path.join(g, "Xyz Drive")):
            os.makedirs(d, exist_ok=True)
        reg = FakeReg({backup.SYNC_ROOTS_KEY + "\\OneDrive!S-1-5-21-9!x\\UserSyncRoots":
                       {"S-1-5-21-9": home}})
        self.assertEqual(backup.cloud_folders(home=home, env={}, drives=[(g, "Google Drive")],
                                              reg=reg, sid=SID), [])
        self.assertEqual(backup._drive_root("g"), "G:\\")
        self.assertEqual(backup._drive_root("G:"), "G:\\")
        self.assertIsNone(backup._drive_root("nope"))

    def test_cloud_folders_survive_bad_data(self):
        home = os.path.join(self.base, "badhome")
        cfg = os.path.join(self.base, "badcfg")
        write(os.path.join(cfg, "Nextcloud", "nextcloud.cfg"), "\x00garbage=\n=\n")
        reg = FakeReg({backup.DRIVEFS_KEY: {"PerAccountPreferences": "{not json"}})
        self.assertEqual(backup.cloud_folders(home=home, env={}, drives=[], reg=reg, cfg=cfg,
                                              sid=SID), [])

    def test_find_backups(self):
        docs = os.path.join(self.base, "finddocs", backup.FOLDER_NAME)
        cloud = os.path.join(self.base, "findcloud")
        cloud_backups = os.path.join(cloud, backup.FOLDER_NAME)
        files = {
            os.path.join(docs, "AddonManager backup 2026-11-10.zip"): 300,
            os.path.join(docs, "Before restore 2026-11-10 2142.zip"): 400,
            os.path.join(docs, "AddonManager backup 2026-11-11.zip.part"): 500,   # unfinished
            os.path.join(docs, "holiday photos.zip"): 600,                        # not ours
            os.path.join(cloud_backups, "addonmanager BACKUP 2026-11-03.zip"): 200,
        }
        for path, mtime in files.items():
            write(path, "x" * 10)
            os.utime(path, (mtime, mtime))
        os.makedirs(os.path.join(docs, "AddonManager backup folder.zip"))       # a folder
        found = backup.find_backups([("Documents", docs), ("Again", docs + os.sep),
                                     ("OneDrive", cloud_backups),
                                     ("Gone", os.path.join(self.base, "nothere")), ("x", None)])
        self.assertEqual([(os.path.basename(f.path), f.place, f.safety, f.size) for f in found],
                         [("Before restore 2026-11-10 2142.zip", "Documents", True, 10),
                          ("AddonManager backup 2026-11-10.zip", "Documents", False, 10),
                          ("addonmanager BACKUP 2026-11-03.zip", "OneDrive", False, 10)])
        self.assertFalse(any(f.online for f in found))

    def test_find_backups_never_opens_files(self):
        """Online-only files: only the listing is used; no file is opened or read."""
        folder = os.path.join(self.base, "findonline")
        write(os.path.join(folder, "AddonManager backup 2026-11-10.zip"), "x")
        with mock.patch("builtins.open", side_effect=AssertionError("opened")), \
                mock.patch.object(backup, "read", side_effect=AssertionError("read")), \
                mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("zip")):
            found = backup.find_backups([("OneDrive", folder)])
        self.assertEqual(len(found), 1)
        for attrs, online in ((0, False), (0x20, False), (0x400000, True), (0x40000, True),
                              (0x1000, True), (0x400020, True)):
            st = mock.Mock(st_file_attributes=attrs)
            self.assertIs(backup.online_only(st), online, hex(attrs))
        self.assertFalse(backup.online_only(object()))   # Linux: no attributes

    def test_place_labels(self):
        docs = os.path.join(self.base, "labeldocs")
        one = os.path.join(self.base, "labelone")
        clouds = [("OneDrive", one)]
        places = backup_ui.backup_places(os.path.join(self.base, "My Backups", backup.FOLDER_NAME),
                                         clouds, docs)
        self.assertEqual([p for p, _f in places], ["Documents", "My Backups", "OneDrive"])
        # Documents synced by OneDrive: shown as OneDrive.
        self.assertEqual(backup_ui.place_label(os.path.join(one, "Documents", backup.FOLDER_NAME),
                                               clouds, os.path.join(one, "Documents")), "OneDrive")
        long_name = os.path.join(self.base, "x" * 40)
        self.assertEqual(len(backup_ui.place_label(long_name, [], docs)), backup_ui.PLACE_MAX)
        self.assertEqual(errors.found_row("2026-11-10 21:30", 12 * 1024 * 1024, "Google Drive",
                                          online=True),
                         "2026-11-10 21:30 · 12 MB · Google Drive (online)")

    def test_linux_documents_from_user_dirs(self):
        home = os.path.join(self.base, "home")
        write(os.path.join(home, ".config", "user-dirs.dirs"),
              'XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        self.assertEqual(backup._linux_documents(home), home + "/Dokumente")
        self.assertEqual(backup._linux_documents(os.path.join(self.base, "x")),
                         os.path.join(self.base, "x", "Documents"))


class SpokenTests(unittest.TestCase):
    """Spoken (AI voice-over addons): their folders can be left out; their settings stay."""

    def setUp(self):
        self.work = fixtures.temp_dir("spoken_")
        self.addCleanup(fixtures.remove, self.work)
        self.root, self.addons, self.client = make_pc(os.path.join(self.work, "pc"))
        for name in ("Spoken", "SpokenQuests", "spokenNPCs", "Unspoken"):
            fixtures.make_addon_folder(self.addons, name, "1.0")
        write(os.path.join(self.addons, "SpokenQuests", "voice.ogg"), "x" * 5000)
        acc = os.path.join(self.client, "WTF", "Account", "ACCOUNT1", "SavedVariables")
        write(os.path.join(acc, "SpokenQuests.lua"), "SpokenDB = {}\n")

    def test_matching(self):
        self.assertTrue(all(backup.is_spoken(n) for n in ("Spoken", "SPOKEN", "spokenx")))
        self.assertFalse(any(backup.is_spoken(n) for n in ("Unspoken", "Spoke", "TaxiTime")))
        found = dict(backup.spoken_folders(self.addons))
        self.assertEqual(sorted(found, key=str.lower), ["Spoken", "spokenNPCs", "SpokenQuests"])
        self.assertGreater(found["SpokenQuests"], 5000)
        self.assertEqual(backup.addons_size(self.addons, tuple(found)),
                         backup.addons_size(self.addons) - sum(found.values()))

    def test_left_out_but_settings_kept(self):
        out = os.path.join(self.work, "b")
        leave = tuple(f for f, _s in backup.spoken_folders(self.addons))
        path, _size, counts = backup.make(self.addons, out, now=NOW, leave_out=leave)
        got = names(path)
        self.assertFalse([n for n in got if n.lower().startswith("addons/spoken")])
        self.assertTrue([n for n in got if n.startswith("AddOns/Unspoken/")])
        self.assertIn("WTF/Account/ACCOUNT1/SavedVariables/SpokenQuests.lua", got)
        info = backup.read(path)
        self.assertEqual(info.left_out, ["Spoken", "spokenNPCs", "SpokenQuests"])
        self.assertNotIn("Spoken", info.addons)
        full = backup.read(backup.make(self.addons, out, now=NOW)[0])   # kept: all there
        self.assertEqual(full.left_out, [])
        self.assertIn("SpokenQuests", full.addons)

    def test_left_out_names_are_checked(self):
        bad = os.path.join(self.work, "bad.zip")
        raw_backup(bad, [("AddOns/A/A_Forever.toc", "x")],
                   meta={"app": "AddonManager", "format": 1,
                         "left_out": ["Spoken", "../x", 5, "a" * 100]})
        self.assertEqual(backup.read(bad).left_out, ["Spoken"])


class SpaceTests(unittest.TestCase):
    """Big backups: room checked before a restore writes anything, and
    the 8 GiB unpacked limit."""

    def setUp(self):
        self.base = fixtures.temp_dir("space_")
        self.addCleanup(shutil.rmtree, self.base, True)
        _r, addons, _c = make_pc(os.path.join(self.base, "pc1"))
        self.path, _s, _n = backup.make(addons, os.path.join(self.base, "backups"))
        _r2, self.addons2, self.client2 = make_pc(os.path.join(self.base, "pc2"),
                                                  account="MINE", tag="new")
        self.safety = os.path.join(self.base, "safety")

    def tree(self):
        out = []
        for folder, _dirs, files in os.walk(self.client2):
            for f in files:
                with open(os.path.join(folder, f), "rb") as fh:
                    out.append((os.path.relpath(os.path.join(folder, f), self.client2),
                                fh.read()))
        return sorted(out)

    def test_restore_checks_space_first(self):
        before = self.tree()
        with self.assertRaises(BackupError) as cm:
            backup.restore(self.path, self.addons2, self.safety,
                           account_map={"ACCOUNT1": "MINE"},
                           disk_usage=lambda p: mock.Mock(free=1000))
        self.assertEqual(cm.exception.kind, "disk_full")
        self.assertGreater(cm.exception.need, backup.SPARE_DISK)
        self.assertIn("about", errors.backup_message(cm.exception))
        self.assertEqual(self.tree(), before)               # nothing changed
        self.assertFalse(os.path.exists(self.safety))      # no safety copy either
        result = backup.restore(self.path, self.addons2, self.safety,
                                account_map={"ACCOUNT1": "MINE"},
                                disk_usage=lambda p: mock.Mock(free=10 ** 12))
        self.assertTrue(result.addons)

    def test_space_needed(self):
        one = backup.space_needed(self.addons2, 100, self.safety, 30)
        self.assertEqual(one, [(self.addons2, 130 + backup.SPARE_DISK)])   # same drive here
        self.assertEqual(backup.space_needed(self.addons2, 100, self.safety, 0),
                         [(self.addons2, 100 + backup.SPARE_DISK)])
        with mock.patch.object(backup, "same_drive", return_value=False):
            self.assertEqual(backup.space_needed(self.addons2, 100, self.safety, 30),
                             [(self.addons2, 100 + backup.SPARE_DISK),
                              (self.safety, 30 + backup.SPARE_DISK)])
        self.assertIsNotNone(backup.free_space(os.path.join(self.base, "not", "made", "yet")))

    def test_max_total(self):
        self.assertEqual(backup.MAX_TOTAL, 8 * 1024 ** 3)
        with mock.patch.object(backup, "MAX_TOTAL", 10):
            with self.assertRaises(BackupError) as cm:
                backup.read(self.path)
        self.assertEqual(cm.exception.kind, "unsafe")


class WindowBackupTests(unittest.TestCase):
    """The hidden window: the Backup window, Make a backup and Restore windows."""

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
        self.clouds = []   # the real PC's cloud folders are never looked at here
        patcher = mock.patch.object(backup, "cloud_folders", lambda: list(self.clouds))
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_backup_window(self):
        """The main window's Backup button opens the Backup window (Make / Restore, the
        last backup); Settings has nothing about backups any more."""
        docs = os.path.join(self.work, "docs")
        for target in ("documents_dir", "default_folder"):
            value = docs if target == "documents_dir" else os.path.join(docs, backup.FOLDER_NAME)
            patcher = mock.patch.object(backup, target, lambda v=value: v)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = self.make_app()
        self.assertIs(app.backup_main_button.master, app.settings_button.master)
        self.assertEqual(app.backup_main_button.cget("text"), errors.BACKUP_HEADING)
        self.assertFalse(app.backup_main_button.instate(["disabled"]))
        settings = app.open_settings(show=False)
        for t in (errors.BACKUP_BUTTON, errors.RESTORE_BUTTON, errors.BACKUP_HEADING,
                  errors.SEND_BUTTON, errors.BACKUP_NOTE):
            self.assertNotIn(t, self.texts(settings))
        app.close_settings()
        hub = app.open_backup_window(show=False)   # what the Backup button calls (shown)
        self.assertIs(app.open_backup_window(show=False), hub)   # only one window
        self.assertEqual(hub.win.state(), "withdrawn")
        texts = self.texts(hub.win)
        for t in (errors.BACKUP_BUTTON, errors.RESTORE_BUTTON, errors.BACKUP_NOTE,
                  errors.BACKUP_THIS_PC):
            self.assertIn(t, texts)
        self.assertNotIn(errors.SEND_BUTTON, texts)   # no relay here: no Send/Receive
        self.assertFalse(hub.make_button.instate(["disabled"]))
        self.pump(app)
        self.assertEqual(hub.last_var.get(), errors.BACKUP_LAST_NONE)
        self.assertEqual(hub.open_button.winfo_manager(), "")   # nothing to open yet
        hub.win.update_idletasks()
        self.assertLessEqual(hub.win.winfo_reqwidth(), 420)
        # Make a backup opens over the Backup window; the line updates when it closes.
        hub.make_button.invoke()
        dlg = app.backup_dialog
        self.assertIsInstance(dlg, backup_ui.BackupDialog)
        self.assertIs(dlg.parent, hub.win)
        self.assertTrue(dlg.start())
        self.assertTrue(app.backup_main_button.instate(["disabled"]))   # busy
        self.assertTrue(hub.make_button.instate(["disabled"]))
        app.close_backup_window()   # refused while the backup runs
        self.assertIs(app.backup_win, hub)
        app.close()                 # the main window waits too
        self.assertFalse(app.closed)
        self.pump(app)
        self.assertTrue(dlg.path and os.path.isfile(dlg.path))
        dlg.close()
        self.pump(app)
        when = backup_ui.found_date(os.path.getmtime(dlg.path))
        self.assertEqual(hub.last_var.get(), errors.backup_last(when, errors.PLACE_DOCUMENTS))
        self.assertEqual(hub.open_button.winfo_manager(), "pack")
        hub.open_button.invoke()
        self.assertEqual(self.opened, [os.path.dirname(dlg.path)])
        self.assertFalse(hub.make_button.instate(["disabled"]))
        # A safety copy or a received file never counts as the last backup.
        f = backup.FoundBackup
        older = f(dlg.path, "Documents", 1, 1, False, False)
        newer = [f(os.path.join(docs, "Before restore 2030-01-01 1200.zip"), "Documents", 1, 9,
                   True, False),
                 f(backup.received_path(docs), "Documents", 1, 8, False, False)]
        self.assertIs(backup_ui.newest_made(newer + [older]), older)
        self.assertIsNone(backup_ui.newest_made(newer))
        self.assertTrue(hub.win.bind("<Escape>"))   # Esc closes it
        app.close_backup_window()
        self.assertIsNone(app.backup_win)
        self.assertTrue(hub.closed)

    def test_status_line_takes_no_room_while_empty(self):
        """Make a backup: no empty status line above the buttons; shown once it has text
        (above the progress bar), gone again when emptied."""
        app = self.make_app()
        dlg = app.open_backup(show=False)
        self.pump(app)
        self.assertEqual(dlg.status.winfo_manager(), "")
        dlg.status_var.set("Working...")
        self.assertEqual(dlg.status.winfo_manager(), "pack")
        slaves = dlg.frame.pack_slaves()
        self.assertLess(slaves.index(dlg.status), slaves.index(dlg.progress))
        dlg.status_var.set("")
        self.assertEqual(dlg.status.winfo_manager(), "")
        dlg.close()

    def test_licences_keep_their_own_lines(self):
        app = self.make_app()
        app.open_settings(show=False)
        dlg = app.open_licences(show=False)
        self.assertEqual(str(dlg.text.cget("wrap")), "none")
        self.assertEqual(str(dlg.hbar.cget("orient")), "horizontal")
        dlg.close()

    def test_make_backup(self):
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_backup(show=False)
        texts = self.texts(dlg.win)
        self.assertIn(errors.BACKUP_PRIVATE, texts)   # the don't-share line
        self.assertEqual(dlg.folder, backup.default_folder())   # Documents by default
        self.assertTrue(dlg.settings_var.get() and dlg.addons_var.get())
        self.pump(app)
        self.assertTrue(dlg.addons_check.cget("text").startswith(errors.BACKUP_ADDONS + " (about"))
        # The progress bar: there (empty) before the start, so the window keeps its size.
        self.assertEqual(dlg.progress.winfo_manager(), "pack")
        self.assertEqual(float(dlg.progress.cget("value")), 0)
        self.assertTrue(dlg.start())
        self.assertEqual(str(dlg.progress.cget("mode")), "indeterminate")   # moving at once
        dlg.handle(("backup_progress", 50, 200))   # bytes written so far: filled by bytes
        self.assertEqual(str(dlg.progress.cget("mode")), "determinate")
        self.assertEqual(float(dlg.progress.cget("value")), 25)
        self.assertEqual(dlg.status_var.get(), errors.backup_making(25))
        self.pump(app)
        self.assertEqual(float(dlg.progress.cget("value")), 100)
        dlg.handle(("backup_progress", 1, 200))    # a late message after the end: ignored
        self.assertEqual(float(dlg.progress.cget("value")), 100)
        self.assertTrue(dlg.path and os.path.isfile(dlg.path))
        self.assertEqual(os.path.dirname(dlg.path), backup.default_folder())
        self.assertTrue(dlg.status_var.get().startswith("Backup saved: AddonManager backup "))
        dlg.open_result()
        self.assertEqual(self.opened, [backup.default_folder()])
        self.assertFalse(app.busy)
        dlg.close()
        self.assertIsNone(app.backup_dialog)

    def test_cloud_buttons_grid(self):
        names_ = ["OneDrive", "Google Drive", "iCloud Drive", "Dropbox", "MEGA", "pCloud",
                  "Box", "Proton Drive", "Nextcloud", "Sync.com"]
        self.clouds = [(n, os.path.join(self.work, "cloud", n)) for n in names_]
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_backup(show=False)
        self.assertEqual(dlg.cloud_buttons, [])   # filled from the thread, not at once
        self.pump(app)
        self.assertEqual([b.cget("text") for b in dlg.cloud_buttons], names_[:9])
        cells = [(int(b.grid_info()["row"]), int(b.grid_info()["column"]))
                 for b in dlg.cloud_buttons]
        self.assertEqual(cells, [(r, c) for r in range(3) for c in range(3)])
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        dlg.cloud_buttons[7].invoke()
        self.assertEqual(dlg.folder, os.path.join(self.work, "cloud", "Proton Drive",
                                                  backup.FOLDER_NAME))
        self.assertEqual(dlg.folder_var.get(), errors.saved_to(dlg.folder))
        dlg.handle(("backup_clouds", self.clouds[:1]))   # a late second message: no change
        self.assertEqual(len(dlg.cloud_buttons), 9)
        dlg.close()
        path = backup.make(self.addons, os.path.join(self.work, "b"), now=NOW)[0]
        rest = app.open_restore(show=False, path=path)
        before = rest.status_var.get()
        rest.handle(("backup_clouds", []))   # a late message: ignored by the Restore window
        self.assertEqual(rest.status_var.get(), before)
        self.assertFalse(rest.running)

    def found_window(self, docs):
        """A Backups found window over a per-test Documents (built hidden, never shown)."""
        patcher = mock.patch.object(backup, "documents_dir", lambda: docs)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = self.make_app()
        app.open_backup_window(show=False)
        dlg = app.open_restore(show=False)   # the Backup window's Restore button
        self.assertIsInstance(dlg, backup_ui.BackupListDialog)
        self.assertEqual(dlg.status_var.get(), errors.FOUND_LOOKING)
        for _ in range(100):
            if dlg.found is not None:
                break
            self.pump(app, timeout=0.1)
        self.assertIsNotNone(dlg.found)
        return app, dlg

    def test_backups_found_list(self):
        docs = os.path.join(self.work, "docs")
        cloud = os.path.join(self.work, "Drive")
        self.clouds = [("Google Drive", cloud)]
        made = backup.make(self.addons, os.path.join(docs, backup.FOLDER_NAME), now=NOW)[0]
        os.utime(made, (2000, 2000))
        in_cloud = backup.make(self.addons, os.path.join(cloud, backup.FOLDER_NAME),
                               now=NOW - datetime.timedelta(days=7))[0]
        os.utime(in_cloud, (1000, 1000))
        safety = os.path.join(docs, backup.FOLDER_NAME, "Before restore 2026-11-10 2142.zip")
        write(safety, "x")
        os.utime(safety, (3000, 3000))
        app, dlg = self.found_window(docs)
        texts = [b.cget("text") for b in dlg.row_buttons]
        self.assertEqual(len(texts), 2)   # safety copies hidden by default
        self.assertTrue(texts[0].endswith(" · Documents") and texts[1].endswith(" · Google Drive"))
        self.assertNotIn(errors.FOUND_SAFETY_HEADING, self.texts(dlg.win))
        dlg.safety_check.invoke()   # safety copies: their own list under the backups
        self.assertIn(errors.FOUND_SAFETY_HEADING, self.texts(dlg.win))
        self.assertEqual(len(dlg.row_buttons), 3)
        self.assertEqual(dlg.shown_backups()[1][0].path, safety)
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        # A late message for another window changes nothing here.
        dlg.handle(("restore_opened", "other.zip", None))
        dlg.handle(("restore_found", []))
        self.assertEqual(len(dlg.row_buttons), 3)
        dlg.row_buttons[1].invoke()   # the cloud one: one click -> Restore window
        self.assertEqual(dlg.status_var.get(), errors.FOUND_OPENING)
        self.assertTrue(dlg.browse_button.instate(["disabled"]))
        self.pump(app)
        for _ in range(50):
            if isinstance(app.backup_dialog, backup_ui.RestoreDialog):
                break
            self.pump(app, timeout=0.1)
        rest = app.backup_dialog
        self.assertIsInstance(rest, backup_ui.RestoreDialog)
        self.assertTrue(dlg.closed)
        self.assertEqual(os.path.normcase(rest.info.path), os.path.normcase(in_cloud))
        self.assertIn("From: ", " ".join(self.texts(rest.win)))

    def test_backups_found_none_and_browse(self):
        docs = os.path.join(self.work, "emptydocs")
        app, dlg = self.found_window(docs)
        self.assertEqual(dlg.row_buttons, [])
        self.assertEqual(dlg.status_var.get(), errors.FOUND_NONE)
        bad = os.path.join(self.work, "bad.zip")
        write(bad, "no zip")
        app.ask_backup_file = lambda: bad
        dlg.browse()
        for _ in range(50):
            if not dlg.opening:
                break
            self.pump(app, timeout=0.1)
        self.assertIn("isn't an Addon Manager backup", dlg.status_var.get())
        self.assertFalse(dlg.browse_button.instate(["disabled"]))
        self.assertIs(app.backup_dialog, dlg)
        app.ask_backup_file = lambda: ""   # cancelled: nothing happens
        dlg.browse()
        self.assertIsNone(dlg.opening)
        dlg.close()
        self.assertIsNone(app.backup_dialog)

    def test_backups_found_max_rows(self):
        docs = os.path.join(self.work, "manydocs")
        for i in range(10):
            p = os.path.join(docs, backup.FOLDER_NAME, f"AddonManager backup 2026-10-{i + 1:02d}.zip")
            write(p, "x")
            os.utime(p, (1000 + i, 1000 + i))
        app, dlg = self.found_window(docs)
        self.assertEqual(len(dlg.row_buttons), backup_ui.MAX_FOUND_ROWS)
        dlg.win.update_idletasks()
        self.assertLessEqual(dlg.win.winfo_reqwidth(), 420)
        self.assertIn("2026-10-10", os.path.basename(dlg.shown_backups()[0][0].path))
        self.assertIn(errors.FOUND_MORE, self.texts(dlg.win))

    def test_backups_found_odd_dates(self):
        """A file dated before 1970: listed last, every row drawn, the status updated. Its
        date is '?' on Windows (it can't show it); Linux shows the real date."""
        docs = os.path.join(self.work, "olddocs")
        old = os.path.join(docs, backup.FOLDER_NAME, "AddonManager backup 1968-12-01.zip")
        new = os.path.join(docs, backup.FOLDER_NAME, "AddonManager backup 2026-11-10.zip")
        write(old, "x")
        write(new, "x")
        os.utime(old, (-34560000, -34560000))
        os.utime(new, (2000, 2000))
        app, dlg = self.found_window(docs)
        texts = [b.cget("text") for b in dlg.row_buttons]
        self.assertEqual(len(texts), 2)
        self.assertEqual(os.path.basename(dlg.shown_backups()[0][1].path),
                         os.path.basename(old))   # sorted last
        if os.name == "nt":
            self.assertTrue(texts[1].startswith("? · "), texts)
        else:
            self.assertRegex(texts[1], r"^(\?|19\d\d-\d\d-\d\d \d\d:\d\d) · ")
        self.assertNotEqual(dlg.status_var.get(), errors.FOUND_LOOKING)
        for bad in (1e20, float("nan")):
            self.assertEqual(backup_ui.found_date(bad), "?")
        if os.name == "nt":
            self.assertEqual(backup_ui.found_date(-34560000), "?")

    def test_no_cloud_buttons(self):
        app = self.make_app()
        dlg = app.open_backup(show=False)
        self.pump(app)
        self.assertEqual(dlg.cloud_buttons, [])
        self.assertNotIn(errors.BACKUP_CLOUD, self.texts(dlg.win))
        self.assertEqual(dlg.folder, backup.default_folder())   # Documents stays the default

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

    def test_spoken_question(self):
        for name in ("Spoken", "SpokenQuests"):
            fixtures.make_addon_folder(self.addons, name, "1.0")
        write(os.path.join(self.client, "WTF", "Account", "ACCOUNT1", "SavedVariables",
                           "Spoken.lua"), "SpokenDB = {}\n")
        app = self.make_app()
        dlg = app.open_backup(show=False)
        self.assertTrue(dlg.start())
        end = time.time() + 20
        while dlg.spoken_ask is None and time.time() < end:   # looked at, then asked
            self.root.update()
            time.sleep(0.02)
        ask = dlg.spoken_ask
        texts = " ".join(self.texts(ask.win))
        for want in ("Spoken (AI voice-over addon)", "Spoken, SpokenQuests",
                     errors.SPOKEN_SKIP, errors.SPOKEN_KEEP, "settings are still saved"):
            self.assertIn(want, texts)
        self.assertFalse(dlg.running)
        ask.answer(None)                     # Esc: kept this time, nothing remembered
        self.pump(app)
        self.assertIsNone(config.load_settings().get("backup_spoken"))
        self.assertFalse(dlg.spoken_var.get())   # the tick shows them kept too
        self.assertIn("AddOns/Spoken/Spoken.lua", names(dlg.path))
        dlg.close()
        dlg = app.open_backup(show=False)
        self.pump(app)
        self.assertTrue(dlg.start())
        while dlg.spoken_ask is None and time.time() < end:
            self.root.update()
        dlg.spoken_ask.answer("skip")        # remembered
        self.pump(app)
        self.assertEqual(config.load_settings()["backup_spoken"], "skip")
        got = names(dlg.path)
        self.assertFalse([n for n in got if n.startswith("AddOns/Spoken")])
        self.assertIn("WTF/Account/ACCOUNT1/SavedVariables/Spoken.lua", got)
        self.assertEqual(backup.read(dlg.path).left_out, ["Spoken", "SpokenQuests"])
        self.assertIn(errors.SPOKEN_LEFT_OUT, dlg.status_var.get() + errors.SPOKEN_LEFT_OUT)
        path = dlg.path
        dlg.close()
        again = app.open_backup(show=False)  # no question now: the tick shows the answer
        self.pump(app)
        self.assertIsNotNone(again.spoken_check)
        self.assertTrue(again.spoken_var.get())
        self.assertIn("Spoken (AI voice-over addon)", again.spoken_label.cget("text"))
        # One tick whose text wraps (lines up with the ticks above).
        self.assertIs(again.spoken_label, again.spoken_check)
        style = again.spoken_check.cget("style")
        self.assertTrue(int(str(ttk.Style(again.win).lookup(style, "wraplength"))) > 0)
        again.win.update_idletasks()
        self.assertLessEqual(again.win.winfo_reqwidth(), 420)
        again.spoken_var.set(False)
        again.save_spoken()                  # changed with the tick: kept from now on
        self.assertEqual(config.load_settings()["backup_spoken"], "keep")
        self.assertTrue(again.start())
        self.assertIsNone(again.spoken_ask)
        self.pump(app)
        self.assertIn("AddOns/Spoken/Spoken.lua", names(again.path))
        again.close()
        rest = app.open_restore(show=False, path=path)   # Restore says what was left out
        self.assertIn(errors.restore_left_out(["Spoken", "SpokenQuests"]),
                      self.texts(rest.win))
        rest.close()

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
        app.open_backup_window(show=False)
        dlg = app.open_restore(show=False, path=path)
        # The date written into backup.json when the backup was made (not today's date).
        made = backup.read(path).created.split(" ")[0]
        self.assertRegex(made, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn(f"From: {made}", " ".join(self.texts(dlg.win)))
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
        app.open_backup_window(show=False)
        bad = os.path.join(self.work, "bad.zip")
        write(bad, "no zip")
        self.assertIsNone(app.open_restore(show=False, path=bad))
        self.assertIn("isn't an Addon Manager backup", app.backup_win.status_var.get())
        listed = app.open_restore(show=False)   # no path: the Backups found window
        self.assertIsInstance(listed, backup_ui.BackupListDialog)
        self.assertIsNone(app.open_restore(show=False))   # one window at a time
        listed.close()
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
