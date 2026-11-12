"""The "Delete old backups" window (Backup window > Old backups...): tick backups found
in the backup folders and move them to the Recycle Bin / Trash (deleted for good only
on a drive without one, after saying so). The rules live in am\\backup_delete.py."""

import logging
import os
import threading
import tkinter as tk
from tkinter import ttk

from am import backup, backup_delete, backup_ui, config, errors, theme

log = logging.getLogger(__name__)

MAX_VISIBLE_ROWS = 8     # more backups: the list scrolls
MAX_ROWS = 500           # sanity cap of rows built
MAX_CONFIRM_LINES = 4    # files named in the question; the rest as "and N more"
MAX_RESULT_NAMES = 3

def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def row_text(f):
    """'2026-11-10 21:30 · 12 MB · OneDrive (online) · received'."""
    text = errors.found_row(backup_ui.found_date(f.mtime), f.size, f.place, f.online)
    if backup.is_received(f.path):
        text += f" · {errors.DELETE_RECEIVED}"
    return text


def total_text(items):
    if not items:
        return errors.DELETE_NOTHING
    return f"{len(items)} ticked · {errors.size_text(sum(f.size for f in items))}"


def confirm_title(n):
    return f"Delete {_plural(n, 'backup')}?"


def _lines(items):
    shown = [f"    {errors.found_row(backup_ui.found_date(f.mtime), f.size, f.place)}"
             for f in items[:MAX_CONFIRM_LINES]]
    if len(items) > MAX_CONFIRM_LINES:
        shown.append(f"    and {len(items) - MAX_CONFIRM_LINES} more")
    return "\n".join(shown)


def confirm_text(items, for_good, clouds):
    """The question before deleting: what goes to the bin, what is deleted for good
    (no bin on that drive), the total, and the cloud / safety-copy lines when they apply."""
    to_bin = [f for f in items if f.path not in for_good]
    gone = [f for f in items if f.path in for_good]
    parts = []
    if to_bin:
        parts.append(f"{errors.DELETE_TO_BIN}\n{_lines(to_bin)}")
    if gone:
        parts.append(f"{errors.DELETE_FOR_GOOD}\n{_lines(gone)}")
    parts.append(f"Total: {errors.size_text(sum(f.size for f in items))}")
    in_cloud = sorted({f.place for f in items if f.place in clouds})
    if in_cloud:
        parts.append(errors.DELETE_CLOUD.format(clouds=" and ".join(in_cloud)))
    if any(f.safety for f in items):
        parts.append(errors.DELETE_SAFETY_LINE)
    return "\n\n".join(parts)


def result_text(results):
    """After deleting: how many went where, and which stayed and why."""
    moved = sum(1 for _f, k in results if k == backup_delete.MOVED)
    deleted = sum(1 for _f, k in results if k == backup_delete.DELETED)
    left = [(f, k) for f, k in results if k not in backup_delete.DONE]
    parts = []
    if moved:
        parts.append(f"{_plural(moved, 'backup')} moved to the {errors.BIN_NAME}.")
    if deleted:
        parts.append(f"{_plural(deleted, 'backup')} deleted for good.")
    if left:
        names = "; ".join(f"{backup_ui.found_date(f.mtime)}: "
                          f"{errors.DELETE_REASONS.get(k, errors.DELETE_REASONS[backup_delete.OTHER])}"
                          for f, k in left[:MAX_RESULT_NAMES])
        more = f" and {len(left) - MAX_RESULT_NAMES} more" if len(left) > MAX_RESULT_NAMES else ""
        parts.append(f"{_plural(len(left), 'backup')} not deleted ({names}{more}).")
    return " ".join(parts)


def _wheel(canvas, widget):
    """Mouse wheel scrolls canvas over widget and everything inside it."""
    widget.bind("<MouseWheel>",
                lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"), add="+")
    widget.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"), add="+")
    widget.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"), add="+")
    for child in widget.winfo_children():
        _wheel(canvas, child)


class DeleteBackupsDialog(backup_ui._Dialog):
    """Ticks per backup (safety copies only when shown, never pre-ticked), the ticked
    total, Tick all but the newest, Delete... (question first) and Close."""

    def __init__(self, app, show=True):
        super().__init__(app, errors.DELETE_TITLE, show)
        self.found = None        # [FoundBackup] once the search is done
        self.places = []         # [(label, folder)] that were searched
        self.clouds = set()      # place labels that are cloud folders
        self.vars = {}           # path -> BooleanVar (kept across redraws)
        self.checks = []
        self.canvas = None
        self._token = 0
        ttk.Label(self.frame, text=errors.DELETE_NOTE, style="Muted.TLabel",
                  wraplength=theme.px(backup_ui.WRAP), justify="left").pack(
            anchor="w", pady=(6, 0))
        self.rows = ttk.Frame(self.frame)
        self.rows.pack(fill="x", pady=(4, 0))
        self.safety_var = tk.BooleanVar(value=False)
        self.safety_check = ttk.Checkbutton(self.frame, text=errors.DELETE_SAFETY,
                                            variable=self.safety_var, command=self.toggle_safety)
        self.safety_check.pack(anchor="w", pady=(8, 0))
        self.newest_button = ttk.Button(self.frame, text=errors.DELETE_NEWEST,
                                        command=self.tick_but_newest)
        self.newest_button.pack(anchor="w", pady=(8, 0))
        self.total_var = tk.StringVar(value=errors.DELETE_NOTHING)
        ttk.Label(self.frame, textvariable=self.total_var, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0))
        self._status_area(collapse=True)
        self.delete_button = ttk.Button(self.buttons, text=errors.DELETE_BUTTON,
                                        style="Accent.TButton", command=self.start)
        self.delete_button.pack(side="left")
        self.close_button = ttk.Button(self.buttons, text=errors.CLOSE, command=self.close)
        self.close_button.pack(side="right")
        self.set_status(errors.DELETE_LOOKING)
        self.render()
        self.search()
        self._show(self.close_button)

    # ---- the list ----
    def search(self):
        self._token += 1
        threading.Thread(target=_list_worker, daemon=True,
                         args=(self.app.queue, self._token,
                               backup_ui.backup_folder(config.load_settings()),
                               self.app.addons_dir)).start()

    def lists(self):
        """(backups, safety copies shown, more): safety copies only when ticked."""
        found = self.found or []
        rows = [f for f in found if not f.safety]
        safety = [f for f in found if f.safety] if self.safety_var.get() else []
        more = len(rows) + len(safety) > MAX_ROWS
        return rows[:MAX_ROWS], safety[:max(0, MAX_ROWS - len(rows))], more

    def visible(self):
        rows, safety, _more = self.lists()
        return rows + safety

    def ticked(self):
        return [f for f in self.visible() if self.vars.get(f.path) and self.vars[f.path].get()]

    def _var(self, f):
        var = self.vars.get(f.path)
        if var is None:
            var = self.vars[f.path] = tk.BooleanVar(value=False)
        return var

    def render(self):
        for child in self.rows.winfo_children():
            child.destroy()
        self.checks, self.canvas = [], None
        if self.found is not None:
            rows, safety, more = self.lists()
            count = len(rows) + len(safety) + (1 if safety else 0)
            holder = self.rows
            if count > MAX_VISIBLE_ROWS:
                canvas = self.canvas = tk.Canvas(self.rows, background=theme.BG,
                                                 highlightthickness=0)
                bar = ttk.Scrollbar(self.rows, orient="vertical", command=canvas.yview,
                                    style="Gold.Vertical.TScrollbar")
                canvas.configure(yscrollcommand=bar.set)
                canvas.pack(side="left", fill="both", expand=True)
                bar.pack(side="right", fill="y")
                holder = ttk.Frame(canvas)
                canvas.create_window((0, 0), window=holder, anchor="nw")
                holder.bind("<Configure>",
                            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
            for f in rows:
                self._row(holder, f)
            if safety:
                ttk.Label(holder, text=errors.DELETE_SAFETY_HEADING, style="Muted.TLabel").pack(
                    anchor="w", pady=(4, 2))
                for f in safety:
                    self._row(holder, f)
            if more:
                ttk.Label(self.rows, text=errors.DELETE_MORE.format(n=MAX_ROWS),
                          style="Muted.TLabel").pack(anchor="w")
            if self.canvas is not None:
                holder.update_idletasks()
                step = max(c.winfo_reqheight() for c in self.checks) + theme.px(2)
                self.canvas.configure(width=holder.winfo_reqwidth(),
                                      height=step * MAX_VISIBLE_ROWS, yscrollincrement=step)
                _wheel(self.canvas, self.canvas)
                _wheel(self.canvas, holder)
            if not self.running:
                self.set_status("" if rows or safety else errors.DELETE_NONE)
        self.update_buttons()

    def _row(self, holder, f):
        c = ttk.Checkbutton(holder, text=row_text(f), variable=self._var(f),
                            command=self.update_buttons)
        c.pack(anchor="w", pady=(0, theme.px(2)))
        c.backup = f
        if self.running:
            c.state(["disabled"])
        self.checks.append(c)

    def update_buttons(self):
        items = self.ticked()
        self.total_var.set(total_text(items))
        idle = not self.running and self.found is not None
        self.delete_button.state(["!disabled"] if idle and items else ["disabled"])
        self.newest_button.state(["!disabled"] if idle and len(self.visible()) > 1
                                 else ["disabled"])
        self.safety_check.state(["!disabled"] if idle else ["disabled"])
        for c in self.checks:
            c.state(["!disabled"] if idle else ["disabled"])

    def toggle_safety(self):
        """Hidden safety copies are never ticked (only what is shown can be deleted)."""
        if not self.safety_var.get():
            for f in self.found or []:
                if f.safety and f.path in self.vars:
                    self.vars[f.path].set(False)
        self.render()

    def tick_but_newest(self):
        """Tick every shown backup except the newest of each list (backups; safety copies
        when shown). Untick the newest ones. Nothing is deleted until Delete... + yes."""
        if self.running:
            return
        rows, safety, _more = self.lists()
        for part in (rows, safety):
            keep = {f.path for f in part} - {f.path for f in backup_delete.but_newest(part)}
            for f in part:
                self._var(f).set(f.path not in keep)
        self.update_buttons()

    # ---- deleting ----
    def start(self):
        if self.running or self.closed:
            return False
        items = self.ticked()
        if not items:
            return False
        for_good = set()
        for f in items:
            try:
                where = self.app.trash.available(f.path)
            except Exception:
                log.exception("Trash check failed")
                where = None
            if not where:
                for_good.add(f.path)
        ok = self.app.confirm(confirm_title(len(items)),
                              confirm_text(items, for_good, self.clouds),
                              errors.DELETE_OK_GOOD if for_good else errors.DELETE_OK_BIN, errors.CANCEL)
        self.grab()
        if not ok or self.closed:
            return False
        self.running = True
        self.set_status(errors.DELETING)
        self.update_buttons()
        self._token += 1
        threading.Thread(target=_delete_worker, daemon=True,
                         args=(self.app.queue, self._token, items, list(self.places),
                               self.app.addons_dir, self.app.trash, frozenset(for_good))).start()
        return True

    def handle(self, msg):
        kind = msg[0]
        if msg[1] != self._token:
            return
        if kind == "backup_delete_found":
            self.found, self.places, self.clouds = msg[2], msg[3], msg[4]
            self.render()
        elif kind == "backup_delete_done":
            results = msg[2]
            done = {f.path for f, k in results if k in backup_delete.DONE}
            self.found = [f for f in self.found or [] if f.path not in done]
            for path in done:
                self.vars.pop(path, None)
            self.running = False
            self.render()
            self.set_status(result_text(results))


def open_delete(app, show=True):
    """Open the Delete old backups window (one backup window at a time; not while a
    backup, restore or install runs, or without a WoW folder). Returns it, or None."""
    if app.busy or not app.addons_dir or app.backup_dialog is not None:
        return None
    app.backup_dialog = DeleteBackupsDialog(app, show=show)
    return app.backup_dialog


def _list_worker(q, token, chosen, addons_dir):
    """("backup_delete_found", token, [FoundBackup], places, cloud place labels)."""
    places, found, clouds = [], [], set()
    try:
        try:
            cloud = backup.cloud_folders()
        except Exception as e:
            log.info("Cloud folder check failed (%s)", type(e).__name__)
            cloud = []
        clouds = {label for label, _f in cloud}
        places = backup_delete.safe_places(
            backup_ui.backup_places(chosen, cloud, backup.documents_dir()), addons_dir)
        found = backup_delete.find(places, addons_dir)
    except Exception as e:
        log.info("Backup search failed (%s)", type(e).__name__)
    q.put(("backup_delete_found", token, found, places, clouds))


def _delete_worker(q, token, items, places, addons_dir, trash_api, for_good):
    results = backup_delete.delete(items, places, addons_dir, trash_api, for_good)
    q.put(("backup_delete_done", token, results))
