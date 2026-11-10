"""The two small Backup windows opened from Settings: Make a backup and Restore a
backup. The work runs in a thread (am\\backup.py); results come back through the App's
queue as ("backup_size"/"backup_clouds"/"backup_progress"/"backup_done"/"backup_error"/
"restore_progress"/"restore_done"/"restore_error", ...)."""

import datetime
import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from am import backup, config, errors, theme

log = logging.getLogger(__name__)

# Linux fonts are wider (Noto/DejaVu 10 pt vs Segoe UI 9 pt): a bit less text width and
# tighter list/cloud buttons there, so both windows stay well under 420 px. Windows as before.
LINUX = sys.platform != "win32"
WRAP = 310 if LINUX else 330   # text width: keeps the windows narrow
SLIM_PADDING = (4, 4)          # Linux only: Cloud.TButton / Row.TButton
CLOUD_COLUMNS = 3
MAX_CLOUD_BUTTONS = 9   # 3 rows
MAX_FOUND_ROWS = 8      # Backups found: older ones via Browse...
MAX_SAFETY_ROWS = 4     # safety copies, when ticked
PLACE_MAX = 12 if LINUX else 16        # characters of a folder name shown as a place


def open_folder(path):
    """Show a folder in Explorer / the file manager. Never raises."""
    try:
        if os.name == "nt":
            os.startfile(path)   # noqa: S606 (a folder the player chose)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        log.exception("Could not open %s", path)


def backup_folder(settings):
    """The player's backup folder (picked before), else Documents\\AddonManager backups."""
    folder = settings.get("backup_folder")
    if isinstance(folder, str) and os.path.isabs(folder) and "\0" not in folder:
        return folder
    return backup.default_folder()


class _Dialog:
    """A small dark modal window over the Settings window (or the main window)."""

    def __init__(self, app, title, show):
        self.app = app
        self.running = False
        self.closed = False
        self.shown = show
        self.parent = app.settings_win if app.settings_win is not None else app.root
        win = self.win = tk.Toplevel(self.parent)
        win.withdraw()
        win.title(title)
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(self.parent)
        style = ttk.Style(win)
        style.configure("Row.TButton", anchor="w")
        if LINUX:
            for name in ("Row.TButton", "Cloud.TButton"):
                style.configure(name, padding=SLIM_PADDING)
            style.configure("Row.TButton", font=theme.font(bigger=-1))   # 9 pt like Windows
        self.frame = ttk.Frame(win, padding=14)
        self.frame.pack(fill="both", expand=True)
        ttk.Label(self.frame, text=title, font=theme.font("bold", bigger=1)).pack(anchor="w")
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())

    def _status_area(self):
        self.status_var = tk.StringVar()
        self.status = ttk.Label(self.frame, textvariable=self.status_var, wraplength=WRAP,
                                justify="left")
        self.status.pack(anchor="w", pady=(10, 0))
        self.progress = ttk.Progressbar(self.frame, mode="determinate", maximum=100)
        self.buttons = ttk.Frame(self.frame)
        self.buttons.pack(fill="x", pady=(12, 0))

    def _show(self, focus):
        if not self.shown:
            return
        win = self.win
        win.update_idletasks()
        p = self.parent
        x = p.winfo_rootx() + max(0, (p.winfo_width() - win.winfo_reqwidth()) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - win.winfo_reqheight()) // 3)
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        theme.dark_title_bar(win)
        focus.focus_set()
        self.grab()

    def grab(self):
        if self.shown and not self.closed:
            try:
                self.win.grab_set()
            except tk.TclError:
                pass

    def alive(self):
        return not self.closed

    def set_status(self, text):
        self.status_var.set(text)

    def set_progress(self, done, total):
        if not self.progress.winfo_manager():
            self.progress.pack(fill="x", pady=(6, 0), before=self.buttons)
        self.progress.configure(value=min(100, done * 100 // max(total, 1)))

    def close(self):
        if self.running or self.closed:
            return   # the work finishes first (it can't be stopped half-way safely)
        self.closed = True
        if self.app.backup_dialog is self:
            self.app.backup_dialog = None
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass
        settings_win = self.app.settings_win
        if self.shown and settings_win is not None:
            try:
                settings_win.grab_set()
            except tk.TclError:
                pass

    def _begin(self, text):
        self.running = True
        self.app.busy = True
        self.app.render()
        self.set_status(text)
        self.set_progress(0, 1)

    def _end(self):
        self.running = False


class BackupDialog(_Dialog):
    """Make a backup: what to include, where it goes, the don't-share line."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.BACKUP_TITLE, show)
        settings = config.load_settings()
        self.folder = backup_folder(settings)
        self.path = None
        self.settings_var = tk.BooleanVar(value=settings.get("backup_settings") is not False)
        self.addons_var = tk.BooleanVar(value=settings.get("backup_addons") is not False)
        self.settings_check = ttk.Checkbutton(self.frame, text=errors.BACKUP_SETTINGS,
                                              variable=self.settings_var)
        self.settings_check.pack(anchor="w", pady=(10, 0))
        self.addons_check = ttk.Checkbutton(self.frame, text=errors.BACKUP_ADDONS,
                                            variable=self.addons_var)
        self.addons_check.pack(anchor="w")

        where = ttk.Frame(self.frame)
        where.pack(fill="x", pady=(10, 0))
        self.folder_var = tk.StringVar(value=errors.saved_to(self.folder))
        ttk.Label(where, textvariable=self.folder_var, wraplength=WRAP - (110 if LINUX else 90),
                  justify="left").pack(side="left", anchor="w")
        self.change_button = ttk.Button(where, text=errors.BACKUP_CHANGE,
                                        command=self.change_folder)
        self.change_button.pack(side="right", anchor="n")
        self.cloud_buttons = []
        self.cloud_area = ttk.Frame(self.frame)   # filled when the cloud check is done
        self.cloud_area.pack(anchor="w", fill="x")
        threading.Thread(target=_cloud_worker, daemon=True, args=(app.queue,)).start()
        ttk.Label(self.frame, text=errors.BACKUP_PRIVATE, style="Muted.TLabel",
                  wraplength=WRAP, justify="left").pack(anchor="w", pady=(10, 0))
        self._status_area()
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.make_button = ttk.Button(self.buttons, text=errors.BACKUP_MAKE,
                                      style="Accent.TButton", command=self.start)
        self.make_button.pack(side="right", padx=(0, 6))
        self.open_button = ttk.Button(self.buttons, text=errors.BACKUP_OPEN_FOLDER,
                                      command=self.open_result)
        threading.Thread(target=_size_worker, daemon=True,
                         args=(app.queue, app.addons_dir)).start()
        self._show(self.make_button)

    def _set_folder(self, folder):
        self.folder = folder
        self.folder_var.set(errors.saved_to(folder))
        settings = config.load_settings()
        settings["backup_folder"] = folder
        config.save_settings(settings)

    def change_folder(self):
        if self.running:
            return
        picked = self.app.ask_backup_folder(self.folder)
        if picked:
            self._set_folder(os.path.abspath(picked))
        self.grab()

    def show_clouds(self, clouds):
        """One button per synced cloud folder, 3 equal columns, at most 9."""
        if self.cloud_buttons or not clouds or self.closed:
            return
        ttk.Label(self.cloud_area, text=errors.BACKUP_CLOUD, style="Muted.TLabel",
                  wraplength=WRAP, justify="left").pack(anchor="w", pady=(8, 0))
        grid = ttk.Frame(self.cloud_area)
        grid.pack(fill="x", pady=(2, 0))
        for c in range(CLOUD_COLUMNS):
            grid.columnconfigure(c, weight=1, uniform="cloud")
        for i, (label, path) in enumerate(clouds[:MAX_CLOUD_BUTTONS]):
            row, col = divmod(i, CLOUD_COLUMNS)
            b = ttk.Button(grid, text=label, style="Cloud.TButton",
                           command=lambda p=path: self.use_cloud(p))
            b.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0 if
                   col == CLOUD_COLUMNS - 1 else 3), pady=(0 if row == 0 else 6, 0))
            if self.running:
                b.state(["disabled"])
            self.cloud_buttons.append(b)

    def use_cloud(self, cloud):
        if not self.running:
            self._set_folder(os.path.join(cloud, backup.FOLDER_NAME))

    def start(self):
        if self.running or self.app.busy or not self.app.addons_dir:
            return False
        addons, settings_on = bool(self.addons_var.get()), bool(self.settings_var.get())
        if not (addons or settings_on):
            self.set_status(errors.BACKUP_PICK_ONE)
            return False
        settings = config.load_settings()
        settings["backup_addons"], settings["backup_settings"] = addons, settings_on
        config.save_settings(settings)
        self.open_button.pack_forget()
        for w in (self.make_button, self.change_button, self.cancel_button,
                  self.settings_check, self.addons_check, *self.cloud_buttons):
            w.state(["disabled"])
        self._begin(errors.BACKUP_MAKING)
        threading.Thread(target=_backup_worker, daemon=True,
                         args=(self.app.queue, self.app.check_game, self.app.addons_dir,
                               self.folder, addons, settings_on)).start()
        return True

    def open_result(self):
        self.app.open_folder(os.path.dirname(self.path) if self.path else self.folder)

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("backup_"):
            return
        if kind == "backup_size":
            self.addons_check.configure(text=errors.addons_label(msg[1]))
            return
        if kind == "backup_clouds":
            self.show_clouds(msg[1])
            return
        if kind == "backup_progress":
            self.set_progress(msg[1], msg[2])
            return
        self._end()
        for w in (self.make_button, self.change_button, self.cancel_button,
                  self.settings_check, self.addons_check, *self.cloud_buttons):
            w.state(["!disabled"])
        if kind == "backup_done":
            self.path = msg[1]
            self.set_progress(1, 1)
            self.set_status(errors.backup_saved(os.path.basename(msg[1]), msg[2]))
            self.cancel_button.configure(text=errors.CLOSE)
            self.open_button.pack(side="left")
        else:
            self.progress.pack_forget()
            self.set_status(msg[1])


class BackupListDialog(_Dialog):
    """Restore, step 1: the backups found in Documents, the chosen backup folder and each
    cloud folder's "AddonManager backups", newest first; one click opens the Restore
    window for it. Browse... picks any other file."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.RESTORE_TITLE, show)
        self.found = None        # [FoundBackup] once the search is done
        self.opening = None      # the path being opened (checked in a thread)
        self.row_buttons = []
        ttk.Label(self.frame, text=errors.FOUND_HEADING, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0))
        self.rows = ttk.Frame(self.frame)
        self.rows.pack(fill="x", pady=(4, 0))
        self.safety_var = tk.BooleanVar(value=False)
        self.safety_check = ttk.Checkbutton(self.frame, text=errors.FOUND_SAFETY,
                                            variable=self.safety_var, command=self.render)
        self.safety_check.pack(anchor="w", pady=(8, 0))
        ttk.Label(self.frame, text=errors.FOUND_NOTE, style="Muted.TLabel", wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(6, 0))
        self._status_area()
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.browse_button = ttk.Button(self.buttons, text=errors.FOUND_BROWSE,
                                        command=self.browse)
        self.browse_button.pack(side="left")
        self.set_status(errors.FOUND_LOOKING)
        threading.Thread(target=_find_worker, daemon=True,
                         args=(app.queue, backup_folder(config.load_settings()))).start()
        self._show(self.browse_button)

    def shown_backups(self):
        """(backups, safety copies, more): at most MAX_FOUND_ROWS backups and, only when
        ticked, MAX_SAFETY_ROWS safety copies (their own list, under its own heading)."""
        found = self.found or []
        rows = [f for f in found if not f.safety]
        safety = [f for f in found if f.safety] if self.safety_var.get() else []
        more = len(rows) > MAX_FOUND_ROWS or len(safety) > MAX_SAFETY_ROWS
        return rows[:MAX_FOUND_ROWS], safety[:MAX_SAFETY_ROWS], more

    def _row(self, f):
        when = found_date(f.mtime)
        b = ttk.Button(self.rows, style="Row.TButton",
                       text=errors.found_row(when, f.size, f.place, f.online),
                       command=lambda: self.pick(f.path, f.online))
        b.pack(fill="x", pady=(0, 4))
        if self.opening:
            b.state(["disabled"])
        self.row_buttons.append(b)

    def render(self):
        self.row_buttons = []
        for child in self.rows.winfo_children():
            child.destroy()
        if self.found is None:
            return
        rows, safety, more = self.shown_backups()
        for f in rows:
            self._row(f)
        if safety:
            ttk.Label(self.rows, text=errors.FOUND_SAFETY_HEADING, style="Muted.TLabel").pack(
                anchor="w", pady=(4, 2))
            for f in safety:
                self._row(f)
        if more:
            ttk.Label(self.rows, text=errors.FOUND_MORE, style="Muted.TLabel").pack(anchor="w")
        if not self.opening:
            self.set_status("" if rows or safety else errors.FOUND_NONE)

    def pick(self, path, online=False):
        """Check the backup in a thread (an online-only file downloads then), then the
        Restore window. Nothing changes until the player says yes there."""
        if self.opening or self.closed or not path:
            return
        self.opening = path
        for w in (*self.row_buttons, self.browse_button, self.safety_check):
            w.state(["disabled"])
        self.set_status(errors.FOUND_DOWNLOADING if online else errors.FOUND_OPENING)
        threading.Thread(target=_open_worker, daemon=True,
                         args=(self.app.queue, path)).start()

    def browse(self):
        if self.opening or self.closed:
            return
        path = self.app.ask_backup_file()
        self.grab()
        if path:
            self.pick(path)

    def handle(self, msg):
        kind = msg[0]
        if kind == "restore_found":
            if self.found is None:
                self.found = msg[1]
                self.render()
        elif kind == "restore_opened" and msg[1] == self.opening:
            app, show = self.app, self.shown
            self.opening = None
            self.close()
            app.backup_dialog = RestoreDialog(app, msg[2], show=show)
        elif kind == "restore_open_error" and msg[1] == self.opening:
            self.opening = None
            for w in (*self.row_buttons, self.browse_button, self.safety_check):
                w.state(["!disabled"])
            self.set_status(msg[2])


class RestoreDialog(_Dialog):
    """Restore a backup: what it holds, which account gets the settings, what to restore."""

    def __init__(self, app, info, show=True):
        super().__init__(app, errors.RESTORE_TITLE, show)
        self.info = info
        self.result = None
        ttk.Label(self.frame, text=errors.backup_from(info), wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(6, 0))
        self.local = backup.local_accounts(app.addons_dir)
        self.account_map = backup.default_map(sorted(info.accounts), self.local)
        self.account_vars = {}
        if info.accounts and self.local:
            for acc in sorted(info.accounts):
                if acc in self.local:
                    continue   # same account here: nothing to ask
                line = ttk.Frame(self.frame)
                line.pack(fill="x", pady=(8, 0))
                ttk.Label(line, text=errors.account_line(acc), wraplength=WRAP - 120,
                          justify="left").pack(side="left", anchor="w")
                var = tk.StringVar(value=self.account_map[acc] or errors.RESTORE_SKIP)
                button = ttk.Menubutton(line, textvariable=var)
                menu = tk.Menu(button, tearoff=0, background=theme.SURFACE,
                               foreground=theme.TEXT, activebackground=theme.ACCENT,
                               activeforeground=theme.ACCENT_TEXT)
                for name in self.local + [errors.RESTORE_SKIP]:
                    menu.add_radiobutton(label=name, value=name, variable=var)
                button["menu"] = menu
                button.pack(side="right")
                self.account_vars[acc] = var
        can_settings = bool(self.local)
        self.addons_var = tk.BooleanVar(value=bool(info.addons))
        self.settings_var = tk.BooleanVar(value=can_settings and info.settings_files > 0)
        self.bindings_var = tk.BooleanVar(value=can_settings and info.binding_files > 0)
        checks = ttk.Frame(self.frame)
        checks.pack(anchor="w", pady=(10, 0))
        self.checks = {}
        for key, text, var, on in (
                ("addons", errors.RESTORE_ADDONS, self.addons_var, bool(info.addons)),
                ("settings", errors.RESTORE_SETTINGS, self.settings_var,
                 can_settings and info.settings_files > 0),
                ("bindings", errors.RESTORE_BINDINGS, self.bindings_var,
                 can_settings and info.binding_files > 0)):
            c = ttk.Checkbutton(checks, text=text, variable=var)
            c.pack(side="left", padx=(0, 10))
            if not on:
                c.state(["disabled"])
            self.checks[key] = c
        if info.accounts and not self.local:
            ttk.Label(self.frame, text=errors.NO_ACCOUNT, wraplength=WRAP,
                      justify="left").pack(anchor="w", pady=(8, 0))
        for text in (errors.RESTORE_MERGE, errors.RESTORE_SAFETY):
            ttk.Label(self.frame, text=text, style="Muted.TLabel", wraplength=WRAP,
                      justify="left").pack(anchor="w", pady=(6, 0))
        self._status_area()
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.restore_button = ttk.Button(self.buttons, text=errors.RESTORE_OK,
                                         style="Accent.TButton", command=self.start)
        self.restore_button.pack(side="right", padx=(0, 6))
        self._show(self.restore_button)

    def chosen_map(self):
        out = dict(self.account_map)
        for acc, var in self.account_vars.items():
            value = var.get()
            out[acc] = value if value in self.local else None
        return out

    def start(self):
        if self.running or self.app.busy or not self.app.addons_dir:
            return False
        flags = dict(addons=bool(self.addons_var.get()), settings=bool(self.settings_var.get()),
                     bindings=bool(self.bindings_var.get()))
        account_map = self.chosen_map()
        try:
            folders, files = backup.plan(self.info, self.app.addons_dir,
                                         account_map=account_map, **flags)
        except backup.BackupError as e:
            self.set_status(errors.backup_message(e))
            return False
        if not folders and not files:
            self.set_status(errors.backup_message(backup.BackupError("nothing")))
            return False
        safety = backup_folder(config.load_settings())
        ok = self.app.confirm(errors.RESTORE_TITLE + "?",
                              errors.restore_confirm(len(folders), len(files), safety),
                              errors.RESTORE_OK, errors.CANCEL)
        self.grab()
        if not ok:
            return False
        for w in (self.restore_button, self.cancel_button, *self.checks.values()):
            w.state(["disabled"])
        self._begin(errors.RESTORING)
        threading.Thread(target=_restore_worker, daemon=True,
                         args=(self.app.queue, self.app.check_game, self.info.path,
                               self.app.addons_dir, safety, flags, account_map)).start()
        return True

    def handle(self, msg):
        kind = msg[0]
        if kind not in ("restore_progress", "restore_done", "restore_error"):
            return   # a late message from a Backup or Backups found window
        if kind == "restore_progress":
            self.set_progress(msg[1], msg[2])
            return
        self._end()
        self.cancel_button.state(["!disabled"])
        if kind == "restore_done":
            self.result = msg[1]
            self.set_progress(1, 1)
            self.set_status(errors.restored_message(msg[1]))
            self.cancel_button.configure(text=errors.CLOSE)
        else:
            self.progress.pack_forget()
            self.set_status(msg[1])
            self.restore_button.state(["!disabled"])
            for key, c in self.checks.items():
                if key == "addons" and self.info.addons:
                    c.state(["!disabled"])
                elif key != "addons" and self.local and (
                        self.info.settings_files if key == "settings" else self.info.binding_files):
                    c.state(["!disabled"])


# ---------- threads (plain values only, never Tk objects) ----------
def _game_open(check_game):
    try:
        return bool(check_game())
    except Exception:
        return False


def _size_worker(q, addons_dir):
    q.put(("backup_size", backup.addons_size(addons_dir) if addons_dir else 0))


def _cloud_worker(q):
    try:
        clouds = backup.cloud_folders()
    except Exception as e:
        log.info("Cloud folder check failed (%s)", type(e).__name__)
        clouds = []
    q.put(("backup_clouds", clouds))


def found_date(mtime):
    """A file date as '2026-11-10 21:30'; '?' when Windows can't show it (before 1970,
    after the year 3000, or not a number)."""
    try:
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "?"


def place_label(folder, clouds, documents):
    """Short place name of a backup folder: the cloud it's in (also a Documents folder
    synced by OneDrive), else "Documents", else the folder's own name."""
    for label, cloud in clouds:
        if backup.is_under(folder, cloud):
            return label
    if documents and backup.is_under(folder, documents):
        return errors.PLACE_DOCUMENTS
    name = os.path.basename(os.path.normpath(folder))
    if name == backup.FOLDER_NAME:
        name = os.path.basename(os.path.dirname(os.path.normpath(folder))) or name
    return name if len(name) <= PLACE_MAX else name[:PLACE_MAX - 1] + "…"


def backup_places(chosen, clouds, documents):
    """[(place label, folder)] to look in: Documents, the chosen folder, the clouds."""
    folders = [os.path.join(documents, backup.FOLDER_NAME) if documents else None, chosen]
    folders += [os.path.join(c, backup.FOLDER_NAME) for _l, c in clouds]
    return [(place_label(f, clouds, documents), f) for f in folders if f]


def _find_worker(q, chosen):
    try:
        clouds = backup.cloud_folders()
    except Exception as e:
        log.info("Cloud folder check failed (%s)", type(e).__name__)
        clouds = []
    try:
        found = backup.find_backups(backup_places(chosen, clouds, backup.documents_dir()))
    except Exception as e:
        log.info("Backup search failed (%s)", type(e).__name__)
        found = []
    q.put(("restore_found", found))


def _open_worker(q, path):
    try:
        q.put(("restore_opened", path, backup.read(path)))
    except Exception as e:
        log.warning("Not a usable backup (%s)", type(e).__name__)
        q.put(("restore_open_error", path, errors.backup_message(e)))


def _backup_worker(q, check_game, addons_dir, folder, addons, settings):
    if _game_open(check_game):
        q.put(("backup_error", errors.GAME_OPEN))
        return
    try:
        path, size, _counts = backup.make(
            addons_dir, folder, addons=addons, settings=settings,
            progress=lambda done, total: q.put(("backup_progress", done, total)))
        q.put(("backup_done", path, size))
    except Exception as e:
        log.exception("Backup failed")
        q.put(("backup_error", errors.backup_message(e)))


def _restore_worker(q, check_game, path, addons_dir, safety, flags, account_map):
    if _game_open(check_game):
        q.put(("restore_error", errors.GAME_OPEN))
        return
    try:
        result = backup.restore(
            path, addons_dir, safety, account_map=account_map,
            progress=lambda done, total: q.put(("restore_progress", done, total)), **flags)
        q.put(("restore_done", result))
    except Exception as e:
        log.exception("Restore failed")
        q.put(("restore_error", errors.backup_message(e)))
