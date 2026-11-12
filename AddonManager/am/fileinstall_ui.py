"""The "Add from file" confirm window and its texts (am\\fileinstall.py does the work).

The window lists what was found in the picked file, one tick per addon, and says what
each one would do. Install calls on_install(dialog, folders); the App runs the install in
a thread and reports back with set_progress / finished / failed. It never installs by
itself, so it can be built hidden in tests."""

import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk

from am import archive, fileinstall, theme, toc, touch, versions

log = logging.getLogger(__name__)

LINUX = sys.platform != "win32"
WRAP = 330 if LINUX else 350    # the window stays under 420 px
MAX_VISIBLE_ROWS = 8            # more addons: the list scrolls
ROW_HEIGHT = 40 if LINUX else 36

# ---------- texts ----------
ADD_BUTTON = "Add file..."
ADD_WIDTH = 10   # characters: the same size as the list's Install/Update/Uninstall buttons
# The button sits in the list box's footer (under the scrolling rows, always in view),
# lined up under the row buttons, with ADD_HINT at the left of the footer.
ADD_HINT = "From a .zip, .rar or .7z file:"
ADD_HINT_EMPTY = "Drop a file here or use Add file:"   # no other addons yet (drag and drop)
ADD_TIP = ("Install an addon from a .zip, .rar or .7z file (from CurseForge, Wago or a "
           "friend). Shortcut: Ctrl+O.")
ADD_KEYS = ("<Control-o>", "<Control-O>")
FOLDER_TIP = "Right-click: install from an unpacked folder. You can also drop a file on the window."
FILE_MENU = "Install from file..."
FOLDER_MENU = "Install from folder..."
FOLDER_DIALOG_TITLE = "Choose the unpacked addon folder"
STATUS_NOT_FOREVER = "Not for WoW Forever"
UNINSTALL = "Uninstall"
TITLE = "Install from file"
FILE_TYPES = [("Addon files", "*.zip *.rar *.7z"), ("All files", "*.*")]
READING = "Reading {name}..."
INSTALLING = "Installing..."
FOUND = "Addons found in the file:"
NOTE_NEW = "New"
NOTE_SAME = "Same version (installs again)"
NOTE_HAND = "Replaces a copy that was put there by hand"
NOTE_GUILD = "Guild addon: kept. It comes from the guild list."
NOTE_NOT_FOREVER = "Not made for WoW Forever: WoW won't load it"
NOTE_OTHER_VERSION = "Made for another WoW version: WoW may call it out of date"
SKIP_BLIZZARD = "{name}: a game folder, never installed"
SKIP_BAD_NAME = "{name}: a name WoW can't use, not installed"
SAFETY_NOTE = ("Addons that are already there are saved first in your backups folder "
               "(Backup > Restore can put them back).")
TRUST_NOTE = "Only install addons from people you trust."
NOTHING_CHOSEN = "Tick at least one addon."
NOTHING_TO_INSTALL = "There is nothing in this file the Addon Manager may install."
INSTALL = "Install"
CANCEL = "Cancel"
CLOSE = "Close"
DROP_HERE = "Drop a .zip, .rar or .7z file here to install it."
ONE_FILE = "Drop one file at a time."
OTHER_ADDONS = "Other addons"
OTHER_NOTE = "installed from a file: no update check"
FROM_FILE = "From file"


MAX_TITLE = 24   # Other addons: longer titles are cut (full title in a tooltip), so the
#                  window stays inside its default 680 px (Linux fonts are wider)


MIN_TITLE_PX = 110   # an Other addon's title may always be this wide (pixels)


def short_title(title, limit=MAX_TITLE):
    """A title that can't make the main window wider: cut with '...' after limit characters."""
    title = title or ""
    return title if len(title) <= limit else title[:limit - 3].rstrip() + "..."


def fit_title(title, max_px, measure):
    """short_title, then cut further until measure(text) <= max_px (the column's width the
    guild rows already need), so a title never sets the window's minimum width."""
    shown = short_title(title)
    n = len(shown)
    while n > 4 and measure(shown) > max_px:
        n -= 1
        shown = (title or "")[:n - 3].rstrip() + "..."
    return shown


def note_for(a):
    """The grey line under an addon's tick in the window."""
    if a.state == fileinstall.GUILD:
        return NOTE_GUILD
    if a.state == fileinstall.UPDATE:
        parts = [f"Update {versions.display(a.installed_version) if a.installed_version else '?'}"
                 f" -> {versions.display(a.version) if a.version else '?'}"]
    else:
        parts = [{fileinstall.NEW: NOTE_NEW, fileinstall.SAME: NOTE_SAME,
                  fileinstall.HAND: NOTE_HAND}[a.state]]
    if a.renamed_from:
        parts.append(f"installs as {a.folder}")
    if a.status == toc.NOT_FOREVER:
        parts.append(NOTE_NOT_FOREVER)
    elif a.status == toc.OTHER_VERSION:
        parts.append(NOTE_OTHER_VERSION)
    return ". ".join(parts)


def left_out_line(plan):
    names, count = [], 0
    for a in plan.addons:
        count += a.left_out_count
        names += [n.rsplit("/", 1)[-1] for n in a.left_out]
    count += len(plan.unused_archives)
    names += [n.replace("\\", "/").rsplit("/", 1)[-1] for n in plan.unused_archives]
    if not count:
        return ""
    shown = ", ".join(dict.fromkeys(names[:3]))
    more = ", ..." if count > 3 else ""
    word = "file" if count == 1 else "files"
    return f"Left out: {count} {word} WoW doesn't use ({shown}{more})."


def message(exc, name=""):
    """A player text for an ArchiveError / InstallError from reading or installing."""
    kind = getattr(exc, "kind", "other")
    detail = getattr(exc, "detail", "")
    texts = {
        "not_found": "The file isn't there any more.",
        "too_big": "This file is too big to be an addon.",
        "bad_archive": "This file is damaged or isn't a .zip, .rar or .7z file.",
        "encrypted": "This file is password-protected. Ask for a file without a password.",
        "unsafe": "This file tries to put files where they don't belong. Nothing was installed.",
        "program": f"This file has a program in it ({detail}), not just an addon. "
                   "Nothing was installed.",
        "no_tar": ("This PC can't open .rar or .7z files (that needs Windows 11). Unpack it "
                   "first (with 7-Zip, for example), then right-click Add file... > "
                   "Install from folder..., or ask for a .zip."
                   if sys.platform == "win32" else
                   "This system can't open .rar or .7z files (bsdtar is missing). Unpack it "
                   "first, then right-click Add file... > Install from folder..., or ask for "
                   "a .zip."),
        "timeout": "Reading this file took too long. Nothing was installed.",
        "empty": "No WoW addon was found in this file.",
        "ambiguous": "This file holds the same addon twice or more than one addon in one "
                     "place, so it isn't clear what to install. Nothing was installed.",
        "in_game": "Pick the folder you unpacked, not the game's own folders.",
        "not_allowed": f"{detail} can't be installed from a file. Nothing was installed.",
        "safety": "The safety copy of the addons already there couldn't be saved, so "
                  "nothing was installed. Check your backups folder in the Backup window.",
        "locked": "A file is in use (is WoW open?). Close WoW and try again.",
        "permission": "Windows didn't allow the change. Nothing was changed for that addon."
                      if sys.platform == "win32" else
                      "No permission to change the AddOns folder.",
        "disk_full": "The disk is full.",
        "link": "The addon's folder is a link; the Addon Manager leaves it alone.",
    }
    return texts.get(kind, "Something went wrong reading this file. Nothing was installed.")


def done_message(result, source_name):
    """The line for the main window after an install."""
    n = len(result.installed)
    word = "addon" if n == 1 else "addons"
    text = f"Installed {n} {word} from {source_name}. Type /reload in WoW (or restart it)."
    if result.failed:
        folder, exc = result.failed
        stop = f"{folder} wasn't installed: {message(exc)}"
        text = stop if not n else f"{text} {stop}"
        if result.not_done:
            text += f" Not tried: {', '.join(result.not_done)}."
    return text


def paths_from_drop(tk_root, data):
    """The file paths in a drop event's data (a Tcl list: {C:/a b.zip} C:/c.zip)."""
    try:
        return [p for p in tk_root.tk.splitlist(data) if p]
    except tk.TclError:
        return []


class FileInstallDialog:
    """The confirm window for one Plan."""

    def __init__(self, parent, plan, on_install, on_close=None, show=True, game_running=False):
        self.plan = plan
        self.on_install = on_install
        self.on_close = on_close
        self.running = self.closed = self.finished = False
        self.shown = show
        self.parent = parent
        win = self.win = tk.Toplevel(parent)
        win.withdraw()
        win.title(TITLE)
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(parent)
        frame = self.frame = ttk.Frame(win, padding=theme.px(12))   # stays under 420 px
        frame.pack(fill="both", expand=True)
        theme.frame_window(win, frame)
        theme.heading(frame, TITLE).pack(fill="x")
        ttk.Label(frame, text=f"From: {plan.name}", wraplength=theme.px(WRAP),
                  justify="left").pack(anchor="w", pady=(6, 0))
        # WoW open: installing still works (like guild installs); say so where it's decided.
        self.game_label = None
        if game_running:
            from am import errors
            self.game_label = tk.Label(frame, text=errors.GAME_RUNNING,
                                       background=theme.BANNER_BG, foreground=theme.BANNER_TEXT,
                                       anchor="w", justify="left", wraplength=theme.px(WRAP),
                                       padx=theme.px(6), pady=theme.px(4),
                                       font=theme.font())
            self.game_label.pack(fill="x", pady=(8, 0))
        ttk.Label(frame, text=FOUND if plan.addons else NOTHING_TO_INSTALL,
                  style="Muted.TLabel", wraplength=theme.px(WRAP)).pack(anchor="w", pady=(8, 2))
        self.vars, self.checks = {}, {}
        self._rows_area()
        for folder, why in plan.skipped:
            text = (SKIP_BLIZZARD if why == fileinstall.BLIZZARD else SKIP_BAD_NAME)
            ttk.Label(frame, text=text.format(name=folder), style="Muted.TLabel",
                      wraplength=theme.px(WRAP), justify="left").pack(anchor="w")
        left = left_out_line(plan)
        if left:
            ttk.Label(frame, text=left, style="Muted.TLabel", wraplength=theme.px(WRAP),
                      justify="left").pack(anchor="w", pady=(8, 0))
        self.safety_label = ttk.Label(frame, text=SAFETY_NOTE, style="Muted.TLabel",
                                      wraplength=theme.px(WRAP), justify="left")
        self.trust = ttk.Label(frame, text=TRUST_NOTE, style="Muted.TLabel", wraplength=theme.px(WRAP))
        self.trust.pack(anchor="w", pady=(8, 0))
        self.status_var = tk.StringVar()
        self.status = ttk.Label(frame, textvariable=self.status_var, wraplength=theme.px(WRAP),
                                justify="left")
        # packed only while it has text (no empty gap above the buttons)
        self.status_var.trace_add("write", lambda *_a: self._place_status())
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.buttons = ttk.Frame(frame)
        self.buttons.pack(fill="x", pady=(12, 0))
        self.cancel_button = ttk.Button(self.buttons, text=CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.install_button = ttk.Button(self.buttons, text=INSTALL, style="Accent.TButton",
                                         command=self.start)
        self.install_button.pack(side="right", padx=(0, theme.px(6)))
        theme.even_buttons(self.buttons, (CLOSE,))
        if not any(a.can_install for a in plan.addons):
            self.install_button.state(["disabled"])
        self._update_safety()
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())
        if show:
            win.update_idletasks()
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_reqwidth()) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{x}+{y}")
            win.deiconify()
            theme.dark_title_bar(win)
            self.install_button.focus_set()
            try:
                win.grab_set()
            except tk.TclError:
                pass

    def _rows_area(self):
        """One tick + grey note per addon; a scrolled list when there are many."""
        addons = self.plan.addons
        holder, canvas = self.frame, None
        if len(addons) > MAX_VISIBLE_ROWS:
            outer = ttk.Frame(self.frame)
            outer.pack(fill="x")
            canvas = tk.Canvas(outer, background=theme.BG, highlightthickness=0,
                               height=theme.px(MAX_VISIBLE_ROWS * ROW_HEIGHT),
                               width=theme.px(WRAP + 16))
            bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=bar.set)
            canvas.pack(side="left", fill="both", expand=True)
            bar.pack(side="right", fill="y")
            holder = ttk.Frame(canvas)
            canvas.create_window((0, 0), window=holder, anchor="nw")
            holder.bind("<Configure>",
                        lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
            self.canvas = canvas
        for a in addons:
            var = tk.BooleanVar(value=a.tick)
            version = versions.display(a.version) if a.version else "?"
            check = ttk.Checkbutton(holder, text=f"{a.title}   {version}", variable=var,
                                    command=self._update_safety)
            check.pack(anchor="w", pady=(4, 0))
            if not a.can_install:
                var.set(False)
                check.state(["disabled"])
            ttk.Label(holder, text=note_for(a), style="Muted.TLabel",
                      wraplength=theme.px(WRAP - 24),
                      justify="left").pack(anchor="w", padx=(theme.px(24), 0))
            self.vars[a.folder] = var
            self.checks[a.folder] = check
        if canvas is not None:   # the wheel works over the rows too, not only the gaps
            _wheel(canvas, canvas)

    def _place_status(self):
        try:
            if not self.status_var.get():
                self.status.pack_forget()
            elif not self.status.winfo_manager():
                after = self.progress if self.progress.winfo_manager() else self.buttons
                self.status.pack(anchor="w", pady=(8, 0), before=after)
        except tk.TclError:
            pass

    def chosen(self):
        return [a.folder for a in self.plan.addons
                if a.can_install and self.vars[a.folder].get()]

    def _update_safety(self):
        replaces = any(a.installed_folder for a in self.plan.addons
                       if a.can_install and self.vars[a.folder].get())
        if replaces and not self.safety_label.winfo_manager():
            self.safety_label.pack(anchor="w", pady=(8, 0), before=self.trust)
        elif not replaces and self.safety_label.winfo_manager():
            self.safety_label.pack_forget()

    def start(self):
        if self.running or self.finished or self.closed:
            return False
        folders = self.chosen()
        if not folders:
            self.status_var.set(NOTHING_CHOSEN)
            return False
        self.running = True
        for w in [self.install_button, self.cancel_button, *self.checks.values()]:
            w.state(["disabled"])
        self.status_var.set(INSTALLING)
        self.set_progress(0, 1)
        self.on_install(self, folders)
        return True

    def set_progress(self, done, total):
        if not self.progress.winfo_manager():
            self.progress.pack(fill="x", pady=(6, 0), before=self.buttons)
        self.progress.configure(value=min(100, done * 100 // max(total, 1)))

    def _finish(self, text):
        self.running = False
        self.finished = True
        self.status_var.set(text)
        self.install_button.pack_forget()
        self.cancel_button.configure(text=CLOSE)
        self.cancel_button.state(["!disabled"])

    def finished_ok(self, result):
        self.set_progress(1, 1)
        self._finish(done_message(result, self.plan.name))

    def failed(self, exc):
        self._finish(message(exc))

    def close(self):
        if self.running or self.closed:
            return   # an install can't be stopped half-way safely
        self.closed = True
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass
        self.plan.close()
        if self.on_close:
            self.on_close(self)


def drop_supported():
    """True if tkinterdnd2 (with its tkdnd library) can be imported in this build."""
    try:
        import tkinterdnd2  # noqa: F401
        return True
    except Exception:
        return False


def enable_drop(root, widget, on_paths):
    """Let files be dropped on widget (and its window). on_paths([paths]). False when drag
    and drop isn't available here (no tkinterdnd2, or its tkdnd library won't load):
    the app then works the same without it. Never raises."""
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD._require(root)
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: (on_paths(paths_from_drop(root, e.data)),
                                               e.action)[1])
        return True
    except Exception:
        import logging
        logging.getLogger(__name__).info("Drag and drop not available", exc_info=True)
        return False


ARCHIVE_KINDS = archive.EXTENSIONS


# ---------- the main window's part (App.files) ----------
class FileAddons:
    """Everything Add from file adds to the main window: the button (right-click: Install
    from folder...), drag and drop, the "Other addons" rows with Uninstall, the scan and
    install threads and their queue messages ("file_*"), and files handed over by a second
    start of the app (am\\instance.py). The App calls build_button, build_rows, render,
    handle, after_load, poll_requests and close; everything else stays in here."""

    def __init__(self, app):
        self.app = app
        self.rows = []            # fileinstall.OtherRow
        self.cells = []
        self.states = {}          # folder -> "removing"
        self.dialog = None        # the open FileInstallDialog
        self.pending = None       # a path to open once the window is free
        self.show = True          # tests: False = dialogs are built hidden
        self.drop_ok = False
        self.installing = False
        self.ask_file = self._ask_file_dialog
        self.ask_folder = self._ask_folder_dialog
        self._grid_rows = 0

    # ----- building -----
    def build_button(self, parent, drop_widget):
        """The list box's footer (parent): a thin line, ADD_HINT at the left and Add file...
        at the right, under the row buttons (same width and right edge). drop_widget: the
        frame filling the window (tkdnd passes a drop on any widget inside it up to it; the
        Tk root itself can't be a drop target in tkinterdnd2)."""
        app = self.app
        tk.Frame(parent, height=1, background=theme.ROW_LINE).pack(
            side="top", fill="x", pady=(0, theme.px(6)))
        # A normal button (as tall as Change...), as wide as the row buttons.
        self.button = ttk.Button(parent, text=ADD_BUTTON, command=self.add_from_file,
                                 width=theme.button_chars(parent))
        self.button.pack(side="right")
        self.hint = ttk.Label(parent, text=ADD_HINT, style="Muted.TLabel",
                              background=theme.PANEL)
        self.hint.pack(side="left", padx=(0, theme.px(8)))
        self.tip = _tooltip(app, self.button, ADD_TIP + " " + FOLDER_TIP)
        self.menu = tk.Menu(app.root, tearoff=0, **theme.menu_options())
        self.menu.add_command(label=FILE_MENU, command=self.add_from_file)
        self.menu.add_command(label=FOLDER_MENU, command=self.add_from_folder)
        self.button.bind("<Button-3>", self._menu_popup)
        # The hint opens the same menu (and with press-and-hold on a touch screen).
        touch.bind_menu(self.hint, self._menu_popup)
        self.drop_ok = enable_drop(app.root, drop_widget, self.dropped)

    def hint_text(self):
        """ADD_HINT, or ADD_HINT_EMPTY while no other addon is installed (only where a
        file can be dropped on the window)."""
        return ADD_HINT_EMPTY if self.drop_ok and not self.rows else ADD_HINT

    def add_key(self, _event=None):
        """Ctrl+O: Add file... (ignored while busy)."""
        self.add_from_file()
        return "break"

    def _menu_popup(self, event):
        state = "normal" if self._free() else "disabled"
        for i in (0, 1):
            self.menu.entryconfigure(i, state=state)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def key(self):
        return tuple((r.folder, r.title) for r in self.rows)

    def refresh(self):
        """Rows from the settings + the tocs on disk; forgets folders that are gone."""
        app = self.app
        tracked = app.settings.get(fileinstall.SETTING)
        rows, kept = fileinstall.other_rows(app.addons_dir, tracked, app._guild_folders())
        if app.addons_dir and kept != fileinstall.clean_tracked(tracked):
            self._save_tracked(kept)
        self.rows = rows

    def _save_tracked(self, tracked):
        from am import config
        app = self.app
        app.settings = config.load_settings()
        if tracked:
            app.settings[fileinstall.SETTING] = tracked
        else:
            app.settings.pop(fileinstall.SETTING, None)
        config.save_settings(app.settings)

    def tracked(self):
        return fileinstall.clean_tracked(self.app.settings.get(fileinstall.SETTING))

    def build_rows(self, frame, start, col_gap):
        """The "Other addons" header + one row per addon, in the list's grid from row start."""
        for i in range(start, start + self._grid_rows):
            frame.rowconfigure(i, minsize=0)
        self.cells, self._grid_rows = [], 0
        if not self.rows:
            return
        # One row height with the guild rows (the list scrolls a row at a time).
        row_h = getattr(self.app, "_row_h", 0)
        gap = getattr(self.app, "_col_gap", lambda c: col_gap)
        head = ttk.Frame(frame, style="List.TFrame")
        head.grid(row=start, column=0, columnspan=5, sticky="sew", pady=(0, theme.px(2)))
        ttk.Label(head, text=OTHER_ADDONS, style="Head.TLabel",
                  background=theme.PANEL).pack(side="left", anchor="s")
        ttk.Label(head, text=OTHER_NOTE, style="Muted.TLabel",
                  background=theme.PANEL).pack(side="left", anchor="s", padx=(theme.px(8), 0))
        frame.rowconfigure(start, minsize=row_h)
        for n, _row in enumerate(self.rows):
            i = start + 1 + n
            labels = []
            for col in range(4):
                extra = {"foreground": theme.NAME, "font": theme.font("bold")} if col == 0 else {}
                label = ttk.Label(frame, background=theme.PANEL, **extra)
                label.grid(row=i, column=col, sticky="nsew", padx=(0, gap(col)))
                labels.append(label)
            button = ttk.Button(frame, width=theme.button_chars(frame),
                                style="Uninstall.Item.TButton")
            button.grid(row=i, column=4, sticky="e")
            for w in labels:
                touch.bind_menu(w, lambda e, k=n: self._row_menu_popup(e, k))
            button.bind("<Button-3>", lambda e, k=n: self._row_menu_popup(e, k))
            frame.rowconfigure(i, minsize=max(row_h, button.winfo_reqheight()))
            self.cells.append({"labels": labels, "button": button,
                               "name_tip": _tooltip(self.app, labels[0], "")})
        self._grid_rows = len(self.rows) + 1

    def render(self):
        app = self.app
        free = self._free()
        app._set_enabled(self.button, free)
        text = self.hint_text()
        if self.hint.cget("text") != text:
            self.hint.configure(text=text)
        for row, cells in zip(self.rows, self.cells):
            name, installed, latest, status = cells["labels"]
            shown = fit_title(row.title, self._title_px(), app._name_width)
            name.configure(text=shown)
            cells["name_tip"].text = row.title if shown != row.title else ""
            installed.configure(text=versions.display(row.version) if row.version else "?")
            latest.configure(text="-", style="Muted.TLabel")
            state = self.states.get(row.folder)
            if state == "removing":
                from am import errors
                status.configure(text=errors.REMOVING, style="TLabel")
            elif row.status == toc.NOT_FOREVER:
                status.configure(text=STATUS_NOT_FOREVER, style="Muted.TLabel")
            else:
                status.configure(text=FROM_FILE, style="Muted.TLabel")
            button = cells["button"]
            button.configure(text=UNINSTALL, command=lambda r=row: self.remove(r))
            app._set_enabled(button, not app.busy and bool(app.addons_dir))

    def _title_px(self):
        """How wide an Other addon's title may be: as wide as the widest guild addon name
        (so the titles never set the window's width), at least MIN_TITLE_PX."""
        app = self.app
        widest = max((app._name_width(r.name) for r in app.rows), default=0)
        return max(widest, MIN_TITLE_PX)

    def _free(self):
        app = self.app
        return bool(app.addons_dir) and not app.busy and not self.installing \
            and (self.dialog is None or self.dialog.closed)

    # ----- picking -----
    def _ask_file_dialog(self):
        from tkinter import filedialog
        return filedialog.askopenfilename(parent=self.app.root, title=TITLE, filetypes=FILE_TYPES)

    def _ask_folder_dialog(self):
        from tkinter import filedialog
        return filedialog.askdirectory(parent=self.app.root, mustexist=True,
                                       title=FOLDER_DIALOG_TITLE)

    def add_from_file(self):
        if not self._free():
            return False
        path = self.ask_file()
        return self.add_from_path(path) if path else False

    def add_from_folder(self):
        if not self._free():
            return False
        path = self.ask_folder()
        return self.add_from_path(path) if path else False

    def dropped(self, paths):
        if len(paths) != 1:
            self.app.set_message(ONE_FILE)
            return False
        return self.open_later(paths[0])

    def open_later(self, path):
        """Open a path now if the window is free, else as soon as it is (a drop or a handed-
        over file during the start-up check)."""
        if self._free():
            return self.add_from_path(path)
        self.pending = path
        return False

    def after_load(self):
        """Called when the list is loaded or a job finished: rows again + any waiting path."""
        if self.pending and self._free():
            path, self.pending = self.pending, None
            self.add_from_path(path)

    def poll_requests(self, take):
        """Files handed over by a second start of the app (take() -> [paths])."""
        try:
            paths = take()
        except Exception:
            log.exception("Reading handed-over files failed")
            return
        if paths:
            self._to_front()
            self.open_later(paths[-1])

    def _to_front(self):
        root = self.app.root
        if not self.show:
            return
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except tk.TclError:
            pass

    def add_from_path(self, path):
        app = self.app
        if not self._free() or not path:
            return False
        app.busy = True
        app.set_message(READING.format(name=os.path.basename(os.path.normpath(path))))
        app.render()
        threading.Thread(target=_scan_worker, daemon=True,
                         args=(app.queue, path, app.addons_dir,
                               sorted(app._guild_folders()), self.tracked(),
                               app.check_game)).start()
        return True

    # ----- installing -----
    def _install(self, dialog, folders):
        from am import backup_ui, config
        app = self.app
        self.installing = True
        app.busy = True
        app.render()
        threading.Thread(target=_install_worker, daemon=True,
                         args=(app.queue, app.check_game, dialog.plan, folders, app.addons_dir,
                               sorted(app._guild_folders()), self.tracked(),
                               backup_ui.backup_folder(config.load_settings()))).start()

    def _dialog_closed(self, dialog):
        if self.dialog is dialog:
            self.dialog = None
        self.app.render()
        self.after_load()

    def handle(self, msg):
        app = self.app
        kind = msg[0]
        if kind == "file_plan":
            app.busy = False
            plan, game = msg[1], bool(msg[2]) if len(msg) > 2 else False
            if app.closed:
                plan.close()
                return
            app.set_message("")
            app.set_banner(errors_game_text() if game else "")
            self.dialog = FileInstallDialog(app.root, plan, self._install,
                                            on_close=self._dialog_closed, show=self.show,
                                            game_running=game)
            app.render()
        elif kind == "file_scan_error":
            app.busy = False
            app.set_message(msg[1])
            app.render()
            self.after_load()
        elif kind == "file_progress":
            if self.dialog is not None and not self.dialog.closed:
                self.dialog.set_progress(msg[1], msg[2])
            if msg[2]:
                app.progress.configure(value=min(100, msg[1] * 100 // msg[2]))
        elif kind == "file_done":
            result, name = msg[1], msg[2]
            self.installing = False
            app.busy = False
            if result.installed:
                self._save_tracked(fileinstall.track(self.tracked(), result, name))
            self.refresh()
            app.set_message(done_message(result, name))
            if self.dialog is not None and not self.dialog.closed:
                self.dialog.finished_ok(result)
            app.render()
        elif kind == "file_error":
            self.installing = False
            app.busy = False
            text = message(msg[1])
            app.set_message(text)
            if self.dialog is not None and not self.dialog.closed:
                self.dialog.failed(msg[1])
            app.render()
        elif kind == "file_game":
            from am import errors
            app.set_banner(errors.GAME_RUNNING if msg[1] else "")
        elif kind == "file_removed":
            folder, title, where, text = msg[1], msg[2], msg[3], msg[4]
            from am import errors
            app.busy = False
            self.states.pop(folder, None)
            self._save_tracked(fileinstall.untrack(self.tracked(), folder))
            self.refresh()
            app.set_message(text or errors.removed_message(title, where))
            app.render()
        elif kind == "file_remove_error":
            app.busy = False
            self.states.pop(msg[1], None)
            app.set_message(msg[2])
            self.refresh()
            app.render()

    # ----- uninstall -----
    def _row_menu_popup(self, event, index):
        from am import errors
        app = self.app
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        can = bool(app.addons_dir and not app.busy)
        app.row_menu.entryconfigure(0, label=errors.remove_menu_label(row.title),
                                    state="normal" if can else "disabled",
                                    command=lambda r=row: self.remove(r))
        try:
            app.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            app.row_menu.grab_release()

    def remove(self, row):
        """Like a guild addon's Uninstall: ask, then to the Recycle Bin / Trash (or delete
        for good where there is none, after saying so). Only addons installed from a file."""
        from am import errors, installer
        app = self.app
        if app.busy or not app.addons_dir:
            return False
        path = os.path.join(app.addons_dir, row.folder)
        if not os.path.isdir(path):
            self.refresh()
            app.render()
            return False
        try:
            where = app.trash.available(app.addons_dir)
        except Exception:
            log.exception("Trash check failed")
            where = None
        others = [r.folder for r in app.rows if app._installed(r.folder)[0]]
        others += [r.folder for r in self.rows if r.folder != row.folder]
        needs = installer.needed_by(app.addons_dir, row.folder, others)
        names = {r.folder: r.name for r in app.rows}
        names.update({r.folder: r.title for r in self.rows})
        needs = [names.get(f, f) for f in needs]
        if not app.confirm(errors.remove_title(row.title),
                           errors.remove_text(row.title, where, needs),
                           errors.REMOVE_OK if where else errors.DELETE_OK,
                           errors.REMOVE_CANCEL):
            return False
        allowed = sorted(set(self.tracked()) | app._guild_folders())
        app.busy = True
        self.states[row.folder] = "removing"
        app.render()
        threading.Thread(target=_remove_worker, daemon=True,
                         args=(app.queue, row.folder, row.title, app.addons_dir, allowed,
                               app.trash.send if where else None, where)).start()
        return True

    @property
    def running(self):
        return self.installing

    def close(self):
        if self.dialog is not None:
            self.dialog.plan.close()


def _wheel(canvas, widget):
    """Mouse wheel scrolls canvas over widget and everything inside it (Windows
    <MouseWheel>, Linux Button-4/5)."""
    widget.bind("<MouseWheel>",
                lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"), add="+")
    widget.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"), add="+")
    widget.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"), add="+")
    for child in widget.winfo_children():
        _wheel(canvas, child)


def _tooltip(app, widget, text):
    from am import ui   # imported here: ui imports this module
    return ui.Tooltip(widget, text)


def errors_game_text():
    from am import errors
    return errors.GAME_RUNNING


def _scan_worker(q, path, addons_dir, guild_folders, tracked, check_game=None):
    """Thread: read the picked file/folder -> ("file_plan", Plan, WoW running) or
    ("file_scan_error", text)."""
    try:
        plan = fileinstall.scan(path, addons_dir, guild_folders, tracked)
        try:
            running = bool(check_game()) if check_game else False
        except Exception:
            running = False
        q.put(("file_plan", plan, running))
    except archive.ArchiveError as e:
        log.info("Add from file refused %s: %s", os.path.basename(path), e)
        q.put(("file_scan_error", message(e)))
    except Exception as e:
        log.exception("Add from file failed")
        q.put(("file_scan_error", message(e)))


def _install_worker(q, check_game, plan, folders, addons_dir, guild_folders, tracked,
                    safety_folder):
    """Thread: install the chosen addons -> ("file_done", Result, name) or ("file_error", e)."""
    try:
        running = bool(check_game())
    except Exception:
        running = False
    q.put(("file_game", running))
    try:
        result = fileinstall.install(plan, folders, addons_dir, guild_folders, tracked,
                                     safety_folder,
                                     progress=lambda d, t: q.put(("file_progress", d, t)))
        q.put(("file_done", result, plan.name))
    except Exception as e:
        log.exception("Install from file failed")
        q.put(("file_error", e))


def _remove_worker(q, folder, title, addons_dir, allowed, send, where):
    from am import errors, installer
    try:
        installer.remove(folder, addons_dir, allowed, send=send)
        q.put(("file_removed", folder, title, where, ""))
    except Exception as e:
        log.exception("Removing %s failed", folder)
        if getattr(e, "kind", "") == "partly":
            q.put(("file_removed", folder, title, None, errors.remove_message(e, title)))
        else:
            q.put(("file_remove_error", folder, errors.remove_message(e, title)))
