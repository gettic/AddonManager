"""The full check's fixes (v0.19 review): closing the app during Receive/Send, codes that
couldn't be saved, Stop with an unreadable codes file, one deleted locked copy among two,
texts, Save zip's added ".zip", and the small helps (Copy and every code in Your codes,
Settings folder links, why a greyed button is greyed, Linux hold wording, short codes,
backup age, number-pad Enter). Hidden windows only; temp folders, fake relay, fake trash."""

import os
import queue
import threading
import time
import unittest
import zipfile
from unittest import mock

from am import (backup, backup_delete, backup_lock, codes, config, errors, fileinstall_ui,
                share)
from am.share_words import WORDS
from tests import fixtures
from tests.fake_relay import FakeRelay
from tests.test_backup import make_pc
from tests.test_lock import CODE, OTHER, FakeTrash, clear_codes, received_zip


def fire(widget, sequence):
    """Run widget's Python bindings for sequence (a withdrawn window gets no real mouse or
    key events): what Tk would call for that event."""
    import re
    script = widget.bind(sequence)
    names = re.findall(r"\[(\S+) %#", script)
    assert names, f"nothing bound to {sequence}"
    fmt = widget._subst_format
    args = [str(widget) if f == "%W" else "??" if f in ("%A", "%K", "%T") else "0"
            for f in fmt]
    for name in names:
        widget.tk.call(name, *args)


class _Windows(unittest.TestCase):
    """A hidden App with its own PC, backup folder, fake relay and fake bin."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("check_fixes_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        import tkinter as tk
        clear_codes(self)
        self.tk = tk
        self.work = fixtures.temp_dir("check_fixes_ui_")
        self.addCleanup(fixtures.remove, self.work)
        self.root_dir, self.addons, self.client = make_pc(os.path.join(self.work, "pc"))
        docs = mock.patch.dict(os.environ, {backup.DOCUMENTS_ENV: os.path.join(self.work, "docs")})
        docs.start()
        self.addCleanup(docs.stop)
        self.backups = os.path.join(self.work, "backups")
        os.makedirs(self.backups, exist_ok=True)
        config.save_settings({"backup_folder": self.backups})
        p = mock.patch.object(backup, "cloud_folders", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        self.relay = FakeRelay()
        self.addCleanup(self.relay.stop)
        self.asked, self.answers, self.copied, self.opened = [], [], [], []
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

    def confirm(self, title, _text, _ok, _cancel):
        self.asked.append(title)
        return self.answers.pop(0) if self.answers else True

    def make_app(self):
        from am import ui
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], confirm=self.confirm,
                     share_url=self.relay.url, trash_api=FakeTrash(os.path.join(self.work, "bin")))
        app.clipboard = self.copied.append
        app.open_folder = self.opened.append
        self.pump(app, lambda: not app.busy)
        return app

    def pump(self, app, cond, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.root.update()
            except self.tk.TclError:   # the app closed (root destroyed)
                pass
            if cond():
                for _ in range(3):
                    try:
                        self.root.update()
                    except self.tk.TclError:
                        break
                return True
            time.sleep(0.02)
        raise AssertionError("timed out")

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def leftovers(self):
        return [n for n in os.listdir(self.backups)
                if n.startswith((backup_lock.UNLOCKED_PREFIX, share.PART_PREFIX))]

    def receive_window(self, app):
        app.open_backup_window(show=False)
        rec = app.open_receive(show=False)
        rec.code_var.set(CODE)
        return rec


# ---------- bug 1: the main window's X during Receive / Send ----------
class CloseDuringTransferTests(_Windows):
    def test_live_receive_stops_then_closes(self):
        for name, value in (("IDLE", 3), ("POLL_WAIT", 1)):
            p = mock.patch.object(share, name, value)
            p.start()
            self.addCleanup(p.stop)
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        ch, _ttl = sender.open()                       # the other PC waits, sends nothing
        app = self.make_app()
        rec = self.receive_window(app)
        rec.code_var.set(share.format_code(ch, WORDS[:3]))
        self.assertTrue(rec.start())
        self.wait(0.3)
        self.assertTrue(rec.transfer_running())
        app.close()
        self.assertFalse(app.closed)                    # waits for the transfer to stop
        self.assertTrue(rec.cancel_event.is_set())
        self.assertEqual(rec.status_var.get(), errors.CLOSING_STOPPING)
        self.pump(app, lambda: app.closed)
        self.assertEqual(self.leftovers(), [])

    def test_send_server_download_asks_first(self):
        from am import share_ui

        def worker(q, tag, url, allow_local, code, folder, cancel):
            q.put(("share_later", tag, "store", "send"))
            cancel.wait(20)
            q.put(("share_error", tag, "stopped"))
        with mock.patch.object(share_ui, "_receive_worker", worker):
            app = self.make_app()
            rec = self.receive_window(app)
            self.assertTrue(rec.start())
            self.pump(app, lambda: rec.store_download)
            self.answers = [False]                       # Keep receiving
            app.close()
            self.assertIn(errors.RECEIVE_STOP_TITLE, self.asked)
            self.wait(0.3)
            self.assertFalse(app.closed)
            self.assertFalse(rec.cancel_event.is_set())
            self.assertTrue(rec.transfer_running())
            self.answers = [True]                        # Stop and delete
            app.close()
            self.assertTrue(rec.cancel_event.is_set())
            self.pump(app, lambda: app.closed)

    def test_locking_step_is_never_cut(self):
        from am import share_ui, ui
        zpath = received_zip(self.work, self.addons)
        started, release = threading.Event(), threading.Event()
        real_keep = backup_lock.keep

        def slow_keep(path, code, remember=None, progress=None, now=None):
            progress(0, 100)
            started.set()
            release.wait(20)
            return real_keep(path, code, remember=remember, progress=progress, now=now)

        def worker(q, tag, url, allow_local, code, folder, cancel):
            try:
                share_ui._kept_received(q, tag, zpath, CODE, cancel)
            except Exception:   # noqa: BLE001
                q.put(("share_error", tag, "stopped"))
        with mock.patch.object(share_ui, "_receive_worker", worker), \
                mock.patch.object(backup_lock, "keep", slow_keep), \
                mock.patch.object(ui, "CLOSE_WAIT", 0.2):
            app = self.make_app()
            rec = self.receive_window(app)
            self.assertTrue(rec.start())
            self.pump(app, lambda: rec.locking)
            app.close()
            self.assertEqual(rec.status_var.get(), errors.CLOSING_LOCKING)
            self.wait(0.6)                               # longer than CLOSE_WAIT
            self.assertFalse(app.closed)
            release.set()
            self.pump(app, lambda: app.closed)
        names = os.listdir(self.backups)
        self.assertEqual(len([n for n in names if n.endswith(backup_lock.EXT)]), 1)
        self.assertEqual([e["code"] for e in codes.locked().values()], [CODE])
        self.assertEqual(self.leftovers(), [])
        self.assertFalse([n for n in names if n.endswith("received.zip")])

    def test_send_stops_then_closes(self):
        app = self.make_app()
        app.open_backup_window(show=False)
        d = app.open_send(show=False)
        d.active = True

        def worker(cancel, tag):
            cancel.wait(20)
            app.queue.put(("share_error", tag, "stopped", True, None))
        d._start_worker(worker, (d.cancel_event, d.tag))
        app.close()
        self.assertFalse(app.closed)
        self.assertTrue(d.cancel_event.is_set())
        self.assertEqual(d.status_var.get(), errors.CLOSING_STOPPING)
        self.pump(app, lambda: app.closed)

    def test_a_stuck_transfer_closes_after_the_wait(self):
        from am import ui
        app = self.make_app()
        rec = self.receive_window(app)
        rec.active = True
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        rec._start_worker(lambda: stuck.wait(20), ())
        with mock.patch.object(ui, "CLOSE_WAIT", 0.3):
            app.close()
            t0 = time.time()
            self.pump(app, lambda: app.closed, timeout=10)
        self.assertLess(time.time() - t0, 5)
        self.assertTrue(rec.worker.is_alive())


# ---------- bugs 2, 3, 6 and Your codes ----------
class CodesAndSaveTests(_Windows):
    def unreadable(self):
        """The codes file can't be read (stand-in: a folder in its place)."""
        path = codes.store_path()
        if os.path.exists(path):
            os.replace(path, path + ".keep")
        os.makedirs(path)

        def back():
            os.rmdir(path)
            if os.path.exists(path + ".keep"):
                os.replace(path + ".keep", path)
        return back

    def test_remember_lock_reports_failure(self):
        self.assertTrue(codes.remember_lock("a" * 32, "x.amlocked", 10, CODE))
        with mock.patch.object(codes, "save", return_value=False):
            self.assertIsNone(codes.remember_lock("b" * 32, "y.amlocked", 10, CODE))
        back = self.unreadable()
        try:
            self.assertIsNone(codes.remember_lock("c" * 32, "z.amlocked", 10, CODE))
        finally:
            back()
        self.assertEqual(set(codes.locked()), {"a" * 32})

    def test_keep_says_when_the_code_was_not_saved(self):
        z = received_zip(self.work, self.addons)
        kept = backup_lock.keep(z, CODE, remember=lambda *a: None)
        self.addCleanup(kept.discard)
        self.assertTrue(kept.locked and os.path.exists(kept.locked))
        self.assertIs(kept.remembered, False)
        z2 = received_zip(os.path.join(self.work, "two"), self.addons)
        kept2 = backup_lock.keep(z2, CODE, remember=codes.remember_lock)
        self.addCleanup(kept2.discard)
        self.assertIs(kept2.remembered, True)

    def test_restore_window_shows_the_code_to_write_down(self):
        from am import backup_ui
        z = received_zip(self.work, self.addons)
        kept = backup_lock.keep(z, CODE, remember=lambda *a: None)
        app = self.make_app()
        app.open_backup_window(show=False)
        rd = app.backup_dialog = backup_ui.RestoreDialog(
            app, backup.read(kept.plain, received=True), show=False, kept=kept)
        self.assertEqual(str(rd.kept_label.cget("text")), errors.kept_no_code(CODE))
        self.assertIn(CODE, errors.kept_no_code(CODE))
        rd.close()

    def sent_code(self):
        sender = share.Relay(self.relay.url, allow_local=True, sleep=lambda s: None)
        ch, _ttl = sender.open()
        sender.start_later(2, "0" * 32, "1" * 64)
        left = sender.put_record(b"r" * 40)
        eid = codes.add_sent(OTHER, ch, sender.token, "kiwi", time.time() + left, 2)
        return sender, eid

    def test_stop_with_an_unreadable_codes_file(self):
        from am import codes_ui
        sender, eid = self.sent_code()
        q = queue.Queue()
        back = self.unreadable()
        try:
            codes_ui._stop_worker(q, "t", self.relay.url, True, eid)
        finally:
            back()
        self.assertEqual(q.get_nowait(), ("backup_codes_stopped", "t", eid, "unreadable"))
        self.assertEqual(sender.status()["state"], "ready")      # the host wasn't asked
        self.assertEqual(codes.sent()[0]["state"], "ready")
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        with mock.patch.object(codes_ui, "_status_worker", lambda *a, **k: None):
            d = hub.open_codes()
            d.handle(("backup_codes_stopped", d.token, eid, "unreadable"))
            self.assertEqual(d.status_var.get(), errors.CODES_STOP_UNREADABLE)
            d.close()

    def test_stop_of_a_code_that_is_gone(self):
        from am import codes_ui
        q = queue.Queue()
        codes_ui._stop_worker(q, "t", self.relay.url, True, "nothere")
        self.assertEqual(q.get_nowait(), ("backup_codes_stopped", "t", "nothere", True))

    def save_window(self, app, target):
        from am import backup_ui
        z = received_zip(self.work, self.addons)
        kept = backup_lock.keep(z, CODE, remember=codes.remember_lock)
        rd = app.backup_dialog = backup_ui.RestoreDialog(
            app, backup.read(kept.plain, received=True), show=False, kept=kept)
        app.ask_save_zip = lambda name: target
        return rd

    def test_save_zip_asks_before_replacing_the_added_name(self):
        usb = os.path.join(self.work, "usb")
        os.makedirs(usb)
        there = os.path.join(usb, "x.txt.zip")
        with open(there, "wb") as f:
            f.write(b"keep me")
        app = self.make_app()
        app.open_backup_window(show=False)
        rd = self.save_window(app, os.path.join(usb, "x.txt"))
        self.answers = [False]
        self.assertFalse(rd.save_zip())
        self.assertIn(errors.SAVE_ZIP_REPLACE_TITLE, self.asked)
        with open(there, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        rd.close()

    def test_save_zip_replaces_after_yes(self):
        usb = os.path.join(self.work, "usb")
        os.makedirs(usb)
        there = os.path.join(usb, "x.txt.zip")
        with open(there, "wb") as f:
            f.write(b"old")
        app = self.make_app()
        app.open_backup_window(show=False)
        rd = self.save_window(app, os.path.join(usb, "x.txt"))
        self.answers = [True]
        self.assertTrue(rd.save_zip())
        self.pump(app, lambda: not rd.saving)
        self.assertTrue(zipfile.is_zipfile(there))
        rd.close()

    # ---------- Your codes: Copy and every code ----------
    def codes_window(self, app):
        from am import codes_ui
        p = mock.patch.object(codes_ui, "_status_worker", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        hub = app.open_backup_window(show=False)
        return hub.open_codes()

    def click(self, widget, event="<Button-1>"):
        fire(widget, event)

    def test_copy_a_sent_code(self):
        codes.add_sent(OTHER, 7, "e" * 32, "kiwi", time.time() + 600, 1)
        app = self.make_app()
        d = self.codes_window(app)
        self.assertEqual([link.code for link in d.copy_links], [OTHER])
        self.click(d.copy_links[0])
        self.assertEqual(self.copied, [OTHER])
        self.assertEqual(d.status_var.get(), errors.CODES_COPIED)
        d.close()

    def test_copy_a_locked_code_once_shown(self):
        codes.remember_lock("a" * 32, "AddonManager backup 2026-11-14 received.amlocked", 10,
                            CODE)
        app = self.make_app()
        d = self.codes_window(app)
        self.assertEqual(d.copy_links, [])                # hidden code: nothing to copy
        [b for b in d.row_buttons if str(b.cget("text")) == errors.CODES_SHOW][0].invoke()
        self.assertEqual([link.code for link in d.copy_links], [CODE])
        self.click(d.copy_links[0])
        self.assertEqual(self.copied, [CODE])
        d.close()

    def many_locked(self, n):
        def add(data):
            for i in range(n):
                data["locked"][f"{i:032x}"] = {
                    "code": CODE, "name": f"AddonManager backup {i}.amlocked", "size": 10,
                    "saved": 1000.0 + i}
        codes._change(add)

    def heads(self, d):
        from tkinter import ttk
        out = []

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Label) and str(c.cget("style")) == "Head.TLabel":
                    out.append(str(c.cget("text")))
                walk(c)
        walk(d.win)
        return out

    def test_every_code_listed_and_scrolls(self):
        for i in range(4):
            codes.add_sent(share.format_code(20 + i, WORDS[i:i + 3]), 20 + i, f"{i}" * 32,
                           "visee", time.time() + 3600, 2)
        self.many_locked(4)
        app = self.make_app()
        d = self.codes_window(app)
        self.assertIsNotNone(d.canvas)
        texts = [str(b.cget("text")) for b in d.row_buttons]
        self.assertEqual(texts.count(errors.CODES_STOP), 4)
        self.assertEqual(texts.count(errors.CODES_SHOW), 4)
        self.assertEqual(self.heads(d), [errors.CODES_SENT + " (4)", errors.CODES_LOCKED + " (4)"])
        d.win.update_idletasks()
        self.assertLess(int(d.canvas.cget("height")), d.holder.winfo_reqheight())
        d.close()

    def test_more_than_fit_the_list(self):
        from am import codes_ui
        self.many_locked(codes_ui.MAX_ROWS + 10)
        app = self.make_app()
        d = self.codes_window(app)
        self.assertEqual(self.heads(d), [errors.codes_head(errors.CODES_LOCKED,
                                                           codes_ui.MAX_ROWS + 10,
                                                           codes_ui.MAX_ROWS)])
        self.assertIn(f"({codes_ui.MAX_ROWS} of {codes_ui.MAX_ROWS + 10}, newest)",
                      self.heads(d)[0])
        self.assertEqual([str(b.cget("text")) for b in d.row_buttons].count(errors.CODES_SHOW),
                         codes_ui.MAX_ROWS)
        d.close()

    def test_the_list_scrolls_with_the_wheel(self):
        self.many_locked(8)
        app = self.make_app()
        d = self.codes_window(app)
        d.win.update_idletasks()
        top = d.canvas.yview()[0]
        row = d.placed[1][0]
        row.event_generate("<MouseWheel>", delta=-120)
        row.event_generate("<Button-5>")
        self.assertGreater(d.canvas.yview()[0], top)
        d.close()


# ---------- bug 4: one of two locked copies deleted ----------
class DeleteLockedCopiesTests(unittest.TestCase):
    NAME = "AddonManager backup 2026-11-14 received.amlocked"

    def setUp(self):
        clear_codes(self)
        self.base = fixtures.temp_dir("check_fixes_delete_")
        self.addCleanup(fixtures.remove, self.base)
        self.places = []
        for label in ("Documents", "USB"):
            folder = os.path.join(self.base, label)
            os.makedirs(folder)
            with open(os.path.join(folder, self.NAME), "wb") as f:
                f.write(b"x" * 20)
            self.places.append((label, folder))
        codes.remember_lock("e" * 32, self.NAME, 20, CODE)
        self.trash = FakeTrash(os.path.join(self.base, "bin"))

    def delete(self, label, for_good=False):
        found = [f for f in backup_delete.find(self.places, None) if f.place == label]
        self.assertEqual(len(found), 1)
        with mock.patch.object(backup_delete, "check", return_value=None):
            if for_good:
                self.trash.where = None
                return backup_delete.delete(found, self.places, None, self.trash,
                                            for_good={found[0].path})
            return backup_delete.delete(found, self.places, None, self.trash)

    def test_the_other_copy_keeps_its_code(self):
        self.assertEqual(self.delete("Documents")[0][1], backup_delete.MOVED)
        self.assertEqual(codes.lock_code("e" * 32), CODE)   # the USB copy still opens
        self.delete("USB")
        self.assertEqual(codes.locked(), {})                # none left: forgotten

    def test_last_copy_deleted_for_good_forgets(self):
        os.remove(os.path.join(self.places[1][1], self.NAME))
        self.assertEqual(self.delete("Documents", for_good=True)[0][1], backup_delete.DELETED)
        self.assertEqual(codes.locked(), {})


# ---------- the texts ----------
class TextTests(unittest.TestCase):
    def test_no_four_dots(self):
        texts = [v for v in vars(errors).values() if isinstance(v, str)]
        texts += [errors.zip_saved("a.zip", False), errors.zip_saved("a.zip", True),
                  errors.kept_no_code(CODE), errors.save_zip_replace("x.zip")]
        self.assertEqual([t for t in texts if "...." in t], [])

    def test_hold_wording_only_on_linux(self):
        linux = os.name != "nt"
        for text in (errors.REMOVE_RIGHT_CLICK, fileinstall_ui.FOLDER_TIP):
            self.assertEqual("press and hold" in text, linux, text)
        self.assertIn("Settings > Open log folder", errors.DETAILS)
        self.assertNotIn("right-click", errors.DETAILS)

    def test_short_codes_hint(self):
        self.assertIn("first 3 letters of each word", errors.RECEIVE_EXAMPLE)
        self.assertEqual(errors.UNLOCK_EXAMPLE, errors.RECEIVE_EXAMPLE)
        short = "12 " + " ".join(w[:3] for w in WORDS[:3])
        self.assertEqual(share.parse_code(short), share.parse_code(CODE))

    def test_days_ago(self):
        import datetime
        now = datetime.datetime(2026, 11, 14, 9, 0).timestamp()
        day = 86400
        self.assertEqual(errors.days_ago(now - 3600, now), "today")
        self.assertEqual(errors.days_ago(now - day, now), "yesterday")
        self.assertEqual(errors.days_ago(now - 4 * day, now), "4 days ago")
        self.assertEqual(errors.days_ago(now + day, now), "")
        self.assertEqual(errors.days_ago("x", now), "")

    def test_backup_last_line(self):
        self.assertEqual(errors.backup_last("2026-11-10 14:02", "Documents", "4 days ago"),
                         "Last backup: 2026-11-10 14:02, 4 days ago (Documents)")
        self.assertEqual(errors.backup_last("2026-11-10 14:02", "Documents"),
                         "Last backup: 2026-11-10 14:02 (Documents)")


# ---------- Settings links, greyed buttons, number-pad Enter ----------
class MainWindowHelpTests(_Windows):
    def test_settings_folder_links(self):
        app = self.make_app()
        os.makedirs(os.path.dirname(config.log_path()), exist_ok=True)
        app.open_settings(show=False)
        links = app.settings_links
        self.assertEqual(str(links["addons"].cget("foreground")), str(__import__(
            "am.theme", fromlist=["LINK"]).LINK))
        fire(links["addons"], "<Button-1>")
        fire(links["log"], "<Button-1>")
        self.assertEqual(self.opened, [app.addons_dir, os.path.dirname(config.log_path())])
        app.close_settings()

    def test_settings_addons_link_greyed_without_a_folder(self):
        from am import theme
        app = self.make_app()
        app.addons_dir = None
        app.open_settings(show=False)
        link = app.settings_links["addons"]
        self.assertEqual(str(link.cget("foreground")), theme.DISABLED)
        self.assertEqual(link.bind("<Button-1>"), "")      # a click does nothing
        self.assertEqual(self.opened, [])
        app.close_settings()

    def test_greyed_all_button_says_why_on_click(self):
        app = self.make_app()
        app.busy = True
        app.render()
        self.assertIn("disabled", app.all_button.state())
        fire(app.all_button, "<ButtonRelease-1>")
        self.assertEqual(app.message_var.get(), errors.BUSY_TIP)
        app.busy = False
        app.render()
        app.set_message("x")
        if "disabled" not in app.all_button.state():   # a button that works: no tip text
            fire(app.all_button, "<ButtonRelease-1>")
            self.assertEqual(app.message_var.get(), "x")

    def test_status_cell_click_shows_the_reason(self):
        app = self.make_app()
        cells = app._cells[0]
        cells["status_tip"].text = "The whole reason."
        fire(cells["status"], "<ButtonRelease-1>")
        self.assertEqual(app.message_var.get(), "The whole reason.")

    def test_backup_window_greyed_button_says_why(self):
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        app.busy = True
        hub.render()
        fire(hub.make_button, "<ButtonRelease-1>")
        self.assertEqual(hub.status_var.get(), errors.BUSY_TIP)
        app.busy = False

    def test_number_pad_enter_receive(self):
        app = self.make_app()
        rec = self.receive_window(app)
        started = []
        rec.start = lambda: started.append(1)
        fire(rec.entry, "<KP_Enter>")
        self.assertEqual(started, [1])
        rec.close()

    def test_number_pad_enter_unlock(self):
        from am import backup_ui
        app = self.make_app()
        app.open_backup_window(show=False)
        u = app.backup_dialog = backup_ui.UnlockDialog(
            app, os.path.join(self.backups, "AddonManager backup x.amlocked"), show=False)
        started = []
        u.start = lambda: started.append(1)
        fire(u.entry, "<KP_Enter>")
        self.assertEqual(started, [1])
        u.close()

    def test_number_pad_enter_spoken_question(self):
        from am import backup_ui
        app = self.make_app()
        hub = app.open_backup_window(show=False)
        got = []
        ask = backup_ui.SpokenAsk(hub, [("SpokenVoice", 10)], 0, got.append, show=False)
        fire(ask.win, "<KP_Enter>")
        self.assertEqual(got, ["skip"])


if __name__ == "__main__":
    unittest.main()
