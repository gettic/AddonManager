"""Removing a guild addon (the row's Uninstall button / right-click Remove): installer.remove, the trash
helpers and the window. Fake WoW folder and a fake trash only: the real Recycle Bin,
Trash and WoW folder are never used."""

import contextlib
import datetime
import gc
import os
import shutil
import time
import tkinter as tk
import unittest
import urllib.parse
from unittest import mock

from am import config, errors, installer, toc, trash, ui
from am.installer import InstallError
from tests import fixtures

GUILD = ("TaxiTime", "GuildHub")


class FakeTrash:
    """Stands in for am.trash: moves folders into bin_dir; where=None = no bin on the drive."""

    def __init__(self, bin_dir, where="bin", fail=None):
        self.bin_dir, self.where, self.fail = bin_dir, where, fail
        self.sent = []
        os.makedirs(bin_dir, exist_ok=True)

    def available(self, _path):
        return self.where

    def send(self, path):
        if self.fail:
            raise self.fail
        self.sent.append(os.path.basename(path))
        shutil.move(path, os.path.join(self.bin_dir, os.path.basename(path)))


def make_link(src, dst):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(src, dst)
    else:
        os.symlink(src, dst)


@contextlib.contextmanager
def in_use(folder):
    """A file inside folder is open. Windows: a real handle (a folder rename then fails).
    Linux allows that, so the refusal is simulated for exactly that folder."""
    if os.name == "nt":
        with open(os.path.join(folder, os.listdir(folder)[0]), "rb"):
            yield
        return
    real = os.replace
    target = os.path.abspath(folder)

    def replace(src, dst, *a, **k):
        if os.path.abspath(os.fspath(src)) == target:
            raise PermissionError(13, "Permission denied (test lock)", src)
        return real(src, dst, *a, **k)
    with mock.patch.object(installer.os, "replace", replace):
        yield


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.temp_dir("remove_")
        self.root, self.addons = fixtures.make_fake_wow(self.base)
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.bin = FakeTrash(os.path.join(self.base, "bin"))
        self.wtf = fixtures.tree_snapshot(os.path.join(self.client, "WTF"))
        self.other = fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon"))

    def tearDown(self):
        # Never touched, whatever happened: WTF (SavedVariables) and other addons.
        self.assertEqual(fixtures.tree_snapshot(os.path.join(self.client, "WTF")), self.wtf)
        self.assertEqual(fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon")),
                         self.other)
        fixtures.remove(self.base)

    def taxi(self):
        return os.path.join(self.addons, "TaxiTime")

    def test_to_trash(self):
        installer.remove("TaxiTime", self.addons, GUILD, send=self.bin.send)
        self.assertFalse(os.path.exists(self.taxi()))
        self.assertEqual(self.bin.sent, ["TaxiTime"])   # real name, so Restore puts it back
        self.assertTrue(os.path.isfile(os.path.join(self.bin.bin_dir, "TaxiTime",
                                                    "TaxiTime_Forever.toc")))
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_delete_for_good(self):
        installer.remove("TaxiTime", self.addons, GUILD, send=None)
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])
        self.assertEqual(os.listdir(self.bin.bin_dir), [])

    def test_refusals(self):
        blizz = os.path.join(self.addons, "Blizzard_Test")
        os.makedirs(blizz)
        cases = (("OtherAddon", GUILD, "not_guild"),           # not on the guild list
                 ("Blizzard_Test", GUILD + ("Blizzard_Test",), "not_guild"),
                 ("..", GUILD + ("..",), "not_guild"),
                 ("", GUILD + ("",), "not_guild"),
                 ("GuildHub", GUILD, "missing"))
        for folder, guild, kind in cases:
            with self.assertRaises(InstallError, msg=folder) as cm:
                installer.remove(folder, self.addons, guild, send=self.bin.send)
            self.assertEqual(cm.exception.kind, kind, folder)
        self.assertTrue(os.path.isdir(blizz))
        self.assertTrue(os.path.isdir(self.taxi()))
        self.assertEqual(self.bin.sent, [])

    def test_not_an_addons_folder(self):
        with self.assertRaises(InstallError) as cm:
            installer.remove("TaxiTime", self.client, GUILD + ("Interface",), send=self.bin.send)
        self.assertEqual(cm.exception.kind, "bad_folder")

    def test_link_left_alone(self):
        real = os.path.join(self.base, "dev", "GuildHub")
        fixtures.make_addon_folder(os.path.dirname(real), "GuildHub", "0.1.0")
        link = os.path.join(self.addons, "GuildHub")
        make_link(real, link)
        try:
            with self.assertRaises(InstallError) as cm:
                installer.remove("GuildHub", self.addons, GUILD, send=self.bin.send)
            self.assertEqual(cm.exception.kind, "link")
            self.assertTrue(os.path.isfile(os.path.join(real, "GuildHub_Forever.toc")))
        finally:
            os.rmdir(link) if os.name == "nt" else os.remove(link)

    def test_in_use_changes_nothing(self):
        before = fixtures.tree_snapshot(self.taxi())
        for send in (self.bin.send, None):
            with in_use(self.taxi()):
                with self.assertRaises(InstallError) as cm:
                    installer.remove("TaxiTime", self.addons, GUILD, send=send)
            self.assertIn(cm.exception.kind, ("locked", "permission"))
            if os.name == "nt":
                self.assertEqual(cm.exception.kind, "locked")
            self.assertEqual(fixtures.tree_snapshot(self.taxi()), before)
            self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon", "TaxiTime"])
        self.assertIn("in use", errors.remove_message(InstallError("locked"), "Taxi Time"))

    def test_trash_fails_changes_nothing(self):
        before = fixtures.tree_snapshot(self.taxi())
        for exc, kind in ((trash.TrashError("other", "x"), "trash"),
                          (trash.TrashError("locked", "x"), "locked"),
                          (trash.TrashError("no_trash", "x"), "no_trash")):
            bad = FakeTrash(self.bin.bin_dir, fail=exc)
            with self.assertRaises(InstallError) as cm:
                installer.remove("TaxiTime", self.addons, GUILD, send=bad.send)
            self.assertEqual(cm.exception.kind, kind)
            self.assertEqual(fixtures.tree_snapshot(self.taxi()), before)
            self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon", "TaxiTime"])

    def test_leftover_backup_does_not_bring_it_back(self):
        backup = os.path.join(self.addons, installer.BACKUP_PREFIX + "TaxiTime")
        fixtures.make_addon_folder(os.path.dirname(backup), os.path.basename(backup), "1.0.0")
        installer.remove("TaxiTime", self.addons, GUILD, send=self.bin.send)
        self.assertEqual(installer.recover(self.addons), [])
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_recover_puts_back_half_removed(self):
        os.replace(self.taxi(), os.path.join(self.addons, installer.REMOVING_PREFIX + "TaxiTime"))
        self.assertEqual(installer.recover(self.addons), [("restored", "TaxiTime")])
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))

    # ---- tester fixes (plans/delete-button/test-report.md) ----
    def test_delete_for_good_read_only_files(self):
        """M1: read-only files (and a read-only folder) are deleted too, nothing left."""
        import stat
        ro = os.path.join(self.taxi(), "zz_ro.lua")
        with open(ro, "w") as f:
            f.write("x")
        sub = os.path.join(self.taxi(), "zz_sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "inner.lua"), "w") as f:
            f.write("x")
        os.chmod(os.path.join(sub, "inner.lua"), stat.S_IREAD)
        os.chmod(ro, stat.S_IREAD)
        os.chmod(sub, stat.S_IREAD | stat.S_IEXEC)
        installer.remove("TaxiTime", self.addons, GUILD, send=None)
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_delete_for_good_partly_says_so(self):
        """M1: if some files still can't go, say "partly", hide the rest, finish next start."""
        def half(path, *a, **k):
            os.remove(os.path.join(path, "TaxiTime_Forever.toc"))
            raise PermissionError(13, "test: can't delete", os.path.join(path, "x.lua"))
        with mock.patch.object(installer.shutil, "rmtree", half):
            with self.assertRaises(InstallError) as cm:
                installer.remove("TaxiTime", self.addons, GUILD, send=None)
        self.assertEqual(cm.exception.kind, "partly")
        text = errors.remove_message(cm.exception, "Taxi Time")
        self.assertIn("only partly deleted", text)
        self.assertNotIn("nothing was removed", text)
        self.assertFalse(os.path.exists(self.taxi()))   # not put back half-deleted
        self.assertEqual(os.listdir(self.addons).count(installer.DELETING_PREFIX + "TaxiTime"), 1)
        self.assertEqual(installer.recover(self.addons),
                         [("deleted", installer.DELETING_PREFIX + "TaxiTime")])
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_access_denied_is_not_always_in_use(self):
        """L1: "access denied" with no open file = a permission problem, not "in use"."""
        e = OSError(13, "Access is denied", None, 5) if os.name == "nt" \
            else PermissionError(13, "Permission denied")
        with mock.patch.object(installer, "_windows_in_use", return_value=False):
            self.assertEqual(installer._remove_error(e, "TaxiTime", self.taxi()).kind,
                             "permission")
        with mock.patch.object(installer, "_windows_in_use", return_value=True):
            self.assertEqual(installer._remove_error(e, "TaxiTime", self.taxi()).kind, "locked")
        self.assertFalse(installer._windows_in_use(self.taxi()))   # nothing open here
        text = errors.remove_message(InstallError("permission"), "Taxi Time")
        self.assertNotIn("in use", text)
        self.assertIn("permission", text)

    def test_stale_leftover_does_not_block_remove(self):
        """L2: an old .addonmanager-removing-X next to X (put back by hand) is cleared."""
        for send in (self.bin.send, None):
            fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")
            stale = os.path.join(self.addons, installer.REMOVING_PREFIX + "TaxiTime")
            shutil.copytree(self.taxi(), stale)
            installer.remove("TaxiTime", self.addons, GUILD, send=send)
            self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_recover_finishes_interrupted_delete(self):
        """L2: app closed during a delete for good: the next start finishes the delete
        instead of putting a half-deleted addon back."""
        doomed = os.path.join(self.addons, installer.DELETING_PREFIX + "TaxiTime")
        os.replace(self.taxi(), doomed)
        os.remove(os.path.join(doomed, "TaxiTime_Forever.toc"))
        self.assertEqual(installer.recover(self.addons),
                         [("deleted", installer.DELETING_PREFIX + "TaxiTime")])
        self.assertEqual(sorted(os.listdir(self.addons)), ["OtherAddon"])

    def test_needed_by(self):
        fixtures.make_addon_folder(self.addons, "GuildHub", "0.4.1")
        with open(toc.toc_path(self.addons, "GuildHub"), "a", encoding="utf-8") as f:
            f.write("## Dependencies: Ace3, taxitime\n")
        self.assertEqual(installer.needed_by(self.addons, "TaxiTime",
                                             ["GuildHub", "Missing", ".."]), ["GuildHub"])
        self.assertEqual(installer.needed_by(self.addons, "GuildHub", ["TaxiTime"]), [])
        os.remove(toc.toc_path(self.addons, "GuildHub"))


class LinuxTrashTests(unittest.TestCase):
    """The freedesktop.org Trash (runs on any OS with a fake data folder)."""

    def setUp(self):
        self.base = fixtures.temp_dir("remove_trash_")
        self.data = os.path.join(self.base, "share")
        self.addons = os.path.join(self.base, "Interface", "AddOns")
        os.makedirs(self.addons)

    def tearDown(self):
        fixtures.remove(self.base)

    def test_send_writes_info_and_moves(self):
        when = datetime.datetime(2026, 11, 10, 12, 30, 5)
        for n in (1, 2):
            folder = fixtures.make_addon_folder(self.addons, "Taxi Time", "1.0.0")
            where = trash.linux_trash_dir(folder, data_home=self.data, uid=1000)
            self.assertEqual(where, (os.path.join(self.data, "Trash"), None))
            moved = trash.linux_send(folder, where[0], where[1], now=when)
            self.assertFalse(os.path.exists(folder))
            name = "Taxi Time" if n == 1 else "Taxi Time.2"
            self.assertEqual(moved, os.path.join(self.data, "Trash", "files", name))
            with open(os.path.join(self.data, "Trash", "info", name + ".trashinfo"),
                      encoding="utf-8") as f:
                text = f.read()
            path = os.path.abspath(folder).replace(os.sep, "/")
            self.assertIn("Path=" + urllib.parse.quote(path, safe="/") + "\n", text)
            self.assertIn("%20", text)
            self.assertIn("DeletionDate=2026-11-10T12:30:05", text)

    def test_other_drive_uses_drive_trash(self):
        drive = os.path.join(self.base, "card")
        addons = os.path.join(drive, "Interface", "AddOns")
        folder = fixtures.make_addon_folder(addons, "TaxiTime", "1.0.0")

        def device(p):
            p = os.path.abspath(p)
            return 2 if (p + os.sep).startswith(drive + os.sep) else 1
        where = trash.linux_trash_dir(folder, data_home=self.data, uid=1000, device=device)
        self.assertEqual(where, (os.path.join(drive, ".Trash-1000"), drive))
        trash.linux_send(folder, *where)
        with open(os.path.join(drive, ".Trash-1000", "info", "TaxiTime.trashinfo"),
                  encoding="utf-8") as f:
            self.assertIn("Path=Interface/AddOns/TaxiTime\n", f.read())

    def test_failed_move_leaves_no_info(self):
        folder = fixtures.make_addon_folder(self.addons, "TaxiTime", "1.0.0")
        where = trash.linux_trash_dir(folder, data_home=self.data, uid=1000)
        with mock.patch.object(trash.os, "rename", side_effect=PermissionError(13, "no")):
            with self.assertRaises(trash.TrashError) as cm:
                trash.linux_send(folder, *where)
        self.assertEqual(cm.exception.kind, "permission")
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(os.listdir(os.path.join(self.data, "Trash", "info")), [])


class WindowRemoveTests(unittest.TestCase):
    """The hidden window: Uninstall button, right-click, confirm, Removed rows, Update all.
    Taxi Time is installed and up to date (its button says Uninstall); Guild Hub isn't."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("remove_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir("remove_ui_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.3.0")   # up to date
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.wtf = fixtures.tree_snapshot(os.path.join(self.client, "WTF"))
        self.trash = FakeTrash(os.path.join(self.work, "bin"))
        self.asked = []
        self.answer = True
        config.save_settings({})
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.assertEqual(fixtures.tree_snapshot(os.path.join(self.client, "WTF")), self.wtf)
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

    def pump(self, app, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while app.busy and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        for _ in range(5):
            self.root.update()
            time.sleep(0.02)

    def make_app(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     trash_api=self.trash)
        self.pump(app)
        return app

    def status(self, app, folder):
        label, button, _ = app.row_widgets[folder]
        return label.cget("text"), button.cget("text") if button else None

    def test_one_button_per_row(self):
        """Installed and current: Uninstall (dimmer style) in the row's only button.
        Not installed: Install. An update: Update, with the right-click hint."""
        app = self.make_app()
        self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
        self.assertIs(app.remove_widgets["TaxiTime"], app.row_widgets["TaxiTime"][1])
        self.assertEqual(app.remove_widgets["TaxiTime"].cget("style"), "Uninstall.TButton")
        self.assertEqual(self.status(app, "GuildHub"), ("Not installed", "Install"))
        self.assertIsNone(app.remove_widgets["GuildHub"])
        self.assertEqual(app.row_widgets["GuildHub"][1].cget("style"), "TButton")
        cells = app._cells[[r.folder for r in app.rows].index("GuildHub")]
        self.assertEqual(cells["button_tip"].text, "")
        # No X column any more: nothing in grid column 5.
        self.assertEqual(app.list_frame.grid_slaves(column=5), [])
        # An update available: Update, no Uninstall, a tip pointing to right-click.
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")
        app.render()
        self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))
        self.assertIsNone(app.remove_widgets["TaxiTime"])
        cells = app._cells[[r.folder for r in app.rows].index("TaxiTime")]
        self.assertEqual(cells["button_tip"].text, errors.REMOVE_RIGHT_CLICK)
        self.assertEqual(app.row_widgets["TaxiTime"][1].cget("style"), "TButton")
        # Busy: the Uninstall button is greyed out like the others.
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.3.0")
        app.busy = True
        app.render()
        self.assertIn("disabled", app.remove_widgets["TaxiTime"].state())
        app.busy = False
        app.render()

    def test_update_finished_then_uninstall_asks(self):
        """After Update the same button says Uninstall; a click still asks first."""
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")
        app = self.make_app()
        app.row_widgets["TaxiTime"][1].invoke()   # Update
        self.pump(app)
        self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
        self.answer = False
        app.row_widgets["TaxiTime"][1].invoke()
        self.pump(app)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))

    def test_x_when_folder_already_gone(self):
        """L3: the folder was deleted by hand after the list was drawn: say so, show Removed."""
        app = self.make_app()
        fixtures.remove(os.path.join(self.addons, "TaxiTime"))
        app.remove_widgets["TaxiTime"].invoke()
        self.pump(app)
        self.assertEqual(self.asked, [])   # no question for something that isn't there
        self.assertEqual(app.message_var.get(), "Taxi Time isn't installed any more.")
        self.assertEqual(self.status(app, "TaxiTime")[0], errors.REMOVED)
        self.assertIsNone(app.remove_widgets["TaxiTime"])

    def test_partly_deleted_shows_removed_with_honest_text(self):
        """M1 in the window: a partly delete counts as removed, with the "partly" text."""
        self.trash.where = None
        app = self.make_app()

        def half(path, *a, **k):
            raise PermissionError(13, "test: can't delete", path)
        with mock.patch.object(installer.shutil, "rmtree", half):
            app.remove_widgets["TaxiTime"].invoke()
            self.pump(app)
        self.assertIn("only partly deleted", app.message_var.get())
        self.assertEqual(self.status(app, "TaxiTime")[0], errors.REMOVED)
        installer.recover(self.addons)

    def test_cancel_changes_nothing(self):
        app = self.make_app()
        self.answer = False
        app.remove_widgets["TaxiTime"].invoke()
        self.pump(app)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0][0], "Remove Taxi Time?")
        self.assertIn(errors.BIN_NAME, self.asked[0][1])
        self.assertEqual(self.asked[0][2], "Remove")
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "TaxiTime")))
        self.assertEqual(self.trash.sent, [])

    def test_remove_then_update_all_skips_then_install_brings_back(self):
        app = self.make_app()
        app.remove_widgets["TaxiTime"].invoke()
        self.pump(app)
        self.assertEqual(self.trash.sent, ["TaxiTime"])
        self.assertFalse(os.path.exists(os.path.join(self.addons, "TaxiTime")))
        self.assertEqual(self.status(app, "TaxiTime"), ("Removed", "Install"))
        self.assertIsNone(app.remove_widgets["TaxiTime"])
        self.assertIn("Taxi Time was removed", app.message_var.get())
        self.assertEqual(config.load_settings().get("removed_addons"), ["TaxiTime"])
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "OtherAddon")))
        # Install all (a removed addon never counts): only GuildHub, TaxiTime stays removed.
        self.assertEqual(app.all_button.cget("text"), errors.ALL_INSTALL)
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))
        self.assertFalse(os.path.exists(os.path.join(self.addons, "TaxiTime")))
        self.assertIn("disabled", app.all_button.state())
        # A restart remembers it.
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        app = self.make_app()
        self.assertEqual(self.status(app, "TaxiTime"), ("Removed", "Install"))
        # Its own Install button brings it back and clears the mark.
        app.row_widgets["TaxiTime"][1].invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
        self.assertNotIn("removed_addons", config.load_settings())

    def test_no_bin_says_delete_for_good(self):
        self.trash.where = None
        app = self.make_app()
        app.remove_widgets["TaxiTime"].invoke()
        self.pump(app)
        self.assertIn("deleted for good", self.asked[0][1])
        self.assertEqual(self.asked[0][2], "Delete")
        self.assertEqual(self.trash.sent, [])   # deleted, not sent to the bin
        self.assertFalse(os.path.exists(os.path.join(self.addons, "TaxiTime")))
        self.assertNotIn("Recycle Bin", app.message_var.get())

    def test_in_use_message_and_nothing_changed(self):
        app = self.make_app()
        with in_use(os.path.join(self.addons, "TaxiTime")):
            app.remove_widgets["TaxiTime"].invoke()
            self.pump(app)
        self.assertTrue(os.path.isdir(os.path.join(self.addons, "TaxiTime")))
        self.assertIn("nothing was removed", app.message_var.get())
        self.assertNotIn("removed_addons", config.load_settings())
        self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))

    def test_needed_by_warning(self):
        fixtures.make_addon_folder(self.addons, "GuildHub", "0.4.1")
        with open(toc.toc_path(self.addons, "GuildHub"), "a", encoding="utf-8") as f:
            f.write("## RequiredDeps: TaxiTime\n")
        app = self.make_app()
        self.answer = False
        app.remove_row(app._row("TaxiTime"))
        self.assertIn("Guild Hub needs Taxi Time", self.asked[0][1])

    def test_right_click_menu(self):
        """Right-click Remove... also on a row whose button says Update (and on the button)."""
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")
        app = self.make_app()
        self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))
        self.assertTrue(app.row_widgets["TaxiTime"][1].bind("<Button-3>"))
        with mock.patch.object(app.row_menu, "tk_popup"):
            app._row_menu_popup(mock.Mock(x_root=0, y_root=0), 0)
            idx = [r.folder for r in app.rows].index("TaxiTime")
            app._row_menu_popup(mock.Mock(x_root=0, y_root=0), idx)
            self.assertEqual(app.row_menu.entrycget(0, "label"), "Remove Taxi Time...")
            self.assertEqual(app.row_menu.entrycget(0, "state"), "normal")
            app.row_menu.invoke(0)
        self.pump(app)
        self.assertEqual(self.trash.sent, ["TaxiTime"])
        with mock.patch.object(app.row_menu, "tk_popup"):
            app._row_menu_popup(mock.Mock(x_root=0, y_root=0), idx)
        self.assertEqual(app.row_menu.entrycget(0, "state"), "disabled")

    def test_back_by_hand_clears_mark(self):
        config.save_settings({"removed_addons": ["TaxiTime"]})
        app = self.make_app()   # TaxiTime is installed: the mark is dropped
        self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
        self.assertNotIn("removed_addons", config.load_settings())

    def test_rows_fit_smallest_window(self):
        """Linux fonts are wider: every row (version texts, status and its button) must fit
        the 680 px window (v0.6.0's Linux build failed at 711 > 680)."""
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")   # Update available
        fixtures.make_addon_folder(self.addons, "GuildHub", "0.4.1")   # Uninstall
        app = self.make_app()
        self.root.update_idletasks()
        self.assertIsNotNone(app.remove_widgets["GuildHub"])
        width = app.list_frame.winfo_reqwidth()
        # The window can never get narrower than the rows (so no button is ever cut off)...
        self.assertLessEqual(width, self.root.minsize()[0] - 20)
        # ...and the default 680 px window still shows them all.
        self.assertLessEqual(self.root.minsize()[0], 680)
        if os.name == "nt":   # Segoe UI 9: the rows fit the normal smallest window
            self.assertLessEqual(width, ui.MIN_WIDTH - 20)
            self.assertEqual(self.root.minsize()[0], ui.MIN_WIDTH)


if __name__ == "__main__":
    unittest.main()
