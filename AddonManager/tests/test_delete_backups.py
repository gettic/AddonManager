"""Delete old backups: am\\backup_delete.py rules and the hidden "Delete old backups"
window. Fake WoW folder, fake Documents and a fake trash only: the real Recycle Bin,
Trash, Documents and WoW folder are never used, and the window is never shown."""

import contextlib
import gc
import os
import shutil
import stat
import time
import tkinter as tk
import unittest
from unittest import mock

from am import backup, backup_delete, backup_delete_ui, config, errors, installer, trash, ui
from tests import fixtures



class FakeTrash:
    """Stands in for am.trash: moves files into bin_dir; where=None = no bin on the drive."""

    def __init__(self, bin_dir, where="bin", fail=None, no_bin_for=()):
        self.bin_dir, self.where, self.fail = bin_dir, where, fail
        self.no_bin_for = set(no_bin_for)
        self.sent = []
        os.makedirs(bin_dir, exist_ok=True)

    def available(self, path):
        return None if os.path.basename(path) in self.no_bin_for else self.where

    def send(self, path):
        if self.fail:
            raise self.fail
        self.sent.append(os.path.basename(path))
        shutil.move(path, os.path.join(self.bin_dir, os.path.basename(path)))


@contextlib.contextmanager
def in_use(path):
    """The file is open: a real handle on Windows; simulated on Linux (removing works there)."""
    if os.name == "nt":
        with open(path, "rb"):
            yield
        return
    real = os.remove
    target = os.path.abspath(path)

    def remove(p, *a, **k):
        if os.path.abspath(os.fspath(p)) == target:
            e = PermissionError(13, "Permission denied (test lock)", p)
            e.winerror = 32
            raise e
        return real(p, *a, **k)
    with mock.patch.object(backup_delete.os, "remove", remove):
        yield


def write(folder, name, size=100, age=0):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    when = time.time() - 3600 * 24 * age
    os.utime(path, (when, when))
    return path


class Setup:
    def make_world(self):
        self.work = fixtures.temp_dir("delete_backups_")
        self.addCleanup(fixtures.remove, self.work)
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.docs = os.path.join(self.work, "docs")
        self.folder = os.path.join(self.docs, backup.FOLDER_NAME)
        self.game_before = fixtures.tree_snapshot(self.root_dir)
        self.trash = FakeTrash(os.path.join(self.work, "bin"))
        self.places = [("Documents", self.folder)]

    def game_untouched(self):
        self.assertEqual(fixtures.tree_snapshot(self.root_dir), self.game_before)

    def found(self):
        return backup_delete.find(self.places, self.addons)


class LogicTests(Setup, unittest.TestCase):
    def setUp(self):
        self.make_world()
        self.new = write(self.folder, "AddonManager backup 2026-11-10.zip", 300, 0)
        self.old = write(self.folder, "AddonManager backup 2026-10-01.zip", 200, 30)
        self.recv = write(self.folder, "AddonManager backup 2026-09-01 received.zip", 150, 60)
        self.safe = write(self.folder, "Before restore 2026-11-09 2130.zip", 100, 1)
        self.foreign = write(self.folder, "holiday photos.zip", 50, 90)
        self.part = write(self.folder, "AddonManager backup 2026-11-11.zip.part", 50, 0)

    def test_lists_own_files_only(self):
        names = [os.path.basename(f.path) for f in self.found()]
        self.assertEqual(names, ["AddonManager backup 2026-11-10.zip",
                                 "Before restore 2026-11-09 2130.zip",
                                 "AddonManager backup 2026-10-01.zip",
                                 "AddonManager backup 2026-09-01 received.zip"])

    def test_moves_to_bin_rest_untouched(self):
        found = {os.path.basename(f.path): f for f in self.found()}
        res = backup_delete.delete([found["AddonManager backup 2026-10-01.zip"]],
                                   self.places, self.addons, self.trash)
        self.assertEqual([k for _f, k in res], [backup_delete.MOVED])
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(self.trash.sent, ["AddonManager backup 2026-10-01.zip"])
        for p in (self.new, self.recv, self.safe, self.foreign, self.part):
            self.assertTrue(os.path.exists(p))
        self.game_untouched()

    def test_no_bin_needs_for_good(self):
        nobin = FakeTrash(os.path.join(self.work, "bin2"), where=None)
        f = next(f for f in self.found() if f.path == self.old)
        res = backup_delete.delete([f], self.places, self.addons, nobin)
        self.assertEqual(res[0][1], backup_delete.NO_BIN)   # never for good unasked
        self.assertTrue(os.path.exists(self.old))
        res = backup_delete.delete([f], self.places, self.addons, nobin, {f.path})
        self.assertEqual(res[0][1], backup_delete.DELETED)
        self.assertFalse(os.path.exists(self.old))
        self.game_untouched()

    def test_read_only_deleted_for_good(self):
        os.chmod(self.old, stat.S_IREAD)
        f = next(f for f in self.found() if f.path == self.old)
        nobin = FakeTrash(os.path.join(self.work, "bin2"), where=None)
        res = backup_delete.delete([f], self.places, self.addons, nobin, {f.path})
        self.assertEqual(res[0][1], backup_delete.DELETED)
        self.assertFalse(os.path.exists(self.old))

    def test_in_use_stays_rest_go(self):
        found = {f.path: f for f in self.found()}
        nobin = FakeTrash(os.path.join(self.work, "bin2"), where=None)
        items = [found[self.old], found[self.recv]]
        with in_use(self.old):
            res = backup_delete.delete(items, self.places, self.addons, nobin,
                                       {self.old, self.recv})
        self.assertEqual([k for _f, k in res], [backup_delete.IN_USE, backup_delete.DELETED])
        self.assertTrue(os.path.exists(self.old))
        self.assertFalse(os.path.exists(self.recv))

    def test_trash_errors_mapped(self):
        f = next(f for f in self.found() if f.path == self.old)
        bad = FakeTrash(os.path.join(self.work, "bin2"), fail=trash.TrashError("locked"))
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, bad),
                         backup_delete.IN_USE)
        bad.fail = trash.TrashError("permission")
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, bad),
                         backup_delete.PERMISSION)
        self.assertTrue(os.path.exists(self.old))

    def test_changed_since_listing(self):
        f = next(f for f in self.found() if f.path == self.old)
        with open(self.old, "ab") as fh:
            fh.write(b"more")
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, self.trash),
                         backup_delete.CHANGED)
        self.assertTrue(os.path.exists(self.old))

    def test_gone(self):
        f = next(f for f in self.found() if f.path == self.old)
        os.remove(self.old)
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, self.trash),
                         backup_delete.GONE)

    def test_replaced_by_link_refused(self):
        f = next(f for f in self.found() if f.path == self.old)
        target = write(os.path.join(self.work, "elsewhere"), "precious.zip", 200)
        os.remove(self.old)
        try:
            os.symlink(target, self.old)
        except (OSError, NotImplementedError):
            os.mkdir(self.old)   # no symlink rights here: a folder of that name instead
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, self.trash),
                         backup_delete.REFUSED)
        self.assertTrue(os.path.exists(target))

    def test_outside_places_refused(self):
        f = next(f for f in self.found() if f.path == self.old)
        other = [("Elsewhere", os.path.join(self.work, "elsewhere"))]
        self.assertEqual(backup_delete.delete_one(f, other, self.addons, self.trash),
                         backup_delete.REFUSED)
        forged = backup.FoundBackup(self.foreign, "Documents", 50,
                                    os.lstat(self.foreign).st_mtime, False, False)
        self.assertEqual(backup_delete.delete_one(forged, self.places, self.addons, self.trash),
                         backup_delete.REFUSED)
        self.assertTrue(os.path.exists(self.old) and os.path.exists(self.foreign))

    def test_game_folder_never_listed_or_deleted(self):
        inside = os.path.join(self.client, backup.FOLDER_NAME)
        game_file = write(inside, "AddonManager backup 2026-01-01.zip", 10, 5)
        self.game_before = fixtures.tree_snapshot(self.root_dir)
        places = [("Game", inside), ("WoW root", self.root_dir), *self.places]
        self.assertEqual(backup_delete.safe_places(places, self.addons), self.places)
        self.assertNotIn(game_file, [f.path for f in backup_delete.find(places, self.addons)])
        forged = backup.FoundBackup(game_file, "Game", 10, os.lstat(game_file).st_mtime,
                                    False, False)
        self.assertEqual(backup_delete.delete_one(forged, places, self.addons, self.trash),
                         backup_delete.REFUSED)
        self.assertTrue(backup_delete.in_game(inside, self.addons))
        self.assertTrue(backup_delete.in_game(self.addons, None))   # no folder known: still caught
        self.game_untouched()

    def test_safety_copy_waits_for_cut_off_restore(self):
        f = next(f for f in self.found() if f.path == self.safe)
        journal = os.path.join(self.addons, installer.RESTORE_JOURNAL)
        with open(journal, "w") as fh:
            fh.write("{}")
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, self.trash),
                         backup_delete.RESTORE)
        os.remove(journal)
        self.assertEqual(backup_delete.delete_one(f, self.places, self.addons, self.trash),
                         backup_delete.MOVED)

    def test_never_opens_files(self):
        """Online-only cloud files must not download: the listing and delete never open."""
        real_open = open

        def no_open(path, *a, **k):
            if str(path).lower().endswith(".zip"):
                raise AssertionError(f"opened {path}")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", no_open), mock.patch("io.open", no_open):
            found = self.found()
            f = next(f for f in found if f.path == self.old)
            f.online = True
            res = backup_delete.delete([f], self.places, self.addons, self.trash)
        self.assertEqual(res[0][1], backup_delete.MOVED)

    def test_but_newest(self):
        found = [f for f in self.found() if not f.safety]
        self.assertEqual([f.path for f in backup_delete.but_newest(found)],
                         [self.old, self.recv])
        self.assertEqual(backup_delete.but_newest([]), [])

    def test_texts(self):
        found = {f.path: f for f in self.found()}
        items = [found[self.old], found[self.safe]]
        text = backup_delete_ui.confirm_text(items, {self.safe}, {"Documents"})
        self.assertIn(errors.DELETE_TO_BIN, text)
        self.assertIn(errors.DELETE_FOR_GOOD, text)
        self.assertIn("Total:", text)
        self.assertIn("other PCs", text)
        self.assertIn(errors.DELETE_SAFETY_LINE, text)
        many = [found[self.old]] * 6
        self.assertIn("and 2 more", backup_delete_ui.confirm_text(many, set(), set()))
        res = backup_delete_ui.result_text([(found[self.old], backup_delete.MOVED),
                                            (found[self.recv], backup_delete.IN_USE)])
        self.assertIn("1 backup moved", res)
        self.assertIn("1 backup not deleted", res)
        self.assertIn("in use", res)
        self.assertIn("received", backup_delete_ui.row_text(found[self.recv]))


class WindowTests(Setup, unittest.TestCase):
    """The hidden window (never shown), opened with backup_delete_ui.open_delete."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("delete_backups_site_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.make_world()
        docs = mock.patch.dict(os.environ, {backup.DOCUMENTS_ENV: self.docs})
        docs.start()
        self.addCleanup(docs.stop)
        p = mock.patch.object(backup, "cloud_folders", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        config.save_settings({})
        self.asked, self.answer = [], True
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()

    def confirm(self, title, text, ok, cancel):
        self.asked.append((title, text, ok, cancel))
        return self.answer

    def pump_until(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if cond():
                for _ in range(3):
                    self.root.update()
                return
            time.sleep(0.02)
        raise AssertionError("timed out")

    def open(self, count=3, safety=1):
        self.files = [write(self.folder, f"AddonManager backup 2026-10-{i + 1:02d}.zip",
                            100 + i, age=i) for i in range(count)]
        self.safeties = [write(self.folder, f"Before install 2026-09-{i + 1:02d} 1200.zip",
                               50, age=40 + i) for i in range(safety)]
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     trash_api=self.trash)
        self.pump_until(lambda: not app.busy)
        d = backup_delete_ui.open_delete(app, show=False)
        self.assertIsNotNone(d)
        self.pump_until(lambda: d.found is not None)
        return app, d

    def test_list_ticks_total_and_buttons(self):
        app, d = self.open()
        self.assertEqual(len(d.checks), 3)                      # safety copies hidden
        self.assertTrue(d.delete_button.instate(["disabled"]))
        self.assertEqual(d.total_var.get(), errors.DELETE_NOTHING)
        d.checks[1].invoke()
        self.assertFalse(d.delete_button.instate(["disabled"]))
        self.assertTrue(d.total_var.get().startswith("1 ticked"))
        d.safety_var.set(True)
        d.toggle_safety()
        self.assertEqual(len(d.checks), 4)
        self.assertFalse(any(d.vars[f.path].get() for f in d.found if f.safety))
        self.assertIsNone(backup_delete_ui.open_delete(app, show=False))   # one at a time
        self.game_untouched()

    def test_tick_all_but_newest_and_delete(self):
        app, d = self.open()
        d.tick_but_newest()
        ticked = {f.path for f in d.ticked()}
        self.assertEqual(ticked, set(self.files[1:]))           # newest kept, no safety copy
        self.answer = False
        self.assertFalse(d.start())
        self.assertTrue(all(os.path.exists(p) for p in self.files))
        title, text, ok, _cancel = self.asked[-1]
        self.assertEqual(title, "Delete 2 backups?")
        self.assertEqual(ok, errors.DELETE_OK_BIN)
        self.answer = True
        self.assertTrue(d.start())
        d.close()                                               # refused while deleting
        self.assertFalse(d.closed)
        self.pump_until(lambda: not d.running)
        self.assertTrue(os.path.exists(self.files[0]))
        self.assertFalse(any(os.path.exists(p) for p in self.files[1:]))
        self.assertEqual(sorted(self.trash.sent), sorted(os.path.basename(p) for p in self.files[1:]))
        self.assertEqual(len(d.checks), 1)
        self.assertIn("2 backups moved", d.status_var.get())
        d.close()
        self.assertTrue(d.closed)
        self.assertIsNone(app.backup_dialog)
        self.game_untouched()

    def test_no_bin_asks_delete_for_good(self):
        app, d = self.open(count=2, safety=0)
        self.trash.no_bin_for = {os.path.basename(self.files[1])}
        d.vars[self.files[1]].set(True)
        d.update_buttons()
        self.assertTrue(d.start())
        self.assertEqual(self.asked[-1][2], errors.DELETE_OK_GOOD)
        self.assertIn(errors.DELETE_FOR_GOOD, self.asked[-1][1])
        self.pump_until(lambda: not d.running)
        self.assertFalse(os.path.exists(self.files[1]))
        self.assertEqual(self.trash.sent, [])
        self.assertIn("deleted for good", d.status_var.get())

    def test_scrolls_over_eight(self):
        _app, d = self.open(count=12, safety=0)
        self.assertIsNotNone(d.canvas)
        self.assertEqual(len(d.checks), 12)
        d.win.update_idletasks()
        from am import theme
        self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420))   # Linux/Deck dialog width

    def test_none_found(self):
        _app, d = self.open(count=0, safety=0)
        self.assertEqual(d.status_var.get(), errors.DELETE_NONE)
        self.assertTrue(d.newest_button.instate(["disabled"]))

    def test_backup_window_button(self):
        """Old backups... shows only once something is found (a safety copy alone too),
        and opens the Delete old backups window."""
        write(self.folder, "Before restore 2026-09-01 1200.zip", 50, 3)
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     trash_api=self.trash)
        self.pump_until(lambda: not app.busy)
        hub = app.open_backup_window(show=False)
        self.assertFalse(hub.old_button.winfo_manager())
        self.pump_until(lambda: hub.old_button.winfo_manager() == "pack")
        self.assertFalse(hub.open_button.winfo_manager())   # no backup of its own yet
        hub.handle(("backup_last", hub._looking, None, False))
        self.assertFalse(hub.old_button.winfo_manager())
        d = hub.open_old()
        self.assertIsInstance(d, backup_delete_ui.DeleteBackupsDialog)
        self.pump_until(lambda: d.found is not None)
        d.safety_var.set(True)
        d.toggle_safety()
        self.assertEqual(len(d.checks), 1)
        d.close()
        app.busy = True
        hub.render()
        self.assertTrue(hub.old_button.instate(["disabled"]))
        app.busy = False

    def test_not_while_busy(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     trash_api=self.trash)
        self.pump_until(lambda: not app.busy)
        app.busy = True
        self.assertIsNone(backup_delete_ui.open_delete(app, show=False))
        app.busy = False


if __name__ == "__main__":
    unittest.main()
