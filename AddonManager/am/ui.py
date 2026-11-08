"""The window: folder line, one row per addon, Install/Update buttons.
Downloads run in one worker thread; it talks to the window through a queue."""

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from am import (APP_NAME, APP_VERSION, config, errors, gamecheck, github, installer, theme, toc,
                versions, wowpath)
from am import manifest as manifest_mod

log = logging.getLogger(__name__)

STATUS_TEXT = {
    versions.UP_TO_DATE: "Up to date",
    versions.UPDATE: "Update available",
    versions.NOT_INSTALLED: "Not installed",
    versions.UNKNOWN: "Installed (version unknown)",
    versions.NEWER: "Newer than the list",
    versions.NO_LATEST: "-",
}
BUTTON_TEXT = {
    versions.UPDATE: "Update",
    versions.NOT_INSTALLED: "Install",
    versions.UNKNOWN: "Reinstall",
}
ALL_STATUSES = (versions.UPDATE, versions.NOT_INSTALLED)


class Tooltip:
    """Small hover text for a widget."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background=theme.SURFACE, foreground=theme.TEXT,
                 relief="solid", borderwidth=1, wraplength=320, justify="left").pack(ipadx=4, ipady=2)

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Row:
    """One addon line in the list."""

    def __init__(self, folder, name, latest=None, addon=None, description=""):
        self.folder, self.name, self.latest = folder, name, latest
        self.addon, self.description = addon, description
        self.state = None   # None, "queued", "installing", "error"
        self.message = ""
        self.problem = ""   # short status when the latest release couldn't be read


class App:
    def __init__(self, root, manifest_location=None, allow_local=False, wow_override=None,
                 check_game=gamecheck.game_running, detect=wowpath.detect, ask_folder=None,
                 github_api=None):
        self.root = root
        self.allow_local = allow_local
        self.github_api = github.api_base(github_api, allow_local)
        self.manifest_location = config.manifest_url(config.load_settings(), manifest_location)
        self.check_game = check_game
        self.detect = detect
        self.ask_folder = ask_folder or self._ask_folder_dialog
        self.settings = config.load_settings()
        self.queue = queue.Queue()
        self.busy = False
        self.closed = False
        self.addons_dir = None
        self.rows = []
        self.manifest = None
        self.manifest_note = ""

        root.title(APP_NAME)
        root.geometry("680x420")
        root.minsize(560, 320)
        root.protocol("WM_DELETE_WINDOW", self.close)
        theme.apply(root)
        self._build()
        self._resolve_folder(wow_override)
        self._after = None
        root.bind("<Destroy>", self._on_destroy, add="+")
        self.check_again(force=False)
        self._after = root.after(100, self._poll)

    # ---------- layout ----------
    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(head, text=versions.display(APP_VERSION), style="Muted.TLabel").pack(side="right")

        folder = ttk.LabelFrame(outer, text="WoW Forever folder", padding=8)
        folder.pack(fill="x", pady=(8, 6))
        self.folder_var = tk.StringVar(value="(not chosen)")
        ttk.Label(folder, textvariable=self.folder_var, wraplength=480).grid(
            row=0, column=0, sticky="w")
        self.folder_button = ttk.Button(folder, text="Change...", command=self.choose_folder)
        self.folder_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        folder.columnconfigure(0, weight=1)

        self.banner_var = tk.StringVar()
        self.banner = tk.Label(outer, textvariable=self.banner_var, background=theme.BANNER_BG,
                               foreground=theme.BANNER_TEXT,
                               anchor="w", justify="left", wraplength=640, padx=6, pady=4)

        self.list_frame = ttk.Frame(outer)
        self.list_frame.pack(fill="both", expand=True, pady=(4, 6))

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", side="bottom")
        buttons = ttk.Frame(bottom)
        buttons.pack(fill="x")
        self.check_button = ttk.Button(buttons, text="Check again", command=self.check_again)
        self.check_button.pack(side="left")
        self.all_button = ttk.Button(buttons, text="Install/update all", command=self.update_all,
                                     style="Accent.TButton")
        self.all_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 2))
        self.message_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.message_var, wraplength=640,
                  justify="left").pack(fill="x")

    def render(self):
        """Rebuild the addon rows from self.rows and the installed .toc files."""
        for child in self.list_frame.winfo_children():
            child.destroy()
        f = self.list_frame
        headers = ("Addon", "Installed", "Latest", "Status", "")
        for col, text in enumerate(headers):
            ttk.Label(f, text=text, font=("Segoe UI", 9, "bold"), style="Muted.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 14), pady=(0, 4))
        f.columnconfigure(3, weight=1)
        self.row_widgets = {}
        any_action = False
        for i, row in enumerate(self.rows, start=1):
            exists, installed_v = self._installed(row.folder)
            status = versions.status(exists, installed_v, row.latest)
            name = ttk.Label(f, text=row.name)
            name.grid(row=i, column=0, sticky="w", padx=(0, 14), pady=2)
            Tooltip(name, row.description)
            ttk.Label(f, text=versions.display(installed_v) if exists else "-").grid(
                row=i, column=1, sticky="w", padx=(0, 14))
            ttk.Label(f, text=versions.display(row.latest) if row.latest else "?").grid(
                row=i, column=2, sticky="w", padx=(0, 14))
            status_text = STATUS_TEXT[status]
            if exists and not installed_v and status == versions.NO_LATEST:
                status_text = STATUS_TEXT[versions.UNKNOWN]
            if row.state == "queued":
                status_text = "Waiting..."
            elif row.state == "installing":
                status_text = "Installing..."
            elif row.state == "error":
                status_text = "✖ Failed (see below)"
            elif row.problem and not row.addon:
                status_text = row.problem
            if row.state == "error":
                status_label = ttk.Label(f, text=status_text, style="Failed.TLabel")
            else:
                status_label = ttk.Label(f, text=status_text, foreground=(
                    "" if row.state else theme.STATUS.get(status, "")))
            status_label.grid(row=i, column=3, sticky="w")
            button = None
            if row.addon and status in BUTTON_TEXT and self.addons_dir:
                button = ttk.Button(f, text=BUTTON_TEXT[status], width=10,
                                    command=lambda r=row: self.install_rows([r]))
                button.grid(row=i, column=4, sticky="e")
                if self.busy:
                    button.state(["disabled"])
                if status in ALL_STATUSES:
                    any_action = True
            self.row_widgets[row.folder] = (status_label, button, status)
        if not self.rows:
            ttk.Label(f, text=self.manifest_note or "No addons to show.").grid(
                row=1, column=0, columnspan=5, sticky="w")
        self._set_enabled(self.all_button, any_action and not self.busy)
        self._set_enabled(self.check_button, not self.busy)
        self._set_enabled(self.folder_button, not self.busy)

    @staticmethod
    def _set_enabled(widget, enabled):
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _installed(self, folder):
        if not self.addons_dir:
            return False, None
        return toc.installed(self.addons_dir, folder)

    def set_message(self, text):
        self.message_var.set(text)

    def set_banner(self, text):
        self.banner_var.set(text)
        if text:
            self.banner.pack(fill="x", before=self.list_frame, pady=(0, 4))
        else:
            self.banner.pack_forget()

    # ---------- WoW folder ----------
    def _set_addons_dir(self, addons_dir, save=True):
        self.addons_dir = addons_dir
        client = os.path.dirname(os.path.dirname(addons_dir))
        self.folder_var.set(client)
        self.folder_button.configure(text="Change...")
        if os.path.isdir(addons_dir):
            for action, name in installer.recover(addons_dir):
                log.info("Recovered %s %s", action, name)
        if save:
            self.settings = config.load_settings()
            self.settings["wow_folder"] = addons_dir
            config.save_settings(self.settings)

    def _no_folder(self, message):
        self.addons_dir = None
        self.folder_var.set("(not chosen)")
        self.folder_button.configure(text="Choose folder")
        self.set_message(message)

    def _resolve_folder(self, override=None):
        if override:
            status, addons_dir = wowpath.from_picked(override)
            if status == "ok":
                self._set_addons_dir(addons_dir, save=False)
                return
        saved = self.settings.get("wow_folder")
        if saved:
            if os.path.isdir(os.path.dirname(saved)) and os.path.basename(saved).lower() == "addons":
                self._set_addons_dir(saved, save=False)
                return
            log.info("Saved folder gone: %s", saved)
        found = self.detect()
        if found:
            self._set_addons_dir(found[0])
        else:
            self._no_folder(errors.FOLDER_GONE if saved else errors.NOT_FOUND)

    def _ask_folder_dialog(self):
        return filedialog.askdirectory(parent=self.root, mustexist=True,
                                       title="Choose your WoW Forever folder")

    def choose_folder(self):
        if self.busy:
            return
        picked = self.ask_folder()
        if not picked:
            return
        status, addons_dir = wowpath.from_picked(picked)
        if status == "ok":
            self._set_addons_dir(addons_dir)
            self.set_message("")
        elif status == "choose":
            self.set_message(errors.CHOOSE_CLIENT)
        else:
            self.set_message(errors.NOT_WOW)
        self.render()

    # ---------- addon list ----------
    def check_again(self, force=True):
        """Load the list and latest versions. force: ask GitHub even if the cache is fresh."""
        if self.busy:
            return
        self.busy = True
        self.set_message("Checking for updates...")
        self.render()
        # Workers get plain values, never `self`, so no Tk object is freed in a thread.
        threading.Thread(target=_load_worker, daemon=True,
                         args=(self.queue, self.check_game, self.manifest_location,
                               self.allow_local, self.github_api, force)).start()

    def _apply_manifest(self, m, error, problems=None):
        self.manifest = m
        if m is not None:
            problems = problems or {}
            self.rows, notes = [], []
            for a in m.addons:
                if a.folder in problems:
                    row = Row(a.folder, a.name, None, None, a.description)
                    row.problem, full = errors.release_message(problems[a.folder], a.name)
                    if full not in notes:
                        notes.append(full)
                else:
                    row = Row(a.folder, a.name, a.version, a, a.description)
                self.rows.append(row)
            self.settings = config.load_settings()
            self.settings["known_addons"] = [[a.folder, a.name] for a in m.addons]
            config.save_settings(self.settings)
            self.manifest_note = "The guild's addon list is empty."
            note = ""
            if m.app_version and versions.compare(m.app_version, APP_VERSION) == 1:
                note = (f"A newer Addon Manager ({versions.display(m.app_version)}) "
                        "is available.")
            if notes:
                note = "  ".join(notes + ([note] if note else []))
            if self.addons_dir:
                self.set_message(note)
        else:
            known = self.settings.get("known_addons") or []
            self.rows = [Row(f, n) for f, n in known
                         if isinstance(f, str) and manifest_mod.is_safe_folder(f)]
            self.manifest_note = error
            self.set_message(error)
        if not self.addons_dir:
            self.set_message(errors.NOT_FOUND)

    # ---------- installing ----------
    def update_all(self):
        rows = []
        for row in self.rows:
            if not row.addon:
                continue
            exists, v = self._installed(row.folder)
            if versions.status(exists, v, row.latest) in ALL_STATUSES:
                rows.append(row)
        if rows:
            self.install_rows(rows)

    def install_rows(self, rows):
        if self.busy or not self.addons_dir or not rows:
            return
        if not wowpath.ensure_addons_dir(self.addons_dir):
            self.set_message(errors.install_message(
                installer.InstallError("permission"), rows[0].name))
            return
        self.busy = True
        for row in rows:
            row.state, row.message = "queued", ""
        self.progress.configure(value=0)
        self.render()
        threading.Thread(target=_install_worker, daemon=True,
                         args=(self.queue, self.check_game, list(rows), self.addons_dir)).start()

    def _row(self, folder):
        return next((r for r in self.rows if r.folder == folder), None)

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self._after = self.root.after(100, self._poll)

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.closed = True
            if self._after:
                try:
                    self.root.after_cancel(self._after)
                except tk.TclError:
                    pass
                self._after = None

    def _handle(self, msg):
        kind = msg[0]
        if kind == "game":
            self.set_banner(errors.GAME_RUNNING if msg[1] else "")
        elif kind == "manifest":
            self._apply_manifest(msg[1], msg[2], msg[3] if len(msg) > 3 else None)
            self.busy = False
            self.render()
        elif kind == "start":
            row = self._row(msg[1])
            if row:
                row.state = "installing"
                self.set_message(f"Downloading {row.name}...")
                self.progress.configure(mode="determinate", value=0)
                self.render()
        elif kind == "progress":
            _, folder, done, total = msg
            if total:
                self.progress.configure(value=min(100, done * 100 // total))
            else:
                self.progress.step(2)
        elif kind == "done":
            row = self._row(msg[1])
            if row:
                row.state = None
                self.set_message(errors.success_message(row.name, versions.display(msg[2])))
            self.progress.configure(value=100)
            self.render()
        elif kind == "error":
            row = self._row(msg[1])
            if row:
                row.state, row.message = "error", msg[2]
            self.render()
        elif kind == "finished":
            self.busy = False
            failed = [r for r in self.rows if r.state == "error"]
            if failed:
                self.set_message("  ".join(r.message for r in failed))
            elif msg[1]:
                if len(msg[1]) == 1:
                    self.set_message(self.message_var.get())
                else:
                    self.set_message("Installed: " + ", ".join(msg[1])
                                     + ". Restart WoW or type /reload to use them.")
            self.render()

    def close(self):
        self.closed = True
        self.root.destroy()


def _game_running(check_game):
    try:
        return bool(check_game())
    except Exception:
        return False


def _load_worker(q, check_game, location, allow_local, github_api=None, force=True):
    """Thread: game check + addon list + latest GitHub releases. Results go to the window
    through q as ("manifest", Manifest or None, error text, {folder: ReleaseError})."""
    q.put(("game", _game_running(check_game)))
    try:
        m = github.load_list(location, allow_local=allow_local)
        m.addons, problems = github.resolve_all(
            m.addons, github_api, allow_local, cache_file=config.cache_path(),
            use_fresh_cache=not force)
        q.put(("manifest", m, None, problems))
    except Exception as e:
        log.warning("Addon list failed: %s", e)
        q.put(("manifest", None, errors.manifest_message(e), None))


def _install_worker(q, check_game, rows, addons_dir):
    """Thread: install rows one after another, reporting through q."""
    q.put(("game", _game_running(check_game)))
    done_msgs = []
    for row in rows:
        q.put(("start", row.folder))
        try:
            def progress(done, total, folder=row.folder):
                q.put(("progress", folder, done, total))
            version = installer.install(row.addon, addons_dir, progress=progress)
            done_msgs.append(f"{row.name} {versions.display(version)}")
            q.put(("done", row.folder, version))
        except Exception as e:
            log.exception("Install of %s failed", row.folder)
            q.put(("error", row.folder, errors.install_message(e, row.name)))
    q.put(("finished", done_msgs))


def run(manifest_location=None, allow_local=False, wow_override=None, hidden=False,
        github_api=None):
    root = tk.Tk()
    if hidden:
        root.withdraw()
    root.report_callback_exception = _report_callback_exception
    app = App(root, manifest_location, allow_local, wow_override, github_api=github_api)
    theme.dark_title_bar(root)
    root.mainloop()
    return app


def _report_callback_exception(exc, value, tb):
    log.error("Unexpected error", exc_info=(exc, value, tb))
