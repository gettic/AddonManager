"""Small main-window helpers (small-fixes job): the summary line, tooltips on greyed
buttons, Enter in questions, F5, the window's size and place, press-and-hold menus,
the folder box menu, the mouse wheel in Add from file. Hidden window, fake data."""
import gc
import os
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

import tests  # noqa: F401  (temp home)
from am import config, errors, fileinstall_ui, github, theme, touch, ui
from tests import fixtures


class Event:
    def __init__(self, x_root=100, y_root=100):
        self.x_root, self.y_root = x_root, y_root


class TextsTests(unittest.TestCase):
    def test_list_summary(self):
        self.assertEqual(errors.list_summary(0, 0), "All addons are up to date.")
        self.assertEqual(errors.list_summary(1, 0), "1 update available.")
        self.assertEqual(errors.list_summary(3, 0), "3 updates available.")
        self.assertEqual(errors.list_summary(0, 1), "1 addon isn't installed yet.")
        self.assertEqual(errors.list_summary(0, 2), "2 addons aren't installed yet.")
        self.assertEqual(errors.list_summary(2, 1),
                         "2 updates available. 1 addon isn't installed yet.")

    def test_restore_texts_point_to_backup(self):
        self.assertIn("Backup > Restore", fileinstall_ui.SAFETY_NOTE)
        from am import installer
        text = fileinstall_ui.message(installer.InstallError("safety"))
        self.assertIn("Backup window", text)
        self.assertNotIn("Settings", text)

    def test_parse_geometry(self):
        self.assertEqual(ui._parse_geometry("700x500+10+20"), (700, 500, 10, 20))
        self.assertEqual(ui._parse_geometry("700x500+-1900+-5"), (700, 500, -1900, -5))
        for bad in (None, "", "700x500", "x+1+2", "700x500+1+2; rm", 5):
            self.assertIsNone(ui._parse_geometry(bad))


class WindowExtrasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("small_fixes_ui_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir("small_fixes_ui_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
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

    def pump(self, app, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while app.busy and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        for _ in range(5):
            self.root.update()
            time.sleep(0.02)

    def make_app(self, wow=True, **kw):
        return ui.App(self.root, os.path.join(self.site, "manifest.json"), True,
                      self.root_dir if wow else None, check_game=lambda: False,
                      detect=lambda: [], **kw)

    def cells(self, app, folder):
        i = [r.folder for r in app.rows].index(folder)
        return app._cells[i]

    # ---------- texts on the main window ----------
    def test_summary_after_check(self):
        app = self.make_app()
        self.pump(app)   # Taxi Time outdated, Guild Hub missing
        self.assertEqual(app.message_var.get(),
                         "1 update available. 1 addon isn't installed yet.")
        app.all_button.invoke()
        self.pump(app)
        app.check_again()
        self.pump(app)
        self.assertEqual(app.message_var.get(), "All addons are up to date.")

    def test_problem_tooltip_has_the_reason(self):
        app = self.make_app()
        self.pump(app)
        app._apply_manifest(app.manifest, None,
                            {"TaxiTime": github.ReleaseError("offline")})
        app.render()
        cells = self.cells(app, "TaxiTime")
        self.assertEqual(cells["status"].cget("text"), "Can't check now")
        self.assertIn("Can't reach the internet", cells["status_tip"].text)
        self.assertEqual(self.cells(app, "GuildHub")["status_tip"].text, "")

    def test_failure_keeps_the_successes(self):
        app = self.make_app()
        self.pump(app)
        app._row("TaxiTime").state = "queued"
        app._handle(("error", "TaxiTime", "Taxi Time couldn't be downloaded."))
        app._handle(("finished", ["Guild Hub"]))
        self.assertEqual(app.message_var.get(),
                         "Installed: Guild Hub.  Taxi Time couldn't be downloaded.")

    def test_greyed_buttons_say_why(self):
        app = self.make_app()
        self.pump(app)
        self.assertEqual(app.all_tip.text, "")
        self.assertEqual(app.backup_tip.text, "")
        app.busy = True
        app.render()
        self.assertEqual(app.all_tip.text, errors.BUSY_TIP)
        self.assertEqual(app.backup_tip.text, errors.BUSY_TIP)
        app.busy = False
        app.render()
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(app.all_tip.text, errors.NOTHING_TO_DO_TIP)

    def test_greyed_backup_without_folder(self):
        app = self.make_app(wow=False)
        self.pump(app)
        self.assertEqual(app.backup_tip.text, errors.NO_FOLDER_TIP)
        self.assertEqual(app.all_tip.text, errors.NO_FOLDER_TIP)

    # ---------- keys ----------
    def test_enter_answers_only_safe_questions(self):
        class Win:
            def __init__(self):
                self.keys = {}

            def bind(self, key, fn):
                self.keys[key] = fn
        for enter, want in ((False, [False]), (True, [True, True, False])):
            got, win = [], Win()
            ui._confirm_keys(win, got.append, enter)
            for key in ("<Return>", "<KP_Enter>", "<Escape>"):
                win.keys[key](None)
            self.assertEqual(got, want, enter)

    def confirm_parts(self, app, enter):
        got = []
        parts = app._confirm_parts("Remove Taxi Time?", "Text", "Remove", "Cancel", enter,
                                   got.append)
        return parts, got

    def space(self, button):
        """What Space does to the focused button (ttk's own TButton key binding)."""
        self.root.tk.call("ttk::button::activate", str(button))

    def test_remove_question_space_and_enter_do_nothing(self):
        """Remove/Delete questions (enter=False): Cancel has the focus, so Space cancels
        and never removes; Enter does nothing; OK only after tabbing to it."""
        app = self.make_app()
        self.pump(app)
        parts, got = self.confirm_parts(app, enter=False)
        self.assertIs(parts["focus"], parts["cancel"])
        self.assertIsNot(parts["focus"], parts["ok"])
        for key in ("<Return>", "<KP_Enter>"):
            self.assertIn("break", parts["win"].bind(key))
        self.space(parts["focus"])    # a stray Space
        self.assertEqual(got, [False])
        parts["win"].destroy()

    def test_safe_question_keeps_ok_focused(self):
        app = self.make_app()
        self.pump(app)
        parts, got = self.confirm_parts(app, enter=True)
        self.assertIs(parts["focus"], parts["ok"])
        self.space(parts["focus"])
        self.assertEqual(got, [True])

    def test_restored_size_capped_to_screen(self):
        app = self.make_app()
        self.pump(app)
        config.save_settings({"window": "3000x2000+0+0"})
        placed = []
        root = self.root
        with mock.patch.object(ui, "_on_screen", lambda *a: True),                 mock.patch.object(root, "winfo_screenwidth", lambda: 1280),                 mock.patch.object(root, "winfo_screenheight", lambda: 800),                 mock.patch.object(root, "geometry", lambda g=None: placed.append(g)):
            app.restore_geometry()
        self.assertEqual(placed, [f"1280x{800 - ui.SCREEN_MARGIN}+0+0"])

    def test_go_back_asks_with_enter(self):
        app = self.make_app()
        self.pump(app)
        seen = []
        app._confirm_dialog = lambda *a, **kw: (seen.append(kw), False)[1]
        app.confirm = app._confirm_dialog
        self.assertFalse(app.ask("T", "X", "OK", "No", enter=True))
        self.assertEqual(seen, [{"enter": True}])

    def test_remove_question_never_on_enter(self):
        """remove_row asks through confirm without enter (Enter stays off)."""
        asked = []
        app = self.make_app(confirm=lambda *a, **kw: (asked.append(kw), False)[1])
        self.pump(app)
        app.remove_row(app._row("TaxiTime"))
        self.pump(app)
        self.assertEqual(asked, [{}])

    def test_ask_gives_fakes_four_arguments(self):
        got = []
        app = self.make_app(confirm=lambda *a: (got.append(a), True)[1])
        self.pump(app)
        self.assertTrue(app.ask("T", "X", "OK", "No", enter=True))
        self.assertEqual(got, [("T", "X", "OK", "No")])

    def test_f5_checks_again(self):
        app = self.make_app()
        self.pump(app)
        calls = []
        app.check_again = lambda *a, **k: calls.append(1)
        for key in ("<F5>", "<Control-r>", "<Control-R>"):
            self.assertIn("_check_key", self.root.bind(key))
        app._check_key()
        app._check_key(None)
        self.assertEqual(len(calls), 2)

    # ---------- layout and the window's place ----------
    def test_bottom_block_packed_before_the_list(self):
        app = self.make_app()
        self.pump(app)
        slaves = app.outer.pack_slaves()
        bottom = app.progress.master
        self.assertLess(slaves.index(bottom), slaves.index(app.list_box))
        self.assertEqual(str(bottom.pack_info()["side"]), "bottom")

    def test_window_grows_once_to_fit(self):
        app = self.make_app()
        self.pump(app)
        sizes = []
        with mock.patch.object(self.root, "winfo_reqheight", lambda: 900), \
                mock.patch.object(self.root, "winfo_screenheight", lambda: 800), \
                mock.patch.object(self.root, "winfo_ismapped", lambda: 0), \
                mock.patch.object(self.root, "geometry", lambda g=None: sizes.append(g)):
            app._need_h = 0
            app._fit_height()
            app._fit_height()   # same need: nothing again
        # the start width is scaled with the display (Linux X servers often report a DPI
        # a bit over 96, so 680 becomes e.g. 709 there)
        self.assertEqual(sizes, [f"{app._size[0]}x{800 - ui.SCREEN_MARGIN}"])
        self.assertEqual(app._size[0], theme.px(ui.START_WIDTH))
        sizes.clear()
        with mock.patch.object(self.root, "winfo_reqheight", lambda: 300), \
                mock.patch.object(self.root, "winfo_ismapped", lambda: 0), \
                mock.patch.object(self.root, "geometry", lambda g=None: sizes.append(g)):
            app._fit_height()   # needs less: never shrinks
        self.assertEqual(sizes, [])

    def test_window_place_remembered(self):
        app = self.make_app()
        self.pump(app)
        app.remember_window = True
        root = self.root
        with mock.patch.object(root, "state", lambda *a: "normal"), \
                mock.patch.object(root, "winfo_ismapped", lambda: 1), \
                mock.patch.object(root, "winfo_width", lambda: 700), \
                mock.patch.object(root, "winfo_height", lambda: 500), \
                mock.patch.object(root, "winfo_x", lambda: -1500), \
                mock.patch.object(root, "winfo_y", lambda: 40):
            app._save_geometry()
        self.assertEqual(config.load_settings()["window"], "700x500+-1500+40")
        placed = []
        with mock.patch.object(ui, "_on_screen", lambda *a: True), \
                mock.patch.object(root, "geometry", lambda g=None: placed.append(g)):
            app.restore_geometry()
        self.assertEqual(placed, ["700x500+-1500+40"])
        placed.clear()
        with mock.patch.object(ui, "_on_screen", lambda *a: False), \
                mock.patch.object(root, "geometry", lambda g=None: placed.append(g)):
            app.restore_geometry()   # that screen is gone: default place
        self.assertEqual(placed, [])
        config.save_settings({"window": "200x100+0+0"})   # smaller than allowed
        with mock.patch.object(ui, "_on_screen", lambda *a: True), \
                mock.patch.object(root, "geometry", lambda g=None: placed.append(g)):
            app.restore_geometry()
        self.assertEqual(placed, [])

    def test_hidden_or_test_window_saves_nothing(self):
        app = self.make_app()
        self.pump(app)
        app.close()
        self.assertNotIn("window", config.load_settings())

    def test_on_screen_uses_the_monitor_check(self):
        seen = []
        ok = ui._on_screen(self.root, 100, 50, 700,
                           monitor_at=lambda *r: (seen.append(r), 0)[1])
        self.assertFalse(ok)
        self.assertEqual(seen, [(140, 50, 760, 80)])
        self.assertTrue(ui._on_screen(self.root, 0, 0, 700, monitor_at=lambda *r: 12345))

    # ---------- menus ----------
    def test_folder_menu_opens_folders(self):
        app = self.make_app()
        self.pump(app)
        opened = []
        app.open_folder = opened.append
        labels = [app.folder_menu.entrycget(i, "label") for i in range(3)]
        self.assertEqual(labels, ["Copy", "Open AddOns folder", "Open log folder"])
        app.open_addons_folder()
        os.makedirs(os.path.dirname(config.log_path()), exist_ok=True)
        app.open_log_folder()
        self.assertEqual(opened, [app.addons_dir, os.path.dirname(config.log_path())])

    def test_press_and_hold_opens_the_menu(self):
        label = ttk.Label(self.root, text="x")
        got, jobs, cancelled = [], [], []
        label.after = lambda ms, fn: (jobs.append((ms, fn)), "job1")[1]
        label.after_cancel = cancelled.append
        hold = touch.bind_menu(label, got.append, long_press=True)
        hold.press(Event(100, 100))
        self.assertEqual(jobs[0][0], touch.HOLD_MS)
        jobs[0][1]()   # held long enough
        self.assertEqual([(e.x_root, e.y_root) for e in got], [(100, 100)])
        hold.press(Event(100, 100))
        hold.cancel()   # let go early
        self.assertEqual(cancelled, ["job1"])
        hold.press(Event(100, 100))
        hold.move(Event(130, 100))   # a drag, not a hold
        self.assertEqual(cancelled, ["job1", "job1"])
        self.assertTrue(label.bind("<Button-3>"))

    def test_no_press_and_hold_when_off(self):
        label = ttk.Label(self.root, text="x")
        self.assertIsNone(touch.bind_menu(label, lambda e: None, long_press=False))
        self.assertTrue(label.bind("<Button-3>"))
        self.assertFalse(label.bind("<ButtonPress-1>"))

    def test_wheel_bound_on_every_row(self):
        canvas = tk.Canvas(self.root)
        holder = ttk.Frame(canvas)
        label = ttk.Label(holder, text="row")
        fileinstall_ui._wheel(canvas, canvas)
        for w in (canvas, holder, label):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.assertTrue(w.bind(seq), (w, seq))


if __name__ == "__main__":
    unittest.main()
