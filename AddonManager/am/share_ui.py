"""The two Send-with-a-code windows (Settings > Backup, and Use a code... in Backups found):
Send to another PC (pick or make a backup, show the code) and Receive (type the code, then
the normal Restore window). The work runs in threads (am\\share.py); results come back
through the App's queue as ("share_found"/"share_made"/"share_code"/"share_progress"/
"share_matched"/"share_sent"/"share_received"/"share_error", ...). Closing a window while
it waits or sends stops the transfer (the relay deletes the mailbox)."""

import logging
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from am import backup, backup_ui, config, errors, share, theme
from am.backup_ui import WRAP

log = logging.getLogger(__name__)

CODE_SIZE = 15 if sys.platform != "win32" else 16
TICK_MS = 1000


def code_font():
    try:
        family = tkfont.nametofont("TkFixedFont").actual("family")
    except tk.TclError:
        family = "Courier"
    return (family, CODE_SIZE, "bold")


class _ShareDialog(backup_ui._Dialog):
    """A transfer that the player can stop at any time with Cancel (or closing the
    window): the thread sees the cancel flag and the relay deletes the mailbox."""

    def __init__(self, app, title, show):
        super().__init__(app, title, show)
        self.cancel_event = threading.Event()
        self.active = False      # a transfer thread is running
        self.tag = object()      # messages of an older try (or window) are ignored

    def mine(self, msg):
        return len(msg) > 1 and msg[1] is self.tag

    def close(self):
        if self.running:
            return   # making the backup: it takes seconds and can't stop half-way
        self.cancel_event.set()
        self.active = False
        super().close()


class SendDialog(_ShareDialog):
    """Send to another PC: what to send, the private-data warning (before any code),
    Get code -> the code, a countdown and Copy -> progress -> Sent."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.SEND_TITLE, show)
        self.newest = None       # FoundBackup
        self.other = None        # a file picked with Other file...
        self.code = None
        self.ends_at = None
        self.started = False     # the other PC joined (progress began)
        ttk.Label(self.frame, text=errors.SEND_WHAT, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0))
        self.choice = tk.StringVar(value="new")
        self.new_radio = ttk.Radiobutton(self.frame, text=errors.SEND_NEW, value="new",
                                         variable=self.choice)
        self.new_radio.pack(anchor="w", pady=(4, 0))
        self.newest_radio = ttk.Radiobutton(self.frame, text=errors.SEND_NEWEST + "...",
                                            value="newest", variable=self.choice)
        self.newest_radio.state(["disabled"])
        self.newest_radio.pack(anchor="w", pady=(2, 0))
        self.other_radio = ttk.Radiobutton(self.frame, text=errors.SEND_CHOSEN, value="other",
                                           variable=self.choice)
        self.other_button = ttk.Button(self.frame, text=errors.SEND_OTHER, command=self.pick)
        self.other_button.pack(anchor="w", pady=(4, 0))
        ttk.Label(self.frame, text=errors.SEND_PRIVATE, wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Label(self.frame, text=errors.SEND_SAFE, style="Muted.TLabel", wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(4, 0))
        self.code_area = ttk.Frame(self.frame)
        self.code_area.pack(fill="x")
        self._status_area()
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.go_button = ttk.Button(self.buttons, text=errors.SEND_GET_CODE,
                                    style="Accent.TButton", command=self.start)
        self.go_button.pack(side="right", padx=(0, 6))
        folder = backup_ui.backup_folder(config.load_settings())
        threading.Thread(target=_newest_worker, daemon=True,
                         args=(app.queue, self.tag, folder)).start()
        self._show(self.go_button)

    def pick(self):
        if self.active or self.closed:
            return
        path = self.app.ask_backup_file()
        self.grab()
        if path:
            self.other = path
            self.other_radio.configure(text=errors.SEND_CHOSEN + os.path.basename(path))
            if not self.other_radio.winfo_manager():
                self.other_radio.pack(anchor="w", pady=(2, 0), before=self.other_button)
            self.choice.set("other")

    def chosen_path(self):
        """None = make a new backup now."""
        c = self.choice.get()
        if c == "newest" and self.newest:
            return self.newest.path
        if c == "other" and self.other:
            return self.other
        return None

    def start(self):
        if self.active or self.closed or self.app.busy or not self.app.addons_dir:
            return False
        path = self.chosen_path()
        settings = config.load_settings()
        flags = dict(addons=settings.get("backup_addons", True) is not False,
                     settings=settings.get("backup_settings", True) is not False)
        for w in (self.new_radio, self.newest_radio, self.other_radio, self.other_button,
                  self.go_button):
            w.state(["disabled"])
        self.active = True
        self.tag = object()
        if path is None:
            self.running = True          # backup.make can't be stopped half-way
            self.app.busy = True
            self.app.render()
            self.set_status(errors.SEND_MAKING)
        else:
            self.set_status(errors.SEND_CONNECTING)
        threading.Thread(target=_send_worker, daemon=True,
                         args=(self.app.queue, self.tag, self.app.check_game,
                               self.app.addons_dir, backup_ui.backup_folder(settings), flags, path,
                               self.app.share_url, self.app.allow_local,
                               self.cancel_event)).start()
        return True

    def show_code(self, code, seconds):
        self.code = code
        self.ends_at = time.monotonic() + seconds
        ttk.Label(self.code_area, text=errors.SEND_CODE_HEAD, wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(10, 0))
        self.code_label = ttk.Label(self.code_area, text=code, font=code_font(),
                                    foreground=theme.TEXT)
        self.code_label.pack(anchor="w", pady=(4, 0))
        line = ttk.Frame(self.code_area)
        line.pack(fill="x", pady=(4, 0))
        self.copy_button = ttk.Button(line, text=errors.SEND_COPY, command=self.copy)
        self.copy_button.pack(side="left")
        self.ends_var = tk.StringVar(value=errors.code_ends(seconds))
        ttk.Label(line, textvariable=self.ends_var, style="Muted.TLabel").pack(
            side="left", padx=(8, 0))
        self.set_status(errors.SEND_WAITING)
        self._tick()

    def copy(self):
        if self.code:
            self.app.clipboard(self.code)
            self.set_status(errors.SEND_COPIED + " " + errors.SEND_WAITING
                            if not self.started else self.status_var.get())

    def _tick(self):
        if self.closed or self.started or self.ends_at is None:
            return
        self.ends_var.set(errors.code_ends(self.ends_at - time.monotonic()))
        try:
            self.win.after(TICK_MS, self._tick)
        except tk.TclError:
            pass

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("share_") or not self.mine(msg):
            return
        if kind == "share_found":
            f = msg[2]
            if f is not None and not self.active:
                self.newest = f
                self.newest_radio.configure(text=errors.SEND_NEWEST + errors.found_row(
                    backup_ui.found_date(f.mtime), f.size, f.place, f.online))
                self.newest_radio.state(["!disabled"])
            return
        if not self.active:
            return
        if kind == "share_made":
            self.running = False
            self.app.busy = False
            self.app.render()
            self.set_status(errors.SEND_CONNECTING)
        elif kind == "share_code":
            self.show_code(msg[2], msg[3])
        elif kind == "share_progress":
            self.started = True
            if self.code:
                self.ends_var.set("")
            self.set_progress(msg[2], msg[3])
            self.set_status(errors.sending(msg[2], msg[3]))
        elif kind == "share_sent":
            self.active = False
            self.set_progress(1, 1)
            self.set_status(errors.SEND_SENT)
            self.cancel_button.configure(text=errors.CLOSE)
        elif kind == "share_error":
            self.active = False
            if self.running:
                self.running = False
                self.app.busy = False
                self.app.render()
            self.progress.pack_forget()
            self.set_status(msg[2])
            self.cancel_button.configure(text=errors.CLOSE)


class ReceiveDialog(_ShareDialog):
    """Receive: type the code -> progress -> the file is checked -> the Restore window."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.RECEIVE_TITLE, show)
        ttk.Label(self.frame, text=errors.RECEIVE_TYPE).pack(anchor="w", pady=(6, 0))
        self.code_var = tk.StringVar()
        self.entry = ttk.Entry(self.frame, textvariable=self.code_var, width=24,
                               font=code_font())
        self.entry.pack(anchor="w", fill="x", pady=(4, 0))
        self.entry.bind("<Return>", lambda _e: self.start())
        ttk.Label(self.frame, text=errors.RECEIVE_EXAMPLE, style="Muted.TLabel").pack(
            anchor="w", pady=(2, 0))
        ttk.Label(self.frame, text=errors.RECEIVE_NOTE, style="Muted.TLabel", wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(10, 0))
        self._status_area()
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.go_button = ttk.Button(self.buttons, text=errors.RECEIVE_OK,
                                    style="Accent.TButton", command=self.start)
        self.go_button.pack(side="right", padx=(0, 6))
        self._show(self.entry)

    def start(self):
        if self.active or self.closed or self.app.busy or not self.app.addons_dir:
            return False
        code = self.code_var.get()
        try:
            share.parse_code(code)
        except share.ShareError as e:
            self.set_status(errors.share_message(e))
            return False
        self.entry.state(["disabled"])
        self.go_button.state(["disabled"])
        self.active = True
        self.tag = object()
        self.cancel_event = threading.Event()
        self.set_status(errors.RECEIVE_CONNECTING)
        threading.Thread(target=_receive_worker, daemon=True,
                         args=(self.app.queue, self.tag, self.app.share_url,
                               self.app.allow_local, code,
                               backup_ui.backup_folder(config.load_settings()),
                               self.cancel_event)).start()
        return True

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("share_") or not self.mine(msg) or not self.active:
            return
        if kind == "share_matched":
            self.set_status(errors.RECEIVE_CONNECTING)
        elif kind == "share_progress":
            self.set_progress(msg[2], msg[3])
            self.set_status(errors.receiving(msg[2], msg[3]) if msg[2] < msg[3]
                            else errors.RECEIVE_CHECKING)
        elif kind == "share_received":
            app, show = self.app, self.shown
            self.active = False
            super().close()
            app.backup_dialog = backup_ui.RestoreDialog(app, msg[3], show=show)
        elif kind == "share_error":
            self.active = False
            self.progress.pack_forget()
            self.set_status(msg[2])
            self.entry.state(["!disabled"])
            self.go_button.state(["!disabled"])


# ---------- threads (plain values only, never Tk objects) ----------
def _newest_worker(q, tag, chosen):
    """The newest normal backup in the places Backups found looks (or None)."""
    try:
        clouds = backup.cloud_folders()
    except Exception:
        clouds = []
    places = backup_ui.backup_places(chosen, clouds, backup.documents_dir())
    try:
        share.clean_partials([folder for _label, folder in places])
        found = backup.find_backups(places)
    except Exception:
        found = []
    q.put(("share_found", tag, newest_own(found)))


def newest_own(found):
    """The newest backup made on this PC: never a safety copy or a file received from
    another PC (by its name or the mark inside). Online-only cloud files are skipped (they
    can't be checked without downloading them; Other file... still picks them)."""
    for f in found:
        if f.safety or f.online or backup.is_received(f.path):
            continue
        if not backup.has_received_mark(f.path):
            return f
    return None


def _send_worker(q, tag, check_game, addons_dir, folder, flags, path, url, allow_local, cancel):
    try:
        if path is None:
            if backup_ui._game_open(check_game):
                q.put(("share_made", tag, None))
                q.put(("share_error", tag, errors.GAME_OPEN))
                return
            try:
                path, _size, _c = backup.make(addons_dir, folder, **flags)
            finally:
                q.put(("share_made", tag, path))
        relay = share.Relay(url, allow_local=allow_local)
        share.send(path, relay, lambda code, ttl: q.put(("share_code", tag, code, ttl)),
                   progress=lambda d, t: q.put(("share_progress", tag, d, t)), cancel=cancel)
        q.put(("share_sent", tag))
    except Exception as e:
        log.warning("Send failed (%s %s)", type(e).__name__, getattr(e, "kind", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))


def _receive_worker(q, tag, url, allow_local, code, folder, cancel):
    try:
        relay = share.Relay(url, allow_local=allow_local)
        path = share.receive(code, relay, folder, cancel=cancel,
                             progress=lambda d, t: q.put(("share_progress", tag, d, t)),
                             on_start=lambda: q.put(("share_matched", tag)))
        q.put(("share_received", tag, path, backup.read(path, received=True)))
    except Exception as e:
        log.warning("Receive failed (%s %s)", type(e).__name__, getattr(e, "kind", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))
