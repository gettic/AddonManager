"""The two Send-with-a-code windows (Settings > Backup, and Use a code... in Backups found):
Send to another PC (pick or make a backup, show the code) and Receive (type the code, then
the normal Restore window). The work runs in threads (am\\share.py); results come back
through the App's queue as ("share_found"/"share_made"/"share_code"/"share_progress"/
"share_matched"/"share_sent"/"share_received"/"share_error", ...). Closing a window while
it waits or sends stops the transfer (the relay deletes the mailbox)."""

import collections
import logging
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from am import backup, backup_ui, config, errors, share, share_later, theme
from am.backup_ui import WRAP

log = logging.getLogger(__name__)

CODE_SIZE = 15 if sys.platform != "win32" else 16
TICK_MS = 1000
DOWNLOAD_POLL_MS = 2000   # how often Downloads is checked for the send-later file


def code_font():
    try:
        family = tkfont.nametofont("TkFixedFont").actual("family")
    except tk.TclError:
        family = "Courier"
    return (family, CODE_SIZE, "bold")


class Speed:
    """Time left from the progress messages: the speed over the last WINDOW seconds,
    shown once MIN_SECONDS have passed (None before)."""
    WINDOW = 30
    MIN_SECONDS = 5

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.points = collections.deque()
        self.start = None

    def left(self, done, total):
        now = self.clock()
        if self.start is None:
            self.start = now
        self.points.append((now, done))
        while len(self.points) > 2 and now - self.points[0][0] > self.WINDOW:
            self.points.popleft()
        t0, d0 = self.points[0]
        if now - self.start < self.MIN_SECONDS or now <= t0 or done <= d0:
            return None
        return max(0, total - done) / ((done - d0) / (now - t0))


class _ShareDialog(backup_ui._Dialog):
    """A transfer that the player can stop at any time with Cancel (or closing the
    window): the thread sees the cancel flag and the relay deletes the mailbox."""

    def __init__(self, app, title, show):
        super().__init__(app, title, show)
        self.cancel_event = threading.Event()
        self.active = False      # a transfer thread is running
        self.tag = object()      # messages of an older try (or window) are ignored
        self.speed = Speed()     # "about N min left"

    def mine(self, msg):
        return len(msg) > 1 and msg[1] is self.tag

    def close(self):
        if self.running:
            return   # making the backup: it takes seconds and can't stop half-way
        self.cancel_event.set()
        self.active = False
        super().close()


class SendDialog(backup_ui._SpokenCheck, _ShareDialog):
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
        self._later_area()
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
        self._spoken_init()
        threading.Thread(target=_spoken_worker, daemon=True,
                         args=(app.queue, self.tag, app.addons_dir)).start()
        self._show(self.go_button)

    def _later_area(self):
        """Send later (opt-in, off by default, remembered): the tick, file.kiwi's terms and
        privacy links, how long the code works, and what file.kiwi sees."""
        settings = config.load_settings()
        self.later_mode = False      # uploading for later (or done)
        self.later_done = False
        self.later = None            # the choice when Get code was clicked
        # A tick given under an older wording of the tick counts as off (tos).
        self.later_var = tk.BooleanVar(value=settings.get("send_later") is True and settings.get(
            "send_later_v") == share_later.CONSENT_VERSION)
        hours = settings.get("send_later_hours")
        ok = (isinstance(hours, int) and not isinstance(hours, bool) and 1 <= hours <= 24
              and hours in share_later.HOURS)   # true / 48 / 3 in a hand-edited file -> 12
        self.hours_var = tk.IntVar(value=hours if ok else share_later.DEFAULT_HOURS)
        area = ttk.Frame(self.frame)
        area.pack(fill="x", pady=(10, 0))
        self.later_check = ttk.Checkbutton(area, variable=self.later_var,
                                           command=self.save_later)
        self.later_check.grid(row=0, column=0, sticky="nw")
        tick_text = ttk.Label(area, text=errors.LATER_TICK, wraplength=WRAP - 30,
                              justify="left")
        tick_text.grid(row=0, column=1, sticky="w")
        tick_text.bind("<Button-1>", lambda _e: self.toggle_later())
        rest = ttk.Frame(area)
        rest.grid(row=1, column=1, sticky="w")
        keep = ttk.Frame(rest)
        keep.pack(anchor="w", pady=(4, 0))
        ttk.Label(keep, text=errors.LATER_KEEP).pack(side="left")
        self.hours_text = tk.StringVar(value=errors.later_hours(self.hours_var.get()))
        self.hours_button = ttk.Menubutton(keep, textvariable=self.hours_text, width=9)
        menu = tk.Menu(self.hours_button, tearoff=0, background=theme.SURFACE,
                       foreground=theme.TEXT, activebackground=theme.ACCENT,
                       activeforeground=theme.ACCENT_TEXT)
        for h in share_later.HOURS:
            menu.add_radiobutton(label=errors.later_hours(h), value=h,
                                 variable=self.hours_var, command=self.save_later)
        self.hours_button["menu"] = menu
        self.hours_button.pack(side="left", padx=(6, 0))
        links = ttk.Frame(rest)
        links.pack(anchor="w", pady=(4, 0))
        self.later_links = []
        for text, url in ((errors.LATER_TERMS, share_later.TERMS_URL),
                          (errors.LATER_PRIVACY, share_later.PRIVACY_URL)):
            link = tk.Label(links, text=text, cursor="hand2", background=theme.BG,
                            foreground=theme.MUTED, font=theme.font("underline", bigger=-1))
            link.pack(side="left", padx=(0, 10))
            link.bind("<Button-1>", lambda _e, u=url: self.app.open_link(u))
            self.later_links.append(link)
        ttk.Label(rest, text=errors.LATER_NOTE, style="Muted.TLabel", wraplength=WRAP - 30,
                  justify="left").pack(anchor="w", pady=(4, 0))
        self._later_state()

    def toggle_later(self):
        if self.active or self.closed:
            return
        self.later_var.set(not self.later_var.get())
        self.save_later()

    def _later_state(self):
        on = self.later_var.get() and not self.active
        self.hours_button.state(["!disabled"] if on else ["disabled"])

    def save_later(self):
        """Remember the tick and the hours (the next Send window starts the same)."""
        self.hours_text.set(errors.later_hours(self.hours_var.get()))
        settings = config.load_settings()
        settings["send_later"] = bool(self.later_var.get())
        settings["send_later_hours"] = int(self.hours_var.get())
        settings["send_later_v"] = share_later.CONSENT_VERSION
        config.save_settings(settings)
        self._later_state()

    def later_choice(self):
        """None = send later is off (the send works as before)."""
        if not self.later_var.get():
            return None
        return {"hours": int(self.hours_var.get()),
                "api": getattr(self.app, "later_api", None) or share_later.API_URL,
                "allow_local": self.app.allow_local}

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
        if path is None and flags["addons"]:
            if self.ask_spoken():
                return True
            flags["leave_out"] = self.leave_out()
            self.spoken_once = None
        for w in (self.new_radio, self.newest_radio, self.other_radio, self.other_button,
                  self.go_button, self.later_check):
            w.state(["disabled"])
        self.later = self.later_choice()
        self.active = True
        self._later_state()
        self.tag = object()
        self.speed = Speed()
        if path is None:
            self.running = True          # backup.make can't be stopped half-way
            self.app.busy = True
            self.app.render()
            self.set_status(errors.SEND_MAKING + (" " + errors.SPOKEN_LEFT_OUT
                                                  if flags.get("leave_out") else ""))
        else:
            self.set_status(errors.SEND_CONNECTING)
        threading.Thread(target=_send_worker, daemon=True,
                         args=(self.app.queue, self.tag, self.app.check_game,
                               self.app.addons_dir, backup_ui.backup_folder(settings), flags, path,
                               self.app.share_url, self.app.allow_local,
                               self.cancel_event, self.later)).start()
        return True

    def _ends_text(self, seconds):
        return errors.later_in(seconds) if self.later else errors.code_ends(seconds)

    def show_code(self, code, seconds):
        self.code = code
        if self.later:   # the countdown runs to the switch to "later"
            seconds = min(seconds, share_later.LATER_AFTER)
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
        self.ends_var = tk.StringVar(value=self._ends_text(seconds))
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
        self.ends_var.set(self._ends_text(self.ends_at - time.monotonic()))
        try:
            self.win.after(TICK_MS, self._tick)
        except tk.TclError:
            pass

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("share_") or not self.mine(msg):
            return
        if kind == "share_spoken":
            if not self.active:
                self.spoken_ready(msg[2], msg[3])
            return
        if kind == "share_found":
            f = msg[2]
            if f is not None and not self.active:
                self.newest = f
                # Wider Linux fonts: the backup goes on its own line.
                gap = "\n" if backup_ui.LINUX else ""
                self.newest_radio.configure(text=errors.SEND_NEWEST + gap + errors.found_row(
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
            left = self.speed.left(msg[2], msg[3])
            self.set_status(errors.later_uploading(msg[2], msg[3], left) if self.later_mode
                            else errors.sending(msg[2], msg[3], left))
        elif kind == "share_later":
            self.started = True
            if self.code:
                self.ends_var.set("")
            if msg[2] == "uploading":
                self.later_mode = True
                self.speed = Speed()
                self.set_status(errors.LATER_UPLOADING_BIG if msg[3] == "big"
                                else errors.LATER_UPLOADING)
            elif msg[2] == "ready":
                self.later_done = True
                until = time.strftime("%H:%M", time.localtime(time.time() + (msg[3] or 0)))
                self.set_status(errors.later_ready(until, self.later["hours"]))
        elif kind == "share_sent":
            self.active = False
            self.set_progress(1, 1)
            if not self.later_done:
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
        self.speed = Speed()
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
            self.set_status(errors.receiving(msg[2], msg[3], self.speed.left(msg[2], msg[3]))
                            if msg[2] < msg[3] else errors.RECEIVE_CHECKING)
        elif kind == "share_later":   # the other PC is still uploading it for later
            self.set_progress(msg[3] or 0, 1000)
            self.set_status(errors.later_waiting(msg[3] or 0))
        elif kind == "share_ticket":
            self.show_ticket(msg[2])
        elif kind == "share_received":
            app, show = self.app, self.shown
            self.active = False
            super().close()
            app.backup_dialog = backup_ui.RestoreDialog(app, msg[3], show=show)
        elif kind == "share_error":
            self.progress.pack_forget()
            self.set_status(msg[2])
            if self.ticket is not None:   # the code is used up: try another download
                self.opening = False
                if self.trying is not None:   # never opened by itself again (the error
                    self.failed.add(self.trying)   # stays readable; Open... still works)
                    self.trying = None
                self.open_button.state(["!disabled"])
                self._poll_download()
                return
            self.active = False
            self.entry.state(["!disabled"])
            self.go_button.state(["!disabled"])

    # ---------- send later: download in the browser, then open it here ----------
    ticket = None
    opening = False
    trying = None        # (path, size, mtime) of the file being opened
    failed = frozenset()  # files that didn't open: the Downloads watch skips them

    def show_ticket(self, ticket):
        """The code unlocked a send-later: Download (browser) and Open the download...;
        the Downloads folder is watched so the file opens by itself once it's there."""
        self.ticket = ticket
        self.failed = set()
        self.progress.pack_forget()
        area = ttk.Frame(self.frame)
        area.pack(fill="x", before=self.status, pady=(10, 0))
        ttk.Label(area, text=errors.LATER_GOT, wraplength=WRAP, justify="left").pack(
            anchor="w")
        ttk.Label(area, text=errors.LATER_KEEP_OPEN, style="Muted.TLabel", wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(4, 0))
        line = ttk.Frame(area)
        line.pack(anchor="w", pady=(6, 0))
        self.download_button = ttk.Button(line, text=errors.LATER_DOWNLOAD,
                                          style="Accent.TButton", command=self.download)
        self.download_button.pack(side="left")
        self.open_button = ttk.Button(line, text=errors.LATER_OPEN, command=self.pick_download)
        self.open_button.pack(side="left", padx=(6, 0))
        self.go_button.pack_forget()
        self.set_status(errors.LATER_LOOKING)
        self._poll_download()

    def download(self):
        if self.ticket is not None and not self.closed:
            self.app.open_link(self.ticket.link)

    def _poll_download(self):
        if self.closed or self.ticket is None or self.opening:
            return
        found = share_later.find_download(self.ticket.name, self.ticket.blob,
                                          share_later.downloads_dirs())
        if found and _file_key(found) not in self.failed:
            self.open_download(found)
            return
        try:
            self.win.after(DOWNLOAD_POLL_MS, self._poll_download)
        except tk.TclError:
            pass

    def pick_download(self):
        if self.closed or self.ticket is None or self.opening:
            return
        ask = getattr(self.app, "ask_later_file", None) or _ask_later_file
        path = ask(self.win)
        self.grab()
        if path:
            self.open_download(path)

    def open_download(self, path):
        if self.opening or self.ticket is None:
            return
        self.opening = True
        self.trying = _file_key(path)
        self.open_button.state(["disabled"])
        self.set_status(errors.LATER_CHECKING)
        threading.Thread(target=_open_later_worker, daemon=True,
                         args=(self.app.queue, self.tag, self.ticket, path,
                               backup_ui.backup_folder(config.load_settings()),
                               self.cancel_event)).start()


def _file_key(path):
    """A downloaded file as (path, size, mtime): a new download of the same name differs."""
    try:
        st = os.stat(path)
    except OSError:
        return (os.path.normcase(os.path.abspath(path)), None, None)
    return (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)


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


def _spoken_worker(q, tag, addons_dir):
    """The Spoken addons a new backup would hold, and all addon folders' bytes."""
    q.put(("share_spoken", tag, backup.spoken_folders(addons_dir) if addons_dir else [],
           backup.addons_size(addons_dir) if addons_dir else 0))


def _send_worker(q, tag, check_game, addons_dir, folder, flags, path, url, allow_local, cancel,
                 later=None):
    try:
        relay = share.Relay(url, allow_local=allow_local)
        if path is None:
            if backup_ui._game_open(check_game):
                q.put(("share_made", tag, None))
                q.put(("share_error", tag, errors.GAME_OPEN))
                return
            if flags.get("addons"):   # too big to send, or no room: say so before zipping
                share.precheck(backup.addons_size(addons_dir, flags.get("leave_out", ())),
                               bool(later), folder)
            try:
                path, _size, _c = backup.make(addons_dir, folder, **flags)
            finally:
                q.put(("share_made", tag, path))
        share.send(path, relay, lambda code, ttl: q.put(("share_code", tag, code, ttl)),
                   progress=lambda d, t: q.put(("share_progress", tag, d, t)), cancel=cancel,
                   later=later, on_later=lambda s, v: q.put(("share_later", tag, s, v)))
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
                             on_start=lambda: q.put(("share_matched", tag)),
                             on_later=lambda s, p: q.put(("share_later", tag, s, p)))
        if isinstance(path, share.LaterTicket):   # uploaded for later: browser step next
            q.put(("share_ticket", tag, path))
            return
        q.put(("share_received", tag, path, backup.read(path, received=True)))
    except Exception as e:
        log.warning("Receive failed (%s %s)", type(e).__name__, getattr(e, "kind", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))


def _open_later_worker(q, tag, ticket, path, folder, cancel):
    """Open the downloaded send-later file into the backup folder (then Restore)."""
    try:
        final = share.open_later(ticket, path, folder, cancel=cancel,
                                 progress=lambda d, t: q.put(("share_progress", tag, d, t)))
        q.put(("share_received", tag, final, backup.read(final, received=True)))
    except Exception as e:
        log.warning("Opening the download failed (%s %s)", type(e).__name__,
                    getattr(e, "kind", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))


def _ask_later_file(parent):
    """Pick the downloaded send-later file (starts in Downloads)."""
    from tkinter import filedialog
    dirs = share_later.downloads_dirs()
    return filedialog.askopenfilename(
        parent=parent, title=errors.LATER_OPEN.rstrip("."),
        initialdir=dirs[0] if dirs and os.path.isdir(dirs[0]) else None,
        filetypes=[("Addon Manager send", "*" + share_later.EXT), ("All files", "*.*")])
