"""The window: folder line, one row per addon, Install/Update buttons.
Downloads run in one worker thread; it talks to the window through a queue."""

import logging
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

from am import (APP_NAME, APP_VERSION, config, errors, gamecheck, github, installer, selfupdate,
                theme, toc, versions, wowpath)
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
NOT_CHOSEN = "(not chosen)"


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
                 github_api=None, app_exe=None, updated_from=None, restart=selfupdate.restart,
                 open_link=None):
        """app_exe: the running .exe (self-update on); None = no self-update (from source,
        self-test). updated_from: set when this run was started by an update."""
        self.root = root
        self.app_exe = app_exe
        self.restart = restart
        self.open_link = open_link or webbrowser.open
        self.app_update = None      # selfupdate.Update offered in the bar
        self.update_mode = None     # None, "offer", "cant", "done"
        self.update_dismissed = False
        self.installing = False
        self.updating = False       # the app's own update is downloading/restarting
        self._was_visible = False
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
        self.folder_note = ""   # folder warning/notice kept in the message line
        self.clipboard = self._set_clipboard   # tests swap this; never the real clipboard

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
        if updated_from:
            # Started by an update: tell the old exe we're up, and don't offer updates now.
            selfupdate.mark_ok(APP_VERSION)
            self.update_mode = "done"
            self._render_update_bar()
        elif self.app_exe:
            threading.Thread(target=_update_check_worker, daemon=True,
                             args=(self.queue, self.github_api, self.allow_local,
                                   self.settings.get(selfupdate.SETTINGS_FAILED))).start()
        self._after = root.after(100, self._poll)

    # ---------- layout ----------
    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        folder = ttk.LabelFrame(outer, text="WoW Forever folder", padding=8)
        folder.pack(fill="x", pady=(0, 6))
        self.folder_var = tk.StringVar(value=NOT_CHOSEN)
        # Read-only but selectable: Ctrl+C and right-click Copy (copy only, no typing).
        self.folder_entry = ttk.Entry(folder, textvariable=self.folder_var, state="readonly",
                                      style="Muted.Path.TEntry", takefocus=0)
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        self.folder_menu = tk.Menu(self.root, tearoff=0, background=theme.SURFACE,
                                   foreground=theme.TEXT, activebackground=theme.ACCENT,
                                   activeforeground=theme.ACCENT_TEXT, borderwidth=1)
        self.folder_menu.add_command(label="Copy", command=self.copy_folder)
        self.folder_entry.bind("<Button-3>", self._folder_menu_popup)
        self.folder_entry.bind("<Control-c>", lambda _e: (self.copy_folder(), "break")[1])
        self.folder_entry.bind("<Control-C>", lambda _e: (self.copy_folder(), "break")[1])
        self.folder_button = ttk.Button(folder, text="Change...", command=self.choose_folder)
        self.folder_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        folder.columnconfigure(0, weight=1)

        self.banner_var = tk.StringVar()
        self.banner = tk.Label(outer, textvariable=self.banner_var, background=theme.BANNER_BG,
                               foreground=theme.BANNER_TEXT,
                               anchor="w", justify="left", wraplength=640, padx=6, pady=4)

        # The app's own update: one line, shown only when there is something to say.
        self.update_bar = tk.Frame(outer, background=theme.SURFACE, highlightthickness=1,
                                   highlightbackground=theme.ACCENT, padx=6, pady=4)
        self.update_text = tk.Label(self.update_bar, background=theme.SURFACE,
                                    foreground=theme.TEXT, anchor="w")
        self.update_text.pack(side="left")
        self.update_link = tk.Label(self.update_bar, text="What's new", cursor="hand2",
                                    background=theme.SURFACE, foreground=theme.BANNER_TEXT,
                                    font=("Segoe UI", 9, "underline"))
        self.update_link.pack(side="left", padx=(8, 0))
        self.update_link.bind("<Button-1>", lambda _e: self.whats_new())
        self.update_later_button = ttk.Button(self.update_bar, text="Later",
                                              command=self.update_later)
        self.update_later_button.pack(side="right")
        self.update_now_button = ttk.Button(self.update_bar, text="Update now",
                                            command=self.update_now, style="Accent.TButton")
        self.update_now_button.pack(side="right", padx=(0, 6))

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
        status_line = ttk.Frame(bottom)
        status_line.pack(fill="x")
        # The app version, dimmed, at the right end of the message line.
        self.version_label = ttk.Label(status_line, text=versions.display(APP_VERSION),
                                       style="Muted.TLabel")
        self.version_label.pack(side="right", anchor="se", padx=(8, 0))
        ttk.Label(status_line, textvariable=self.message_var, wraplength=580,
                  justify="left").pack(side="left", fill="x", expand=True)

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
        self._render_update_bar()

    # ---------- the app's own update ----------
    def _render_update_bar(self):
        """Show or hide the update bar. Hidden while addons install or after Later."""
        mode = self.update_mode
        show = bool(mode) and not self.update_dismissed and not self.installing
        if not show:
            self.update_bar.pack_forget()
            return
        if mode == "done":
            self.update_text.configure(text=errors.app_updated(versions.display(APP_VERSION)))
            self.update_now_button.pack_forget()
            self.update_later_button.configure(text="OK")
        else:
            if mode == "cant":
                self.update_text.configure(text=errors.APP_CANT_HERE_BAR)
                self.update_now_button.configure(text=errors.APP_DOWNLOAD_BUTTON)
                self._set_enabled(self.update_now_button, True)
            else:
                self.update_text.configure(text=errors.app_update_bar(
                    versions.display(self.app_update.version)))
                self.update_now_button.configure(text="Update now")
                self._set_enabled(self.update_now_button, not self.busy)
            if not self.update_now_button.winfo_manager():
                self.update_now_button.pack(side="right", padx=(0, 6),
                                            before=self.update_later_button)
            self.update_later_button.configure(text="Later")
        self._set_enabled(self.update_later_button, not self.updating)
        if not self.update_bar.winfo_manager():
            self.update_bar.pack(fill="x", before=self.list_frame, pady=(0, 4))

    def whats_new(self):
        if self.update_mode == "done":
            tag = "v" + versions.plain(APP_VERSION)
            url = f"https://github.com/{config.APP_REPO}/releases/tag/{tag}"
        elif self.app_update:
            url = self.app_update.page_url
        else:
            url = selfupdate.DOWNLOAD_PAGE
        self.open_link(url)

    def update_later(self):
        if self.updating:
            return
        self.update_dismissed = True
        self._render_update_bar()

    def update_now(self):
        if self.update_mode == "cant":
            self.open_link(selfupdate.DOWNLOAD_PAGE)
            return
        if self.update_mode != "offer" or self.busy or not self.app_update or not self.app_exe:
            return
        try:
            selfupdate.check_folder(self.app_exe, self.addons_dir)
        except selfupdate.UpdateError as e:
            log.info("App update not possible here: %s", e)
            self.update_mode = "cant"
            self.set_message(errors.app_update_message(e))
            self.render()
            return
        self.busy = True
        self.updating = True
        self.set_message(errors.APP_DOWNLOADING)
        self.progress.configure(value=0)
        self.render()
        threading.Thread(target=_app_update_worker, daemon=True,
                         args=(self.queue, self.app_update, self.app_exe,
                               self.allow_local)).start()

    def _app_swapped(self):
        """The new exe is in place: start it and wait for it to say it's up."""
        self.set_message(errors.APP_RESTARTING)
        selfupdate.clear_ok()
        try:
            proc = self.restart(self.app_exe, APP_VERSION)
        except Exception:
            log.exception("Could not start the new exe")
            selfupdate.rollback(self.app_exe)
            self._was_visible = False
            self._app_finished(False)
            return
        self._was_visible = self.root.state() != "withdrawn"
        self.root.withdraw()
        threading.Thread(target=_app_wait_worker, daemon=True,
                         args=(self.queue, self.app_update.version, self.app_exe, proc)).start()

    def _app_finished(self, ok):
        if ok:
            log.info("Updated to %s; closing the old version", self.app_update.version)
            self.close()
            return
        selfupdate.remember_failed(self.app_update.version)
        if self._was_visible:
            self.root.deiconify()
        self.busy = False
        self.updating = False
        self.set_message(errors.app_rolled_back(versions.display(self.app_update.version)))
        self.app_update, self.update_mode = None, None
        self.render()

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
    def _set_addons_dir(self, addons_dir, save=True, source="auto"):
        """source: 'auto' (found by the app, re-checked at start) or 'picked' (by hand)."""
        self.addons_dir = addons_dir
        client = os.path.dirname(os.path.dirname(addons_dir))
        self.folder_var.set(client)
        self.folder_entry.configure(style="Path.TEntry")
        self.folder_button.configure(text="Change...")
        if os.path.isdir(addons_dir):
            for action, name in installer.recover(addons_dir):
                log.info("Recovered %s %s", action, name)
        if save:
            self.settings = config.load_settings()
            self.settings["wow_folder"] = addons_dir
            self.settings["wow_folder_source"] = source
            config.save_settings(self.settings)

    def _no_folder(self, message):
        self.addons_dir = None
        self.folder_var.set(NOT_CHOSEN)
        self.folder_entry.configure(style="Muted.Path.TEntry")
        self.folder_button.configure(text="Choose folder")
        self.folder_note = message
        self.set_message(message)

    def _resolve_folder(self, override=None):
        """Override (dev) > saved folder > automatic search. A saved folder that the app
        found itself ('auto', or old settings without a source) is re-checked: if it is
        another WoW version (beta, PTR, Classic...), search again for WoW Forever."""
        self.folder_note = ""
        if override:
            status, addons_dir = wowpath.from_picked(override)
            if status in ("ok", "other"):
                self._set_addons_dir(addons_dir, save=False)
                return
        saved = self.settings.get("wow_folder")
        if not isinstance(saved, str) or not saved.strip() or "\0" in saved:
            saved = None   # damaged setting: search again
        wrong = None
        if saved:
            if os.path.isdir(os.path.dirname(saved)) and os.path.basename(saved).lower() == "addons":
                client = os.path.dirname(os.path.dirname(saved))
                if wowpath.client_kind(client) != wowpath.OTHER:
                    self._set_addons_dir(saved, save=False)
                    return
                if self.settings.get("wow_folder_source") == "picked":
                    self._set_addons_dir(saved, save=False)
                    self.folder_note = errors.not_forever(client)
                    self.set_message(self.folder_note)
                    return
                log.info("Saved folder is not a WoW Forever client, searching again: %s", saved)
                wrong = client
            else:
                log.info("Saved folder gone: %s", saved)
        found = self.detect()
        if found:
            self._set_addons_dir(found[0])
            if wrong:
                self.folder_note = errors.switched_folder(
                    wrong, os.path.dirname(os.path.dirname(found[0])))
                self.set_message(self.folder_note)
            return
        others = list(getattr(found, "others", []) or [])
        if wrong and os.path.normcase(wrong) not in {os.path.normcase(o) for o in others}:
            others.insert(0, wrong)
        if others:
            self._no_folder(errors.only_other_clients(others))
        else:
            self._no_folder(errors.FOLDER_GONE if saved else errors.NOT_FOUND)

    # ---------- copy the folder path ----------
    def _set_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def copy_folder(self):
        """Copy the selected part of the folder path, or all of it. Returns the text."""
        text = ""
        try:
            if self.folder_entry.selection_present():
                first = self.folder_entry.index("sel.first")
                last = self.folder_entry.index("sel.last")
                text = self.folder_var.get()[first:last]
        except tk.TclError:
            text = ""
        if not text:
            text = self.folder_var.get()
        if not text or text == NOT_CHOSEN:
            return ""
        self.clipboard(text)
        return text

    def _folder_menu_popup(self, event):
        state = "normal" if self.addons_dir else "disabled"
        self.folder_menu.entryconfigure(0, state=state)
        try:
            self.folder_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.folder_menu.grab_release()

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
            self._set_addons_dir(addons_dir, source="picked")
            self.folder_note = ""
            self.set_message("")
        elif status == "other":
            self._set_addons_dir(addons_dir, source="picked")
            self.folder_note = errors.not_forever(os.path.dirname(os.path.dirname(addons_dir)))
            self.set_message(self.folder_note)
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
            if (m.app_version and versions.compare(m.app_version, APP_VERSION) == 1
                    and not self.app_exe):   # the built app shows the update bar instead
                note = (f"A newer Addon Manager ({versions.display(m.app_version)}) "
                        "is available.")
            if notes:
                note = "  ".join(notes + ([note] if note else []))
            if self.addons_dir:
                self.set_message(self._with_folder_note(note))
        else:
            known = self.settings.get("known_addons") or []
            self.rows = [Row(k[0], k[1]) for k in known
                         if isinstance(k, (list, tuple)) and len(k) == 2
                         and isinstance(k[0], str) and isinstance(k[1], str)
                         and manifest_mod.is_safe_folder(k[0])]
            self.manifest_note = error
            self.set_message(self._with_folder_note(error))
        if not self.addons_dir:
            self.set_message(self.folder_note or errors.NOT_FOUND)

    def _with_folder_note(self, text):
        return "  ".join(t for t in (self.folder_note, text) if t)

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
        self.installing = True
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
        elif kind == "app_update":
            if msg[1] is not None and self.update_mode is None:
                self.app_update, self.update_mode = msg[1], "offer"
                self._render_update_bar()
        elif kind == "app_progress":
            _, done, total = msg
            if total:
                self.progress.configure(value=min(100, done * 100 // total))
        elif kind == "app_error":
            self.busy = False
            self.updating = False
            self.set_message(msg[1])
            self.render()
        elif kind == "app_swapped":
            self.progress.configure(value=100)
            self._app_swapped()
        elif kind == "app_started":
            self._app_finished(msg[1])
        elif kind == "finished":
            self.busy = False
            self.installing = False
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


def _update_check_worker(q, github_api, allow_local, failed):
    """Thread: is there a newer Addon Manager? ("app_update", Update or None)."""
    q.put(("app_update", selfupdate.check(APP_VERSION, github_api, allow_local,
                                          cache_file=config.cache_path(), failed=failed)))


def _app_update_worker(q, update, exe, allow_local):
    """Thread: download + check + swap the app's own exe."""
    try:
        selfupdate.fetch(update, exe, allow_local,
                         progress=lambda done, total: q.put(("app_progress", done, total)))
        selfupdate.swap(exe)
        q.put(("app_swapped",))
    except Exception as e:
        log.exception("App update failed")
        q.put(("app_error", errors.app_update_message(e)))


def _app_wait_worker(q, version, exe, proc):
    """Thread: wait for the new exe to say it's up; roll back if it doesn't."""
    ok = selfupdate.wait_ok(version, proc)
    if not ok:
        log.warning("New exe %s didn't confirm; rolling back", version)
        selfupdate.rollback(exe, proc)
    q.put(("app_started", ok))


def run(manifest_location=None, allow_local=False, wow_override=None, hidden=False,
        github_api=None, updated_from=None):
    root = tk.Tk()
    if hidden:
        root.withdraw()
    root.report_callback_exception = _report_callback_exception
    app = App(root, manifest_location, allow_local, wow_override, github_api=github_api,
              app_exe=selfupdate.exe_path(), updated_from=updated_from)
    theme.dark_title_bar(root)
    root.mainloop()
    return app


def _report_callback_exception(exc, value, tb):
    log.error("Unexpected error", exc_info=(exc, value, tb))
