"""The two Send-with-a-code windows (Settings > Backup, and Use a code... in Backups found):
Send to another PC (pick or make a backup, show the code) and Receive (type the code, then
the normal Restore window). The work runs in threads (am\\share.py); results come back
through the App's queue as ("share_found"/"share_game"/"share_made"/"share_code"/"share_progress"/
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

from am import (backup, backup_lock, backup_ui, codes, config, errors, share, share_later,
                share_send, theme)
from am.backup_ui import WRAP

log = logging.getLogger(__name__)

# Send later's hosts in the order tried (the consent list): name, then (link text, page).
LATER_HOSTS = (
    (errors.LATER_HOST_ADMINFORGE, ((errors.LATER_TERMS, share_send.ADMINFORGE_TERMS_URL),
                                    (errors.LATER_PRIVACY, share_send.ADMINFORGE_PRIVACY_URL))),
    (errors.LATER_HOST_VISEE, ((errors.LATER_ABOUT, share_send.VISEE_ABOUT_URL),
                               (errors.LATER_LIST, share_send.VISEE_LIST_URL))),
    (errors.LATER_HOST_KIWI, ((errors.LATER_TERMS, share_later.TERMS_URL),
                              (errors.LATER_PRIVACY, share_later.PRIVACY_URL))),
)

CODE_SIZE = 15 if sys.platform != "win32" else 16
TICK_MS = 1000
DOWNLOAD_POLL_MS = 2000   # how often Downloads is checked for the send-later file
# Send: the gap between lines of one group (the choices, the send-later details) stays
# 2 px at every scaling; the window must fit a 1080p screen at 150 % (test_look).
TIGHT = 2


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
            # Making the backup: Cancel/Esc/X stops it (the half-written file is removed);
            # the window stays until the thread says so (it frees the app's busy flag).
            if not self.cancel_event.is_set():
                self.cancel_event.set()
                self.set_status(errors.BACKUP_STOPPING)
            return
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
        self.left_out = False    # Spoken addons left out of the new backup
        self.made = None         # the backup made for a send that failed (a retry sends it)
        # What to send: folded into one line (what_line) once a send starts, so the window
        # stays inside small screens (Steam Deck, 150 % on 1080p); back after an error.
        box = self.what_box = ttk.Frame(self.frame)
        box.pack(fill="x")
        # "What to send:" with Other file... at its right (one row: a shorter window).
        head = ttk.Frame(box)
        head.pack(fill="x", pady=(TIGHT, 0))
        ttk.Label(head, text=errors.SEND_WHAT, style="Muted.TLabel").pack(side="left",
                                                                          anchor="s")
        # Folded: one line with the choice made, and after an error a Change... link that
        # opens the choices again (what_row).
        row = self.what_row = ttk.Frame(self.frame)
        self.what_line = ttk.Label(row, style="Muted.TLabel", justify="left",
                                   wraplength=theme.px(WRAP - 70))
        self.what_line.pack(side="left", anchor="nw", fill="x", expand=True)
        backup_ui.fit_wrap(self.what_line)   # a long file name wraps at its real width
        self.change_link = tk.Label(row, text=errors.SEND_CHANGE, cursor="hand2",
                                    background=theme.BG, foreground=theme.LINK,
                                    font=theme.font("underline"))
        self.change_link.bind("<Button-1>", lambda _e: self.unfold())
        self.choice = tk.StringVar(value="new")
        settings = config.load_settings()
        self.new_radio = ttk.Radiobutton(box, value="new", variable=self.choice,
                                         text=errors.send_new(
                                             settings.get("backup_addons", True) is not False,
                                             settings.get("backup_settings", True) is not False))
        self.new_radio.pack(anchor="w", pady=(TIGHT, 0))
        self.newest_radio = ttk.Radiobutton(box, text=errors.SEND_NEWEST + "...",
                                            value="newest", variable=self.choice)
        self.newest_radio.state(["disabled"])
        self.newest_radio.pack(anchor="w", pady=(TIGHT, 0))
        self.other_radio = ttk.Radiobutton(box, text=errors.SEND_CHOSEN, value="other",
                                           variable=self.choice)
        self.other_button = ttk.Button(head, text=errors.SEND_OTHER, command=self.pick,
                                       width=max(theme.button_chars(self.frame), theme.chars(
                                           self.frame, (errors.SEND_OTHER,))))
        self.other_button.pack(side="right")
        # WoW open: a new backup still works; the strip (under the choices, only while
        # "A new backup" is picked and the choices are open) says what it holds.
        self.game_open = False
        self.game_label = backup_ui.game_strip(self.frame, errors.SEND_GAME_OPEN)
        self.private_label = ttk.Label(self.frame, text=errors.SEND_PRIVATE,
                                       wraplength=theme.px(WRAP), justify="left")
        self.private_label.pack(anchor="w", pady=(theme.px(4), 0))
        self.choice.trace_add("write", lambda *_a: self._game_state())
        # "Sent encrypted, works once, for 10 minutes": only while file.kiwi is off (with
        # the tick the code works for the chosen hours and the file.kiwi note says the rest;
        # see _later_state). It also keeps the window short.
        self.safe_label = ttk.Label(self.frame, text=errors.SEND_SAFE, style="Muted.TLabel",
                                    wraplength=theme.px(WRAP), justify="left")
        self.safe_label.pack(anchor="w", pady=(theme.px(4), 0))
        self._later_area()
        self.code_area = ttk.Frame(self.frame)
        self.code_area.pack(fill="x")
        self._status_area(collapse=True)
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.go_button = ttk.Button(self.buttons, text=errors.SEND_GET_CODE,
                                    style="Accent.TButton", command=self.start)
        self.go_button.pack(side="right", padx=(0, theme.px(6)))
        folder = backup_ui.backup_folder(config.load_settings())
        threading.Thread(target=_newest_worker, daemon=True,
                         args=(app.queue, self.tag, folder)).start()
        self._spoken_init()
        threading.Thread(target=_spoken_worker, daemon=True,
                         args=(app.queue, self.tag, app.addons_dir, app.check_game)).start()
        self._show(self.go_button, extra=(errors.SEND_TRY_AGAIN, errors.SEND_GET_CODE))

    def _game_state(self):
        """The WoW-open strip shows while WoW is open, "A new backup" is picked and the
        choices are open (folded while a send runs: the window stays short)."""
        want = (self.game_open and self.choice.get() == "new" and not self.closed
                and not self.active)
        try:
            if want and not self.game_label.winfo_manager():
                self.game_label.pack(fill="x", pady=(theme.px(4), 0), before=self.private_label)
            elif not want and self.game_label.winfo_manager():
                self.game_label.pack_forget()
        except tk.TclError:
            pass   # the window is closing

    def show_game(self, running):
        if running and not self.game_open:
            self.game_open = True
            self._game_state()

    def _later_area(self):
        """Send later (opt-in, off by default, remembered): the tick, Upload right away (not
        remembered), file.kiwi's terms and privacy links, how long the code works, and what
        file.kiwi sees."""
        settings = config.load_settings()
        self.later_mode = False      # uploading for later (or done)
        self.later_done = False
        self.later_host = None       # a share_send.SITES key / "kiwi"
        self.later = None            # the choice when Get code was clicked
        # A tick given under an older wording of the tick counts as off (tos).
        self.later_var = tk.BooleanVar(value=settings.get("send_later") is True and settings.get(
            "send_later_v") == share_later.CONSENT_VERSION)
        hours = settings.get("send_later_hours")
        ok = (isinstance(hours, int) and not isinstance(hours, bool) and 1 <= hours <= 24
              and hours in share_later.HOURS)   # true / 48 / 3 in a hand-edited file -> 12
        self.hours_var = tk.IntVar(value=hours if ok else share_later.DEFAULT_HOURS)
        area = self.later_box = ttk.Frame(self.frame)
        area.pack(fill="x", pady=(theme.px(4), 0))
        self.later_check = ttk.Checkbutton(area, variable=self.later_var,
                                           command=self.save_later)
        self.later_check.grid(row=0, column=0, sticky="nw")
        tick_text = ttk.Label(area, text=errors.LATER_TICK, wraplength=theme.px(WRAP - 24),
                              justify="left")
        tick_text.grid(row=0, column=1, sticky="w")
        tick_text.bind("<Button-1>", lambda _e: self.toggle_later())
        # Upload right away, the hours, the links and the note: shown only while the tick
        # is on (the window stays short enough for small screens), see _later_state.
        rest = self.later_rest = ttk.Frame(area)
        rest.grid(row=1, column=1, sticky="w")
        # Upload right away (no 5-min wait for the other PC): off each time, never saved.
        self.now_var = tk.BooleanVar(value=False)
        now = ttk.Frame(rest)
        now.pack(anchor="w", pady=(TIGHT, 0))
        self.now_check = ttk.Checkbutton(now, variable=self.now_var)
        self.now_check.grid(row=0, column=0, sticky="nw")
        self.now_label = ttk.Label(now, text=errors.LATER_NOW_TICK,
                                   wraplength=theme.px(WRAP - 60), justify="left")
        self.now_label.grid(row=0, column=1, sticky="w")
        self.now_label.bind("<Button-1>", lambda _e: self.toggle_now())
        keep = ttk.Frame(rest)
        keep.pack(anchor="w", pady=(TIGHT, 0))
        ttk.Label(keep, text=errors.LATER_KEEP).pack(side="left")
        self.hours_text = tk.StringVar(value=errors.later_hours(self.hours_var.get()))
        self.hours_button = ttk.Menubutton(keep, textvariable=self.hours_text, width=9)
        menu = tk.Menu(self.hours_button, tearoff=0, **theme.menu_options())
        for h in share_later.HOURS:
            menu.add_radiobutton(label=errors.later_hours(h), value=h,
                                 variable=self.hours_var, command=self.save_later)
        self.hours_button["menu"] = menu
        self.hours_button.pack(side="left", padx=(theme.px(6), 0))
        links = self.later_hosts = ttk.Frame(rest)
        links.pack(anchor="w", pady=(TIGHT, 0))
        self.later_links = []
        # The hosts in the order tried, one row each: name, then its pages as small links.
        for row, (host, pages) in enumerate(LATER_HOSTS):
            tk.Label(links, text=host, background=theme.BG, foreground=theme.MUTED,
                     font=theme.font(bigger=-1)).grid(row=row, column=0, sticky="w",
                                                      padx=(0, theme.px(8)))
            cell = tk.Frame(links, background=theme.BG)
            cell.grid(row=row, column=1, sticky="w")
            for text, url in pages:
                link = tk.Label(cell, text=text, cursor="hand2", background=theme.BG,
                                foreground=theme.MUTED, font=theme.font("underline", bigger=-1))
                link.pack(side="left", padx=(0, theme.px(4)))
                link.bind("<Button-1>", lambda _e, u=url: self.app.open_link(u))
                self.later_links.append(link)
        ttk.Label(rest, text=errors.LATER_NOTE, style="Muted.TLabel", wraplength=theme.px(WRAP - 24),
                  justify="left").pack(anchor="w", pady=(TIGHT, 0))
        self._later_state()

    def toggle_later(self):
        if self.active or self.closed:
            return
        self.later_var.set(not self.later_var.get())
        self.save_later()

    def toggle_now(self):
        if self.active or self.closed or not self.later_var.get():
            return
        self.now_var.set(not self.now_var.get())

    def _later_state(self):
        """The hours menu and Upload right away work only with the file.kiwi tick on and
        nothing running (also after an error, see _after_error). With the tick off they
        are hidden, and Upload right away is unticked (it never comes back ticked)."""
        ticked = bool(self.later_var.get())
        if not ticked:
            self.now_var.set(False)
        if ticked and self.safe_label.winfo_manager():
            self.safe_label.pack_forget()
        elif not ticked and not self.safe_label.winfo_manager():
            self.safe_label.pack(anchor="w", pady=(theme.px(4), 0), before=self.later_box)
        show = ticked and not self.active   # folded away while a send runs (_fold)
        if show and not self.later_rest.winfo_manager():
            self.later_rest.grid(row=1, column=1, sticky="w")
        elif not show and self.later_rest.winfo_manager():
            self.later_rest.grid_remove()
        on = ticked and not self.active
        for w in (self.hours_button, self.now_check):
            w.state(["!disabled"] if on else ["disabled"])
        self.now_label.configure(style="TLabel" if on else "Muted.TLabel")

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
                "allow_local": self.app.allow_local,
                "now": bool(self.now_var.get()),
                # the Send servers first (tests: their own list), then file.kiwi
                "send": list(getattr(self.app, "later_send", None)
                             or share_send.sites_to_try())}

    def pick(self):
        if self.active or self.closed:
            return
        path = self.app.ask_backup_file()
        self.grab()
        if path:
            self.other = path
            self.other_radio.configure(text=errors.SEND_CHOSEN + os.path.basename(path))
            if not self.other_radio.winfo_manager():
                self.other_radio.pack(anchor="w", pady=(TIGHT, 0), after=self.newest_radio)
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
        fresh = path is None
        if fresh and self.made and os.path.isfile(self.made):
            path = self.made     # a retry: send the backup made for the failed try again
        self.made = None
        settings = config.load_settings()
        flags = dict(addons=settings.get("backup_addons", True) is not False,
                     settings=settings.get("backup_settings", True) is not False)
        if path is None and flags["addons"]:
            if self.ask_spoken():
                return True
            flags["leave_out"] = self.leave_out()
            flags["others"] = backup_ui.other_record(self.app)   # Other addons record
            # spoken_once stays: a retry in this window uses the same answer (no 2nd ask).
        self._reset_try()
        for w in (self.new_radio, self.newest_radio, self.other_radio, self.other_button,
                  self.go_button, self.later_check):
            w.state(["disabled"])
        self.later = self.later_choice()
        self.active = True
        self._later_state()
        self._fold(True)
        if path is None:
            self.running = True          # making the backup (Cancel stops it, see close)
            self.left_out = bool(flags.get("leave_out"))
            self.app.busy = True
            self.app.render()
            # The bar moves at once while the files are listed, then fills by bytes.
            self.progress.pack(fill="x", pady=(theme.px(6), 0), before=self.buttons)
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(15)
            self.set_status(errors.SEND_MAKING + (" " + errors.SPOKEN_LEFT_OUT
                                                  if self.left_out else ""))
        else:
            self.set_status(errors.SEND_CONNECTING)
        threading.Thread(target=_send_worker, daemon=True,
                         args=(self.app.queue, self.tag, self.app.check_game,
                               self.app.addons_dir, backup_ui.backup_folder(settings), flags, path,
                               self.app.share_url, self.app.allow_local,
                               self.cancel_event, self.later, fresh)).start()
        return True

    def _reset_try(self):
        """A clean start for every try: nothing of a failed try is left (its late messages
        are ignored by the new tag; a new cancel flag, so an old Stop doesn't stop this)."""
        self.tag = object()
        self.speed = Speed()
        self.cancel_event = threading.Event()
        self._clear_code()
        self.started = False
        self.later_mode = False
        self.later_done = False
        self.later_host = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.progress.pack_forget()
        self.set_status("")
        self.cancel_button.configure(text=errors.CANCEL)   # it said Close after the error

    def _clear_code(self):
        """The code, Copy and countdown go (a failed try's code can't be used)."""
        self.code = None
        self.ends_at = None
        self._stop_tick()
        for w in self.code_area.winfo_children():
            w.destroy()
        self.code_area.configure(height=1)   # the emptied frame shrinks back

    def _fold(self, folded, change=False):
        """While a send runs, "What to send" is one line (the choice made) and the
        file.kiwi details are hidden: the window stays short (Steam Deck: 800 px). After an
        error that Try again can fix it stays one line, with Change... (change=True) to open
        the choices again; the file.kiwi details come back (they show before every try)."""
        try:
            if folded:
                radio = {"newest": self.newest_radio, "other": self.other_radio}.get(
                    self.choice.get())
                what = (" ".join(str(radio.cget("text")).split()) if radio is not None
                        else errors.SEND_FOLDED_NEW)
                self.what_line.configure(text=errors.SEND_FOLDED + " " + what)
                if change and not self.change_link.winfo_manager():
                    self.change_link.pack(side="right", anchor="ne", padx=(theme.px(8), 0))
                elif not change and self.change_link.winfo_manager():
                    self.change_link.pack_forget()
                if not self.what_row.winfo_manager():
                    self.what_row.pack(fill="x", pady=(theme.px(4), 0), before=self.what_box)
                self.what_box.pack_forget()
            else:
                if not self.what_box.winfo_manager():
                    self.what_box.pack(fill="x", before=self.what_row)
                self.what_row.pack_forget()
            self._later_state()   # hidden while a send runs, back while ticked
            self._game_state()
        except tk.TclError:
            pass   # the window is closing

    def unfold(self):
        """Change... after an error: the choices open again."""
        if self.active or self.closed:
            return
        self._fold(False)

    def _after_error(self, retry):
        """The window works again: the choices as they were, and Try again (or Get code when
        the same try can't work, e.g. a picked file that isn't a backup). Click-only.
        Try again: "What to send" stays one line (Change... opens it); Get code: open."""
        self._fold(bool(retry), change=True)
        for w in (self.new_radio, self.other_radio, self.other_button, self.go_button,
                  self.later_check):
            w.state(["!disabled"])
        if self.newest is not None:
            self.newest_radio.state(["!disabled"])
        self._later_state()
        self.go_button.configure(text=errors.SEND_TRY_AGAIN if retry else errors.SEND_GET_CODE)

    def _determinate(self):
        if str(self.progress.cget("mode")) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    def _ends_text(self, seconds):
        if self.later and self.later.get("now"):
            return ""   # no countdown: it goes up at once, the code works for the hours
        return errors.later_in(seconds) if self.later else errors.code_ends(seconds)

    def show_code(self, code, seconds):
        self.code = code
        if self.later:   # the countdown runs to the switch to "later"
            seconds = min(seconds, share_later.LATER_AFTER)
        self.ends_at = time.monotonic() + seconds
        ttk.Label(self.code_area, text=errors.SEND_CODE_HEAD, wraplength=theme.px(WRAP),
                  justify="left").pack(anchor="w", pady=(theme.px(10), 0))
        self.code_label = ttk.Label(self.code_area, text=code, font=code_font(),
                                    foreground=theme.TEXT)
        self.code_label.pack(anchor="w", pady=(theme.px(4), 0))
        line = ttk.Frame(self.code_area)
        line.pack(fill="x", pady=(theme.px(4), 0))
        self.copy_button = ttk.Button(line, text=errors.SEND_COPY, command=self.copy,
                                      width=theme.button_chars(line))
        self.copy_button.pack(side="left")
        self.ends_var = tk.StringVar(value=self._ends_text(seconds))
        # Wraps beside Copy (the "uploaded for later" countdown is two lines).
        room = max(theme.px(120), theme.px(WRAP) - self.copy_button.winfo_reqwidth()
                   - theme.px(8))
        ttk.Label(line, textvariable=self.ends_var, style="Muted.TLabel", wraplength=room,
                  justify="left").pack(side="left", padx=(theme.px(8), 0))
        self.set_status(errors.SEND_WAITING)
        self._tick()

    def copy(self):
        if self.code:
            self.app.clipboard(self.code)
            self.set_status(errors.SEND_COPIED + " " + errors.SEND_WAITING
                            if not self.started else self.status_var.get())

    tick_job = None

    def _tick(self):
        self.tick_job = None
        if self.closed or self.started or self.ends_at is None:
            return
        self.ends_var.set(self._ends_text(self.ends_at - time.monotonic()))
        try:
            self.tick_job = self.win.after(TICK_MS, self._tick)
        except tk.TclError:
            pass

    def _stop_tick(self):
        """No countdown left waiting (a new code starts its own; closing leaves none)."""
        if self.tick_job is not None:
            try:
                self.win.after_cancel(self.tick_job)
            except tk.TclError:
                pass
            self.tick_job = None

    def close(self):
        if not self.running:
            self._stop_tick()
        super().close()

    def handle(self, msg):
        kind = msg[0]
        if not kind.startswith("share_") or not self.mine(msg):
            return
        if kind == "share_spoken":
            if not self.active:
                self.spoken_ready(msg[2], msg[3])
            self.show_game(len(msg) > 4 and msg[4])
            return
        if kind == "share_game":   # WoW open when the new backup is made: it goes on
            self.show_game(msg[2])
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
        if kind == "share_making":   # bytes of the new backup written so far
            if not self.running:
                return
            self._determinate()
            self.set_progress(msg[2], msg[3])
            if not self.cancel_event.is_set():   # keep "Stopping..."
                self.set_status(errors.backup_making(int(self.progress.cget("value")),
                                                     self.left_out))
        elif kind == "share_made":
            self.running = False
            self.app.busy = False
            self.app.render()
            # The making bar goes; it comes back for the sending progress.
            self._determinate()
            self.progress.configure(value=0)
            self.progress.pack_forget()
            if msg[2] is not None:
                self.set_status(errors.SEND_CONNECTING)
        elif kind == "share_code":
            self.show_code(msg[2], msg[3])
        elif kind == "share_progress":
            self.started = True
            if self.code:
                self.ends_var.set("")
            self.set_progress(msg[2], msg[3])
            left = self.speed.left(msg[2], msg[3])
            self.set_status(errors.later_uploading(msg[2], msg[3], left, self.later_host)
                            if self.later_mode
                            else errors.sending(msg[2], msg[3], left))
        elif kind == "share_later":
            self.started = True
            if self.code:
                self.ends_var.set("")
            if msg[2] == "host":   # where it goes: a Send server or file.kiwi
                self.later_host = msg[3]
            elif msg[2] == "uploading":
                self.later_mode = True
                self.speed = Speed()
                self.set_status(errors.later_uploading_text(msg[3], self.later_host))
            elif msg[2] == "kiwi_down":   # file.kiwi isn't answering: it tries again by itself
                until = time.strftime("%H:%M", time.localtime(time.time() + (msg[3] or 0)))
                self.set_status(errors.later_kiwi_down(until))
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
            self.progress.stop()
            self.progress.pack_forget()
            self._clear_code()       # that code can't be used any more
            self.made = msg[4] if len(msg) > 4 else None
            self.set_status(msg[2])
            self.cancel_button.configure(text=errors.CLOSE)
            self._after_error(msg[3] if len(msg) > 3 else True)


class ReceiveDialog(_ShareDialog):
    """Receive: type the code -> progress -> the file is checked -> the Restore window."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.RECEIVE_TITLE, show)
        ttk.Label(self.frame, text=errors.RECEIVE_TYPE).pack(anchor="w", pady=(theme.px(6), 0))
        self.code_var = tk.StringVar()
        self.entry = ttk.Entry(self.frame, textvariable=self.code_var, width=24,
                               font=code_font())
        self.entry.pack(anchor="w", fill="x", pady=(theme.px(4), 0))
        self.entry.bind("<Return>", lambda _e: self.start())
        code = clipboard_code(getattr(app, "read_clipboard", None))
        if code:   # a copied code: filled in and selected (typing replaces it)
            self.code_var.set(code)
            self.entry.select_range(0, "end")
            self.entry.icursor("end")
        ttk.Label(self.frame, text=errors.RECEIVE_EXAMPLE, style="Muted.TLabel").pack(
            anchor="w", pady=(theme.px(2), 0))
        ttk.Label(self.frame, text=errors.RECEIVE_NOTE, style="Muted.TLabel", wraplength=theme.px(WRAP),
                  justify="left").pack(anchor="w", pady=(theme.px(10), 0))
        self._status_area(collapse=True)
        self.cancel_button = ttk.Button(self.buttons, text=errors.CANCEL, command=self.close)
        self.cancel_button.pack(side="right")
        self.go_button = ttk.Button(self.buttons, text=errors.RECEIVE_OK,
                                    style="Accent.TButton", command=self.start)
        self.go_button.pack(side="right", padx=(0, theme.px(6)))
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
        self.store_download = False
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
        elif kind == "share_later" and msg[2] == "store":   # downloading from a Send
            self.store_download = True                     # server: closing asks first
        elif kind == "share_later":   # the other PC is still uploading it for later
            self.set_progress(msg[3] or 0, 1000)
            self.set_status(errors.later_waiting(msg[3] or 0))
        elif kind == "share_ticket":
            self.show_ticket(msg[2])
        elif kind == "share_locking":   # the locked kept copy is being written
            self.set_progress(msg[2], msg[3])
            self.set_status(errors.receive_locking(int(self.progress.cget("value"))))
        elif kind == "share_received":
            app, show = self.app, self.shown
            self.active = False
            super().close()
            app.backup_dialog = backup_ui.RestoreDialog(
                app, msg[3], show=show, kept=msg[4] if len(msg) > 4 else None)
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

    store_download = False   # downloading a send kept on a Send server
    asking = False

    def close(self):
        """Cancel/Esc/X. While a Send-server download runs, stopping deletes that send for
        good, so ask first (only a yes stops it)."""
        if self.active and self.store_download and not self.closed:
            if self.asking:
                return
            self.asking = True
            try:
                ok = self.app.ask(errors.RECEIVE_STOP_TITLE, errors.RECEIVE_STOP_TEXT,
                                  errors.RECEIVE_STOP_OK, errors.RECEIVE_STOP_KEEP)
            finally:
                self.asking = False
            if not ok or self.closed:   # kept on, or it finished while the question was up
                return
        super().close()

    # ---------- send later: download in the browser, then open it here ----------
    ticket = None
    opening = False
    trying = None        # (path, size, mtime) of the file being opened
    failed = frozenset()  # files that didn't open: the Downloads watch skips them

    def show_ticket(self, ticket):
        """The code unlocked a send-later: Download (browser) and Open file...;
        the Downloads folder is watched so the file opens by itself once it's there."""
        self.ticket = ticket
        self.failed = set()
        self.progress.pack_forget()
        area = ttk.Frame(self.frame)
        below = (self.status if self.status.winfo_manager() else
                 self.progress if self.progress.winfo_manager() else self.buttons)
        area.pack(fill="x", before=below, pady=(theme.px(10), 0))
        # The texts wrap at the width the window has now (px(WRAP) can be wider than the
        # code entry and its note: the window would widen when the ticket shows).
        width = self.buttons.winfo_width()
        wrap = min(theme.px(WRAP), width) if width > 20 else theme.px(WRAP)

        def note(text, style="TLabel", gap=0):
            label = ttk.Label(area, text=text, style=style, wraplength=wrap, justify="left")
            label.pack(anchor="w", fill="x", pady=(gap, 0))
            backup_ui.fit_wrap(label)
        note(errors.LATER_GOT)
        note(errors.LATER_KEEP_OPEN, "Muted.TLabel", theme.px(4))
        # Download and Open file...: two equal halves of the width the window
        # already has (a small char width + sticky ew: the pair never widens the window).
        line = ttk.Frame(area)
        line.pack(fill="x", pady=(theme.px(6), 0))
        line.columnconfigure((0, 1), weight=1, uniform="ticket")
        self.download_button = ttk.Button(line, text=errors.LATER_DOWNLOAD, width=1,
                                          style="Accent.TButton", command=self.download)
        self.download_button.grid(row=0, column=0, sticky="ew", padx=(0, theme.px(3)))
        self.open_button = ttk.Button(line, text=errors.LATER_OPEN, width=1,
                                      command=self.pick_download)
        self.open_button.grid(row=0, column=1, sticky="ew", padx=(theme.px(3), 0))
        # The code is used up now: if the browser doesn't open, the link can still be
        # copied into it by hand. Copy link sits at the right, like the window's buttons.
        note(errors.LATER_LINK_NOTE, "Muted.TLabel", theme.px(8))
        row = ttk.Frame(area)
        row.pack(fill="x", pady=(theme.px(4), 0))
        self.copy_link_button = ttk.Button(row, text=errors.LATER_COPY_LINK,
                                           command=self.copy_link,
                                           width=max(theme.button_chars(row), theme.chars(
                                               row, [errors.LATER_COPY_LINK])))
        self.copy_link_button.pack(side="right")
        self.link_var = tk.StringVar(value=ticket.link)
        self.link_entry = ttk.Entry(row, textvariable=self.link_var, state="readonly",
                                    style="Path.TEntry", width=10, font=theme.font())
        self.link_entry.pack(side="left", fill="x", expand=True, padx=(0, theme.px(6)),
                             ipady=theme.px(1))
        self.go_button.pack_forget()
        self.set_status(errors.LATER_LOOKING)
        self._poll_download()

    def download(self):
        if self.ticket is not None and not self.closed:
            if self.app.open_link(self.ticket.link) is False:   # no browser started
                self.set_status(errors.LATER_NO_BROWSER)

    def copy_link(self):
        if self.ticket is not None and not self.closed:
            self.app.clipboard(self.ticket.link)
            self.set_status(errors.LATER_LINK_COPIED)

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
                               self.cancel_event, self.code_var.get())).start()


MAX_CLIPBOARD = 80   # characters: anything longer is not a code


def clipboard_code(read):
    """A valid code from the clipboard (read() -> text), written the normal way, or "".
    Read once, only when the Receive window opens; anything that isn't a valid code is
    ignored. The text is never sent anywhere or logged."""
    try:
        text = read() if read else ""
    except Exception:
        return ""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CLIPBOARD:
        return ""
    try:
        channel, words = share.parse_code(text)
    except share.ShareError:
        return ""
    return share.format_code(channel, words)


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


def _spoken_worker(q, tag, addons_dir, check_game=None):
    """The Spoken addons a new backup would hold, all addon folders' bytes, and whether
    WoW is open (process names only, see gamecheck)."""
    q.put(("share_spoken", tag, backup.spoken_folders(addons_dir) if addons_dir else [],
           backup.addons_size(addons_dir) if addons_dir else 0,
           backup_ui._game_open(check_game) if check_game else False))


_NO_RETRY_BACKUP = ("nothing", "newer_format", "unsafe", "inside_game", "not_backup", "bad_zip")


def check_failed(exc):
    """The backup to send didn't pass the send check (backup.read refused it)."""
    return (isinstance(exc, share.ShareError) and getattr(exc, "kind", "") == "not_backup"
            and str(getattr(exc, "detail", "")).startswith(share.CHECK_PREFIX))


def retry_helps(exc, fresh=False):
    """Can the same try work when the player clicks Try again? Not for a picked file that
    isn't a backup, a backup that's too big, a backup that can't be made here, or a new
    backup that failed the send check (a new one would fail the same way)."""
    kind = getattr(exc, "kind", "")
    if isinstance(exc, share.ShareError):
        if kind == "too_big":
            return False
        if kind == "not_backup":   # a picked file stays bad; so does a new one that failed
            return getattr(exc, "detail", "") == "file changed" or (
                fresh and not check_failed(exc))
    return kind not in _NO_RETRY_BACKUP


def _later_hook(q, tag):
    """on_later for a send: "kept" (what Stop and "picked up?" need) goes into this PC's
    encrypted codes file (am/codes.py), never into the queue or the log; the rest is
    posted as ("share_later", tag, state, value)."""
    def on_later(state, value):
        if state == "kept":
            try:
                codes.add_sent(value["code"], value["channel"], value["token"], value["host"],
                               time.time() + (value["left"] or 0), value["hours"],
                               value["parts"])
            except Exception as e:   # noqa: BLE001 (Your codes is a help, never a blocker)
                log.warning("Could not remember the sent code (%s)", type(e).__name__)
            return
        q.put(("share_later", tag, state, value))
    return on_later


def _send_worker(q, tag, check_game, addons_dir, folder, flags, path, url, allow_local, cancel,
                 later=None, fresh=None):
    """fresh: the backup is made for this send (None = when no path is given). A retry
    passes the backup made for the failed try with fresh=True.
    ("share_error", tag, text, retry, made): made = the new backup a retry can send again."""
    fresh = path is None if fresh is None else fresh
    made = None
    try:
        relay = share.Relay(url, allow_local=allow_local)
        if path is None:
            if backup_ui._game_open(check_game):   # WoW open: made anyway, the strip says so
                q.put(("share_game", tag, True))
            if flags.get("addons"):   # too big to send, or no room: say so before zipping
                share.precheck(backup.addons_size(addons_dir, flags.get("leave_out", ())),
                               bool(later), folder)
            try:
                path, _size, _c = backup.make(
                    addons_dir, folder, cancel=cancel, progress=backup_ui.progress_poster(
                        lambda d, t: q.put(("share_making", tag, d, t))), **flags)
            finally:
                q.put(("share_made", tag, path))
            made = path
        elif fresh:
            made = path
        share.send(path, relay, lambda code, ttl: q.put(("share_code", tag, code, ttl)),
                   progress=lambda d, t: q.put(("share_progress", tag, d, t)), cancel=cancel,
                   later=later, on_later=_later_hook(q, tag))
        q.put(("share_sent", tag))
    except Exception as e:
        log.warning("Send failed (%s %s %s)", type(e).__name__, getattr(e, "kind", ""),
                    getattr(e, "detail", ""))
        msg = (errors.share_message(e, fresh=fresh) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        bad = isinstance(e, share.ShareError) and getattr(e, "kind", "") == "not_backup"
        # A new backup that failed the send check is kept as `made` too: Get code then
        # tries that same file (no new zip on every click) and says so again.
        keep = made if (not bad or (fresh and check_failed(e))) else None
        q.put(("share_error", tag, msg, retry_helps(e, fresh), keep))


def _kept_received(q, tag, path, code, cancel):
    """The received backup is kept LOCKED with its code (am/backup_lock.py; this PC
    remembers the code in am/codes.py); its plain copy stays hidden while the Restore
    window shows it. Posts "share_received" with the backup.read info and the Kept."""
    kept = backup_lock.keep(
        path, code, remember=codes.remember_lock,
        progress=backup_ui.progress_poster(lambda d, t: q.put(("share_locking", tag, d, t))))
    try:
        if cancel is not None and cancel.is_set():   # the window was closed meanwhile
            raise share.ShareError("cancelled")
        info = backup.read(kept.plain, received=True)
    except BaseException:
        kept.discard()
        raise
    q.put(("share_received", tag, kept.plain, info, kept))


def _receive_worker(q, tag, url, allow_local, code, folder, cancel):
    try:
        relay = share.Relay(url, allow_local=allow_local)
        path = share.receive(code, relay, folder, cancel=cancel,
                             progress=lambda d, t: q.put(("share_progress", tag, d, t)),
                             on_start=lambda: q.put(("share_matched", tag)),
                             on_later=lambda s, p: q.put(("share_later", tag, s, p)),
                             hidden=True)
        if isinstance(path, share.LaterTicket):   # uploaded for later: browser step next
            q.put(("share_ticket", tag, path))
            return
        _kept_received(q, tag, path, code, cancel)
    except Exception as e:
        log.warning("Receive failed (%s %s %s)", type(e).__name__, getattr(e, "kind", ""),
                    getattr(e, "detail", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))


def _open_later_worker(q, tag, ticket, path, folder, cancel, code=None):
    """Open the downloaded send-later file into the backup folder (then Restore)."""
    try:
        final = share.open_later(ticket, path, folder, cancel=cancel,
                                 progress=lambda d, t: q.put(("share_progress", tag, d, t)),
                                 hidden=bool(code))
        if code:
            _kept_received(q, tag, final, code, cancel)
        else:   # no code known (tests calling the worker by hand): kept as the plain zip
            q.put(("share_received", tag, final, backup.read(final, received=True)))
    except Exception as e:
        log.warning("Opening the download failed (%s %s %s)", type(e).__name__,
                    getattr(e, "kind", ""), getattr(e, "detail", ""))
        msg = (errors.share_message(e) if isinstance(e, share.ShareError)
               else errors.backup_message(e))
        q.put(("share_error", tag, msg))


def _ask_later_file(parent):
    """Pick the downloaded send-later file (starts in Downloads)."""
    from tkinter import filedialog
    dirs = share_later.downloads_dirs()
    return filedialog.askopenfilename(
        parent=parent, title=errors.LATER_OPEN_TITLE,
        initialdir=dirs[0] if dirs and os.path.isdir(dirs[0]) else None,
        filetypes=[("Addon Manager send", "*" + share_later.EXT), ("All files", "*.*")])
