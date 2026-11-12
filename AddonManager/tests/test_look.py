"""The look (new-look job): the window frame and knot corners, the art per display
scaling, the heading font, every window's buttons (text never cut, same kind = same
width, one height per row, the main window's right edges), nothing under a knot, and the
scrolling list with its fixed headers. Hidden windows, fake data; never shown."""
import datetime
import gc
import os
import re
import time
import tkinter as tk
import tkinter.font as tkfont
import unittest
import zipfile
from tkinter import ttk
from unittest import mock

import tests  # noqa: F401  (temp home + documents)
from am import backup, config, errors, fileinstall, fileinstall_ui, share, theme, ui
from tests import fixtures


def all_widgets(w):
    out = [w]
    for c in w.winfo_children():
        out += all_widgets(c)
    return out


def x_in(widget, top):
    """widget's x inside top (works on a withdrawn window: sums the parents' x)."""
    x = 0
    while widget is not top and widget is not None:
        x += widget.winfo_x()
        widget = widget.master
    return x


def y_in(widget, top):
    y = 0
    while widget is not top and widget is not None:
        y += widget.winfo_y()
        widget = widget.master
    return y


def shown(widget):
    """Packed/gridded/placed all the way up (the windows stay withdrawn, so winfo_ismapped
    is always false)."""
    w = widget
    while w is not None and w.winfo_class() not in ("Tk", "Toplevel"):
        if not w.winfo_manager():
            return False
        w = w.master
    return True


def width(widget):
    """Its real width; a withdrawn dialog is never laid out, so there its requested one
    (the off-screen renders check the real sizes of every window)."""
    return widget.winfo_width() if widget.winfo_width() > 1 else widget.winfo_reqwidth()


def height(widget):
    return widget.winfo_height() if widget.winfo_height() > 1 else widget.winfo_reqheight()


def visible(widget, top):
    """widget's rectangle inside top, cut by its parents (the list's clipping frame)."""
    x, y = x_in(widget, top), y_in(widget, top)
    x2, y2 = x + widget.winfo_width(), y + widget.winfo_height()
    p = widget.master
    while p is not top and p is not None:
        px_, py_ = x_in(p, top), y_in(p, top)
        x, y = max(x, px_), max(y, py_)
        x2, y2 = min(x2, px_ + p.winfo_width()), min(y2, py_ + p.winfo_height())
        p = p.master
    return x, y, x2 - x, y2 - y


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()
        gc.collect()

    def test_art_step(self):
        self.assertEqual(theme.art_step(1.0), (1, "1x"))
        self.assertEqual(theme.art_step(1.25), (1.5, "1_5x"))
        self.assertEqual(theme.art_step(1.5), (1.5, "1_5x"))
        self.assertEqual(theme.art_step(2.0), (2, "2x"))
        for step in ("1x", "1_5x", "2x"):
            for corner in theme.CORNERS:
                path = os.path.join(theme.FRAME_ART, theme.CORNER_NAME.format(
                    corner=corner, step=step))
                self.assertTrue(os.path.isfile(path), path)

    def test_head_font_falls_back(self):
        with mock.patch.object(theme, "HEAD_CHOICES", ("No Such Font 123",)):
            theme.apply(self.root)
            self.assertEqual(theme.head_font()[0], theme.FONT_FAMILY)
        theme.apply(self.root)
        if os.name == "nt":
            self.assertEqual(theme.HEAD_FAMILY, "Georgia")

    def test_frame_and_missing_art(self):
        theme.apply(self.root)
        content = ttk.Frame(self.root)
        content.pack()
        art = theme.Frame(self.root, content, corners=True)
        self.assertEqual(sorted(art.labels), sorted(theme.CORNERS))
        self.assertGreater(art.clear, 0)
        # the canvas is under everything
        self.assertEqual(self.root.tk.call("winfo", "children", self.root)[0], str(art.canvas))
        top = tk.Toplevel(self.root)
        top.withdraw()
        other = ttk.Frame(top)
        other.pack()
        with mock.patch.object(theme, "FRAME_ART", os.path.join(fixtures.temp_dir("look_"), "x")):
            art2 = theme.Frame(top, other, corners=True)   # no art: lines only, no crash
        self.assertEqual(art2.labels, {})
        self.assertEqual(art2.clear, 0)
        art2.draw()

    def test_focus_and_disabled_colours(self):
        theme.apply(self.root)
        s = ttk.Style(self.root)
        for style in ("TButton", "Accent.TButton", "Item.TButton", "Accent.Item.TButton"):
            self.assertEqual(s.lookup(style, "bordercolor", ["focus"]), theme.FOCUS, style)
        for style in ("TCheckbutton", "TRadiobutton"):
            self.assertEqual(s.lookup(style, "focuscolor"), theme.FOCUS)
            # a greyed tick is grey too (not the bright gold of a clickable one)
            self.assertEqual(s.lookup(style, "indicatorforeground", ["disabled"]),
                             theme.DISABLED)
        self.assertEqual(theme.DISABLED, "#7a705e")
        # red buttons: a plain gold border at rest, so the bright focus colour stands out
        for style in ("Accent.TButton", "Accent.Item.TButton"):
            self.assertEqual(s.lookup(style, "bordercolor"), theme.BORDER)


class LookWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir("look_ui_")
        cls.site = fixtures.make_server_files(cls.base)
        many = os.path.join(cls.base, "many")
        os.makedirs(many)
        entries = []
        for i in range(25):
            folder = f"Addon{i:02d}"
            z = fixtures.make_zip(os.path.join(many, f"{folder}-1.0.{i}.zip"), folder, f"1.0.{i}")
            entries.append({"folder": folder, "name": f"Addon {i:02d}", "description": "x",
                            "version": f"1.0.{i}", "url": os.path.basename(z),
                            "sha256": fixtures.sha256(z), "size": os.path.getsize(z)})
        fixtures.write_manifest(os.path.join(many, "manifest.json"), entries)
        cls.many = many

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir("look_ui_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        fixtures.make_addon_folder(self.addons, "TaxiTime", "1.2.0")   # an update
        fixtures.make_addon_folder(self.addons, "Bagnon", "10.2.7")
        config.save_settings({"file_addons": {"Bagnon": {
            "name": "Bagnon", "version": "10.2.7", "file": "Bagnon.zip", "date": "2026-11-01"}}})
        self.root = tk.Tk()
        self.root.withdraw()
        self.base_scaling = float(self.root.tk.call("tk", "scaling"))

    def tearDown(self):
        try:
            self.root.tk.call("tk", "scaling", self.base_scaling)
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        theme.SCALE = 1.0
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

    def make_app(self, manifest=None):
        app = ui.App(self.root, manifest or os.path.join(self.site, "manifest.json"), True,
                     self.root_dir, check_game=lambda: False, detect=lambda: [],
                     confirm=lambda *a: False, share_url="https://relay.invalid")
        self.pump(app)
        self.root.update_idletasks()
        return app

    # ---------- buttons ----------
    def check_buttons(self, win, name):
        """Every button: its text fits; returns the mapped buttons."""
        win.update_idletasks()
        st = ttk.Style(win)
        out = []
        for w in all_widgets(win):
            if w.winfo_class() not in ("TButton", "TMenubutton") or not shown(w):
                continue
            style = str(w.cget("style")) or w.winfo_class()
            f = tkfont.Font(root=win, font=st.lookup(style, "font") or "TkDefaultFont")
            pad = st.lookup(style, "padding")
            nums = re.findall(r"\d+", str(pad))
            padx = int(nums[0]) if nums else 0
            need = f.measure(str(w.cget("text"))) + 2 * padx
            self.assertLessEqual(need, width(w), f"{name}: {w.cget('text')!r} is cut")
            out.append(w)
        return out

    def check_row(self, buttons, name):
        """Buttons side by side in one row: one width and one height."""
        buttons = [b for b in buttons if shown(b)]
        if len(buttons) < 2:
            return
        if all(b.winfo_width() <= 1 and b.winfo_manager() == "grid"
               and {"e", "w"} <= set(str(b.grid_info()["sticky"])) for b in buttons):
            return   # not laid out, stretched in equal columns (the renders check these)
        self.assertEqual(len({width(b) for b in buttons}), 1,
                         f"{name}: {[(b.cget('text'), width(b)) for b in buttons]}")
        self.assertEqual(len({height(b) for b in buttons}), 1, name)

    def check_dialog(self, win, name):
        self.check_height(win, name)   # every dialog fits a small screen too
        buttons = self.check_buttons(win, name)
        self.assertTrue(buttons, name)
        rows = {}
        for b in buttons:   # side by side: one grid row, or packed left/right
            how = b.winfo_manager()
            if how == "grid":
                key = (str(b.master), "row", str(b.grid_info()["row"]))
            elif how == "pack" and str(b.pack_info()["side"]) in ("left", "right"):
                key = (str(b.master), "side")
            else:
                key = (str(b),)
            rows.setdefault(key, []).append(b)
        for group in rows.values():
            if all(isinstance(b, ttk.Button) for b in group):
                self.check_row(group, name)
        row_w = theme.button_chars(win)
        for b in buttons:
            if isinstance(b, ttk.Button) and str(b.cget("style")) not in ("Row.TButton",
                                                                          "Cloud.TButton"):
                probe = ttk.Button(b.master, text="", width=row_w, style=b.cget("style"))
                least = probe.winfo_reqwidth()
                probe.destroy()
                self.assertGreaterEqual(max(width(b), b.winfo_reqwidth()), least,
                                        f"{name}: {b.cget('text')!r} narrower than a row button")

    # The tallest a window may be (its content, no title bar): a Steam Deck screen is 800 px
    # (Linux stand-in at 100 %: 760), a 1080p laptop at 150 % about 1000 px (the Linux
    # stand-in at 150 %: 960, see test_buttons_linux_fonts_150). None = no check.
    height_limit = 760

    def check_height(self, win, name):
        if self.height_limit is None:
            return
        win.update_idletasks()
        self.assertLessEqual(win.winfo_reqheight(), self.height_limit,
                             f"{name}: too tall for a small screen")

    def check_knots(self, win, art, name):
        art.draw()
        win.update_idletasks()
        squares = [(x_in(lab, win), y_in(lab, win), lab.winfo_reqwidth(), lab.winfo_reqheight())
                   for lab in art.labels.values()]
        for w in all_widgets(win):
            cls = w.winfo_class()
            if cls not in ("TLabel", "Label", "TButton", "TEntry", "TProgressbar",
                           "TCheckbutton", "TRadiobutton", "TMenubutton") or not shown(w):
                continue
            if w in art.labels.values():
                continue
            if cls in ("TLabel", "Label") and not (str(w.cget("text")) or str(w.cget("image"))
                                                  or str(w.cget("textvariable"))):
                continue
            x, y, ww, hh = visible(w, win)
            if ww <= 0 or hh <= 0:
                continue
            for sx, sy, sw, sh in squares:
                self.assertFalse(x < sx + sw and sx < x + ww and y < sy + sh and sy < y + hh,
                                 f"{name}: {w} under a knot"
                                 )

    def run_checks(self):
        app = self.make_app()
        app.update_mode = "offer"
        from am import selfupdate
        app.app_update = selfupdate.Update("0.99.0", "v0.99.0", "https://x", "0" * 64, 1)
        app.render()
        self.root.geometry(f"{theme.px(680)}x{theme.px(560)}")
        self.root.update_idletasks()
        root = self.root
        buttons = self.check_buttons(root, "main")
        # bottom row: one width, one height
        self.check_row([app.check_button, app.all_button, app.backup_main_button,
                        app.settings_button], "bottom row")
        # one column: Change..., Add file..., Later and the row buttons, same width and
        # the same right edge
        column = [app.folder_button, app.files.button, app.update_later_button]
        column += [w[1] for w in app.row_widgets.values() if w[1] is not None]
        column += [c["button"] for c in app.files.cells]
        rights = {x_in(b, root) + b.winfo_width() for b in column}
        widths = {b.winfo_width() for b in column}
        self.assertEqual(len(widths), 1, [(b.cget("text"), b.winfo_width()) for b in column])
        self.assertEqual(len(rights), 1, [(b.cget("text"), x_in(b, root) + b.winfo_width())
                                          for b in column])
        # row buttons one height; Update now as tall as Later
        rows = [w[1] for w in app.row_widgets.values() if w[1] is not None]
        self.assertEqual(len({b.winfo_height() for b in rows}), 1)
        self.assertEqual(app.update_now_button.winfo_height(),
                         app.update_later_button.winfo_height())
        self.assertTrue(buttons)
        self.check_knots(root, app.frame_art, "main")
        # the banner title fits
        title = app.title_label
        self.assertLessEqual(x_in(title, root) + title.winfo_reqwidth(),
                             root.winfo_width() - app.frame_art.clear)
        # Settings
        win = app.open_settings(show=False)
        self.check_dialog(win, "settings")
        app.close_settings()
        # Backup window and its small windows
        hub = app.open_backup_window(show=False)
        self.check_dialog(hub.win, "backup window")
        self.check_knots(hub.win, hub.win._am_frame, "backup window")
        # a backup found: Open folder + Old backups... + Close fit, one size, <= 420 px
        now = time.time()
        found = [backup.FoundBackup(os.path.join(self.work, f"AddonManager backup 2026-1{i}-01.zip"),
                                    "OneDrive" if i else "Documents", (i + 1) * 300 * 2 ** 20,
                                    now - i * 86400 * 30, False, i == 2) for i in range(3)]
        found.append(backup.FoundBackup(os.path.join(self.work, "Before restore 2026-09-01 1200.zip"),
                                        "Documents", 2 ** 20, now - 86400 * 90, True, False))
        hub.handle(("backup_last", hub._looking, found[0], True))
        self.assertEqual(hub.old_button.winfo_manager(), "grid")
        self.check_dialog(hub.win, "backup window (found)")
        self.check_knots(hub.win, hub.win._am_frame, "backup window (found)")
        self.assertLessEqual(hub.win.winfo_reqwidth(), theme.px(420), "backup window width")
        # Delete old backups, with backups listed and two ticked
        d = hub.open_old()
        d.handle(("backup_delete_found", d._token, found, [], {"OneDrive"}))
        d.safety_var.set(True)
        d.toggle_safety()
        d.tick_but_newest()
        self.assertTrue(d.ticked())
        self.check_dialog(d.win, "delete backups")
        self.check_knots(d.win, d.win._am_frame, "delete backups")
        d.win.update_idletasks()
        self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "delete window width")
        self.assertEqual(d.delete_button.winfo_reqwidth(), d.close_button.winfo_reqwidth())
        d.close()
        for opener, name in ((lambda: app.open_backup(show=False), "make backup"),
                             (lambda: app.open_restore(show=False), "restore list"),
                             (lambda: app.open_send(show=False), "send"),
                             (lambda: app.open_receive(show=False), "receive")):
            d = opener()
            self.check_dialog(d.win, name)
            d.win.update_idletasks()
            self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), f"{name} width")
            if name in ("make backup", "send"):   # WoW open: the WoW-open strip fits too
                d.show_game(True)
                self.assertEqual(d.game_label.winfo_manager(), "pack")
                self.check_dialog(d.win, f"{name} (WoW open)")
                d.win.update_idletasks()
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), f"{name} WoW open")
                lab = d.game_label
                self.assertLessEqual(lab.winfo_reqwidth(), d.frame.winfo_reqwidth(),
                                     f"{name}: WoW-open strip cut")
                self.check_height(d.win, f"{name} (WoW open)")
            if name == "make backup":   # saved: Open folder joins the row, still <= 420 px
                d.made_with_game = True
                d.handle(("backup_done", os.path.join(self.work, "AddonManager backup x.zip"),
                          41 * 2 ** 20, ["WTF/Account/A/SavedVariables/WeakAuras.lua"]))
                self.check_dialog(d.win, "make backup (saved)")
                d.win.update_idletasks()
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "make backup saved")
            if name == "restore list":   # the safety copies tick wraps
                d.safety_var.set(True)
                d.render()
                d.win.update_idletasks()
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "restore list")
            if name == "restore list" and d.code_button is not None:
                # a clear gap between Use a code... and Cancel; the note wraps to the window
                pad = re.findall(r"\d+", str(d.cancel_button.pack_info()["padx"]))
                self.assertGreaterEqual(int(pad[0]), theme.px(12))
                self.assertEqual(d.code_button.pack_info()["side"], "left")
                self.assertTrue(d.note.bind("<Configure>"))
            if name == "send":
                # after an error: Try again fits, same width as Get code, same row width
                d.win.update_idletasks()
                before = (width(d.go_button), d.go_button.winfo_reqwidth())
                d.active = True          # a try that ends with an error
                d.handle(("share_error", d.tag, errors.share_message(
                    share.ShareError("offline")), True, None))
                self.assertEqual(str(d.go_button.cget("text")), errors.SEND_TRY_AGAIN)
                self.check_dialog(d.win, "send (try again)")
                d.win.update_idletasks()
                self.assertEqual((width(d.go_button), d.go_button.winfo_reqwidth()), before)
                self.assertEqual(width(d.go_button), width(d.cancel_button))
                # file.kiwi tick + Upload right away on, then the uploading-now status:
                # the sub-tick's text fits, the window stays <= 420 px
                d.later_var.set(True)
                d.now_var.set(True)
                d._later_state()
                d.win.update_idletasks()
                self.check_dialog(d.win, "send (upload right away)")
                lab = d.now_label
                self.assertLessEqual(x_in(lab, d.win) + lab.winfo_reqwidth(),
                                     width(d.win), "upload right away text cut")
                self.assertLessEqual(lab.winfo_reqwidth(), int(lab.cget("wraplength")) + 2)
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "send width")
                # Try again: "What to send" stays one line with Change...; the file.kiwi
                # note shows (it is shown before every try)
                self.assertTrue(d.what_row.winfo_manager())
                self.assertTrue(d.change_link.winfo_manager())
                self.assertTrue(d.later_rest.winfo_manager())
                self.check_height(d.win, "send (ticks on, after an error)")
                # Change...: every choice open again, ticks on, WoW open, error still shown
                d.unfold()
                self.assertTrue(d.what_box.winfo_manager())
                self.assertFalse(d.what_row.winfo_manager())
                d.win.update_idletasks()
                self.check_dialog(d.win, "send (choices open after an error)")
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "send width (open)")
                self.check_height(d.win, "send (choices open, ticks on, after an error)")
                d.active = True
                d.later = d.later_choice()
                d._fold(True)            # what start() does: the choices fold to one line
                self.assertTrue(d.what_row.winfo_manager())
                self.assertFalse(d.change_link.winfo_manager())
                self.assertFalse(d.what_box.winfo_manager())
                d.show_code(share.format_code(37, share.new_words()), 600)
                d.handle(("share_later", d.tag, "uploading", "now"))
                self.assertEqual(d.status_var.get(), errors.LATER_UPLOADING_NOW)
                d.win.update_idletasks()
                self.check_dialog(d.win, "send (uploading now)")
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "send width (now)")
                self.check_height(d.win, "send (code shown, uploading now)")
                # the normal countdown ("uploaded for later" if not joined) wraps beside Copy
                d.later = {"hours": 12}
                d.ends_var.set(d._ends_text(299))
                d.win.update_idletasks()
                self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "send width (later)")
                self.check_height(d.win, "send (code shown, countdown)")
                d.active = False
            d.close()
        lic = app.open_licences(show=False)
        lic.win.update_idletasks()
        self.assertEqual(str(lic.vbar.cget("style")), "Gold.Vertical.TScrollbar")
        self.assertEqual(str(lic.hbar.cget("style")), "Gold.Horizontal.TScrollbar")
        lic.close()
        made = backup.make(self.addons, os.path.join(self.work, "made"),
                           now=datetime.datetime(2026, 11, 9, 21, 14))[0]
        d = app.open_restore(show=False, path=made)
        self.check_dialog(d.win, "restore")
        d.close()
        from am import backup_ui
        d = app.backup_dialog = backup_ui.RestoreDialog(app, backup.read(made), show=False,
                                                        back=True)
        self.check_dialog(d.win, "restore (with Back)")
        d.win.update_idletasks()
        self.assertLessEqual(d.win.winfo_reqwidth(), theme.px(420), "restore with Back")
        d.close()
        app.close_backup_window()
        # the yes/no question
        parts = app._confirm_parts("Uninstall Taxi Time?", "text", errors.REMOVE_OK,
                                   errors.REMOVE_CANCEL, False, lambda ok: None)
        self.check_dialog(parts["win"], "confirm")
        parts["win"].destroy()
        # Add from file
        pack = os.path.join(self.work, "pack.zip")
        with zipfile.ZipFile(pack, "w") as z:
            for folder, ver in (("WeakAuras", "5.12.0"), ("Bagnon", "10.2.8")):
                z.writestr(f"{folder}/{folder}.toc", fixtures.toc_text(folder, ver))
                z.writestr(f"{folder}/{folder}.lua", "-- x")
        plan = fileinstall.scan(pack, self.addons, sorted(app._guild_folders()),
                                app.files.tracked(), tmp_root=self.work)
        fd = fileinstall_ui.FileInstallDialog(root, plan, lambda *a: None, show=False)
        self.check_dialog(fd.win, "file install")
        # no empty status line above the buttons; it shows once it has text
        self.assertFalse(fd.status.winfo_manager())
        fd.status_var.set("x")
        self.assertEqual(fd.status.winfo_manager(), "pack")
        fd.status_var.set("")
        self.assertFalse(fd.status.winfo_manager())
        fd.close()
        app.closed = True

    def test_buttons_100(self):
        self.run_checks()

    def test_buttons_150(self):
        self.root.tk.call("tk", "scaling", self.base_scaling * 1.5)
        self.height_limit = 1000
        self.run_checks()

    def linux_on_windows(self):
        """Linux simulated on Windows: Linux constants, slim bottom row and Verdana 10
        (the metrics of DejaVu Sans, the font the Linux build's CI has) with a DejaVu-like
        serif, at the CI's display scaling (1.043). Measured: this gives the CI's widths
        to within 1-2 px (Segoe UI x 1.2 was 50+ px too narrow)."""
        from am import backup_ui, share_ui
        families = set(tkfont.families(self.root))
        family = "Verdana" if "Verdana" in families else theme.FONT_FAMILY
        # Every import-time Linux value (the real Linux numbers), so the windows are
        # measured as they are built there.
        patches = [mock.patch.object(ui, "LINUX_SLIM_BUTTONS", True),
                   mock.patch.object(ui, "SETTINGS_BUTTON", "Settings"),
                   mock.patch.object(ui, "BETA_MAIN_TEXT", "Betas"),
                   mock.patch.object(ui, "COL_GAP", ui.LINUX_COL_GAP),
                   mock.patch.object(ui, "VERSION_SAMPLE", ui.LINUX_VERSION_SAMPLE),
                   mock.patch.object(backup_ui, "LINUX", True),
                   mock.patch.object(backup_ui, "WRAP", 310),
                   mock.patch.object(share_ui, "WRAP", 310),
                   mock.patch.object(backup_ui, "PLACE_MAX", 12),
                   mock.patch.object(backup_ui, "MENU_CHARS", 12),
                   mock.patch.object(backup_ui, "ACCOUNT_WRAP", 160),
                   mock.patch.object(backup_ui, "NOTICES_CHARS", 40),
                   mock.patch.object(fileinstall_ui, "LINUX", True),
                   mock.patch.object(fileinstall_ui, "WRAP", 330),
                   mock.patch.object(fileinstall_ui, "ROW_HEIGHT", 40),
                   mock.patch.object(share_ui, "CODE_SIZE", 15),
                   mock.patch.object(theme, "FONT_FAMILY", family),
                   mock.patch.object(theme, "FONT_SIZE", 10),
                   mock.patch.object(theme, "BUTTON_FAMILY", None),
                   mock.patch.object(theme, "HEAD_CHOICES", ("DejaVu Serif", "Georgia"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return 1.043

    def test_buttons_linux_fonts(self):
        """Linux fonts (on Windows: simulated, see linux_on_windows)."""
        if os.name == "nt":
            self.root.tk.call("tk", "scaling", self.base_scaling * self.linux_on_windows())
        self.run_checks()

    def test_buttons_linux_fonts_150(self):
        """Linux fonts at 150 % (the CI's 150 % is 1.56)."""
        factor = self.linux_on_windows() * 1.5 if os.name == "nt" else 1.5
        self.root.tk.call("tk", "scaling", self.base_scaling * factor)
        # The real Linux CI runs test_buttons_150 with these fonts against 1000 px; the
        # stand-in measured 1030 where the CI measured 1028, so 960 here keeps a margin.
        self.height_limit = 960
        self.run_checks()

    # ---------- the scrolling list ----------
    def test_list_scrolls_with_fixed_headers(self):
        def right_gap(app, button):
            box = app.list_box
            return (x_in(box, self.root) + box.winfo_width()
                    - x_in(button, self.root) - button.winfo_width())

        # Few addons: everything fits, no bar.
        few = self.make_app()
        self.assertFalse(few.list_bar.winfo_manager(), "all rows fit: no bar")
        gap_open = right_gap(few, few.row_widgets["TaxiTime"][1])
        few.closed = True
        for child in self.root.winfo_children():
            child.destroy()
        # 25 addons on a Steam Deck sized screen (800 px tall: the window may grow to
        # 720 px, not more): the bar shows, the buttons keep their place. (Without the
        # fixed screen, a big test X server lets the window grow until everything fits.)
        screen = mock.patch.object(self.root, "winfo_screenheight", lambda: 800)
        screen.start()
        self.addCleanup(screen.stop)
        app = self.make_app(os.path.join(self.many, "manifest.json"))
        self.assertLessEqual(app._size[1], 800 - ui.SCREEN_MARGIN)
        root = self.root
        app._list_resized()
        root.update_idletasks()
        self.assertEqual(app.list_bar.winfo_manager(), "grid")
        self.assertEqual(right_gap(app, app.row_widgets["Addon00"][1]), gap_open)
        # a row at a time: wheel (Windows), Button-5 (Linux), the bar's arrows/moveto
        step = app._row_h
        self.assertGreater(step, 0)
        label = app._cells[3]["name"]
        label.event_generate("<MouseWheel>", delta=-120)
        self.assertEqual(app._list_top, step)
        label.event_generate("<Button-5>")
        self.assertEqual(app._list_top, 2 * step)
        label.event_generate("<MouseWheel>", delta=120)
        self.assertEqual(app._list_top, step)
        app.list_yview("moveto", 1.0)
        self.assertEqual(app._list_top % step, 0)
        h, seen, _step, most = app._list_sizes()
        self.assertEqual(app._list_top, most)
        self.assertLess(most - (h - seen), step)   # less than a row of space at the end
        # whole rows only: header + n rows, the rest an empty strip at the bottom
        self.assertEqual((seen - app._head_h()) % step, 0)
        spare = app.list_view.winfo_height() - seen
        if spare > 0:
            self.assertEqual(app.list_cover.winfo_manager(), "place")
            self.assertEqual(app.list_cover.winfo_height() if app.list_cover.winfo_height() > 1
                             else int(app.list_cover.place_info()["height"]), spare)
        # the fixed headers stay lined up with the columns
        root.update_idletasks()
        for col, copy in enumerate(app._head_labels[:4]):
            src = app.list_frame.grid_slaves(row=0, column=col)[0]
            self.assertEqual(x_in(copy, root), x_in(src, root) - 0)
            self.assertEqual(copy.cget("text"), src.cget("text"))
        self.assertTrue(app.list_head.winfo_manager())
        # every row is one height (scrolling stops on a row's edge)
        heights = {app.list_frame.grid_bbox(0, i, 4, i)[3] for i in range(1, 16)}
        self.assertEqual(heights, {step})
        app.closed = True

    def test_update_row_band_and_disabled_all(self):
        app = self.make_app()
        i = [r.folder for r in app.rows].index("TaxiTime") + 1
        self.assertIn(i, app._hl_rows)
        cells = app._cells[i - 1]
        for key in ("name", "installed", "latest", "status"):
            self.assertEqual(str(cells[key].cget("background")), theme.HIGHLIGHT)
        app.busy = True
        app.render()
        self.assertIn("disabled", app.all_button.state())
        self.assertEqual(ttk.Style(self.root).lookup("Accent.TButton", "foreground",
                                                     ["disabled"]), theme.DISABLED)
        app.busy = False
        app.closed = True


if __name__ == "__main__":
    unittest.main()
