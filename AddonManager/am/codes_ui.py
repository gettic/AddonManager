"""The "Your codes" window (Backup window > Codes...): this PC's send-later codes that may
still work (time left, picked up or not, Stop, a Copy link; more than 6 rows scroll) and
the codes of locked kept backups (Show
code, a Forget button, right-click works too). The codes come from am\\codes.py (encrypted on disk); nothing
here logs or posts a code into the queue."""

import datetime
import logging
import threading
import time
import tkinter as tk
from tkinter import ttk

from am import backup_delete_ui, backup_ui, codes, errors, share, share_send, theme, touch
from am.backup_ui import WRAP

log = logging.getLogger(__name__)

POLL_MS = 60 * 1000          # ask the relay again while the window is open
MAX_ROWS = 50                # per group (newest first); more = "(50 of N, newest)"
VISIBLE_ROWS = 6             # rows in view (3 + 3 fit the Linux 150 % limit); more scroll
OPEN_STATES = ("ready", "uploading", "unknown")


def _date(epoch):
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "?"


def summary(now=None):
    """(active sent codes, soonest seconds left or None, locked codes) for the Backup
    window's line. Never raises."""
    now = time.time() if now is None else now
    try:
        sent = codes.sent(now=now)
        locked = codes.locked()
    except Exception as e:   # noqa: BLE001
        log.info("Codes not readable (%s)", type(e).__name__)
        return 0, None, 0
    active = [e for e in sent if e.get("state", "ready") in OPEN_STATES]
    left = min((e["end"] - now for e in active), default=None)
    return len(active), left, len(locked)


class CodesDialog(backup_ui._Dialog):
    def __init__(self, app, show=True):
        super().__init__(app, errors.CODES_TITLE, show)
        self.token = object()          # this window's messages only
        self.poll_job = None
        self.stopping = set()
        self.shown_codes = set()
        self.live = {}                 # entry id -> (state, left) from the relay
        self.area = ttk.Frame(self.frame)
        self.area.pack(fill="x")
        self.note = ttk.Label(self.frame, text=errors.CODES_NOTE, style="Muted.TLabel",
                              wraplength=theme.px(WRAP), justify="left")
        self.note.pack(anchor="w", fill="x", pady=(theme.px(10), 0))
        backup_ui.fit_wrap(self.note)
        self._status_area(collapse=True)
        # A status line (Stopped / couldn't reach) takes the note's place, so the window
        # never grows past a small screen (Linux 150 %: 960 px) with every row shown.
        self.status_var.trace_add("write", lambda *_a: self._note_or_status())
        self.close_button = ttk.Button(self.buttons, text=errors.CLOSE, command=self.close)
        self.close_button.pack(side="right")
        self.row_buttons = []
        self.render()
        self._show(self.close_button,
                   extra=(errors.CODES_STOP, errors.CODES_SHOW, errors.CODES_HIDE,
                          errors.CODES_FORGET_BUTTON))
        self.check_status()

    def _note_or_status(self):
        try:
            if self.status_var.get():
                self.note.pack_forget()
            elif not self.note.winfo_manager():
                self.note.pack(anchor="w", fill="x", pady=(theme.px(10), 0), before=self.buttons)
        except tk.TclError:
            pass

    # ---------- rows ----------
    def _button_chars(self, parent):
        return max(theme.button_chars(parent),
                   theme.chars(parent, [errors.CODES_STOP, errors.CODES_SHOW,
                                        errors.CODES_HIDE, errors.CODES_FORGET_BUTTON,
                                        errors.CLOSE, errors.CANCEL]))

    def render(self):
        for child in self.area.winfo_children():
            child.destroy()
        self.row_buttons = []
        self.copy_links = []
        self.canvas = None
        self.placed = []               # (widget, top gap, is a row) in list order
        self.holder = self.area
        now = time.time()
        sent = sorted(codes.sent(now=now), key=lambda e: e.get("made", 0), reverse=True)
        locked = sorted(codes.locked().items(), key=lambda kv: kv[1].get("saved", 0),
                        reverse=True)
        self.sent_ids = [e["id"] for e in sent]
        if not sent and not locked:
            ttk.Label(self.area, text=errors.CODES_NONE, wraplength=theme.px(WRAP),
                      justify="left").pack(anchor="w", pady=(theme.px(10), 0))
            return
        wrap = theme.px(WRAP) * 3 // 5
        chars = self._button_chars(self.area)
        if min(len(sent), MAX_ROWS) + min(len(locked), MAX_ROWS) > VISIBLE_ROWS:
            # more than fit a small screen: every code in a list that scrolls
            canvas = self.canvas = tk.Canvas(self.area, background=theme.BG,
                                             highlightthickness=0)
            bar = ttk.Scrollbar(self.area, orient="vertical", command=canvas.yview,
                                style="Gold.Vertical.TScrollbar")
            canvas.configure(yscrollcommand=bar.set)
            canvas.pack(side="left", fill="both", expand=True)
            bar.pack(side="right", fill="y", padx=(theme.px(6), 0))
            self.holder = ttk.Frame(canvas)
            item = canvas.create_window((0, 0), window=self.holder, anchor="nw")
            # the rows fill the list's width: their buttons sit right next to the bar
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
            self.holder.bind("<Configure>",
                             lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        if sent:
            self._head(errors.codes_head(errors.CODES_SENT, len(sent), MAX_ROWS))
            for i, e in enumerate(sent[:MAX_ROWS]):
                state, left = self.live.get(e["id"], (e.get("state", "ready"), None))
                if left is None and state in ("ready", "unknown"):
                    left = max(0, e["end"] - now)
                when = _date(e["state_at"]) if e.get("state_at") else None
                host = errors.HOST_NAMES.get(e.get("host"), e.get("host") or "")
                sub = f"{host}: {errors.code_state(state, left, when)}" if host else \
                    errors.code_state(state, left, when)
                b = self._row(i, e["code"], sub, errors.CODES_STOP, wrap, chars,
                              lambda e=e: self.stop(e), bold=True, copy=e["code"])
                if (state not in OPEN_STATES or e["id"] in self.stopping
                        or not self.app.share_url):
                    b.state(["disabled"])
        if locked:
            self._head(errors.codes_head(errors.CODES_LOCKED, len(locked), MAX_ROWS))
            for i, (fid, e) in enumerate(locked[:MAX_ROWS]):
                name = e.get("name", "").rsplit(".", 1)[0]
                showing = fid in self.shown_codes
                sub = e["code"] if showing else errors.locked_line(_date(e.get("saved")), True)
                b = self._row(i, name, sub, errors.CODES_HIDE if showing else errors.CODES_SHOW,
                              wrap, chars, lambda fid=fid: self.toggle_code(fid),
                              forget=lambda fid=fid: self.forget(fid),
                              copy=e["code"] if showing else None)
                self.row_buttons.append(b)
                self.row_buttons.append(self._forget_button)
        if self.canvas is not None:
            self._fit_canvas()

    def _fit_canvas(self):
        """The list shows VISIBLE_ROWS rows (with their headings); the rest scrolls."""
        self.holder.update_idletasks()
        height = rows = 0
        for w, pad, is_row in self.placed:
            height += pad + w.winfo_reqheight()
            rows += is_row
            if rows >= VISIBLE_ROWS:
                break
        step = max(1, min(w.winfo_reqheight() for w, _p, r in self.placed if r) // 2)
        self.canvas.configure(width=self.holder.winfo_reqwidth(), height=height,
                              yscrollincrement=step)
        backup_delete_ui._wheel(self.canvas, self.canvas)
        backup_delete_ui._wheel(self.canvas, self.holder)

    def _head(self, text):
        w = ttk.Label(self.holder, text=text, style="Head.TLabel", font=theme.head_font(1))
        w.pack(anchor="w", pady=(theme.px(10), 0))
        self.placed.append((w, theme.px(10), False))

    def copy_code(self, code):
        """The Copy link: the code onto the clipboard (never logged)."""
        if self.closed:
            return
        self.app.clipboard(code)
        self.set_status(errors.CODES_COPIED)

    def _copy_line(self, line, text, style, font):
        """The code with a small gold "Copy" link right after it."""
        box = ttk.Frame(line)
        lab = ttk.Label(box, text=text, style=style, font=font)
        lab.pack(side="left")
        link = tk.Label(box, text=errors.CODES_COPY, cursor="hand2", background=theme.BG,
                        foreground=theme.LINK, font=theme.font("underline"))
        link.pack(side="left", padx=(theme.px(8), 0))
        link.bind("<Button-1>", lambda _e, t=text: self.copy_code(t))
        link.code = text
        self.copy_links.append(link)
        return box, lab

    def _row(self, i, title, sub, button_text, wrap, chars, command, bold=False, forget=None,
             copy=None):
        line = ttk.Frame(self.holder)
        pad = theme.px(6 if i else 4)
        line.pack(fill="x", pady=(pad, 0))
        self.placed.append((line, pad, True))
        b = ttk.Button(line, text=button_text, width=chars, command=command)
        if forget is None:
            b.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(theme.px(8), 0))
        else:   # Show code over Forget (a visible button: no right-click on a Steam Deck)
            b.grid(row=0, column=1, sticky="ne", padx=(theme.px(8), 0))
            self._forget_button = ttk.Button(line, text=errors.CODES_FORGET_BUTTON,
                                             width=chars, command=forget)
            self._forget_button.grid(row=1, column=1, rowspan=2, sticky="ne",
                                     padx=(theme.px(8), 0), pady=(theme.px(4), 0))
        if copy is not None and bold:   # a sent code: Copy right after the code
            tbox, t = self._copy_line(line, title, "TLabel", theme.font("bold"))
            tbox.grid(row=0, column=0, sticky="w")
        else:
            t = ttk.Label(line, text=title, wraplength=wrap, justify="left",
                          font=theme.font("bold") if bold else theme.font())
            t.grid(row=0, column=0, sticky="w")
        if copy is not None and not bold:   # a shown locked code: Copy right after it
            sbox, s = self._copy_line(line, sub, "Muted.TLabel", None)
            sbox.grid(row=1, column=0, sticky="w")
        else:
            s = ttk.Label(line, text=sub, style="Muted.TLabel", wraplength=wrap, justify="left")
            s.grid(row=1, column=0, sticky="w")
        line.columnconfigure(0, weight=1)
        if forget is not None:
            for w in (line, t, s):
                touch.bind_menu(w, lambda event, f=forget: self._forget_menu(event, f))
        if bold:
            self.row_buttons.append(b)
        return b

    def _forget_menu(self, event, forget):
        menu = tk.Menu(self.win, tearoff=0, **theme.menu_options())
        menu.add_command(label=errors.CODES_FORGET, command=forget)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------- actions ----------
    def toggle_code(self, fid):
        if fid in self.shown_codes:
            self.shown_codes.discard(fid)
        else:
            self.shown_codes.add(fid)
        self.render()

    def forget(self, fid):
        ok = self.app.ask(errors.CODES_FORGET_TITLE, errors.CODES_FORGET_TEXT,
                          errors.CODES_FORGET_OK, errors.CANCEL)
        self.grab()
        if ok and not self.closed:
            codes.forget_lock(fid)
            self.shown_codes.discard(fid)
            self.render()

    def stop(self, entry):
        if entry["id"] in self.stopping or not self.app.share_url or self.closed:
            return False
        ok = self.app.ask(errors.CODES_STOP_TITLE, errors.codes_stop_text(entry.get("host")),
                          errors.CODES_STOP_OK, errors.CANCEL)
        self.grab()
        if not ok or self.closed:
            return False
        self.stopping.add(entry["id"])
        self.render()
        threading.Thread(target=_stop_worker, daemon=True,
                         args=(self.app.queue, self.token, self.app.share_url,
                               self.app.allow_local, entry["id"])).start()
        return True

    def check_status(self):
        """Ask the relay about the codes that may still be waiting (in a thread)."""
        self.poll_job = None
        if self.closed or not self.app.share_url:
            return
        ids = [i for i in self.sent_ids
               if self.live.get(i, ("ready",))[0] in OPEN_STATES]
        if ids:
            threading.Thread(target=_status_worker, daemon=True,
                             args=(self.app.queue, self.token, self.app.share_url,
                                   self.app.allow_local, ids)).start()
        try:
            self.poll_job = self.win.after(POLL_MS, self.check_status)
        except tk.TclError:
            pass

    def close(self):
        if self.poll_job is not None:
            try:
                self.win.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None
        super().close()

    def handle(self, msg):
        if len(msg) < 2 or msg[1] is not self.token or self.closed:
            return
        if msg[0] == "backup_codes_status":
            self.live.update(msg[2])
            self.render()
        elif msg[0] == "backup_codes_stopped":
            entry_id, ok = msg[2], msg[3]
            self.stopping.discard(entry_id)
            if ok == "unreadable":
                self.set_status(errors.CODES_STOP_UNREADABLE)
            else:
                if ok:
                    self.live[entry_id] = ("stopped", None)
                self.set_status(errors.CODES_STOPPED if ok else errors.CODES_STOP_FAILED)
            self.render()


# ---------- threads (plain values only) ----------
def _entry(entry_id, strict=False):
    """The sent code's entry, or None when it's gone (its time ended). strict: a codes
    file that can't be read now raises codes.Unreadable (instead of looking gone)."""
    for e in codes.sent(strict=strict):
        if e["id"] == entry_id:
            return e
    return None


def _status_worker(q, token, url, allow_local, ids):
    """{entry id: (state, left)} from the relay; final states are saved with their time."""
    out = {}
    for entry_id in ids:
        e = _entry(entry_id)
        if e is None:
            continue
        try:
            st = share.Relay.for_sender(url, e["ch"], e["token"], allow_local).status()
        except share.ShareError as ex:
            log.info("Code status not known (%s)", ex.kind)
            continue
        if st is None:   # the relay doesn't know it (or is older): keep what we had
            out[entry_id] = ("unknown", None) if time.time() < e["end"] else ("ended", None)
            continue
        out[entry_id] = (st["state"], st["left"])
        if st["state"] != e.get("state"):
            codes.update_sent(entry_id, state=st["state"],
                              state_at=time.time() if st["state"] not in OPEN_STATES else None)
    q.put(("backup_codes_status", token, out))


def _stop_worker(q, token, url, allow_local, entry_id):
    """ok: True (stopped, or gone already), False (the host wasn't reached) or
    "unreadable" (the codes file can't be read now: nothing was asked or changed)."""
    try:
        e = _entry(entry_id, strict=True)
    except codes.Unreadable:
        q.put(("backup_codes_stopped", token, entry_id, "unreadable"))
        return
    ok = e is None
    if e is not None:
        ok = share.Relay.for_sender(url, e["ch"], e["token"], allow_local).stop()
        site = e.get("host")
        if e.get("parts") and site in share_send.SITES:
            try:
                share_send.Send(site, allow_local=allow_local).delete_all(
                    [share_send.Part(p["id"], p["owner"], b"", 0, 0) for p in e["parts"]])
            except Exception as ex:   # noqa: BLE001 (the server deletes it by itself later)
                log.info("Send copy not deleted (%s)", type(ex).__name__)
        if ok:
            codes.update_sent(entry_id, state="stopped", state_at=time.time())
    q.put(("backup_codes_stopped", token, entry_id, ok))


def open_codes(app, show=True):
    """Codes... in the Backup window (one small window at a time)."""
    if app.backup_dialog is not None:
        return None
    app.backup_dialog = CodesDialog(app, show=show)
    return app.backup_dialog
