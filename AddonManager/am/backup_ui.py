"""The Backup window (main window's Backup button) and the small windows it opens: Make
a backup and Restore a backup. The work runs in a thread (am\\backup.py); results come
back through the App's queue as ("backup_size"/"backup_game"/"backup_clouds"/"backup_progress"/
"backup_done"/"backup_error"/"backup_last"/"restore_progress"/"restore_done"/
"restore_error", ...)."""

import datetime
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from am import backup, config, errors, sysenv, theme, versions

log = logging.getLogger(__name__)

# Linux fonts are wider (Noto/DejaVu 10 pt vs Segoe UI 9 pt): a bit less text width and
# tighter list/cloud buttons there, so both windows stay well under 420 px. Windows as before.
LINUX = sys.platform != "win32"
WRAP = 310 if LINUX else 330   # text width: keeps the windows narrow
SLIM_PADDING = (4, 4)          # Linux only: Cloud.TButton / Row.TButton
HUB_PADDING = (3, 3)           # Linux only: the Backup window's buttons (Hub.TButton)
CLOUD_COLUMNS = 3
MAX_CLOUD_BUTTONS = 9   # 3 rows
MAX_FOUND_ROWS = 8      # Backups found: older ones via Browse...
MAX_SAFETY_ROWS = 4     # safety copies, when ticked
PLACE_MAX = 12 if LINUX else 16        # characters of a folder name shown as a place
MENU_CHARS = 12 if LINUX else 16       # Restore's account pick: fixed width, long names cut
ACCOUNT_WRAP = 160 if LINUX else 210   # the "Settings of account ... go to:" text beside it


def fit_wrap(label, inset=0):
    """The label's text wraps at the width it really gets (the window may be wider or
    narrower than the wrap it started with). inset: the label's own padding + border."""
    def resized(event):
        want = event.width - inset
        if event.width > 1 and want > 20 and abs(
                int(float(str(label.cget("wraplength")) or 0)) - want) > 2:
            label.configure(wraplength=want)
    label.bind("<Configure>", resized, add="+")


def _wrapped_lines(f, text, width):
    """The lines of text when wrapped at width (greedy, at spaces, like Tk)."""
    lines, line = [], ""
    for word in text.split(" "):
        test = f"{line} {word}" if line else word
        if line and f.measure(test) > width:
            lines.append(line)
            line = word
        else:
            line = test
    return lines + [line]


def even_wrap(label):
    """No lone word on the last line: when the label's last line would be under a third
    of its wrap width, wrap a bit narrower (same number of lines, evened out)."""
    try:
        text = str(label.cget("text"))
        width = int(float(str(label.cget("wraplength")) or 0))
        name = (ttk.Style(label).lookup(str(label.cget("style")) or "TLabel", "font")
                or theme.font())
        f = tkfont.Font(root=label, font=name)
    except (tk.TclError, ValueError):
        return
    if width <= 0 or "\n" in text:
        return
    lines = _wrapped_lines(f, text, width)
    if len(lines) < 2 or f.measure(lines[-1]) * 3 >= width:
        return
    lo, hi = width // 2, width   # the narrowest width with the same number of lines
    while lo < hi:
        mid = (lo + hi) // 2
        if len(_wrapped_lines(f, text, mid)) == len(lines):
            hi = mid
        else:
            lo = mid + 1
    # Tk counts the space after a line's last word too: room for it, plus a little.
    label.configure(wraplength=min(width, hi + f.measure(" ") + theme.px(2)))


def game_strip(parent, text=None):
    """The "WoW is open" strip (banner colours) of Make a backup / Send (not packed here)."""
    pad = theme.px(6)
    label = tk.Label(parent, text=text or errors.BACKUP_GAME_OPEN, background=theme.BANNER_BG,
                     foreground=theme.BANNER_TEXT, anchor="w", justify="left",
                     wraplength=theme.px(WRAP), padx=pad, pady=theme.px(3), font=theme.font())
    fit_wrap(label, inset=2 * (pad + int(float(str(label.cget("borderwidth"))))))
    return label


def open_folder(path):
    """Show a folder in Explorer / the file manager (sysenv: the system's environment on
    Linux). Never raises."""
    return sysenv.open_folder(path)


def backup_folder(settings):
    """The player's backup folder (picked before), else Documents\\AddonManager backups."""
    folder = settings.get("backup_folder")
    if isinstance(folder, str) and os.path.isabs(folder) and "\0" not in folder:
        return folder
    return backup.default_folder()


def owner_window(app):
    """The window the backup dialogs open over: the Backup window, else Settings (the
    Licences window), else the main window."""
    if app.backup_win is not None:
        return app.backup_win.win
    return app.settings_win if app.settings_win is not None else app.root


class BackupWindow:
    """The Backup window (the main window's Backup button): Make / Restore on this PC,
    Send / Receive with another PC (only with a relay), the last backup's date and place.
    Modal like Settings; can't close while a backup or restore runs."""

    def __init__(self, app, show=True):
        self.app = app
        self.shown = show
        self.closed = False
        self.last = None          # FoundBackup of the newest own backup, once found
        self._looking = 0         # searches started (only the newest one's answer counts)
        win = self.win = tk.Toplevel(app.root)
        win.withdraw()
        win.title(errors.BACKUP_HEADING)
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(app.root)
        pad = theme.px(14)
        frame = ttk.Frame(win, padding=pad)
        frame.pack(fill="both", expand=True)
        art = theme.frame_window(win, frame, corners=True)
        # The knots sit in the corners: keep the content out of their squares.
        c = max(0, art.clear - art.margin - pad) + theme.px(4)
        frame.configure(padding=(pad + c, pad, pad + c, pad + c))
        theme.heading(frame, errors.BACKUP_HEADING).pack(fill="x", padx=(0, 0))
        ttk.Label(frame, text=errors.BACKUP_NOTE, style="Muted.TLabel", wraplength=theme.px(WRAP),
                  justify="left").pack(anchor="w", pady=(theme.px(2), 0))
        self.make_button, self.restore_button = self._pair(
            frame, errors.BACKUP_THIS_PC, (errors.BACKUP_BUTTON, app.open_backup, True),
            (errors.RESTORE_BUTTON, app.open_restore, False))
        self.share_buttons = []
        if app.share_url:
            self.share_buttons = list(self._pair(
                frame, errors.BACKUP_OTHER_PC, (errors.SEND_BUTTON, app.open_send, False),
                (errors.RECEIVE_BUTTON, app.open_receive, False)))
        self.last_var = tk.StringVar(value=errors.BACKUP_LAST_LOOKING)
        ttk.Label(frame, textvariable=self.last_var, wraplength=theme.px(WRAP), justify="left").pack(
            anchor="w", pady=(theme.px(12), 0))
        # Open folder + Old backups...: a pair row under the last-backup line, packed only
        # once something is found (Open folder: an own backup; Old backups...: any file,
        # safety copies too). Old backups... opens am\backup_delete_ui.py's window.
        self.found_row = ttk.Frame(frame)
        self.open_button = self._pair_button(self.found_row, errors.BACKUP_OPEN_BACKUPS,
                                             self.open_result, False)
        self.old_button = self._pair_button(self.found_row, errors.DELETE_OPEN,
                                            self.open_old, False)
        from am import ui   # imported here: ui imports this module
        # Tooltips: why a button is greyed (render); Old backups... says what it does.
        self.tips = {b: ui.Tooltip(b, "") for b in self.buttons()}
        self.tips[self.old_button] = ui.Tooltip(self.old_button, errors.DELETE_OPEN_TIP)
        # The status line takes room only while it has text (no empty gap).
        self.status_var = tk.StringVar()
        self.status = ttk.Label(frame, textvariable=self.status_var,
                                wraplength=theme.px(WRAP), justify="left")
        bar = self.bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(theme.px(12), 0))
        self.status_var.trace_add("write", lambda *_a: self._place_status())
        self.close_button = self._pair_button(bar, errors.CLOSE, app.close_backup_window, False)
        bar.columnconfigure(0, weight=1)   # Close sits in the right-hand pair column
        self._cell(self.close_button, 1)
        self._even_pairs()
        win.protocol("WM_DELETE_WINDOW", app.close_backup_window)
        win.bind("<Escape>", lambda _e: app.close_backup_window())
        self.render()
        self.refresh()
        if show:
            win.update_idletasks()
            root = app.root
            x = root.winfo_rootx() + max(0, (root.winfo_width() - win.winfo_reqwidth()) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{x}+{y}")
            win.deiconify()
            theme.dark_title_bar(win)
            self.make_button.focus_set()
            win.grab_set()

    def _place_status(self):
        try:
            if not self.status_var.get():
                self.status.pack_forget()
            elif not self.status.winfo_manager():
                self.status.pack(anchor="w", pady=(theme.px(4), 0), before=self.bar)
        except tk.TclError:
            pass   # the window is closing

    def _pair(self, frame, heading, left, right):
        """A gold heading and two equal buttons side by side."""
        ttk.Label(frame, text=heading, style="Head.TLabel", font=theme.head_font(1)).pack(
            anchor="w", pady=(theme.px(12), 0))
        grid = ttk.Frame(frame)
        grid.pack(anchor="w", pady=(theme.px(4), 0))
        out = []
        for col, (text, command, accent) in enumerate((left, right)):
            # The small windows open shown only when this one is (tests: never shown).
            b = self._pair_button(grid, text, lambda c=command: c(show=self.shown), accent)
            self._cell(b, col)
            out.append(b)
        return out

    def _pair_button(self, parent, text, command, accent):
        """One of the window's buttons. Linux: `HUB_PADDING` (wider fonts; with "Send to
        another PC..." the window stays well inside 420 px)."""
        slim = "Hub." if LINUX else ""
        if LINUX:
            for name in ("Hub.TButton", "Hub.Accent.TButton"):
                ttk.Style(parent).configure(name, padding=tuple(theme.px(n) for n in HUB_PADDING))
        return ttk.Button(parent, text=text, command=command,
                          style=slim + ("Accent.TButton" if accent else "TButton"))

    @staticmethod
    def _cell(button, col):
        """A button in a pair row's column (the gap is split over the two cells)."""
        button.grid(row=0, column=col, sticky="ew",
                    padx=(0, theme.px(3)) if col == 0 else (theme.px(3), 0))

    def _even_pairs(self):
        """Every button of this window gets one width: the same text width in characters
        (the widest text, at least a row button) and, for the pair rows, one fixed column
        width in pixels (no stretching, so 1 px font rounding can never make two buttons
        differ). The pair rows are at least as wide as the text lines, so Close (packed
        right) ends where the pair rows end."""
        buttons = [*self.buttons(), self.open_button, self.old_button, self.close_button]
        style = str(buttons[1].cget("style")) or "TButton"
        width = max(theme.button_chars(self.win, style),
                    theme.chars(self.win, [b.cget("text") for b in buttons], style))
        for b in buttons:
            b.configure(width=width)
        self.win.update_idletasks()
        gap = theme.px(3)
        column = max(max(b.winfo_reqwidth() for b in buttons) + gap,
                     -(-theme.px(WRAP) // 2))
        for b in buttons:
            if b.master is self.bar:
                self.bar.columnconfigure(1, minsize=column, weight=0)
            else:
                b.master.columnconfigure(0, minsize=column, weight=0)
                b.master.columnconfigure(1, minsize=column, weight=0)
        self.column_px = column

    def buttons(self):
        return [self.make_button, self.restore_button, *self.share_buttons]

    def render(self):
        """Actions only while nothing else runs and the WoW folder is known."""
        ok = not self.app.busy and bool(self.app.addons_dir)
        why = (errors.BUSY_TIP if self.app.busy else
               errors.BACKUP_NO_FOLDER_TIP if not self.app.addons_dir else "")
        for b in (*self.buttons(), self.old_button):
            b.state(["!disabled"] if ok else ["disabled"])
            tip = self.tips.get(b)
            if tip is not None:
                tip.text = why or (errors.DELETE_OPEN_TIP if b is self.old_button else "")

    def refresh(self):
        """Look for the newest own backup again (in a thread)."""
        if self.closed:
            return
        self._looking += 1
        threading.Thread(target=_last_worker, daemon=True,
                         args=(self.app.queue, self._looking,
                               backup_folder(config.load_settings()))).start()

    def set_status(self, text):
        self.status_var.set(text)

    def handle(self, msg):
        """("backup_last", search number, FoundBackup or None[, any backup found]).
        The last item also counts safety copies and received files (Old backups...)."""
        if self.closed or msg[1] != self._looking:
            return
        self.last = f = msg[2]
        any_found = bool(msg[3]) if len(msg) > 3 else bool(f)
        self.last_var.set(errors.backup_last(found_date(f.mtime), f.place) if f
                          else errors.BACKUP_LAST_NONE)
        self.open_button.grid_remove()
        self.old_button.grid_remove()
        if f:
            self._cell(self.open_button, 0)
        if any_found:
            self._cell(self.old_button, 1 if f else 0)
        try:
            if not (f or any_found):
                self.found_row.pack_forget()
            elif not self.found_row.winfo_manager():
                below = self.status if self.status.winfo_manager() else self.bar
                self.found_row.pack(anchor="w", pady=(theme.px(6), 0), before=below)
        except tk.TclError:
            pass   # the window is closing

    def open_result(self):
        if self.last is not None:
            self.app.open_folder(os.path.dirname(self.last.path))

    def open_old(self):
        """The Delete old backups window over this one."""
        from am import backup_delete_ui   # imported here: it imports this module
        return backup_delete_ui.open_delete(self.app, show=self.shown)

    def destroy(self):
        self.closed = True
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass


def spoken_answer():
    """The remembered answer about the Spoken addons: "skip", "keep" or None (never asked)."""
    a = config.load_settings().get("backup_spoken")
    return a if a in ("skip", "keep") else None


def save_spoken_answer(answer):
    settings = config.load_settings()
    settings["backup_spoken"] = answer
    config.save_settings(settings)


def _has_spoken(addons_dir):
    try:
        return any(backup.is_spoken(f) for f in backup.addon_folders(addons_dir))
    except (backup.BackupError, OSError):
        return False


class SpokenAsk:
    """The one-time question over a Make backup / Send window: leave the Spoken addons'
    folders out? done("skip" / "keep") saves the answer; Esc or closing keeps them this
    time without saving (it asks again next time)."""

    def __init__(self, dialog, spoken, others, done, show=True):
        self.done_cb = done
        self.closed = False
        self.shown = show
        self.parent = dialog.win
        total = sum(size for _f, size in spoken)
        win = self.win = tk.Toplevel(self.parent)
        win.withdraw()
        win.title(errors.SPOKEN_TITLE)
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(self.parent)
        frame = ttk.Frame(win, padding=theme.px(14))
        frame.pack(fill="both", expand=True)
        theme.frame_window(win, frame)
        theme.heading(frame, errors.SPOKEN_TITLE).pack(fill="x")
        for text, style, gap in (
                (errors.spoken_found([f for f, _s in spoken], total), "TLabel", 10),
                (errors.SPOKEN_WHY, "TLabel", 8),
                (errors.SPOKEN_KEPT, "Muted.TLabel", 8),
                (errors.spoken_sizes(others, others + total), "Muted.TLabel", 8)):
            ttk.Label(frame, text=text, style=style, wraplength=theme.px(WRAP), justify="left").pack(
                anchor="w", pady=(gap, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(theme.px(14), 0))
        self.keep_button = ttk.Button(buttons, text=errors.SPOKEN_KEEP,
                                      command=lambda: self.answer("keep"))
        self.keep_button.pack(side="right")
        self.skip_button = ttk.Button(buttons, text=errors.SPOKEN_SKIP, style="Accent.TButton",
                                      command=lambda: self.answer("skip"))
        self.skip_button.pack(side="right", padx=(0, theme.px(6)))
        theme.even_buttons(buttons)
        win.protocol("WM_DELETE_WINDOW", lambda: self.answer(None))
        win.bind("<Escape>", lambda _e: self.answer(None))
        win.bind("<Return>", lambda _e: self.answer("skip"))
        if show:
            win.update_idletasks()
            p = self.parent
            x = p.winfo_rootx() + max(0, (p.winfo_width() - win.winfo_reqwidth()) // 2)
            y = p.winfo_rooty() + max(0, (p.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{x}+{y}")
            win.deiconify()
            theme.dark_title_bar(win)
            self.skip_button.focus_set()
            try:
                win.grab_set()
            except tk.TclError:
                pass

    def answer(self, answer):
        if self.closed:
            return
        self.closed = True
        if answer is not None:
            save_spoken_answer(answer)
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass
        if self.shown:
            try:
                self.parent.grab_set()
            except tk.TclError:
                pass
        self.done_cb(answer)


class _SpokenCheck:
    """For a window that makes a backup: the Spoken addons found (a thread looks), and the
    question the first time. Call spoken_ready() with the scan; ask_spoken() in start()."""

    def _spoken_init(self):
        self.spoken = None        # [(folder, bytes)] once looked at
        self.others = 0           # bytes of the other addon folders
        self.spoken_once = None   # "keep" for this backup only (question closed with Esc)
        self.spoken_pending = False
        self.spoken_ask = None

    def spoken_ready(self, spoken, total):
        self.spoken = spoken
        self.others = max(0, total - sum(size for _f, size in spoken))
        self.spoken_shown()
        if self.spoken_pending:
            self.spoken_pending = False
            self.start()

    def ask_spoken(self):
        """True when start() must wait (still looking, or the question is open)."""
        if self.spoken is None and not _has_spoken(self.app.addons_dir):
            self.spoken = []          # the names alone say there are none: no need to wait
        if self.spoken is None:
            self.spoken_pending = True
            self.set_status(errors.SPOKEN_CHECKING)
            return True
        if self.spoken and spoken_answer() is None and self.spoken_once is None:
            if self.spoken_ask is None or self.spoken_ask.closed:
                self.spoken_ask = SpokenAsk(self, self.spoken, self.others,
                                            self._spoken_answered, show=self.shown)
            return True
        return False

    def _spoken_answered(self, answer):
        self.spoken_once = "keep" if answer is None else None
        self.spoken_changed(answer)
        if not self.closed:
            self.start()

    def spoken_changed(self, answer):
        pass

    def spoken_shown(self):
        pass

    def leave_out(self):
        """The Spoken folders to leave out of this backup."""
        if not self.spoken or self.spoken_once == "keep":
            return ()
        return tuple(f for f, _s in self.spoken) if self.spoken_skip() else ()

    def spoken_skip(self):
        return spoken_answer() == "skip"


class _Dialog:
    """A small dark modal window over the Backup window, the Settings window (Licences)
    or the main window."""

    def __init__(self, app, title, show):
        self.app = app
        self.running = False
        self.closed = False
        self.shown = show
        self.parent = owner_window(app)
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
        self.frame = ttk.Frame(win, padding=theme.px(14))
        self.frame.pack(fill="both", expand=True)
        theme.frame_window(win, self.frame)
        theme.heading(self.frame, title).pack(fill="x")
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())

    def _status_area(self, collapse=False):
        """Status line, progress bar (packed when used) and the button row. collapse:
        the status line takes no room while it is empty (no gap above the buttons)."""
        self.status_var = tk.StringVar()
        self.status = ttk.Label(self.frame, textvariable=self.status_var, wraplength=theme.px(WRAP),
                                justify="left")
        # Wraps at the width the window really has (px(WRAP) could be wider than the rest
        # of the content and widen the window when a long message shows).
        fit_wrap(self.status)
        if not collapse:
            self.status.pack(anchor="w", fill="x", pady=(theme.px(10), 0))
        self.progress = ttk.Progressbar(self.frame, mode="determinate", maximum=100)
        self.buttons = ttk.Frame(self.frame)
        self.buttons.pack(fill="x", pady=(theme.px(12), 0))
        if collapse:
            self.status_var.trace_add("write", lambda *_a: self._place_status())

    def _place_status(self):
        """Show the status line only while it has text (collapse mode)."""
        try:
            if not self.status_var.get():
                self.status.pack_forget()
            elif not self.status.winfo_manager():
                below = self.progress if self.progress.winfo_manager() else self.buttons
                # px(6): a little tighter than a full section gap (Send must fit a 1080p
                # screen at 150 % with every line shown, test_look)
                self.status.pack(anchor="w", fill="x", pady=(theme.px(6), 0), before=below)
        except tk.TclError:
            pass   # the window is closing

    def _show(self, focus, extra=()):
        # The button row: one width (>= a row button), measured in the real font.
        # extra: texts a button gets later (the width never changes then).
        theme.even_buttons(self.buttons, (errors.CLOSE, errors.CANCEL) + tuple(extra))
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
        self._status_wrap()
        self.status_var.set(text)

    def _status_wrap(self):
        """Before a message shows: wrap it at the width the window has now (never wider
        than px(WRAP)), so a long message can't widen the window; fit_wrap follows later."""
        try:
            width = self.buttons.winfo_width()   # the button row fills the content width
            if width > 20:
                self.status.configure(wraplength=min(theme.px(WRAP), width))
        except tk.TclError:
            pass   # the window is closing

    def set_progress(self, done, total):
        if not self.progress.winfo_manager():
            self.progress.pack(fill="x", pady=(theme.px(6), 0), before=self.buttons)
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
        if self.parent is not self.app.root:
            if self.shown:
                try:
                    self.parent.grab_set()   # the Backup (or Settings) window is modal again
                except tk.TclError:
                    pass
            hub = self.app.backup_win
            if hub is not None and self.parent is hub.win:
                hub.refresh()   # a backup may have been made or received

    def _begin(self, text):
        self.running = True
        self.app.busy = True
        self.app.render()
        self.set_status(text)
        self.set_progress(0, 1)

    def _end(self):
        self.running = False


NOTICES_NAME = "THIRD-PARTY-NOTICES.txt"
NOTICES_CHARS = 40 if LINUX else 52     # text width: the window stays under 420 px


def notices_path():
    """THIRD-PARTY-NOTICES.txt: inside the built program (next to its unpacked files),
    or the AddonManager folder when run from source."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))
    return os.path.join(base, NOTICES_NAME)


def read_notices():
    try:
        with open(notices_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return errors.LICENCES_MISSING


SPOKEN_STYLE = "Wrap.TCheckbutton"   # the Spoken tick: its text wraps (style wraplength)


class LicenceDialog(_Dialog):
    """Licences (from Settings): the bundled THIRD-PARTY-NOTICES.txt, read-only."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.LICENCES_TITLE, show)
        box = ttk.Frame(self.frame)
        box.pack(fill="both", expand=True, pady=(theme.px(8), 0))
        # wrap none: the notices file has its own line breaks and tables; a sideways
        # scrollbar shows the rest of a long line.
        self.text = tk.Text(box, width=NOTICES_CHARS, height=24, wrap="none",
                            background=theme.FIELD, foreground=theme.TEXT,
                            insertbackground=theme.TEXT, relief="flat", borderwidth=0,
                            highlightthickness=1, highlightbackground=theme.BORDER,
                            font=theme.font(bigger=-1), padx=theme.px(6), pady=theme.px(4))
        bar = self.vbar = ttk.Scrollbar(box, orient="vertical", command=self.text.yview,
                                        style="Gold.Vertical.TScrollbar")
        self.hbar = ttk.Scrollbar(box, orient="horizontal", command=self.text.xview,
                                  style="Gold.Horizontal.TScrollbar")
        self.text.configure(yscrollcommand=bar.set, xscrollcommand=self.hbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns", padx=(theme.px(4), 0))
        self.hbar.grid(row=1, column=0, sticky="ew", pady=(theme.px(4), 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        notices = read_notices()
        if notices == errors.LICENCES_MISSING:   # one sentence: wrap it
            self.text.configure(wrap="word")
            self.hbar.grid_remove()
        self.text.insert("1.0", notices)
        self.text.configure(state="disabled")   # read-only (selecting and copying work)
        self.buttons = ttk.Frame(self.frame)
        self.buttons.pack(fill="x", pady=(theme.px(10), 0))
        self.close_button = ttk.Button(self.buttons, text=errors.CLOSE, command=self.close)
        self.close_button.pack(side="right")
        self._show(self.close_button)

    def handle(self, msg):
        return


class BackupDialog(_SpokenCheck, _Dialog):
    """Make a backup: what to include, where it goes, the don't-share line."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.BACKUP_TITLE, show)
        settings = config.load_settings()
        self.folder = backup_folder(settings)
        self.path = None
        self.cancel_event = None    # set by Cancel/Esc while making: the zip stops
        self.left_out = False       # Spoken addons left out of this run (kept in the text)
        self.settings_var = tk.BooleanVar(value=settings.get("backup_settings") is not False)
        self.addons_var = tk.BooleanVar(value=settings.get("backup_addons") is not False)
        self.settings_check = ttk.Checkbutton(self.frame, text=errors.BACKUP_SETTINGS,
                                              variable=self.settings_var)
        self.settings_check.pack(anchor="w", pady=(theme.px(10), 0))
        self.addons_check = ttk.Checkbutton(self.frame, text=errors.BACKUP_ADDONS,
                                            variable=self.addons_var)
        self.addons_check.pack(anchor="w")
        self._spoken_init()
        self.spoken_var = tk.BooleanVar(value=spoken_answer() != "keep")
        self.spoken_check = None    # shown only when Spoken addons are there
        self.game_open = False      # WoW was open at the last check (strip shown)
        self.made_with_game = False   # this run: WoW open when the backup was made

        where = self.where = ttk.Frame(self.frame)
        where.pack(fill="x", pady=(theme.px(10), 0))
        # WoW open: the backup still works; the strip says what it holds (shown by the
        # game check when the window opens, or at Make time).
        self.game_label = game_strip(self.frame)
        ttk.Label(where, text=errors.SAVED_TO).pack(side="left", anchor="s")
        self.change_button = ttk.Button(where, text=errors.BACKUP_CHANGE,
                                        command=self.change_folder,
                                        width=theme.button_chars(where))
        self.change_button.pack(side="right")
        self.folder_var = tk.StringVar(value=self.folder)
        ttk.Label(self.frame, textvariable=self.folder_var, style="Muted.TLabel",
                  wraplength=theme.px(WRAP), justify="left").pack(
            anchor="w", fill="x", pady=(theme.px(4), 0))
        self.cloud_buttons = []
        self.cloud_area = ttk.Frame(self.frame)   # filled when the cloud check is done
        self.cloud_area.pack(anchor="w", fill="x")
        threading.Thread(target=_cloud_worker, daemon=True, args=(app.queue,)).start()
        ttk.Label(self.frame, text=errors.BACKUP_PRIVATE, style="Muted.TLabel",
                  wraplength=theme.px(WRAP), justify="left").pack(
            anchor="w", pady=(theme.px(10), 0))
        self._status_area(collapse=True)
        # The progress bar is there from the start (empty), so the window never changes
        # size when the backup begins.
        self.progress.pack(fill="x", pady=(theme.px(6), 0), before=self.buttons)
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.make_button = ttk.Button(self.buttons, text=errors.BACKUP_MAKE,
                                      style="Accent.TButton", command=self.start)
        self.make_button.pack(side="right", padx=(0, theme.px(6)))
        self.open_button = ttk.Button(self.buttons, text=errors.BACKUP_OPEN_FOLDER,
                                      command=self.open_result)
        threading.Thread(target=_size_worker, daemon=True,
                         args=(app.queue, app.addons_dir, app.check_game)).start()
        self._show(self.make_button)

    def show_game(self, running):
        """WoW open: the strip under the ticks (never hidden again while the window is
        open: the backup made now still holds the settings of the last /reload)."""
        if running and not self.game_open and not self.closed:
            self.game_open = True
            self.game_label.pack(fill="x", pady=(theme.px(6), 0), before=self.where)

    def _set_folder(self, folder):
        self.folder = folder
        self.folder_var.set(folder)
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
                  wraplength=theme.px(WRAP), justify="left").pack(anchor="w", pady=(theme.px(8), 0))
        grid = ttk.Frame(self.cloud_area)
        grid.pack(fill="x", pady=(theme.px(2), 0))
        for c in range(CLOUD_COLUMNS):
            grid.columnconfigure(c, weight=1, uniform="cloud")
        for i, (label, path) in enumerate(clouds[:MAX_CLOUD_BUTTONS]):
            row, col = divmod(i, CLOUD_COLUMNS)
            b = ttk.Button(grid, text=label, style="Cloud.TButton",
                           command=lambda p=path: self.use_cloud(p))
            gap = theme.px(3)
            b.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else gap, 0 if
                   col == CLOUD_COLUMNS - 1 else gap), pady=(0 if row == 0 else theme.px(6), 0))
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
        if addons and self.ask_spoken():
            return True
        leave_out = self.leave_out() if addons else ()
        self.spoken_once = None
        settings = config.load_settings()
        settings["backup_addons"], settings["backup_settings"] = addons, settings_on
        config.save_settings(settings)
        self.open_button.pack_forget()
        # Cancel stays live: it stops the backup (the half-written file is removed).
        for w in (self.make_button, self.change_button,
                  self.settings_check, self.addons_check, *self.cloud_buttons,
                  *([self.spoken_check] if self.spoken_check else [])):
            w.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        self.left_out = bool(leave_out)
        self.made_with_game = False
        self.cancel_event = threading.Event()
        self._begin(errors.BACKUP_MAKING + (" " + errors.SPOKEN_LEFT_OUT if leave_out else ""))
        # Moving at once while the files are listed; filled by bytes written from the
        # first "backup_progress" on (see handle).
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        threading.Thread(target=_backup_worker, daemon=True,
                         args=(self.app.queue, self.app.check_game, self.app.addons_dir,
                               self.folder, addons, settings_on, leave_out,
                               other_record(self.app), self.cancel_event)).start()
        return True

    def close(self):
        """Cancel/Esc/X while making: stop the backup (the window stays to say so)."""
        if self.running:
            self.stop()
            return
        super().close()

    def stop(self):
        if not self.running or self.cancel_event is None or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.state(["disabled"])
        self.set_status(errors.BACKUP_STOPPING)

    def spoken_skip(self):
        return bool(self.spoken_var.get())

    def spoken_changed(self, answer):
        # Esc (None): kept for this backup, so the tick shows them kept too.
        self.spoken_var.set(answer == "skip")
        self._addons_label()

    def save_spoken(self):
        save_spoken_answer("skip" if self.spoken_var.get() else "keep")
        self._addons_label()

    def _addons_label(self):
        skip = self.spoken and self.spoken_var.get() and spoken_answer() is not None
        total = self.others + (0 if skip else sum(s for _f, s in self.spoken or []))
        self.addons_check.configure(text=errors.addons_label(total))

    def spoken_shown(self):
        spoken = self.spoken
        if spoken and self.spoken_check is None and not self.closed:
            # One tick whose text wraps (a wide Linux font keeps the window <= 420 px), so
            # it lines up with the two ticks above. spoken_label: the same widget.
            ttk.Style(self.win).configure(SPOKEN_STYLE, wraplength=theme.px(WRAP - 30))
            self.spoken_check = ttk.Checkbutton(
                self.frame, variable=self.spoken_var, command=self.save_spoken,
                text=errors.spoken_tick(sum(s for _f, s in spoken)), style=SPOKEN_STYLE)
            self.spoken_check.pack(anchor="w", after=self.addons_check)
            self.spoken_label = self.spoken_check
            if self.running:
                self.spoken_check.state(["disabled"])
        self._addons_label()

    def _determinate(self):
        if str(self.progress.cget("mode")) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    def open_result(self):
        self.app.open_folder(os.path.dirname(self.path) if self.path else self.folder)

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("backup_"):
            return
        if kind == "backup_size":
            self.spoken_ready(msg[2], msg[1])
            self.show_game(len(msg) > 3 and msg[3])
            return
        if kind == "backup_game":   # WoW open at Make time: the backup goes on
            self.made_with_game = bool(msg[1])
            self.show_game(msg[1])
            return
        if kind == "backup_clouds":
            self.show_clouds(msg[1])
            return
        if kind == "backup_progress":
            if not self.running:
                return
            self._determinate()
            self.set_progress(msg[1], msg[2])
            if not (self.cancel_event and self.cancel_event.is_set()):   # keep "Stopping..."
                self.set_status(errors.backup_making(int(self.progress.cget("value")),
                                                     self.left_out))
            return
        self._determinate()
        self._end()
        for w in (self.make_button, self.change_button, self.cancel_button,
                  self.settings_check, self.addons_check, *self.cloud_buttons,
                  *([self.spoken_check] if self.spoken_check else [])):
            w.state(["!disabled"])
        if kind == "backup_done":
            self.path = msg[1]
            self.set_progress(1, 1)
            self.set_status(errors.backup_saved(os.path.basename(msg[1]), msg[2],
                                                self.made_with_game,
                                                msg[3] if len(msg) > 3 else ()))
            self.cancel_button.configure(text=errors.CLOSE)
            self.open_button.pack(side="left")
        else:
            self.progress.configure(value=0)
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
            anchor="w", pady=(theme.px(6), 0))
        self.rows = ttk.Frame(self.frame)
        self.rows.pack(fill="x", pady=(theme.px(4), 0))
        self.safety_var = tk.BooleanVar(value=False)
        # The tick's text wraps (wider Linux fonts would widen the window past 420 px).
        ttk.Style(self.win).configure(SPOKEN_STYLE, wraplength=theme.px(WRAP - 30))
        self.safety_check = ttk.Checkbutton(self.frame, text=errors.FOUND_SAFETY,
                                            variable=self.safety_var, command=self.render,
                                            style=SPOKEN_STYLE)
        self.safety_check.pack(anchor="w", pady=(theme.px(8), 0))
        self.note = ttk.Label(self.frame, text=errors.FOUND_NOTE, style="Muted.TLabel",
                              wraplength=theme.px(WRAP), justify="left")
        self.note.pack(anchor="w", fill="x", pady=(theme.px(6), 0))
        fit_wrap(self.note)
        self._status_area(collapse=True)
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right", padx=(theme.px(12), 0))
        self.browse_button = ttk.Button(self.buttons, text=errors.FOUND_BROWSE,
                                        command=self.browse)
        self.browse_button.pack(side="left")
        self.code_button = None
        if app.share_url:   # Receive from another PC instead (am\share_ui.py)
            self.code_button = ttk.Button(self.buttons, text=errors.USE_CODE,
                                          command=self.use_code)
            self.code_button.pack(side="left", padx=(theme.px(6), 0))
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
        b.pack(fill="x", pady=(0, theme.px(4)))
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
                anchor="w", pady=(theme.px(4), theme.px(2)))
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
        for w in (*self.row_buttons, self.browse_button, self.safety_check,
                  *([self.code_button] if self.code_button else [])):
            w.state(["disabled"])
        self.set_status(errors.FOUND_DOWNLOADING if online else errors.FOUND_OPENING)
        threading.Thread(target=_open_worker, daemon=True,
                         args=(self.app.queue, path)).start()

    def use_code(self):
        """Close this list and open the Receive window in its place."""
        if self.opening or self.closed:
            return None
        app, show = self.app, self.shown
        self.close()
        return app.open_receive(show=show)

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
            app.backup_dialog = RestoreDialog(app, msg[2], show=show, back=True)
        elif kind == "restore_open_error" and msg[1] == self.opening:
            self.opening = None
            for w in (*self.row_buttons, self.browse_button, self.safety_check,
                  *([self.code_button] if self.code_button else [])):
                w.state(["!disabled"])
            self.set_status(msg[2])


class RestoreDialog(_Dialog):
    """Restore a backup: what it holds, which account gets the settings, what to restore."""

    def __init__(self, app, info, show=True, back=False):
        """back: opened from the Backups found list (Back / Esc go back there)."""
        super().__init__(app, errors.RESTORE_TITLE, show)
        self.info = info
        self.result = None
        self.menu_buttons = []   # the account picks (greyed while restoring)
        from_label = ttk.Label(self.frame, text=errors.backup_from(info),
                               wraplength=theme.px(WRAP), justify="left")
        from_label.pack(anchor="w", pady=(theme.px(6), 0))
        even_wrap(from_label)   # no lone word on its last line
        self.local = backup.local_accounts(app.addons_dir)
        # A file from another PC (send code): the player always picks their own account
        # for each one in the file; nothing is pre-filled, not even the same name.
        received = getattr(info, "received", False)
        self.account_map = ({acc: None for acc in info.accounts} if received
                            else backup.default_map(sorted(info.accounts), self.local))
        self.account_vars = {}
        if info.accounts and self.local:
            for acc in sorted(info.accounts):
                if acc in self.local and not received:
                    continue   # same account here: nothing to ask
                line = ttk.Frame(self.frame)
                line.pack(fill="x", pady=(theme.px(8), 0))
                ttk.Label(line, text=errors.account_line(acc), wraplength=theme.px(ACCOUNT_WRAP),
                          justify="left").pack(side="left", anchor="w")
                var = tk.StringVar(value=self.account_map[acc] or (
                    errors.RESTORE_CHOOSE if received else errors.RESTORE_SKIP))
                button = ttk.Menubutton(line, textvariable=var, width=MENU_CHARS)
                menu = tk.Menu(button, tearoff=0, **theme.menu_options())
                for name in self.local + [errors.RESTORE_SKIP]:
                    menu.add_radiobutton(label=name, value=name, variable=var)
                button["menu"] = menu
                button.pack(side="right")
                self.menu_buttons.append(button)
                self.account_vars[acc] = var
        can_settings = bool(self.local)
        self.addons_var = tk.BooleanVar(value=bool(info.addons) and not received)
        self.settings_var = tk.BooleanVar(value=can_settings and info.settings_files > 0)
        self.bindings_var = tk.BooleanVar(value=can_settings and info.binding_files > 0)
        checks = ttk.Frame(self.frame)
        checks.pack(anchor="w", pady=(theme.px(10), 0))
        self.checks = {}
        for key, text, var, on in (
                ("addons", errors.RESTORE_ADDONS, self.addons_var, bool(info.addons)),
                ("settings", errors.RESTORE_SETTINGS, self.settings_var,
                 can_settings and info.settings_files > 0),
                ("bindings", errors.RESTORE_BINDINGS, self.bindings_var,
                 can_settings and info.binding_files > 0)):
            c = ttk.Checkbutton(checks, text=text, variable=var)
            if LINUX:   # wider fonts: one tick per line keeps the window narrow
                c.pack(anchor="w")
            else:
                c.pack(side="left", padx=(0, theme.px(10)))
            if not on:
                c.state(["disabled"])
            self.checks[key] = c
        if received and info.addons:
            ttk.Label(self.frame, text=errors.RECEIVED_ADDONS_NOTE, wraplength=theme.px(WRAP),
                      justify="left").pack(anchor="w", pady=(theme.px(8), 0))
            line = errors.received_addons(*not_guild_addons(info, app.rows))
            if line:
                ttk.Label(self.frame, text=line, style="Muted.TLabel", wraplength=theme.px(WRAP),
                          justify="left").pack(anchor="w", pady=(theme.px(4), 0))
        if getattr(info, "left_out", None):
            ttk.Label(self.frame, text=errors.restore_left_out(info.left_out),
                      style="Muted.TLabel", wraplength=theme.px(WRAP), justify="left").pack(
                anchor="w", pady=(theme.px(8), 0))
        guild = {f.lower() for f in _guild_folders(app)}   # those show as guild rows
        others = sorted((f for f in (getattr(info, "others", None) or {})
                         if f.lower() not in guild), key=str.lower)
        self.others_label = None
        if others:
            self.others_label = ttk.Label(self.frame, text=errors.restore_others(others),
                                          style="Muted.TLabel", wraplength=theme.px(WRAP),
                                          justify="left")
            self.others_label.pack(anchor="w", pady=(theme.px(8), 0))
        if info.accounts and not self.local:
            ttk.Label(self.frame, text=errors.NO_ACCOUNT, wraplength=theme.px(WRAP),
                      justify="left").pack(anchor="w", pady=(theme.px(8), 0))
        for text in (errors.RESTORE_MERGE, errors.RESTORE_SAFETY):
            note = ttk.Label(self.frame, text=text, style="Muted.TLabel",
                             wraplength=theme.px(WRAP), justify="left")
            note.pack(anchor="w", pady=(theme.px(6), 0))
            even_wrap(note)
        self._status_area(collapse=True)
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.restore_button = ttk.Button(self.buttons, text=errors.RESTORE_OK,
                                         style="Accent.TButton", command=self.start)
        self.restore_button.pack(side="right", padx=(0, theme.px(6)))
        self.back_button = None
        if back:   # back to the Backups found list (Cancel closes everything)
            self.back_button = ttk.Button(self.buttons, text=errors.BACK, command=self.back)
            self.back_button.pack(side="left")
            self.win.bind("<Escape>", lambda _e: self.back())
        self._show(self.restore_button)

    def back(self):
        """Close this window and show the Backups found list again (not while restoring,
        and not after a restore: then Close/Esc just close)."""
        if self.running or self.closed:
            return None
        if self.result is not None:
            self.close()
            return None
        app, show = self.app, self.shown
        self.close()
        return app.open_restore(show=show)

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
        if ((flags["settings"] or flags["bindings"])
                and any(v.get() == errors.RESTORE_CHOOSE for v in self.account_vars.values())):
            self.set_status(errors.RESTORE_CHOOSE_FIRST)
            return False
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
        for w in (self.restore_button, self.cancel_button, *self.checks.values(),
                  *self.menu_buttons, *([self.back_button] if self.back_button else [])):
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
            if not self.running:
                return   # a late message after the end
            self.set_progress(msg[1], msg[2])
            self.set_status(errors.restoring(int(self.progress.cget("value"))))
            return
        self._end()
        self.cancel_button.state(["!disabled"])
        if kind == "restore_done":
            self.result = msg[1]
            self.set_progress(1, 1)
            self.set_status(errors.restored_message(msg[1]))
            self.cancel_button.configure(text=errors.CLOSE)
            if self.back_button is not None:   # done: nothing to go back to
                self.back_button.pack_forget()
        else:
            self.progress.pack_forget()
            self.set_status(msg[1])
            for w in (self.restore_button, *self.menu_buttons,
                      *([self.back_button] if self.back_button else [])):
                w.state(["!disabled"])
            for key, c in self.checks.items():
                if key == "addons" and self.info.addons:
                    c.state(["!disabled"])
                elif key != "addons" and self.local and (
                        self.info.settings_files if key == "settings" else self.info.binding_files):
                    c.state(["!disabled"])


def not_guild_addons(info, rows):
    """([not on the guild list], [guild addons whose version differs from the guild's
    latest, e.g. 'TaxiTime 2.0']) for the addons in a backup. rows: the App's addon rows."""
    latest = {r.folder: r.latest for r in rows}
    versions_in = backup.addon_versions(info)
    not_listed, different = [], []
    for folder in info.addons:
        if folder not in latest:
            not_listed.append(folder)
            continue
        have, want = versions_in.get(folder), latest[folder]
        if want and not (have and versions.same(have, want)):
            different.append(f"{folder} {have}" if have else folder)
    return not_listed, different


# ---------- threads (plain values only, never Tk objects) ----------
def _game_open(check_game):
    try:
        return bool(check_game())
    except Exception:
        return False


def _size_worker(q, addons_dir, check_game=None):
    """("backup_size", bytes, spoken folders, WoW open). The game check matches process
    names only (gamecheck)."""
    q.put(("backup_size", backup.addons_size(addons_dir) if addons_dir else 0,
           backup.spoken_folders(addons_dir) if addons_dir else [],
           _game_open(check_game) if check_game else False))


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


def _search(chosen):
    """[FoundBackup] in Documents, the chosen folder and the clouds (newest first)."""
    try:
        clouds = backup.cloud_folders()
    except Exception as e:
        log.info("Cloud folder check failed (%s)", type(e).__name__)
        clouds = []
    places = backup_places(chosen, clouds, backup.documents_dir())
    try:
        from am import share   # leftover unfinished downloads (app closed while receiving)
        share.clean_partials([folder for _label, folder in places])
    except Exception as e:
        log.info("Clean-up of unfinished downloads failed (%s)", type(e).__name__)
    try:
        return backup.find_backups(places)
    except Exception as e:
        log.info("Backup search failed (%s)", type(e).__name__)
        return []


def _find_worker(q, chosen):
    q.put(("restore_found", _search(chosen)))


def newest_made(found):
    """The newest backup made here (the Backup window's "Last backup" line): never a
    safety copy or a file received from another PC (by name; no file is opened)."""
    for f in found:
        if not f.safety and not backup.is_received(f.path):
            return f
    return None


def _last_worker(q, number, chosen):
    found = _search(chosen)
    q.put(("backup_last", number, newest_made(found), bool(found)))


def _open_worker(q, path):
    try:
        q.put(("restore_opened", path, backup.read(path)))
    except Exception as e:
        log.warning("Not a usable backup (%s)", type(e).__name__)
        q.put(("restore_open_error", path, errors.backup_message(e)))


def _guild_folders(app):
    """The guild list's folders (empty when unknown). Never raises."""
    try:
        return [f for f in app._guild_folders() if isinstance(f, str)]
    except Exception:
        return []


def other_record(app):
    """The app's "Other addons" record (a plain dict copy, read on the Tk thread) for a new
    backup's backup.json; {} when there is none. Never raises."""
    try:
        files = getattr(app, "files", None)
        return dict(files.tracked()) if files is not None else {}
    except Exception:
        log.exception("Could not read the Other addons record")
        return {}


def progress_poster(post):
    """progress(done, total) for a worker thread that calls post(done, total) only when
    the whole percent changes (at most 101 queue messages per run, the last at 100 %)."""
    last = [None]

    def progress(done, total):
        percent = min(100, done * 100 // max(total, 1))
        if percent != last[0]:
            last[0] = percent
            post(done, total)
    return progress


def _backup_worker(q, check_game, addons_dir, folder, addons, settings, leave_out=(),
                   others=None, cancel=None):
    """WoW open: the backup is made anyway ("backup_game" first: the window shows the
    strip and the saved line says the settings are from the last /reload).
    ("backup_done", path, size, skipped arcnames)."""
    if _game_open(check_game):
        q.put(("backup_game", True))
    try:
        path, size, counts = backup.make(
            addons_dir, folder, addons=addons, settings=settings, leave_out=leave_out,
            others=others, cancel=cancel,
            progress=progress_poster(lambda done, total: q.put(("backup_progress", done, total))))
        q.put(("backup_done", path, size, list(counts.get("skipped", ()))))
    except Exception as e:
        if getattr(e, "kind", "") == "cancelled":
            log.info("Backup stopped by the player")
        else:
            log.exception("Backup failed")
        q.put(("backup_error", errors.backup_message(e)))


def _restore_worker(q, check_game, path, addons_dir, safety, flags, account_map):
    if _game_open(check_game):
        q.put(("restore_error", errors.GAME_OPEN))
        return
    try:
        result = backup.restore(
            path, addons_dir, safety, account_map=account_map,
            progress=progress_poster(lambda done, total: q.put(("restore_progress", done, total))),
            **flags)
        q.put(("restore_done", result))
    except Exception as e:
        log.exception("Restore failed")
        q.put(("restore_error", errors.backup_message(e)))
