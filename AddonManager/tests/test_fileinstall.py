"""Add from file: any addon from a .zip/.rar/.7z or a folder. All on a fake WoW folder in
temp; WTF and an unrelated OtherAddon are checked untouched where files get installed."""

import os
import shutil
import stat
import sys
import tkinter as tk
import unittest
import zipfile

from am import archive, backup, fileinstall, fileinstall_ui, installer, instance, theme, toc
from am.archive import ArchiveError
from am.installer import InstallError
from tests import archive_fixtures as af
from tests.fixtures import make_addon_folder, make_fake_wow, temp_dir, tree_snapshot

GUILD = ["TaxiTime"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir("fileinst_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root, self.addons = make_fake_wow(self.tmp)
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.wtf = os.path.join(self.client, "WTF")
        self.wtf_before = tree_snapshot(self.wtf)
        self.other_before = tree_snapshot(os.path.join(self.addons, "OtherAddon"))
        self.safety = os.path.join(self.tmp, "backups")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.plans = []

    def tearDown(self):
        for p in self.plans:
            p.close()

    def untouched(self):
        self.assertEqual(tree_snapshot(self.wtf), self.wtf_before)
        self.assertEqual(tree_snapshot(os.path.join(self.addons, "OtherAddon")), self.other_before)

    def zip(self, entries, name="pick.zip"):
        return af.write_zip(os.path.join(self.tmp, name), entries)

    def scan(self, path, tracked=None, runner=None):
        plan = fileinstall.scan(path, self.addons, GUILD, tracked or {}, runner=runner,
                                tmp_root=self.work)
        self.plans.append(plan)
        return plan

    def install(self, plan, folders=None, tracked=None):
        folders = folders if folders is not None else [a.folder for a in plan.addons if a.tick]
        return fileinstall.install(plan, folders, self.addons, GUILD, tracked or {}, self.safety)

    def assertRefused(self, path, kind, runner=None):
        before = set(os.listdir(self.work))
        with self.assertRaises(ArchiveError) as cm:
            self.scan(path, runner=runner)
        self.assertEqual(cm.exception.kind, kind, cm.exception)
        self.assertEqual(set(os.listdir(self.work)), before, "temp left behind")
        return cm.exception


class TocTests(unittest.TestCase):
    def test_pick_order(self):
        names = ["A.toc", "A_Mainline.toc", "A-Camelot.toc", "A_Forever.toc", "A_Classic.toc"]
        self.assertEqual(toc.pick(names, "A"), "A_Forever.toc")
        self.assertEqual(toc.pick(names[:3], "A"), "A-Camelot.toc")
        self.assertEqual(toc.pick(names[:2], "A"), "A_Mainline.toc")
        self.assertEqual(toc.pick(["a.TOC", "A_Vanilla.toc"], "A"), "a.TOC")
        self.assertIsNone(toc.pick(["A_Classic.toc", "A-TBC.toc"], "A"))
        self.assertEqual(toc.pick(["X_Classic.toc"], "X_Classic"), "X_Classic.toc")
        self.assertIsNone(toc.pick(["B.toc"], "A"))
        self.assertEqual(toc.pick(["Bagnon_Config.toc"], "Bagnon_Config"), "Bagnon_Config.toc")
        self.assertFalse(toc.belongs("Bagnon_Config.toc", "Bagnon"))

    def test_status_title(self):
        self.assertEqual(toc.interfaces("## Interface: 120105, 16001, 11509\n"),
                         [120105, 16001, 11509])
        self.assertEqual(toc.forever_status("A.toc", "## Interface: 120105, 16001"), toc.FOREVER)
        self.assertEqual(toc.forever_status("A.toc", "## Interface: 11507"), toc.OTHER_VERSION)
        self.assertEqual(toc.forever_status("A.toc", "## Title: x"), toc.OTHER_VERSION)
        self.assertEqual(toc.forever_status(None, ""), toc.NOT_FOREVER)
        self.assertEqual(toc.title("## Title: |cff00ff00Details!|r |TInterface\\x:0|t Dmg\n", "D"),
                         "Details! Dmg")
        self.assertEqual(toc.title("## Notes: x\n", "Fallback"), "Fallback")


class ZipSafetyTests(Base):
    def test_attacks_refused(self):
        good = af.addon_files("Good")
        cases = {
            "unsafe": [{"../evil.lua": "x"}, {"/abs/A.toc": "x"}, {"C:/x/A.toc": "x"},
                       {"A/CON/x.lua": "x"}, {"A/x./y.lua": "x"},
                       {"A/A.toc": "x", "a/a.toc": "y"}],
            "program": [{"A/A.toc": "x", "A/setup.exe": "MZ"}, {"A/A.toc": "x", "A/x.DLL": "x"},
                        {"A/A.toc": "x", "A/run.bat": "x"}, {"A/A.toc": "x", "A/x.ps1": "x"},
                        {"A/A.toc": "x", "A/x.lnk": "x"},
                        {"A/A.toc": "x", "A/README": "MZ\x90\x00"},
                        {"A/A.toc": "x", "A/tool.lua": "\x7fELF...."}],
        }
        for kind, entry_sets in cases.items():
            for entries in entry_sets:
                with self.subTest(kind=kind, entries=list(entries)):
                    e = dict(good)
                    e.update(entries)
                    self.assertRefused(self.zip(e), kind)
        self.untouched()

    def test_link_encrypted_bomb_count(self):
        link = zipfile.ZipInfo("A/link.lua")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assertRefused(self.zip([("A/A.toc", "x"), (link, "../../x")]), "unsafe")
        enc = self.zip({"A/A.toc": "x"}, "enc.zip")
        with open(enc, "r+b") as f:   # set the "encrypted" flag in both headers
            data = bytearray(f.read())
            data[6] |= 1
            cd = data.index(b"PK\x01\x02")
            data[cd + 8] |= 1
            f.seek(0)
            f.write(data)
        self.assertRefused(enc, "encrypted")
        self.assertRefused(self.zip([("A/A.toc", "x"), ("A/big.lua", b"\0" * (5 * 1024 * 1024))]),
                           "too_big")
        old = archive.MAX_ENTRIES
        archive.MAX_ENTRIES = 10
        self.addCleanup(setattr, archive, "MAX_ENTRIES", old)
        self.assertRefused(self.zip({f"A/{i}.lua": "x" for i in range(12)}), "too_big")

    def test_total_size_and_file_size(self):
        old = archive.MAX_UNPACKED
        archive.MAX_UNPACKED = 1000
        self.addCleanup(setattr, archive, "MAX_UNPACKED", old)
        self.assertRefused(self.zip({"A/A.toc": "x", "A/a.lua": os.urandom(2000)}), "too_big")
        archive.MAX_UNPACKED = old
        oldf = archive.MAX_FILE
        archive.MAX_FILE = 100
        self.addCleanup(setattr, archive, "MAX_FILE", oldf)
        self.assertRefused(self.zip(af.addon_files("A")), "too_big")

    def test_not_an_archive_and_missing(self):
        p = os.path.join(self.tmp, "notes.txt")
        with open(p, "w") as f:
            f.write("hello")
        self.assertRefused(p, "bad_archive")
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"PK\x03\x04garbage")
        self.assertRefused(bad, "bad_archive")
        self.assertRefused(os.path.join(self.tmp, "gone.zip"), "not_found")
        self.assertRefused(self.zip({"readme.txt": "no addon"}), "empty")

    def test_renamed_file_still_read(self):
        p = self.zip(af.addon_files("A"), name="a.dat")
        self.assertEqual([a.folder for a in self.scan(p).addons], ["A"])

    def test_left_out_and_blizzard(self):
        e = af.addon_files("A", extra={"A/release.sh": "#!/bin/sh", "A/.pkgmeta": "x",
                                       "A/.github/workflows/x.yml": "x", "A/data.json": "{}"})
        e.update(af.addon_files("Blizzard_Bags"))
        e["__MACOSX/A/._A.toc"] = "junk"
        plan = self.scan(self.zip(e))
        self.assertEqual([a.folder for a in plan.addons], ["A"])
        self.assertEqual(plan.skipped, [("Blizzard_Bags", fileinstall.BLIZZARD)])
        a = plan.addons[0]
        self.assertEqual(a.left_out_count, 4)
        self.assertIn("Left out: 4 files", fileinstall_ui.left_out_line(plan))
        self.install(plan)
        installed = os.path.join(self.addons, "A")
        self.assertTrue(os.path.isfile(os.path.join(installed, "Libs", "LibStub", "LibStub.lua")))
        for name in ("release.sh", ".pkgmeta", ".github", "data.json"):
            self.assertFalse(os.path.exists(os.path.join(installed, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.addons, "Blizzard_Bags")))
        self.untouched()


class LayoutTests(Base):
    def folders(self, entries, **kw):
        return sorted((a.folder, a.renamed_from) for a in self.scan(self.zip(entries), **kw).addons)

    def test_layouts(self):
        self.assertEqual(self.folders(af.addon_files("Details")), [("Details", None)])
        pack = {}
        for f in ("Ace3", "LibStub", "Details"):
            pack.update(af.addon_files(f))
        self.assertEqual([f for f, _ in self.folders(pack)], ["Ace3", "Details", "LibStub"])
        self.assertEqual(self.folders(af.addon_files("Addon", prefix="Addon-main/")),
                         [("Addon", None)])
        self.assertEqual(self.folders(af.addon_files("Deep", prefix="Pack v2/inner/")),
                         [("Deep", None)])
        self.assertRefused(self.zip(af.addon_files("TooDeep", prefix="a/b/c/")), "empty")
        # GitHub source zip: Addon-main/Addon.toc -> installed as Addon
        gh = {k.replace("Addon/", "Addon-main/"): v for k, v in af.addon_files("Addon").items()}
        self.assertEqual(self.folders(gh), [("Addon", "Addon-main")])
        # tocs at the top of the file, no folder
        root = {k.replace("Solo/", ""): v for k, v in af.addon_files("Solo").items()}
        self.assertEqual(self.folders(root), [("Solo", "")])
        self.assertRefused(self.zip({"A.toc": "x", "B.toc": "y"}), "ambiguous")
        both = dict(af.addon_files("Same"))
        both.update(af.addon_files("Same", prefix="wrap/"))
        self.assertRefused(self.zip(both), "ambiguous")

    def test_names_with_marks_and_suffix_tocs(self):
        e = af.addon_files("!BugGrabber", toc_name="!BugGrabber_Forever.toc")
        e.update(af.addon_files("My Addon", toc_name="My Addon-Mainline.toc", interface="110000"))
        e.update(af.addon_files("Old", toc_name="Old_Vanilla.toc", interface="11507"))
        plan = self.scan(self.zip(e))
        by = {a.folder: a for a in plan.addons}
        self.assertEqual(by["!BugGrabber"].status, toc.FOREVER)
        self.assertEqual(by["My Addon"].status, toc.OTHER_VERSION)
        self.assertEqual(by["Old"].status, toc.NOT_FOREVER)
        self.assertFalse(by["Old"].tick)
        self.assertIn("Not made for WoW Forever", fileinstall_ui.note_for(by["Old"]))
        self.install(plan)
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "!BugGrabber")))
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "My Addon")))
        self.assertFalse(os.path.exists(os.path.join(self.addons, "Old")))

    def test_nested_zips(self):
        inner_a = af.write_zip(os.path.join(self.tmp, "a.zip"), af.addon_files("InA"))
        inner_b = af.write_zip(os.path.join(self.tmp, "b.zip"), af.addon_files("InB"))
        with open(inner_a, "rb") as fa, open(inner_b, "rb") as fb:
            outer = {"Pack/a.zip": fa.read(), "Pack/b.zip": fb.read(), "readme.txt": "x"}
        self.assertEqual([f for f, _ in self.folders(outer)], ["InA", "InB"])
        # tocs and an inner zip: the zip is left out
        mixed = dict(af.addon_files("Top"))
        with open(inner_a, "rb") as fa:
            mixed["extras/a.zip"] = fa.read()
        plan = self.scan(self.zip(mixed))
        self.assertEqual([a.folder for a in plan.addons], ["Top"])
        self.assertEqual(len(plan.unused_archives), 1)
        # two levels deep: not opened
        with open(self.zip(outer, "level1.zip"), "rb") as f:
            self.assertRefused(self.zip({"level1.zip": f.read()}, "level2.zip"), "empty")
        # same addon in two inner zips
        with open(inner_a, "rb") as fa:
            data = fa.read()
        self.assertRefused(self.zip({"x/a.zip": data, "y/a.zip": data}), "ambiguous")
        old = archive.MAX_INNER
        archive.MAX_INNER = 1
        self.addCleanup(setattr, archive, "MAX_INNER", old)
        self.assertRefused(self.zip(outer), "too_big")

    def test_inner_program_refused(self):
        inner = af.write_zip(os.path.join(self.tmp, "i.zip"),
                             dict(af.addon_files("X"), **{"X/x.exe": "MZ"}))
        with open(inner, "rb") as f:
            self.assertRefused(self.zip({"i.zip": f.read()}), "program")

    def test_folder_source(self):
        src = af.write_tree(os.path.join(self.tmp, "unpacked"), af.addon_files("FromDir"))
        plan = self.scan(src)
        self.assertEqual(plan.kind, archive.FOLDER)
        self.install(plan)
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "FromDir", "FromDir.toc")))
        with self.assertRaises(ArchiveError) as cm:
            self.scan(self.addons)
        self.assertEqual(cm.exception.kind, "in_game")
        with self.assertRaises(ArchiveError):
            self.scan(self.root)
        with self.assertRaises(ArchiveError):
            self.scan(os.path.join(self.addons, "OtherAddon"))
        self.untouched()

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows only")
    def test_folder_with_junction_refused(self):
        import subprocess
        src = af.write_tree(os.path.join(self.tmp, "j"), af.addon_files("J"))
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(src, "J", "out"), self.wtf],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(os.path.isjunction(os.path.join(src, "J", "out")))
        self.assertRefused(src, "unsafe")


class TarTests(Base):
    def test_real_rar_and_7z(self):
        if not af.system_tar_reads_rar(self.tmp):
            self.skipTest("this system's tar can't read rar")
        entries = {"Rarred/": None, "Rarred/Rarred_Forever.toc": af.toc("Rarred", "3.1"),
                   "Rarred/core.lua": "-- x", "Rarred/\u00e4\u00f6.lua": "-- y",
                   "Rarred/tool.sh": "#!/bin/sh"}
        plan = self.scan(af.write_rar5(os.path.join(self.tmp, "a.rar"), entries))
        self.assertEqual(plan.kind, archive.RAR)
        a = plan.addons[0]
        self.assertEqual((a.folder, a.version, a.left_out_count), ("Rarred", "3.1", 1))
        self.install(plan)
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "Rarred", "\u00e4\u00f6.lua")))
        # 7z made by the same tar
        import subprocess
        src = af.write_tree(os.path.join(self.tmp, "seven"), af.addon_files("Seven"))
        z = os.path.join(self.tmp, "s.7z")
        subprocess.run([archive.tar_path(), "-a", "-cf", z, "-C", src, "Seven"],
                       capture_output=True, creationflags=archive._no_window())
        plan = self.scan(z)
        self.assertEqual((plan.kind, [a.folder for a in plan.addons]), (archive.SEVEN_Z, ["Seven"]))
        self.untouched()

    def test_real_rar_attacks(self):
        if not af.system_tar_reads_rar(self.tmp):
            self.skipTest("this system's tar can't read rar")
        r = lambda e, n="x.rar": af.write_rar5(os.path.join(self.tmp, n), e)  # noqa: E731
        self.assertRefused(r({"A/A.toc": "x", "../evil.lua": "x"}), "unsafe")
        self.assertRefused(r({"A/A.toc": "x", "A/x.exe": "MZ"}), "program")
        self.assertRefused(r({"A/A.toc": "x", "A/x.lua": "MZ\x90"}), "program")
        self.assertRefused(r({"A/A.toc": "x", "a/A.TOC": "y"}), "unsafe")
        with open(os.path.join(self.tmp, "broken.rar"), "wb") as f:
            f.write(b"Rar!\x1a\x07\x01\x00" + bytes(40))
        self.assertRefused(os.path.join(self.tmp, "broken.rar"), "bad_archive")

    def test_cut_rar_refused_at_every_point(self):
        """M2: a .rar cut off anywhere (also exactly between two headers, where libarchive
        reports nothing) is refused as damaged; RAR5 and RAR4."""
        real = af.system_tar_reads_rar(self.tmp)
        for writer, name in ((af.write_rar5, "RarOne"), (af.write_rar4, "RarFour")):
            files = {k: v for k, v in af.addon_files(name).items()}
            full = writer(os.path.join(self.tmp, f"{name}.rar"), files)
            with open(full, "rb") as f:
                data = f.read()
            archive.check_rar_complete(full)   # the whole file is fine
            if real:
                self.assertEqual([a.folder for a in self.scan(full).addons], [name])
            cut = os.path.join(self.tmp, f"{name}-cut.rar")
            fake = af.FakeTar(names=[(k, False) for k in files], files=files)
            for n in range(1, len(data)):
                with open(cut, "wb") as f:
                    f.write(data[:n])
                with self.subTest(writer=name, cut=n):
                    with self.assertRaises(ArchiveError) as cm:
                        self.scan(cut, runner=None if real else fake)
                    self.assertIn(cm.exception.kind, ("bad_archive", "no_tar"))
            # a changed byte in a header (CRC) and a multi-volume part are refused too
            bad = bytearray(data)
            bad[-3] ^= 0xFF
            with open(cut, "wb") as f:
                f.write(bad)
            self.assertRaises(ArchiveError, archive.check_rar_complete, cut)
        self.untouched()

    def test_rar4_real(self):
        if not af.system_tar_reads_rar(self.tmp):
            self.skipTest("this system's tar can't read rar")
        p = af.write_rar4(os.path.join(self.tmp, "four.rar"),
                          {"Four/Four.toc": af.toc("Four", "4.0"), "Four/Four.lua": "-- x"})
        plan = self.scan(p)
        self.assertEqual([(a.folder, a.version) for a in plan.addons], [("Four", "4.0")])

    def rar_file(self):
        return af.write_rar5(os.path.join(self.tmp, "f.rar"), {"A/A.toc": "x"})

    def test_fake_runner(self):
        p = self.rar_file()
        files = af.addon_files("Faked")
        plan = self.scan(p, runner=af.FakeTar(names=[(k, False) for k in files], files=files))
        self.assertEqual([a.folder for a in plan.addons], ["Faked"])
        # tar writes a program where the listing showed a plain file
        bad = dict(files, **{"Faked/Faked.lua": "MZ\x90"})
        self.assertRefused(p, "program", af.FakeTar(names=[(k, False) for k in files], files=bad))
        # more or fewer files on disk than listed: damaged (M2)
        more = dict(files, **{"Faked/extra.lua": "x"})
        self.assertRefused(p, "bad_archive", af.FakeTar(names=[(k, False) for k in files],
                                                        files=more))
        fewer = dict(list(files.items())[:2])
        self.assertRefused(p, "bad_archive", af.FakeTar(names=[(k, False) for k in files],
                                                        files=fewer))
        self.assertRefused(p, "unsafe", af.FakeTar(names=[("../x", False)], files=files))
        self.assertRefused(p, "no_tar", af.FakeTar(error=ArchiveError("no_tar", "rar")))
        self.assertRefused(p, "timeout", af.FakeTar(names=[("A/A.toc", False)],
                                                    unpack_error=ArchiveError("timeout", "")))
        old = archive.MAX_UNPACKED
        archive.MAX_UNPACKED = 100
        self.addCleanup(setattr, archive, "MAX_UNPACKED", old)
        self.assertRefused(p, "too_big", af.FakeTar(names=[("A/A.toc", False)],
                                                    files={"A/A.toc": "x" * 500}))

    def test_tar_errors_and_missing(self):
        self.assertEqual(archive._tar_error("tar.exe: Unrecognized archive format", "rar").kind,
                         "no_tar")
        self.assertEqual(archive._tar_error("Encrypted file is unsupported", "rar").kind,
                         "encrypted")
        self.assertEqual(archive._tar_error("Header CRC error", "rar").kind, "bad_archive")
        runner = archive.TarRunner(exe=os.path.join(self.tmp, "no-such-tar.exe"))
        self.assertRefused(self.rar_file(), "no_tar", runner)

    def test_linux_env_for_frozen(self):
        old_frozen = getattr(sys, "frozen", None)
        sys.frozen = True
        os.environ["LD_LIBRARY_PATH"], os.environ["LD_LIBRARY_PATH_ORIG"] = "/tmp/_MEI", "/usr/lib"
        try:
            env = archive._tar_env()
        finally:
            del os.environ["LD_LIBRARY_PATH"], os.environ["LD_LIBRARY_PATH_ORIG"]
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen
        if os.name != "nt":
            self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/lib")
            self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_windows_tar_full_path(self):
        if os.name == "nt":
            self.assertTrue(archive.tar_path() is None
                            or archive.tar_path().lower().endswith("system32\\tar.exe"))


class ReplaceTests(Base):
    def test_guild_never_replaced(self):
        e = dict(af.addon_files("TaxiTime", "9.9"))
        e.update(af.addon_files("New"))
        plan = self.scan(self.zip(e))
        taxi = plan.addon("TaxiTime")
        self.assertEqual(taxi.state, fileinstall.GUILD)
        self.assertFalse(taxi.tick)
        before = tree_snapshot(os.path.join(self.addons, "TaxiTime"))
        with self.assertRaises(InstallError) as cm:
            self.install(plan, ["TaxiTime", "New"])
        self.assertEqual(cm.exception.kind, "not_allowed")
        self.assertFalse(os.path.exists(os.path.join(self.addons, "New")), "nothing done")
        self.install(plan)
        self.assertEqual(tree_snapshot(os.path.join(self.addons, "TaxiTime")), before)
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "New")))
        # also in another letter case
        plan = self.scan(self.zip(af.addon_files("taxitime"), "lower.zip"))
        self.assertEqual(plan.addons[0].state, fileinstall.GUILD)

    def test_hand_copy_replaced_with_safety_copy(self):
        make_addon_folder(self.addons, "HandMade", "1.0", extra="hand_only")
        plan = self.scan(self.zip(af.addon_files("HandMade", "2.0")))
        a = plan.addons[0]
        self.assertEqual((a.state, a.installed_version), (fileinstall.HAND, "1.0"))
        result = self.install(plan)
        self.assertEqual(result.installed, [("HandMade", "2.0", "HandMade")])
        self.assertTrue(os.path.basename(result.safety).startswith("Before install "))
        self.assertFalse(os.path.exists(os.path.join(self.addons, "HandMade", "hand_only.lua")))
        info = backup.read(result.safety)
        self.assertEqual((info.kind, info.addons), ("safety", ["HandMade"]))
        backup.restore(result.safety, self.addons, os.path.join(self.tmp, "safety2"),
                       settings=False, bindings=False)
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "HandMade", "hand_only.lua")))
        self.untouched()

    def test_tracked_update_and_same(self):
        tracked = fileinstall.track({}, self.install(self.scan(self.zip(af.addon_files("T", "1.0"))
                                                                ), tracked={}), "t.zip")
        self.assertEqual(tracked["T"]["version"], "1.0")
        plan = self.scan(self.zip(af.addon_files("T", "1.2"), "t2.zip"), tracked=tracked)
        self.assertEqual(plan.addons[0].state, fileinstall.UPDATE)
        self.assertIn("Update v1.0 -> v1.2", fileinstall_ui.note_for(plan.addons[0]))
        plan = self.scan(self.zip(af.addon_files("T", "1.0"), "t3.zip"), tracked=tracked)
        self.assertEqual(plan.addons[0].state, fileinstall.SAME)

    def test_existing_spelling_kept(self):
        make_addon_folder(self.addons, "details", "1.0")
        plan = self.scan(self.zip(af.addon_files("Details", "2.0")))
        self.assertEqual(plan.addons[0].installed_folder, "details")
        result = self.install(plan)
        self.assertEqual(result.installed[0][0], "details")
        self.assertEqual([n for n in os.listdir(self.addons) if n.lower() == "details"],
                         ["details"])

    def test_failure_keeps_old_and_stops(self):
        make_addon_folder(self.addons, "B", "1.0", extra="b_old")
        e = {}
        for f in ("A", "B", "C"):
            e.update(af.addon_files(f, "2.0"))
        plan = self.scan(self.zip(e))
        real = installer.swap_in

        def failing(staged, target, backup_path, folder):
            if folder == "B":
                raise InstallError("locked", "B")
            return real(staged, target, backup_path, folder)
        installer.swap_in = failing
        self.addCleanup(setattr, installer, "swap_in", real)
        result = self.install(plan)
        self.assertEqual([f for f, _v, _t in result.installed], ["A"])
        self.assertEqual((result.failed[0], result.failed[1].kind, result.not_done),
                         ("B", "locked", ["C"]))
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "B", "b_old.lua")))
        self.assertFalse(os.path.exists(os.path.join(self.addons, "C")))
        self.assertIn("B wasn't installed", fileinstall_ui.done_message(result, "x.zip"))
        self.assertFalse([n for n in os.listdir(self.addons) if n.startswith(".addonmanager")])
        self.untouched()

    def test_nothing_installed_no_safety_copy(self):
        make_addon_folder(self.addons, "Z", "1.0")
        plan = self.scan(self.zip(af.addon_files("Z", "2.0")))
        real = installer.swap_in
        installer.swap_in = lambda *a: (_ for _ in ()).throw(InstallError("locked", "Z"))
        self.addCleanup(setattr, installer, "swap_in", real)
        result = self.install(plan)
        self.assertIsNone(result.safety)
        self.assertFalse(os.listdir(self.safety))

    def test_safety_folder_inside_game_refused(self):
        make_addon_folder(self.addons, "Z", "1.0")
        plan = self.scan(self.zip(af.addon_files("Z", "2.0")))
        with self.assertRaises(InstallError) as cm:
            fileinstall.install(plan, ["Z"], self.addons, GUILD, {}, os.path.join(self.client, "x"))
        self.assertEqual(cm.exception.kind, "safety")
        self.assertEqual(toc.describe(os.path.join(self.addons, "Z"), "Z")[2], "1.0")

    def test_crash_mid_swap_recovered(self):
        make_addon_folder(self.addons, "Crash", "1.0", extra="crash_old")
        os.replace(os.path.join(self.addons, "Crash"),
                   os.path.join(self.addons, installer.BACKUP_PREFIX + "Crash"))
        installer.recover(self.addons)
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "Crash", "crash_old.lua")))


class TrackingTests(Base):
    def test_rows_remove_and_needed_by(self):
        e = dict(af.addon_files("!Lib Core", "1.1"))
        e.update(af.addon_files("UsesLib", "1.0"))
        e["UsesLib/UsesLib.toc"] = "## Interface: 16001\n## Version: 1.0\n## Dependencies: !Lib Core\n"
        plan = self.scan(self.zip(e))
        tracked = fileinstall.track({"bad/name": {}, "Gone": {"version": "1"}},
                                    self.install(plan), "pack.zip", today="2026-11-10")
        rows, kept = fileinstall.other_rows(self.addons, tracked)
        self.assertEqual([(r.folder, r.version) for r in rows], [("!Lib Core", "1.1"),
                                                                 ("UsesLib", "1.0")])
        self.assertNotIn("Gone", kept)
        self.assertEqual(kept["UsesLib"], {"name": "UsesLib", "version": "1.0",
                                           "file": "pack.zip", "date": "2026-11-10"})
        self.assertEqual(installer.needed_by(self.addons, "!Lib Core", list(kept)), ["UsesLib"])
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        send = lambda p: shutil.move(p, os.path.join(bin_dir, os.path.basename(p)))  # noqa
        installer.remove("!Lib Core", self.addons, GUILD + list(kept), send=send)
        self.assertTrue(os.path.isdir(os.path.join(bin_dir, "!Lib Core")))
        with self.assertRaises(InstallError):   # never an addon that isn't tracked
            installer.remove("OtherAddon", self.addons, GUILD + list(kept), send=send)
        self.assertEqual(list(fileinstall.untrack(kept, "!lib core")), ["UsesLib"])
        self.untouched()

    def test_clean_tracked(self):
        self.assertEqual(fileinstall.clean_tracked([1]), {})
        self.assertEqual(fileinstall.clean_tracked({"..": {}, "Blizzard_X": {}, "A": "x",
                                                    "B": {"version": 3, "file": "f"}}),
                         {"B": {"file": "f"}})

    def test_addon_name_ok(self):
        for ok in ("!BugGrabber", "My Addon", "Details", "a.b"):
            self.assertTrue(installer.addon_name_ok(ok), ok)
        for bad in ("", "..", ".addonmanager-x", "Blizzard_UI", "blizzard_x", "a/b", "CON",
                    "x.", "x" * 101):
            self.assertFalse(installer.addon_name_ok(bad), bad)


class DialogTests(Base):
    def setUp(self):
        super().setUp()
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.addCleanup(self.tk.destroy)
        theme.apply(self.tk)

    def test_dialog_rows_and_install(self):
        make_addon_folder(self.addons, "Hand", "1.0")
        e = {}
        for f in ("Hand", "TaxiTime", "Fresh"):
            e.update(af.addon_files(f, "2.0"))
        e.update(af.addon_files("Old", toc_name="Old_Classic.toc"))
        e.update(af.addon_files("Blizzard_X"))
        plan = fileinstall.scan(self.zip(e), self.addons, GUILD, {}, tmp_root=self.work)
        calls, closed = [], []
        d = fileinstall_ui.FileInstallDialog(self.tk, plan, lambda dlg, f: calls.append(f),
                                             on_close=closed.append, show=False)
        self.assertEqual(sorted(d.chosen()), ["Fresh", "Hand"])
        self.assertTrue(d.checks["TaxiTime"].instate(["disabled"]))
        self.assertTrue(d.safety_label.winfo_manager())
        d.vars["Hand"].set(False)
        d._update_safety()
        self.assertFalse(d.safety_label.winfo_manager())
        d.vars["Fresh"].set(False)
        self.assertFalse(d.start())
        self.assertEqual(d.status_var.get(), fileinstall_ui.NOTHING_CHOSEN)
        d.vars["Fresh"].set(True)
        self.assertTrue(d.start())
        self.assertEqual(calls, [["Fresh"]])
        d.close()
        self.assertFalse(d.closed, "can't close while installing")
        result = fileinstall.install(plan, calls[0], self.addons, GUILD, {}, self.safety)
        d.finished_ok(result)
        self.assertIn("Installed 1 addon from pick.zip", d.status_var.get())
        d.close()
        self.assertEqual(closed, [d])
        self.assertIsNone(plan.work, "temp removed on close")
        self.tk.update_idletasks()
        self.untouched()

    def test_many_rows_scroll_and_width(self):
        e = {}
        for i in range(12):
            e.update(af.addon_files(f"Addon{i:02d} with a rather long name"))
        plan = fileinstall.scan(self.zip(e), self.addons, GUILD, {}, tmp_root=self.work)
        d = fileinstall_ui.FileInstallDialog(self.tk, plan, lambda *a: None, show=False)
        self.assertTrue(hasattr(d, "canvas"))
        self.tk.update_idletasks()
        self.assertLessEqual(d.win.winfo_reqwidth(), 420)
        d.close()

    def test_messages(self):
        for kind in ("not_found", "too_big", "bad_archive", "encrypted", "unsafe", "program",
                     "no_tar", "timeout", "empty", "ambiguous", "in_game", "not_allowed",
                     "safety", "locked", "permission", "disk_full", "link", "whatever"):
            self.assertTrue(fileinstall_ui.message(ArchiveError(kind, "x.exe")))
        self.assertIn("x.exe", fileinstall_ui.message(ArchiveError("program", "x.exe")))
        self.assertEqual(fileinstall_ui.paths_from_drop(self.tk, "{C:/a b.zip} C:/c.rar"),
                         ["C:/a b.zip", "C:/c.rar"])


class CleanTempTests(unittest.TestCase):
    def test_only_old_own_folders_removed(self):
        """L3: old AddonManager_file_* folders go at start; fresh ones and others stay."""
        import time as _time
        tmp = temp_dir("fileinst_clean_")
        self.addCleanup(shutil.rmtree, tmp, True)
        old = os.path.join(tmp, fileinstall.TEMP_PREFIX + "old")
        fresh = os.path.join(tmp, fileinstall.TEMP_PREFIX + "fresh")
        other = os.path.join(tmp, "AddonManager_build_old")
        target = os.path.join(tmp, "keep_me")
        for p in (old, fresh, other, target):
            os.makedirs(os.path.join(p, "sub"))
            with open(os.path.join(p, "sub", "f.lua"), "w") as f:
                f.write("x")
        with open(os.path.join(tmp, fileinstall.TEMP_PREFIX + "file.txt"), "w") as f:
            f.write("not a folder")
        link = os.path.join(tmp, fileinstall.TEMP_PREFIX + "link")
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(target, link)
        else:
            os.symlink(target, link)
        then = _time.time() - 3 * 60 * 60
        for p in (old, other):
            os.utime(p, (then, then))
        removed = fileinstall.clean_temp(tmp)
        self.assertEqual(removed, [os.path.basename(old)])
        for p in (fresh, other, target):
            self.assertTrue(os.path.isfile(os.path.join(p, "sub", "f.lua")), p)
        self.assertFalse(os.path.exists(old))


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("fileinst_home_")
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_lock_and_requests(self):
        a, b = instance.Lock(self.home), instance.Lock(self.home)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertTrue(instance.send_request(self.home, os.path.join(self.home, "x.zip")))
        self.assertTrue(instance.send_request(self.home, os.path.join(self.home, "y.rar")))
        self.assertEqual([os.path.basename(p) for p in instance.take_requests(self.home)],
                         ["x.zip", "y.rar"])
        self.assertEqual(instance.take_requests(self.home), [])
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_same_instant_requests_keep_order(self):
        """Requests sent in the same clock tick still come out in the order sent."""
        from unittest import mock
        import time as _time
        frozen = _time.time_ns()
        names = [f"r{i}.zip" for i in range(8)]
        with mock.patch.object(instance.time, "time_ns", return_value=frozen):
            for n in names:
                self.assertTrue(instance.send_request(self.home, os.path.join(self.home, n)))
        self.assertEqual([os.path.basename(p) for p in instance.take_requests(self.home)], names)

    def test_old_style_names_sorted_by_time(self):
        """Old "<seconds.mmm>-<hex>" names sort by their time among new ones."""
        import time as _time
        folder = os.path.join(self.home, instance.REQUEST_DIR)
        os.makedirs(folder)
        now = _time.time()
        old_name = f"{now - 5:.3f}-ffff.txt"
        with open(os.path.join(folder, old_name), "wb") as f:
            f.write(os.path.join(self.home, "first.zip").encode("utf-8"))
        self.assertTrue(instance.send_request(self.home, os.path.join(self.home, "second.zip")))
        for bad in ("nan-a.txt", "inf-b.txt", "zz-c.txt"):
            with open(os.path.join(folder, bad), "wb") as f:
                f.write(b"relative.zip")
        self.assertEqual([os.path.basename(p) for p in instance.take_requests(self.home)],
                         ["first.zip", "second.zip"])
        self.assertEqual(os.listdir(folder), [])

    def test_old_and_bad_requests_dropped(self):
        instance.send_request(self.home, os.path.join(self.home, "old.zip"))
        folder = os.path.join(self.home, instance.REQUEST_DIR)
        for name, data in (("1-a.txt", b"relative.zip"), ("2-b.txt", b"\xff\xfe"),
                           ("3-c.txt", b"x" * 5000)):
            with open(os.path.join(folder, name), "wb") as f:
                f.write(data)
        import time as _time
        self.assertEqual(instance.take_requests(self.home, now=_time.time() + 600), [])
        self.assertEqual(os.listdir(folder), [])


class WindowFileTests(unittest.TestCase):
    """The hidden window: Add from file, the Other addons rows, Uninstall, hand-over."""

    @classmethod
    def setUpClass(cls):
        from tests import fixtures
        cls.fixtures = fixtures
        cls.base = fixtures.temp_dir("fileinst_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def setUp(self):
        from am import config
        from tests.test_remove import FakeTrash
        self.work = temp_dir("fileinst_ui_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root_dir, self.addons = make_fake_wow(self.work)
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.wtf = tree_snapshot(os.path.join(self.client, "WTF"))
        self.trash = FakeTrash(os.path.join(self.work, "bin"))
        self.asked = []
        config.save_settings({"backup_folder": os.path.join(self.work, "backups")})
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.assertEqual(tree_snapshot(os.path.join(self.client, "WTF")), self.wtf)
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        import gc
        self.root = None
        gc.collect()

    def confirm(self, *args):
        self.asked.append(args)
        return True

    def pump(self, app, until=None, timeout=15):
        import time
        end = time.time() + timeout
        self.root.update()
        while time.time() < end and (app.busy or (until is not None and not until())):
            self.root.update()
            time.sleep(0.02)
        for _ in range(3):
            self.root.update()

    def make_app(self, **kw):
        from am import ui
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     trash_api=self.trash, **kw)
        app.files.show = False
        self.pump(app)
        return app

    def pack(self, name="pack.zip"):
        e = dict(af.addon_files("!Lib Core", "1.1"))
        e.update(af.addon_files("Details", "2.0"))
        e.update(af.addon_files("TaxiTime", "9.0"))
        return af.write_zip(os.path.join(self.work, name), e)

    def install_pack(self, app):
        self.assertTrue(app.files.add_from_path(self.pack()))
        self.pump(app, until=lambda: app.files.dialog is not None)
        d = app.files.dialog
        self.assertEqual(sorted(d.chosen()), ["!Lib Core", "Details"])
        self.assertTrue(d.start())
        self.pump(app, until=lambda: d.finished)
        return d

    def test_add_from_file_and_other_rows(self):
        from am import config
        app = self.make_app()
        self.assertTrue(app.files.button.instate(["!disabled"]))
        d = self.install_pack(app)
        self.assertIn("Installed 2 addons from pack.zip", app.message_var.get())
        tracked = config.load_settings()[fileinstall.SETTING]
        self.assertEqual(sorted(tracked), ["!Lib Core", "Details"])
        self.assertEqual([(r.folder, r.version) for r in app.files.rows],
                         [("!Lib Core", "1.1"), ("Details", "2.0")])
        name, inst, latest, status = app.files.cells[1]["labels"]
        self.assertEqual((name.cget("text"), inst.cget("text"), latest.cget("text"),
                          status.cget("text")), ("Details", "v2.0", "-", fileinstall_ui.FROM_FILE))
        self.assertEqual(app.files.cells[1]["button"].cget("text"), "Uninstall")
        # never in the guild rows: Install/update all and the usage ping use those only
        self.assertNotIn("Details", [r.folder for r in app.rows])
        d.close()
        self.assertIsNone(app.files.dialog)
        # TaxiTime (guild) untouched
        self.assertEqual(toc.installed(self.addons, "TaxiTime")[1], "1.2.0")
        # the window stays narrow enough
        self.root.update_idletasks()
        self.assertLessEqual(self.root.minsize()[0], 680)

    def test_busy_greys_button(self):
        app = self.make_app()
        app.busy = True
        app.render()
        self.assertTrue(app.files.button.instate(["disabled"]))
        self.assertFalse(app.files.add_from_file())
        app.busy = False
        app.render()

    def test_uninstall_other_addon(self):
        from am import config
        app = self.make_app()
        self.install_pack(app).close()
        row = next(r for r in app.files.rows if r.folder == "!Lib Core")
        self.assertTrue(app.files.remove(row))
        self.pump(app)
        self.assertEqual(self.trash.sent, ["!Lib Core"])
        self.assertEqual(list(config.load_settings()[fileinstall.SETTING]), ["Details"])
        self.assertNotIn("removed_addons", config.load_settings())
        self.assertEqual([r.folder for r in app.files.rows], ["Details"])
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "OtherAddon")))

    def test_gone_folder_dropped_and_scan_error(self):
        from am import config
        app = self.make_app()
        self.install_pack(app).close()
        shutil.rmtree(os.path.join(self.addons, "Details"))
        app.check_again()
        self.pump(app)
        self.assertEqual([r.folder for r in app.files.rows], ["!Lib Core"])
        self.assertEqual(list(config.load_settings()[fileinstall.SETTING]), ["!Lib Core"])
        bad = af.write_zip(os.path.join(self.work, "bad.zip"),
                           dict(af.addon_files("X"), **{"X/x.exe": "MZ"}))
        app.files.add_from_path(bad)
        self.pump(app)
        self.assertIn("program", app.message_var.get())
        self.assertIsNone(app.files.dialog)

    def test_open_path_at_start_and_two_dropped(self):
        app = self.make_app(open_path=self.pack())
        self.pump(app, until=lambda: app.files.dialog is not None)
        self.assertIsNotNone(app.files.dialog)
        app.files.dialog.close()
        self.assertFalse(app.files.dropped(["a.zip", "b.zip"]))
        self.assertEqual(app.message_var.get(), fileinstall_ui.ONE_FILE)

    def test_handed_over_from_a_second_start(self):
        from am import config
        lock = instance.Lock(config.home_dir())
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        app = self.make_app(instance_lock=lock)
        self.assertFalse(instance.Lock(config.home_dir()).acquire(), "second start can't lock")
        self.assertTrue(instance.send_request(config.home_dir(), self.pack()))
        self.pump(app, until=lambda: app.files.dialog is not None)
        self.assertIsNotNone(app.files.dialog, "the open window got the file")
        self.assertEqual(app.files.dialog.plan.name, "pack.zip")
        app.files.dialog.close()

    def test_before_install_listed_as_safety_copy(self):
        make_addon_folder(self.addons, "Hand", "1.0")
        plan = fileinstall.scan(af.write_zip(os.path.join(self.work, "h.zip"),
                                             af.addon_files("Hand", "2.0")),
                                self.addons, GUILD, {}, tmp_root=self.work)
        result = fileinstall.install(plan, ["Hand"], self.addons, GUILD, {},
                                     os.path.join(self.work, "backups"))
        plan.close()
        found = backup.find_backups([("Documents", os.path.join(self.work, "backups"))])
        self.assertEqual([(os.path.basename(f.path), f.safety) for f in found],
                         [(os.path.basename(result.safety), True)])

    def test_bottom_row_fits_at_150_and_175_percent(self):
        """M1: at 150 % / 175 % display scaling the window can't get narrower than the
        bottom row (Check again ... Backup, Settings), so no button is cut off. 100 % unchanged."""
        from am import ui
        base = float(self.root.tk.call("tk", "scaling"))
        for factor in (1.0, 1.5, 1.75):
            with self.subTest(factor=factor):
                self.root.tk.call("tk", "scaling", base * factor)
                for child in self.root.winfo_children():
                    child.destroy()
                app = self.make_app()
                self.root.update_idletasks()
                row = app.settings_button.master
                need = sum(w.winfo_reqwidth() for w in row.winfo_children()) + 20
                self.assertGreaterEqual(self.root.minsize()[0], row.winfo_reqwidth() + 20)
                self.assertGreaterEqual(self.root.minsize()[0], need - 40)
                self.assertIn(app.backup_main_button, row.winfo_children())
                # Add file... is as big as the list's Install/Update/Uninstall button, and
                # its text isn't cut (its own text needs no more than that width)
                add = app.files.button
                row_buttons = [w[1] for w in app.row_widgets.values() if w[1] is not None]
                self.assertTrue(row_buttons)
                for b in row_buttons:
                    self.assertEqual((add.winfo_reqwidth(), add.winfo_reqheight()),
                                     (b.winfo_reqwidth(), b.winfo_reqheight()))
                self.assertIn(str(add.cget("style")), ("", "TButton"))   # same look as Install
                from tkinter import ttk
                natural = ttk.Button(add.master, text=add.cget("text"), width=0)
                self.assertLessEqual(natural.winfo_reqwidth(), add.winfo_reqwidth())
                natural.destroy()
                # the Add file... line above the list fits too
                self.assertGreaterEqual(self.root.minsize()[0],
                                        app.files.button.master.winfo_reqwidth() + 20)
                if factor == 1.0:
                    self.assertEqual(self.root.minsize()[0],
                                     max(ui.MIN_WIDTH, app._min_width))
                    self.assertLessEqual(row.winfo_reqwidth(), 560 - 20)
                app.closed = True
        self.root.tk.call("tk", "scaling", base)

    LONG_TITLE = "Very Long Addon Name Number 0 With Extra Words And More"

    def install_long_title(self, app):
        e = af.addon_files("LongOne")
        e["LongOne/LongOne.toc"] = (f"## Interface: 16001\n## Title: {self.LONG_TITLE}\n"
                                    "## Version: 1\n")
        app.files.add_from_path(af.write_zip(os.path.join(self.work, "long.zip"), e))
        self.pump(app, until=lambda: app.files.dialog is not None)
        d = app.files.dialog
        d.start()
        self.pump(app, until=lambda: d.finished)
        d.close()
        self.root.update_idletasks()

    def test_long_title_cut_with_tooltip(self):
        """L1: a long ## Title never sets the window's width (it is cut to the width the
        guild names already need); the full title is in the tooltip."""
        app = self.make_app()
        self.root.update_idletasks()
        before = self.root.minsize()[0]
        self.install_long_title(app)
        cells = app.files.cells[0]
        shown = cells["labels"][0].cget("text")
        self.assertTrue(shown.endswith("...") and len(shown) <= fileinstall_ui.MAX_TITLE)
        self.assertLessEqual(app._text_width(shown), app.files._title_px())
        self.assertEqual(cells["name_tip"].text, self.LONG_TITLE)
        widest = max(app._text_width(r.name) for r in app.rows)
        self.assertLessEqual(self.root.minsize()[0],
                             before + max(0, fileinstall_ui.MIN_TITLE_PX - widest))
        self.assertLessEqual(self.root.minsize()[0], 680)

    def test_linux_layout(self):
        """The Linux layout (on Linux for real; on Windows simulated: Linux fonts are about
        1.2x wider): Add file... on its own line above the list, slim bottom-row buttons
        (Backup and Settings as narrow as their text) with real room inside 540 px, and a
        long Other-addon title keeps the window well under 680 px. The Windows layout is
        checked by test_stats."""
        from unittest import mock
        from am import ui
        simulate = os.name == "nt"
        base = float(self.root.tk.call("tk", "scaling"))
        patches = [mock.patch.object(ui, "LINUX_SLIM_BUTTONS", True),
                   mock.patch.object(ui, "NARROW_WIDTH", 0),
                   mock.patch.object(ui, "CHECK_WIDTH", 0),
                   mock.patch.object(ui, "SETTINGS_BUTTON", "Settings"),
                   mock.patch.object(ui, "BETA_MAIN_TEXT", "Betas")] if simulate else []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if simulate:
            # Tk's scaling outlives this window on Windows: always put it back here, before
            # tearDown destroys the window (a cleanup would run too late).
            self.root.tk.call("tk", "scaling", base * 1.2)
        try:
            self._linux_layout_checks(base)
        finally:
            self.root.tk.call("tk", "scaling", base)

    def _linux_layout_checks(self, base):
        app = self.make_app()
        self.root.update_idletasks()
        bottom = app.settings_button.master
        self.assertIsNot(app.files.button.master, bottom)
        self.assertNotIn(app.files.button, bottom.winfo_children())
        self.assertEqual(app.check_button.cget("style"), "Slim.TButton")
        self.assertIs(app.backup_main_button.master, bottom)   # [Betas] [Backup] [Settings]
        self.assertEqual(app.backup_main_button.cget("style"), "Slim.TButton")
        self.assertEqual(str(app.backup_main_button.cget("width")), "0")   # as narrow as its text
        self.assertLessEqual(bottom.winfo_reqwidth(), 540 - 30)
        # Install all and Update all: the same width, so the row fits with either text
        from am import errors
        sizes = []
        for text in (errors.ALL_INSTALL, errors.ALL_UPDATE):
            app.all_button.configure(text=text)
            self.root.update_idletasks()
            sizes.append((app.all_button.winfo_reqwidth(), bottom.winfo_reqwidth()))
            self.assertLessEqual(bottom.winfo_reqwidth(), 540 - 30)
        self.assertEqual(sizes[0], sizes[1])
        self.assertLessEqual(app.files.button.master.winfo_reqwidth(), 540 - 30)
        for b in [w[1] for w in app.row_widgets.values() if w[1] is not None]:
            self.assertEqual((app.files.button.winfo_reqwidth(), app.files.button.winfo_reqheight()),
                             (b.winfo_reqwidth(), b.winfo_reqheight()))
        # the button above the list, below the folder line
        children = app.list_frame.master.pack_slaves()
        self.assertLess(children.index(app.files.button.master), children.index(app.list_frame))
        before = self.root.minsize()[0]
        self.install_long_title(app)
        # the title adds nothing to what the guild rows already need (and stays in 680 px)
        widest = max(app._text_width(r.name) for r in app.rows)
        self.assertLessEqual(self.root.minsize()[0],
                             before + max(0, fileinstall_ui.MIN_TITLE_PX - widest))
        self.assertLessEqual(self.root.minsize()[0], 680)
        # right-click menu still there
        self.assertEqual(app.files.menu.entrycget(1, "label"), fileinstall_ui.FOLDER_MENU)

    def test_fit_title(self):
        measure = lambda s: 7 * len(s)  # noqa: E731
        self.assertEqual(fileinstall_ui.fit_title("Short", 200, measure), "Short")
        cut = fileinstall_ui.fit_title(self.LONG_TITLE, 100, measure)
        self.assertTrue(cut.endswith("...") and measure(cut) <= 100, cut)

    def test_tracked_guild_addon_shows_once(self):
        """L2: an addon from a file that is on the guild list now: only the guild row."""
        from am import config
        settings = config.load_settings()
        settings[fileinstall.SETTING] = {"TaxiTime": {"version": "1.2.0"},
                                         "OtherAddon": {"version": "9.9"}}
        config.save_settings(settings)
        app = self.make_app()
        self.assertEqual([r.folder for r in app.files.rows], ["OtherAddon"])
        self.assertIn("TaxiTime", [r.folder for r in app.rows])
        self.assertEqual(list(config.load_settings()[fileinstall.SETTING]), ["OtherAddon"])

    def test_game_running_warning_in_confirm_window(self):
        """I3: WoW open = install still allowed, and the confirm window says so."""
        app = self.make_app()
        app.check_game = lambda: True
        app.files.add_from_path(self.pack())
        self.pump(app, until=lambda: app.files.dialog is not None)
        d = app.files.dialog
        from am import errors
        self.assertIsNotNone(d.game_label)
        self.assertEqual(d.game_label.cget("text"), errors.GAME_RUNNING)
        self.assertTrue(d.game_label.winfo_manager())
        d.close()
        app.check_game = lambda: False
        app.files.add_from_path(self.pack("p2.zip"))
        self.pump(app, until=lambda: app.files.dialog is not None)
        self.assertIsNone(app.files.dialog.game_label)
        app.files.dialog.close()

    @unittest.skipUnless(fileinstall_ui.drop_supported(),
                         "tkinterdnd2 isn't installed in this Python (pip install --require-hashes "
                         "-r requirements-app.txt); build.ps1 and CI install it and test this")
    def test_drop_loads_on_windows(self):
        app = self.make_app()
        if os.name == "nt":
            self.assertTrue(app.files.drop_ok)


if __name__ == "__main__":
    unittest.main()
