"""The window: folder line, one row per addon, Install/Update buttons.
Downloads run in one worker thread; it talks to the window through a queue."""

import dataclasses
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import font as tkfont

from am import (APP_NAME, APP_VERSION, backup, backup_lock, backup_ui, config, errors,
                gamecheck, github,
                fileinstall, fileinstall_ui, icon, installer, instance, selfupdate, share_ui,
                stats, sysenv, theme, toc, touch, trash, versions, wowpath)
from am import manifest as manifest_mod

log = logging.getLogger(__name__)

STATUS_TEXT = {
    versions.UP_TO_DATE: "Up to date",
    versions.UPDATE: "Update available",
    versions.NOT_INSTALLED: "Not installed",
    versions.UNKNOWN: "Installed (version unknown)",
    versions.NEWER: "Newer than the guild's",
    versions.NO_LATEST: "-",
}
BUTTON_TEXT = {
    versions.UPDATE: "Update",
    versions.NOT_INSTALLED: "Install",
    versions.UNKNOWN: "Reinstall",
}
ALL_STATUSES = (versions.UPDATE, versions.NOT_INSTALLED)
NOT_CHOSEN = "(not chosen)"
CHOOSE_FOLDER = "Choose folder..."   # the folder button while no WoW folder is known
NO_CLIPBOARD_ENV = "ADDONMANAGER_NO_CLIPBOARD"   # set by the tests: never read the clipboard
VERSION_SAMPLE = "v10.10.10-beta.10"   # sets the Installed/Latest column widths
# Linux fonts (Noto Sans / DejaVu 10) are wider: a slightly shorter sample and less space
# between columns keep every row inside the 680 px window there.
# Windows (Segoe UI 9) looks exactly as before.
LINUX_VERSION_SAMPLE = "v1.10.0-beta.10"
LINUX_COL_GAP = 6
if os.name != "nt":
    VERSION_SAMPLE = LINUX_VERSION_SAMPLE
SHARE_SETTING = "share_stats"   # settings.json: False = no usage ping at all
REMOVED_SETTING = "removed_addons"   # settings.json: guild addons the player removed
UNINSTALL_BUTTON = "Uninstall"   # the row's button when the addon is installed and current
MIN_WIDTH = 580   # the window's smallest width (more if the rows need it)
MIN_HEIGHT = 360   # at 100 % (theme.px scales it)
START_WIDTH, START_HEIGHT = 680, 500   # first start at 100 % (scaled; taller if needed)
# The banner at the top of the main window (the user's words). If it doesn't fit on one
# line in the smallest window, BANNER_HEAD is the heading and BANNER_SUB a line under it.
BANNER_TITLE = "Gettic's Addon Manager - A Member of Dane Hird"
BANNER_HEAD, BANNER_SUB = "Gettic's Addon Manager", "A Member of Dane Hird"
ROW_GAP = 3   # px (100 %) above and below each list row's button
SCREEN_MARGIN = 80   # the window never grows taller than the screen minus this (taskbar)
WINDOW_SETTING = "window"   # 'WxH+X+Y' where the player left the window
# Linux only: the bottom row's buttons with less side padding (its fonts are about 1.2x
# wider), so the row keeps real room inside the smallest window. Windows unchanged.
LINUX_SLIM_BUTTONS = os.name != "nt"
SLIM_PADDING = (6, 3)   # px at 100 %: less side padding, same height as every button
COL_GAP = 10 if os.name == "nt" else LINUX_COL_GAP   # space after the list's text columns
# The gear needs a symbol font; Linux (Noto Sans) may not have one, so plain text there.
CLOSE_WAIT = 30         # s: X during a Receive/Send waits this long for it to stop, at most
CLOSE_POLL_MS = 200     # (the locking step of a received backup is never cut off)
COPY_MENU = "Copy"
OPEN_ADDONS_MENU = "Open AddOns folder"
OPEN_LOG_MENU = "Open log folder"
SETTINGS_BUTTON = "⚙ Settings" if os.name == "nt" else "Settings"
# The bottom row's four buttons share one width: the widest of their texts measured in
# the button font (theme.chars), so they never cut a text and line up.
BETA_TEXT = "Show beta versions"
# The main window's box: shorter on Linux, whose fonts are wider (keeps the bottom row
# inside the smallest window). The Settings window always says BETA_TEXT.
BETA_MAIN_TEXT = BETA_TEXT if os.name == "nt" else "Betas"
BETA_NOTE = ("Also offer test (beta) versions when newer than the normal one; Install all "
             "and Update all include them. To go back, switch this off and click the addon's "
             "\"Use v...\" button.")
SHARE_TEXT = "Share anonymous counts"
SHARE_NOTE = ("Once a day, sends which guild addons you have, their versions and whether "
              "they're up to date. No name, account or ID. Only the app's maker sees the totals.")


class Tooltip:
    """Small hover text for a widget. on_click(text): a click on the widget while it is
    greyed (or on a label) passes the text there too (no hover on a touch screen)."""

    def __init__(self, widget, text, on_click=None):
        self.widget, self.text, self.tip = widget, text, None
        self.on_click = on_click
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        if on_click is not None:
            widget.bind("<ButtonRelease-1>", self.clicked, add="+")

    def clicked(self, _event=None):
        """The tip text for a greyed button or a label; nothing for a button that works."""
        if not self.text or self.on_click is None:
            return
        greyed = True   # a label: its tip is the whole reason
        if isinstance(self.widget, (ttk.Button, ttk.Checkbutton)):
            try:
                greyed = self.widget.instate(["disabled"])
            except tk.TclError:
                return
        if greyed:
            self.on_click(self.text)

    def show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background=theme.SURFACE, foreground=theme.TEXT,
                 relief="flat", borderwidth=0, highlightthickness=1,
                 highlightbackground=theme.BORDER, wraplength=theme.px(320), justify="left",
                 font=theme.font()).pack(ipadx=theme.px(4), ipady=theme.px(2))

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _share_on(settings):
    """Share anonymous counts: on unless the player switched it off (missing = on)."""
    return settings.get(SHARE_SETTING) is not False


def _is_installed_beta(row, installed_v):
    """An installed beta: a pre-release version text (the .toc has no flag), or the
    version of the addon's newest GitHub pre-release (flagged, whatever its tag)."""
    if not installed_v:
        return False
    if versions.is_prerelease(installed_v):
        return True
    beta_v = row.addon.beta_version if row.addon else ""
    return bool(beta_v) and versions.same(installed_v, beta_v)


class Row:
    """One addon line in the list."""

    def __init__(self, folder, name, latest=None, addon=None, description=""):
        self.folder, self.name, self.latest = folder, name, latest
        self.addon, self.description = addon, description
        self.state = None   # None, "queued", "installing", "error"
        self.message = ""
        self.problem = ""   # short status when the latest release couldn't be read
        self.problem_kind = ""   # its ReleaseError kind (e.g. no_release)
        self.problem_full = ""   # the whole reason (the Status tooltip)


class App:
    def __init__(self, root, manifest_location=None, allow_local=False, wow_override=None,
                 check_game=gamecheck.game_running, detect=wowpath.detect, ask_folder=None,
                 github_api=None, app_exe=None, updated_from=None, restart=selfupdate.restart,
                 open_link=None, confirm=None, stats_url=None, trash_api=None,
                 share_url=None, open_path=None, instance_lock=None):
        """app_exe: the running .exe (self-update on); None = no self-update (from source,
        self-test). updated_from: set when this run was started by an update.
        confirm(title, text, ok_text, cancel_text) -> bool: the yes/no question (tests
        pass their own, so no dialog ever opens there). stats_url: where the daily anonymous
        count goes; None = config.STATS_URL for the built app only (never dev runs or
        self-tests), "" = never."""
        self.confirm = confirm or self._confirm_dialog
        # trash_api: available(path) -> "bin"/"trash"/None and send(path); tests pass a fake
        # so the real Recycle Bin is never used there.
        self.trash = trash_api or trash
        self._widths = {}   # measured text widths (see _text_width)
        self._rows_key, self._cells = None, []   # the list's widgets (see _build_rows)
        self.root = root
        self.app_exe = app_exe
        self.restart = restart
        self.open_link = open_link or sysenv.open_link
        self.app_update = None      # selfupdate.Update offered in the bar
        self.update_mode = None     # None, "offer", "cant", "done"
        self.update_dismissed = False
        self.installing = False
        self.updating = False       # the app's own update is downloading/restarting
        self._was_visible = False
        self.allow_local = allow_local
        if stats_url is None:
            stats_url = config.STATS_URL if (app_exe and not allow_local) else ""
        self.stats_url = stats_url
        # Send a backup with a code (am\share_ui.py): the relay; "" = buttons hidden.
        self.share_url = config.SHARE_URL if share_url is None else share_url
        self._stats_started = False   # at most one ping per run
        # The app's own update check must be done first (latest/outdated for the app).
        self._app_checked = not app_exe or bool(updated_from)
        self._app_newer = False
        self._app_checking = False   # an app update check is running (see _start_app_check)
        self.github_api = github.api_base(github_api, allow_local)
        self.manifest_location = config.manifest_url(config.load_settings(), manifest_location)
        self.check_game = check_game
        self.detect = detect
        self.ask_folder = ask_folder or self._ask_folder_dialog
        self.settings = config.load_settings()
        self.queue = queue.Queue()
        self.busy = False
        self.closed = False
        # X while a Receive/Send runs: its thread, its window and when it was stopped
        self._exit_worker = self._exit_dialog = None
        self._exit_since = 0.0
        self.addons_dir = None
        self.rows = []
        self.manifest = None
        self.manifest_note = ""
        self.folder_note = ""   # folder warning/notice kept in the message line
        self.clipboard = self._set_clipboard   # tests swap this; never the real clipboard
        # Read only when the Receive window opens (a code to pre-fill); tests swap it too.
        self.read_clipboard = self._get_clipboard
        # Backup and restore (Settings window): the open Make/Restore window, and the
        # file/folder pickers + "Open folder" (tests swap these; no dialog opens there).
        self.backup_dialog = None
        self.ask_backup_file = self._ask_backup_file_dialog
        self.ask_save_zip = self._ask_save_zip_dialog
        self.ask_backup_folder = self._ask_backup_folder_dialog
        self.open_folder = backup_ui.open_folder
        # Add from file (am\fileinstall_ui.py): button, drag and drop, "Other addons" rows.
        self.files = fileinstall_ui.FileAddons(self)
        # Files handed over by a second start (am\instance.py): only while this run holds
        # the lock (main takes it); tests pass their own lock or none.
        self.instance_lock = instance_lock
        self._polls = 0

        root.title(APP_NAME)
        theme.apply(root)   # first: sizes below follow the display scaling
        start = [theme.px(START_WIDTH), theme.px(START_HEIGHT)]
        root.geometry(f"{start[0]}x{start[1]}")
        root.minsize(MIN_WIDTH, theme.px(MIN_HEIGHT))
        self._min_width = MIN_WIDTH   # grows if the list's rows need more (see _fit_min_width)
        self._need_h = 0              # the height the content asked for last (see _fit_height)
        self._size = start            # used until the window is on screen
        self.remember_window = False  # run() turns it on (never in hidden/test runs)
        root.protocol("WM_DELETE_WINDOW", self.close)
        icon.apply(root)
        self._build()
        # F5 / Ctrl+R: Check again (ignored while busy).
        for key in ("<F5>", "<Control-r>", "<Control-R>"):
            root.bind(key, self._check_key)
        # Ctrl+O: Add file... (ignored while busy).
        for key in fileinstall_ui.ADD_KEYS:
            root.bind(key, self.files.add_key)
        self._resolve_folder(wow_override)
        self._after = None
        root.bind("<Destroy>", self._on_destroy, add="+")
        self._show_known_list()
        self.check_again(force=False)
        if open_path:   # a file dropped on the program's icon: opened once the list is in
            self.files.open_later(open_path)
        if updated_from:
            # Started by an update: tell the old exe we're up, and don't offer updates now.
            selfupdate.mark_ok(APP_VERSION)
            self.update_mode = "done"
            self._render_update_bar()
        else:
            self._start_app_check(force=False)
        self._after = root.after(100, self._poll)

    # ---------- layout ----------
    def _build(self):
        # Every box (folder, update bar, list) has the same inner padding, so Change...,
        # Add file..., Later and the row buttons end at one right edge.
        P = self.box_pad = theme.px(8)
        # Right inside every box: room for the list's thin scroll bar (px(4) + bar + px(4)),
        # so Change..., Add file..., Later and the row buttons share one right edge with or
        # without the bar.
        Q = self.box_right = 2 * theme.px(4) + theme.px(6)
        self.margin = theme.px(14)
        outer = self.outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        # The gold double line + knot corners (theme.Frame); `clear` keeps text and
        # buttons out of the knots' squares.
        self.frame_art = theme.Frame(self.root, outer, self.margin, corners=True)
        clear = self.corner_clear = max(0, self.frame_art.clear - self.margin)
        self._build_banner(outer, clear)

        folder = ttk.LabelFrame(outer, text="WoW Forever folder",
                                padding=(P, P, Q - 1, P))   # its border is 2 px
        folder.pack(fill="x", pady=(0, theme.px(6)))
        self.folder_var = tk.StringVar(value=NOT_CHOSEN)
        # Read-only but selectable: Ctrl+C and right-click Copy (copy only, no typing).
        self.folder_entry = ttk.Entry(folder, textvariable=self.folder_var, state="readonly",
                                      style="Muted.Path.TEntry", takefocus=0,
                                      font=theme.font())   # Linux: Tk's text font is smaller
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        # A long path shows its end (...\World of Warcraft\_forever_), the useful part.
        self.folder_var.trace_add("write", lambda *_a: self._folder_to_end())
        self.folder_entry.bind("<Configure>", lambda _e: self._folder_to_end(), add="+")
        self.folder_menu = tk.Menu(self.root, tearoff=0, **theme.menu_options())
        self.folder_menu.add_command(label=COPY_MENU, command=self.copy_folder)
        self.folder_menu.add_command(label=OPEN_ADDONS_MENU, command=self.open_addons_folder)
        self.folder_menu.add_command(label=OPEN_LOG_MENU, command=self.open_log_folder)
        touch.bind_menu(self.folder_entry, self._folder_menu_popup)
        # Right-click on an addon row: Remove <name>... (see _row_menu_popup).
        self.row_menu = tk.Menu(self.root, tearoff=0, **theme.menu_options())
        self.row_menu.add_command(label="Uninstall...")
        self.folder_entry.bind("<Control-c>", lambda _e: (self.copy_folder(), "break")[1])
        self.folder_entry.bind("<Control-C>", lambda _e: (self.copy_folder(), "break")[1])
        self.folder_button = ttk.Button(folder, text="Change...", command=self.choose_folder,
                                        width=self._row_chars())
        self.folder_button.grid(row=0, column=1, sticky="e", padx=(P, 0))
        folder.columnconfigure(0, weight=1)

        self.banner_var = tk.StringVar()
        self.banner = tk.Label(outer, textvariable=self.banner_var, background=theme.BANNER_BG,
                               foreground=theme.BANNER_TEXT, highlightthickness=1,
                               highlightbackground=theme.ACCENT, anchor="w", justify="left",
                               wraplength=theme.px(400), padx=P, pady=theme.px(4),
                               font=theme.font())
        # Wraps at the width the banner really gets (never cut at the right).
        backup_ui.fit_wrap(self.banner, inset=2 * (
            P + 1 + int(float(str(self.banner.cget("borderwidth"))))))

        # The app's own update: one line, shown only when there is something to say.
        self.update_bar = tk.Frame(outer, background=theme.UPDATE_BG, highlightthickness=1,
                                   highlightbackground=theme.ACCENT_BORDER, padx=P,
                                   pady=theme.px(4))
        self.update_text = tk.Label(self.update_bar, background=theme.UPDATE_BG,
                                    foreground=theme.TEXT, anchor="w", font=theme.font())
        self.update_text.pack(side="left")
        self.update_link = tk.Label(self.update_bar, text="What's new", cursor="hand2",
                                    background=theme.UPDATE_BG, foreground=theme.LINK,
                                    font=theme.font("underline"))
        self.update_link.pack(side="left", padx=(theme.px(8), 0))
        self.update_link.bind("<Button-1>", lambda _e: self.whats_new())
        # Later: as wide as the row buttons; Update now: its own measured width.
        self.update_later_button = ttk.Button(self.update_bar, text="Later",
                                              command=self.update_later,
                                              width=self._row_chars())
        self.update_later_button.pack(side="right", padx=(0, Q - P))
        self.update_now_button = ttk.Button(
            self.update_bar, text="Update now", command=self.update_now, style="Accent.TButton",
            width=self._row_chars())
        self.update_now_button.pack(side="right", padx=(0, theme.px(6)))

        # The list is packed after the bottom block (below), so a short window scrolls the
        # list, never cuts the buttons or the message line.
        self._build_list_box(outer)
        # "Add file...": in the list box's footer, always in view (not scrolled).
        self.files.build_button(self.list_footer, outer)
        self._wheel_all(self.list_footer)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", side="bottom")
        buttons = ttk.Frame(bottom)
        buttons.pack(fill="x")
        # Linux: slimmer bottom-row buttons (wider fonts); Windows keeps the normal padding.
        slim = ""
        if LINUX_SLIM_BUTTONS:
            style = ttk.Style(self.root)
            for name in ("Slim.TButton", "Slim.Accent.TButton"):
                style.configure(name, padding=tuple(theme.px(n) for n in SLIM_PADDING))
            slim = "Slim."
        width = theme.chars(self.root, ("Check again", errors.ALL_INSTALL, errors.ALL_UPDATE,
                                        errors.BACKUP_HEADING, SETTINGS_BUTTON), slim + "TButton")
        self.check_button = ttk.Button(buttons, text="Check again", command=self.check_again,
                                       style=slim + "TButton", width=width)
        self.check_button.pack(side="left")
        self.check_tip = Tooltip(self.check_button, errors.CHECK_TIP)
        # Install all / Update all (see render): one fixed width for both texts, so the
        # row doesn't move when the text changes.
        self.all_button = ttk.Button(buttons, text=errors.ALL_INSTALL, command=self.update_all,
                                     style=slim + "Accent.TButton")
        self.all_button.configure(width=width)
        self.all_button.pack(side="left", padx=(theme.px(8), 0))
        self.all_tip = Tooltip(self.all_button, "", on_click=self.set_message)
        # App settings live in a small Settings window (see open_settings).
        # Show beta versions: off by default. Share anonymous counts: on by default.
        self.beta_var = tk.BooleanVar(value=self.settings.get("show_betas") is True)
        self.share_var = tk.BooleanVar(value=_share_on(self.settings))
        self.settings_win = self.beta_check = self.share_check = None
        self.backup_win = None   # the Backup window (am\backup_ui.BackupWindow) while open
        self.settings_button = ttk.Button(buttons, text=SETTINGS_BUTTON, width=width,
                                          command=self.open_settings, style=slim + "TButton")
        self.settings_button.pack(side="right")
        # Backup: its own window (Make / Restore / Send / Receive), left of Settings.
        self.backup_main_button = ttk.Button(buttons, text=errors.BACKUP_HEADING,
                                             width=width, style=slim + "TButton",
                                             command=self.open_backup_window)
        self.backup_main_button.pack(side="right", padx=(0, theme.px(8)))
        self.backup_tip = Tooltip(self.backup_main_button, "", on_click=self.set_message)
        # Show beta versions also in the main window (easy to toggle), left of Backup.
        # Same variable and command as the box in Settings, so both always match.
        self.beta_main_check = ttk.Checkbutton(buttons, text=BETA_MAIN_TEXT, variable=self.beta_var,
                                               command=self.toggle_betas)
        self.beta_main_check.pack(side="right", padx=theme.px(8))
        Tooltip(self.beta_main_check, BETA_NOTE)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(theme.px(8), theme.px(2)))
        self.message_var = tk.StringVar()
        status_line = ttk.Frame(bottom)
        status_line.pack(fill="x")
        # The app version, dimmed, at the right end of the message line.
        self.version_label = ttk.Label(status_line, text=versions.display(APP_VERSION),
                                       style="Muted.TLabel")
        # Both ends indented by `clear`: the bottom knots never cover the text.
        self.version_label.pack(side="right", anchor="se", padx=(theme.px(8), clear))
        self.message_label = ttk.Label(status_line, textvariable=self.message_var,
                                       wraplength=theme.px(400), justify="left")
        self.message_label.pack(side="left", fill="x", expand=True, padx=(clear, 0))
        backup_ui.fit_wrap(self.message_label)   # wraps at its real width
        self.list_box.pack(fill="both", expand=True, pady=(theme.px(4), theme.px(8)))

    def _build_banner(self, outer, clear):
        """The slim banner: the guild's crest + the title (one line, or a heading and a
        smaller line under it when one line doesn't fit the smallest window)."""
        head = self.head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, theme.px(6)))
        size = 48 if theme.SCALE >= 1.4 else 32
        self._logo = None
        logo_w = 0
        try:
            self._logo = tk.PhotoImage(master=self.root, file=os.path.join(
                icon.ASSETS, f"AddonManager_{size}.png"))
            self.logo_label = ttk.Label(head, image=self._logo)
            self.logo_label.pack(side="left", padx=(clear + theme.px(4), 0))
            logo_w = size + clear + theme.px(4)
        except tk.TclError:
            log.info("Banner logo missing")
        text_box = ttk.Frame(head)
        text_box.pack(side="left", padx=(theme.px(10), 0))
        room = (theme.px(MIN_WIDTH) - 2 * self.margin - logo_w - theme.px(10)
                - self.frame_art.clear)   # the top-right knot too
        title_font = ttk.Style(self.root).lookup("Title.TLabel", "font")
        try:
            wide = tkfont.Font(root=self.root, font=title_font).measure(BANNER_TITLE)
        except tk.TclError:
            wide = 0
        self.title_label = ttk.Label(text_box, style="Title.TLabel",
                                     text=BANNER_TITLE if wide <= room else BANNER_HEAD)
        self.title_label.pack(anchor="w")
        self.subtitle_label = None
        if wide > room:
            self.subtitle_label = ttk.Label(text_box, text=BANNER_SUB, style="Subtitle.TLabel")
            self.subtitle_label.pack(anchor="w")
        tk.Frame(outer, height=1, background=theme.LINE).pack(fill="x", pady=(0, theme.px(8)))

    def _row_chars(self):
        """One width for Change..., Add file..., Later and the rows' Install/Update/Uninstall."""
        return theme.button_chars(self.root)

    # ---------- the list box: scrolls, fixed column headers ----------
    def _build_list_box(self, outer):
        """The list in a gold box that scrolls when its rows don't fit. The rows sit in a
        clipping frame (`list_view`) and move by whole rows (place y = -top), so they always
        stop on a row's edge under the fixed column headers (a copy over the list, lined up
        with the real header row). The thin scroll bar's column is always kept, so the row
        buttons never move."""
        P = self.box_pad
        box = self.list_box = tk.Frame(outer, background=theme.PANEL, highlightthickness=1,
                                       highlightbackground=theme.BORDER, borderwidth=0)
        view = self.list_view = tk.Frame(box, background=theme.PANEL, width=1, height=1)
        view.grid(row=0, column=0, sticky="nsew")
        bar = self.list_bar = ttk.Scrollbar(box, orient="vertical", command=self.list_yview,
                                            style="Gold.Vertical.TScrollbar")
        # The bar's column: px(4) + bar + px(4), always reserved (self.box_right).
        bar.grid(row=0, column=1, sticky="ns", padx=theme.px(4), pady=P)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(1, minsize=max(self.box_right,
                                           bar.winfo_reqwidth() + 2 * theme.px(4)))
        bar.grid_remove()
        # The footer (Add file..., see fileinstall_ui.build_button): under the scrolling
        # part, so it never scrolls away; its right edge = the row buttons'.
        self.list_footer = ttk.Frame(box, style="List.TFrame")
        self.list_footer.grid(row=1, column=0, sticky="ew", padx=(P, 0), pady=(0, P))
        self.list_frame = ttk.Frame(view, style="List.TFrame", padding=(P, 0, 0, P - 2))
        self.list_frame.place(x=0, y=0, relwidth=1, height=1)
        self._list_top = 0     # pixels scrolled (a whole number of rows)
        self.list_frame.bind("<Configure>", self._list_resized, add="+")
        view.bind("<Configure>", self._list_resized, add="+")
        self.list_head = tk.Frame(box, background=theme.PANEL)
        self.list_head.place(in_=view, x=0, y=0, relwidth=1, height=1)
        self.list_cover = tk.Frame(view, background=theme.PANEL)   # see _list_sizes
        self._head_labels = []
        self._row_lines, self._row_marks = [], []
        self._hl_rows, self._head_rows = [], []
        self._row_h = 0
        for w in (view, self.list_frame, self.list_head, self.list_cover, box):
            self._wheel(w)

    def _wheel(self, widget):
        """Mouse wheel over this widget scrolls the list a row at a time (Windows
        <MouseWheel>, Linux Button-4/5)."""
        if getattr(widget, "_am_wheel", False):
            return
        widget._am_wheel = True
        widget.bind("<MouseWheel>", self._wheel_scroll, add="+")
        widget.bind("<Button-4>", lambda _e: self.list_yview("scroll", -1, "units"), add="+")
        widget.bind("<Button-5>", lambda _e: self.list_yview("scroll", 1, "units"), add="+")

    def _wheel_scroll(self, event):
        self.list_yview("scroll", -1 if event.delta > 0 else 1, "units")

    def _wheel_all(self, widget):
        self._wheel(widget)
        for child in widget.winfo_children():
            self._wheel_all(child)

    def _list_sizes(self):
        """(list height, used height, row step, biggest top). When the list doesn't fit,
        the used height is the header plus a whole number of rows (the rest of the box is
        an empty strip at the bottom, `list_cover`), so no part of a row ever shows."""
        h = self.list_frame.winfo_reqheight()
        seen = self.list_view.winfo_height()
        step = max(1, self._row_h)
        if seen <= 1 or h <= seen:
            return h, seen, step, 0
        head = self._head_h()
        if seen - head >= step:
            seen = head + (seen - head) // step * step
        # Scrolled to the end, the last row sits at the bottom with less than a row of
        # space under it.
        return h, seen, step, -(-(h - (self.box_pad - 2) - seen) // step) * step

    def _head_h(self):
        try:
            return self.list_frame.grid_bbox(0, 0, 4, 0)[3]
        except (tk.TclError, IndexError, TypeError):
            return 0

    def list_yview(self, *args):
        """Scroll bar / wheel: ("moveto", fraction) or ("scroll", n, "units"|"pages")."""
        h, seen, step, most = self._list_sizes()
        top = self._list_top
        try:
            if args and args[0] == "moveto":
                top = float(args[1]) * max(h, most + seen)
            elif args and args[0] == "scroll":
                n = int(args[1])
                rows = max(1, seen // step) if len(args) > 2 and args[2] == "pages" else 1
                top += n * rows * step
        except (ValueError, IndexError):
            return
        self._set_top(top)

    def _set_top(self, top):
        h, seen, step, most = self._list_sizes()
        top = min(max(0, int(round(top / step)) * step), most)
        self._list_top = top
        try:
            self.list_frame.place_configure(y=-top, height=h)
            spare = self.list_view.winfo_height() - seen
            if most > 0 and spare > 0:
                self.list_cover.place(x=0, y=seen, relwidth=1, height=spare)
                self.list_cover.lift()
            else:
                self.list_cover.place_forget()
            total = max(h, top + seen, 1)
            first, last = top / total, min(1.0, (top + seen) / total) if seen > 1 else 1.0
            self.list_bar.set(first, last)
            if most <= 0:
                self.list_bar.grid_remove()
            else:
                self.list_bar.grid()
        except tk.TclError:
            pass

    def _list_resized(self, _event=None):
        """The list box asks for the whole list's size (so the window grows to fit before
        it scrolls); the rows are as wide as the box (Status stretches)."""
        f, view = self.list_frame, self.list_view
        try:
            w, h = f.winfo_reqwidth(), f.winfo_reqheight()
            if int(view.cget("width")) != w or int(view.cget("height")) != h:
                view.configure(width=w, height=h)
        except tk.TclError:
            return
        self._set_top(self._list_top)
        self._draw_rows()
        # Once more when Tk is idle: <Configure> can come before grid has placed the
        # columns (first paint: no Status header, short row lines). Grid places them in
        # an idle call queued with this one, so the redraw waits one idle round more.
        if not getattr(self, "_draw_due", None):
            try:
                self._draw_due = self.root.after_idle(self._draw_rows_idle, 1)
            except tk.TclError:
                pass

    def _draw_rows_idle(self, rounds=0):
        try:
            if rounds > 0:
                self._draw_due = self.root.after_idle(self._draw_rows_idle, rounds - 1)
                return
        except tk.TclError:
            return
        self._draw_due = None
        self._draw_rows()

    def _draw_rows(self):
        """Lines between rows (gold under headers), the band behind rows with an update and
        the fixed header copy, all from the grid's real row/column places. Placed frames:
        the grid's rows stay the addon rows the code and tests read."""
        f = self.list_frame
        try:
            if not f.winfo_exists():
                return
            cols, rows = f.grid_size()
        except tk.TclError:
            return
        self._row_lines = [w for w in self._row_lines if w.winfo_exists()]
        self._row_marks = [w for w in self._row_marks if w.winfo_exists()]
        full = max(f.winfo_width(), f.winfo_reqwidth())
        last = rows - 1
        while len(self._row_lines) < rows:
            line = tk.Frame(f, height=1, background=theme.ROW_LINE)
            self._wheel(line)
            self._row_lines.append(line)
        for i, line in enumerate(self._row_lines):
            x, y, w, h = f.grid_bbox(0, i, 4, i) if i < rows else (0, 0, 0, 0)
            if i == 0 or i >= last or h <= 0:
                line.place_forget()
                continue
            line.configure(background=theme.LINE if i in self._head_rows else theme.ROW_LINE)
            line.place(x=x, y=y + h - 1, width=w, height=1)
            line.lift()
        for mark in self._row_marks:
            mark.place_forget()
        for n, i in enumerate(self._hl_rows):
            if n >= len(self._row_marks):
                mark = tk.Frame(f, background=theme.HIGHLIGHT)
                self._wheel(mark)
                self._row_marks.append(mark)
            x, y, w, h = f.grid_bbox(0, i, 4, i)
            mark = self._row_marks[n]
            mark.place(x=0, y=y, width=full, height=h - 1)
            tk.Misc.lower(mark)
        # The fixed header: each copy at its column's x; as tall as the header row.
        try:
            _x, _y, _w, head_h = f.grid_bbox(0, 0, 4, 0)
            self.list_head.place_configure(height=max(1, head_h))
            self.list_head.lift()
            for col, label in enumerate(self._head_labels):
                src = f.grid_slaves(row=0, column=col)
                if src:
                    label.place(x=src[0].winfo_x(), y=src[0].winfo_y(),
                                height=src[0].winfo_height() or src[0].winfo_reqheight())
        except tk.TclError:
            pass

    def _text_width(self, text):
        """Pixel width of text in the labels' font (measured once per text)."""
        cache = self._widths
        if text not in cache:
            name = ttk.Style(self.root).lookup("TLabel", "font") or "TkDefaultFont"
            try:
                cache[text] = tkfont.Font(root=self.root, font=name).measure(text)
            except tk.TclError:
                cache[text] = tkfont.nametofont("TkDefaultFont").measure(text)
        return cache[text]

    def _name_width(self, text):
        """Pixel width of an addon name in the list's bold name font (cached)."""
        cache = self._widths
        key = ("name", text)
        if key not in cache:
            try:
                cache[key] = tkfont.Font(root=self.root, font=theme.font("bold")).measure(text)
            except tk.TclError:
                cache[key] = self._text_width(text)
        return cache[key]

    def _build_rows(self, key):
        """Create the list's widgets for self.rows (only when the addons change; render
        then only updates their text and state, so nothing flickers)."""
        for child in self.list_frame.winfo_children():
            child.destroy()
        f = self.list_frame
        for i in range(1, len(self._cells) + 1):   # the old rows' fixed heights
            f.rowconfigure(i, minsize=0)
        for label in self._head_labels:
            label.destroy()
        # Every row (and the "Other addons" heading) is one height, so scrolling a row at a
        # time always stops on a row's edge under the fixed header.
        probe = ttk.Button(f, text="X", style="Item.TButton")
        self._row_h = probe.winfo_reqheight() + 2 * theme.px(ROW_GAP)
        probe.destroy()
        headers = ("Addon", "Installed", "Latest", "Status", "")
        self._head_labels = []
        for col, text in enumerate(headers):
            ttk.Label(f, text=text, style="Head.TLabel", background=theme.PANEL).grid(
                row=0, column=col, sticky="nsw", padx=(0, self._col_gap(col)))
            copy = ttk.Label(self.list_head, text=text, style="Head.TLabel",
                             background=theme.PANEL)
            self._head_labels.append(copy)
        f.rowconfigure(0, minsize=self._row_h)
        self._head_rows = [0]
        f.columnconfigure(3, weight=1)
        # Fixed widths for Installed and Latest, so Status and the buttons don't jump
        # sideways when a version text gets longer (e.g. "v1.4.0-beta.1 (beta)").
        for col, sample in ((1, VERSION_SAMPLE), (2, VERSION_SAMPLE + " (beta)")):
            f.columnconfigure(col, minsize=self._text_width(sample) + COL_GAP)
        self._cells = []
        for i, _row in enumerate(self.rows, start=1):
            # Labels fill their cell (sticky nsew): the row's background reaches the edges.
            name = ttk.Label(f, foreground=theme.NAME, font=theme.font("bold"),
                             background=theme.PANEL)
            name.grid(row=i, column=0, sticky="nsew", padx=(0, self._col_gap(0)))
            installed = ttk.Label(f, background=theme.PANEL)
            installed.grid(row=i, column=1, sticky="nsew", padx=(0, self._col_gap(1)))
            latest = ttk.Label(f, background=theme.PANEL)
            latest.grid(row=i, column=2, sticky="nsew", padx=(0, self._col_gap(2)))
            status = ttk.Label(f, background=theme.PANEL)
            status.grid(row=i, column=3, sticky="nsew", padx=(0, self._col_gap(3)))
            # One button per row: Install / Update / Reinstall / Use vX / Uninstall (render).
            button = ttk.Button(f, width=self._row_chars(), style="Item.TButton")
            button.grid(row=i, column=4, sticky="e")
            button.grid_remove()
            # Right-click on the row (or its button): Remove <name>...
            for widget in (name, installed, latest, status):
                touch.bind_menu(widget, lambda e, n=i - 1: self._row_menu_popup(e, n))
            # Buttons: right-click only (a press-and-hold would also click them).
            button.bind("<Button-3>", lambda e, n=i - 1: self._row_menu_popup(e, n))
            # Every row one height (a button + a gap), so texts don't jump when a button
            # shows or hides and scrolling snaps to rows.
            f.rowconfigure(i, minsize=self._row_h)
            self._cells.append({"name": name, "name_tip": Tooltip(name, ""),
                                "installed": installed, "latest": latest, "status": status,
                                # a click shows the whole reason on the message line
                                "status_tip": Tooltip(status, "", on_click=self.set_message),
                                "button": button,
                                "button_tip": Tooltip(button, "")})
        if not self.rows:
            ttk.Label(f, text=self.manifest_note or "No addons to show.",
                      background=theme.PANEL).grid(row=1, column=0, columnspan=5, sticky="w")
            f.rowconfigure(1, minsize=self._row_h)
        # Addons installed from a file: their own section under the guild's list.
        start = max(len(self.rows), 1) + 1
        self.files.build_rows(f, start, COL_GAP)
        if self.files.rows:
            self._head_rows.append(start)
        self._wheel_all(f)
        self._rows_key = key

    def _col_gap(self, col):
        """Space after a list column: at least px(12) between Status and the buttons."""
        if col >= 4:
            return 0
        return max(COL_GAP, theme.px(12)) if col == 3 else COL_GAP

    def render(self):
        """Show self.rows and the installed .toc files. The widgets are made again only
        when the addons (or the empty-list note) change; otherwise updated in place."""
        key = (tuple((r.folder, r.name) for r in self.rows),
               None if self.rows else (self.manifest_note or ""), self.files.key())
        if getattr(self, "_rows_key", None) != key:
            self._build_rows(key)
        self.row_widgets = {}
        self.remove_widgets = {}
        removed = self._removed()
        any_action = any_update = False
        updates = missing = 0   # counted rows, for the summary after a check
        highlighted = []        # list rows with an update: a soft gold band
        for i, row in enumerate(self.rows, start=1):
            cells = self._cells[i - 1]
            exists, installed_v = self._installed(row.folder)
            status = versions.status(exists, installed_v, row.latest)
            cells["name"].configure(text=row.name)
            cells["name_tip"].text = row.description
            cells["installed"].configure(text=versions.display(installed_v) if exists else "-")
            latest_text = versions.display(row.latest) if row.latest else "-"
            if row.latest and row.addon and row.addon.beta:
                latest_text += " (beta)"
            cells["latest"].configure(text=latest_text)
            status_text = STATUS_TEXT[status]
            if exists and not installed_v and status == versions.NO_LATEST:
                status_text = STATUS_TEXT[versions.UNKNOWN]
            beta_on = bool(self.beta_var.get())
            installed_beta = bool(exists and _is_installed_beta(row, installed_v))
            # Box off and a beta installed that is newer than the normal version: offer going back.
            go_back = (installed_beta and not beta_on and status == versions.NEWER
                       and row.addon is not None and not row.addon.beta)
            no_normal = (installed_beta and not beta_on and row.addon is None
                         and row.problem_kind == "no_release")
            if go_back or no_normal:
                status_text = errors.BETA_INSTALLED
            was_removed = not exists and row.folder in removed
            if was_removed:
                status_text = errors.REMOVED
            if row.state == "queued":
                status_text = "Waiting..."
            elif row.state == "installing":
                status_text = "Installing..."
            elif row.state == "removing":
                status_text = errors.REMOVING
            elif row.state == "error":
                status_text = "✖ Failed (see below)"
            elif row.problem and not row.addon and not no_normal:
                status_text = row.problem
            status_label = cells["status"]
            band = (status == versions.UPDATE and not row.state and not was_removed
                    and bool(self.addons_dir))
            back = theme.HIGHLIGHT if band else theme.PANEL
            if band:
                highlighted.append(i)
            for key in ("name", "installed", "latest"):
                cells[key].configure(background=back)
            if row.state == "error":
                status_label.configure(text=status_text, style="Failed.TLabel", foreground="",
                                       background=theme.ERROR_BG)
            elif was_removed and not row.state:
                status_label.configure(text=status_text, style="Muted.TLabel", foreground="",
                                       background=back)
            else:
                status_label.configure(text=status_text, style="TLabel", background=back,
                                       foreground=("" if row.state else
                                                   theme.STATUS.get(status, "")))
            if no_normal and not row.state:
                cells["status_tip"].text = errors.NO_NORMAL_YET
            elif row.problem and not row.addon and not row.state:
                cells["status_tip"].text = row.problem_full   # e.g. "Can't check now": why
            else:
                cells["status_tip"].text = ""
            # One button: the action the row needs; Uninstall when installed and nothing
            # else is needed. Right-click Remove works on every installed row.
            button = cells["button"]
            uninstall = None
            if go_back and self.addons_dir:
                text = errors.go_back_button(versions.display(row.latest))
                button.configure(text=text, style="Item.TButton", width=max(
                    self._row_chars(), theme.chars(self.root, (text,), "Item.TButton")),
                                 command=lambda r=row: self.go_back(r))
            elif row.addon and status in BUTTON_TEXT and self.addons_dir:
                button.configure(text=BUTTON_TEXT[status], width=self._row_chars(),
                                 style=("Accent.Item.TButton" if status in ALL_STATUSES
                                        else "Item.TButton"),
                                 command=lambda r=row: self.install_rows([r]))
                if status in ALL_STATUSES and not was_removed:
                    any_action = True
                    any_update = any_update or status == versions.UPDATE
                    updates += status == versions.UPDATE
                    missing += status == versions.NOT_INSTALLED
            elif exists and self.addons_dir:
                button.configure(text=UNINSTALL_BUTTON, width=self._row_chars(),
                                 style="Uninstall.Item.TButton",
                                 command=lambda r=row: self.remove_row(r))
                uninstall = button
            else:
                button = None
            if button is None:
                cells["button"].grid_remove()
                cells["button_tip"].text = ""
            else:
                self._set_enabled(button, not self.busy)
                button.grid()
                cells["button_tip"].text = (errors.REMOVE_TIP if uninstall else
                                            errors.REMOVE_RIGHT_CLICK if exists else "")
            self.row_widgets[row.folder] = (status_label, button, status)
            self.remove_widgets[row.folder] = uninstall
        # Update all when an installed addon has an update, else Install all. Kept as it
        # is while busy (checking or installing), so it doesn't flip during a run.
        if not self.busy:
            self.all_button.configure(text=errors.ALL_UPDATE if any_update else errors.ALL_INSTALL)
        self._counts = (updates, missing)
        self._hl_rows = highlighted
        self._set_enabled(self.all_button, any_action and not self.busy)
        self._set_enabled(self.check_button, not self.busy)
        self._set_enabled(self.folder_button, not self.busy)
        self._set_enabled(self.beta_main_check, not self.busy)
        self._set_enabled(self.backup_main_button, not self.busy and bool(self.addons_dir))
        # Greyed-out buttons say why (empty while they can be clicked).
        self.all_tip.text = (errors.BUSY_TIP if self.busy else
                             "" if any_action else
                             errors.NO_FOLDER_TIP if not self.addons_dir else
                             errors.NOTHING_TO_DO_TIP)
        self.backup_tip.text = (errors.BUSY_TIP if self.busy else
                                errors.NO_FOLDER_TIP if not self.addons_dir else "")
        if self.beta_check is not None:   # only while the Settings window is open
            self._set_enabled(self.beta_check, not self.busy)
        if self.backup_win is not None:
            self.backup_win.render()
        self.files.render()
        self._render_update_bar()
        self._fit_min_width()
        self._list_resized()
        # Once more when Tk is idle: a longer text (e.g. "(beta)") changes the list's
        # requested width without any event the canvas would see.
        if not getattr(self, "_resize_due", False):
            self._resize_due = self.root.after_idle(self._list_resized_idle)

    def _list_resized_idle(self):
        self._resize_due = False
        self._list_resized()

    def _fit_min_width(self):
        """Never let the window get narrower than the list's rows (wider Linux fonts), so
        the row buttons can't be cut off. At least MIN_WIDTH."""
        self.list_frame.update_idletasks()
        # The bottom row's buttons too: at 150 % display scaling and more they need more
        # than the list (Check again ... Backup, Settings would be cut off). Add file... is
        # in the list box (its footer).
        m = 2 * self.margin
        need = max(MIN_WIDTH, self.list_box.winfo_reqwidth() + m,
                   self.settings_button.master.winfo_reqwidth() + m)
        if need != self._min_width:
            self._min_width = need
            self.root.minsize(need, theme.px(MIN_HEIGHT))
        self._fit_height()

    def _fit_height(self):
        """When the content needs more height than before (more rows, a banner, the update
        bar), make the window tall enough once, up to the screen. Never shrinks it, and a
        player who makes it smaller afterwards keeps that until the content grows again."""
        need = self.root.winfo_reqheight()
        if need == self._need_h:
            return
        self._need_h = need
        try:
            if self.root.state() == "zoomed" or self.root.attributes("-fullscreen"):
                return
        except tk.TclError:
            pass
        mapped = self.root.winfo_ismapped()
        width = self.root.winfo_width() if mapped else self._size[0]
        height = self.root.winfo_height() if mapped else self._size[1]
        target = min(need, self.root.winfo_screenheight() - SCREEN_MARGIN)
        if target > height:
            self._size = [width, target]
            self.root.geometry(f"{width}x{target}")

    # ---------- remembering the window's place ----------
    def restore_geometry(self):
        """Put the window where the player left it, if that place is still on a screen."""
        self.remember_window = True
        saved = config.load_settings().get(WINDOW_SETTING)
        size = _parse_geometry(saved)
        if size is None:
            return
        w, h, x, y = size
        if w < self._min_width or h < theme.px(MIN_HEIGHT):
            return
        # Never bigger than the screen (a smaller screen than last time).
        w = max(self._min_width, min(w, self.root.winfo_screenwidth()))
        h = max(theme.px(MIN_HEIGHT), min(h, self.root.winfo_screenheight() - SCREEN_MARGIN))
        if not _on_screen(self.root, x, y, w):
            log.info("Saved window place is off-screen; using the default")
            return
        self._size = [w, h]
        self.root.geometry(f"{self._size[0]}x{self._size[1]}+{x}+{y}")

    def _save_geometry(self):
        if not self.remember_window:
            return
        try:
            if self.root.state() != "normal" or not self.root.winfo_ismapped():
                return
            text = (f"{self.root.winfo_width()}x{self.root.winfo_height()}"
                    f"+{self.root.winfo_x()}+{self.root.winfo_y()}")
        except tk.TclError:
            return
        settings = config.load_settings()
        if settings.get(WINDOW_SETTING) != text:
            settings[WINDOW_SETTING] = text
            config.save_settings(settings)

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
                self.update_now_button.configure(text=errors.APP_DOWNLOAD_BUTTON, width=max(
                    self._row_chars(), theme.chars(self.root, (errors.APP_DOWNLOAD_BUTTON,))))
                self._set_enabled(self.update_now_button, True)
            else:
                self.update_text.configure(text=errors.app_update_bar(
                    versions.display(self.app_update.version)))
                self.update_now_button.configure(text="Update now", width=max(
                    self._row_chars(), theme.chars(self.root, ("Update now",))))
                self._set_enabled(self.update_now_button, not self.busy)
            if not self.update_now_button.winfo_manager():
                self.update_now_button.pack(side="right", padx=(0, theme.px(6)),
                                            before=self.update_later_button)
            self.update_later_button.configure(text="Later")
        self._set_enabled(self.update_later_button, not self.updating)
        if not self.update_bar.winfo_manager():
            self.update_bar.pack(fill="x", before=self.list_box, pady=(0, theme.px(4)))

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

    def _folder_to_end(self):
        try:
            self.folder_entry.xview_moveto(1.0)
        except tk.TclError:
            pass   # the window is closing

    def set_banner(self, text):
        self.banner_var.set(text)
        if text:
            self.banner.pack(fill="x", before=self.list_box, pady=(0, theme.px(4)))
        else:
            self.banner.pack_forget()

    # ---------- WoW folder ----------
    def _set_addons_dir(self, addons_dir, save=True, source="auto"):
        """source: 'auto' (found by the app, re-checked at start) or 'picked' (by hand)."""
        self.addons_dir = addons_dir
        client = os.path.dirname(os.path.dirname(addons_dir))
        self.folder_var.set(client)
        self.folder_entry.configure(style="Path.TEntry")
        self.folder_button.configure(text="Change...", width=self._row_chars())
        if os.path.isdir(addons_dir):
            # A restore that was cut off is undone first (it owns its backup folders).
            if ("undone", "restore") in backup.recover_restore(addons_dir):
                self.folder_note = errors.RESTORE_UNDONE   # kept after "Checking..."
                self.set_message(errors.RESTORE_UNDONE)
            for action, name in installer.recover(addons_dir):
                log.info("Recovered %s %s", action, name)
        if save:
            self.settings = config.load_settings()
            self.settings["wow_folder"] = addons_dir
            self.settings["wow_folder_source"] = source
            config.save_settings(self.settings)
        self.files.refresh()   # the "Other addons" of this folder

    def _no_folder(self, message):
        self.addons_dir = None
        self.folder_var.set(NOT_CHOSEN)
        self.folder_entry.configure(style="Muted.Path.TEntry")
        # Wider than the row buttons while it says this (nothing to line up with then).
        self.folder_button.configure(text=CHOOSE_FOLDER, width=max(
            self._row_chars(), theme.chars(self.root, (CHOOSE_FOLDER,))))
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

    def _get_clipboard(self):
        """The clipboard's text, or "" (nothing, not text, or a test run). Never logged."""
        if os.environ.get(NO_CLIPBOARD_ENV):
            return ""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return ""
        return text if isinstance(text, str) else ""

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

    def open_addons_folder(self):
        if self.addons_dir and os.path.isdir(self.addons_dir):
            self.open_folder(self.addons_dir)

    def open_log_folder(self):
        """The folder with log.txt (handy when asking for help)."""
        folder = os.path.dirname(config.log_path())
        if os.path.isdir(folder):
            self.open_folder(folder)

    def _folder_menu_popup(self, event):
        state = "normal" if self.addons_dir else "disabled"
        self.folder_menu.entryconfigure(0, state=state)
        self.folder_menu.entryconfigure(1, state=state)
        try:
            self.folder_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.folder_menu.grab_release()

    def ask(self, title, text, ok_text, cancel_text, enter=False):
        """self.confirm, with Enter = OK in the app's own dialog when enter is True (only
        for questions that don't remove, delete or overwrite anything). Test fakes get
        the four plain arguments."""
        if self.confirm == self._confirm_dialog:
            return self._confirm_dialog(title, text, ok_text, cancel_text, enter=enter)
        return self.confirm(title, text, ok_text, cancel_text)

    def _confirm_dialog(self, title, text, ok_text, cancel_text, enter=False):
        """A small dark modal question over the window. True if ok_text was clicked.
        enter: Enter also answers OK (never for Remove/Delete/Restore questions)."""
        result = {"ok": False}

        def answer(ok):
            result["ok"] = ok
            parts["win"].destroy()
        parts = self._confirm_parts(title, text, ok_text, cancel_text, enter, answer)
        win = parts["win"]
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_reqheight()) // 3)
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        theme.dark_title_bar(win)
        parts["focus"].focus_set()
        win.grab_set()
        self.root.wait_window(win)
        return result["ok"]

    def _confirm_parts(self, title, text, ok_text, cancel_text, enter, answer):
        """The question window, built withdrawn (shown by _confirm_dialog). Focus: OK only
        for safe questions (enter); otherwise Cancel, so a stray Space or Enter never
        removes or deletes anything (OK only after tabbing to it or a click)."""
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(title)
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(self.root)
        frame = ttk.Frame(win, padding=theme.px(14))
        frame.pack(fill="both", expand=True)
        theme.frame_window(win, frame)
        theme.heading(frame, title, bigger=2).pack(fill="x")
        ttk.Label(frame, text=text, wraplength=theme.px(360), justify="left").pack(
            anchor="w", pady=(theme.px(8), theme.px(12)))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        cancel_button = ttk.Button(buttons, text=cancel_text, command=lambda: answer(False))
        cancel_button.pack(side="right")
        ok_button = ttk.Button(buttons, text=ok_text, style="Accent.TButton",
                               command=lambda: answer(True))
        ok_button.pack(side="right", padx=(0, theme.px(6)))
        theme.even_buttons(buttons)   # OK and Cancel one width, >= a row button
        win.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        _confirm_keys(win, answer, enter)
        return {"win": win, "ok": ok_button, "cancel": cancel_button,
                "focus": ok_button if enter else cancel_button}

    def go_back(self, row):
        """Installed beta -> the newest normal version of that addon, after a yes."""
        if self.busy or not self.addons_dir or not row.addon or row.addon.beta:
            return
        exists, installed_v = self._installed(row.folder)
        if not (exists and _is_installed_beta(row, installed_v)):
            return
        if not self.ask(errors.GO_BACK_TITLE,
                        errors.go_back_text(row.name, versions.display(installed_v),
                                            versions.display(row.latest)),
                        errors.GO_BACK_OK, errors.GO_BACK_CANCEL, enter=True):
            return
        log.info("Going back from %s %s to %s", row.folder, installed_v, row.latest)
        self.install_rows([row])

    # ---------- removing an addon ----------
    def _removed(self):
        """Folders of guild addons the player removed (Install all / Update all skip them)."""
        value = config.load_settings().get(REMOVED_SETTING) or []
        return {f for f in value if isinstance(f, str) and manifest_mod.is_safe_folder(f)}

    def _set_removed(self, folders):
        self.settings = config.load_settings()
        if set(self.settings.get(REMOVED_SETTING) or []) == set(folders):
            return
        if folders:
            self.settings[REMOVED_SETTING] = sorted(folders)
        else:
            self.settings.pop(REMOVED_SETTING, None)
        config.save_settings(self.settings)

    def _guild_folders(self):
        if self.manifest is not None:
            return {a.folder for a in self.manifest.addons}
        return {r.folder for r in self.rows}

    def _row_menu_popup(self, event, index):
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        can = bool(self.addons_dir and not self.busy and self._installed(row.folder)[0])
        self.row_menu.entryconfigure(0, label=errors.remove_menu_label(row.name),
                                     state="normal" if can else "disabled",
                                     command=lambda r=row: self.remove_row(r))
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def remove_row(self, row):
        """Ask, then move the addon's folder to the Recycle Bin / Trash (or delete it for
        good on a drive without one, after saying so). Only guild addons; never WTF."""
        if self.busy or not self.addons_dir:
            return False
        if not self._installed(row.folder)[0]:
            # Already gone (deleted by hand since the last check): say so and show it as
            # Removed, so the row matches what's on disk.
            self._set_removed(self._removed() | {row.folder})
            self.set_message(errors.remove_message(installer.InstallError("missing"), row.name))
            self.render()
            return False
        try:
            where = self.trash.available(self.addons_dir)
        except Exception:
            log.exception("Trash check failed")
            where = None
        others = [r.folder for r in self.rows
                  if r.folder != row.folder and self._installed(r.folder)[0]]
        needs = installer.needed_by(self.addons_dir, row.folder, others)
        needs = [next((r.name for r in self.rows if r.folder == f), f) for f in needs]
        if not self.confirm(errors.remove_title(row.name),
                            errors.remove_text(row.name, where, needs),
                            errors.REMOVE_OK if where else errors.DELETE_OK,
                            errors.REMOVE_CANCEL):
            return False
        log.info("Removing %s (%s)", row.folder, where or "delete for good")
        self.busy = True
        row.state = "removing"
        self.render()
        threading.Thread(target=_remove_worker, daemon=True,
                         args=(self.queue, row.folder, row.name, self.addons_dir,
                               sorted(self._guild_folders()),
                               self.trash.send if where else None, where)).start()
        return True

    def _ask_folder_dialog(self):
        extra = {}
        if os.name != "nt":
            # Linux: the game sits in a hidden folder (~/.local/share/Steam/...), so let
            # Tk's folder dialog show hidden folders and start where Steam keeps them.
            _show_hidden_folders(self.root)
            start = _linux_start_folder()
            if start:
                extra["initialdir"] = start
        return filedialog.askdirectory(parent=self.root, mustexist=True,
                                       title="Choose your WoW Forever folder", **extra)

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
    def _check_key(self, _event=None):
        """F5 / Ctrl+R."""
        self.check_again()

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
                               self.allow_local, self.github_api, force,
                               bool(self.beta_var.get()))).start()
        if force:   # Check again / F5 / Ctrl+R: the app's own update too
            # Bring back an offer hidden with Later (not the "updated" note's OK).
            if self.update_dismissed and self.update_mode in ("offer", "cant"):
                self.update_dismissed = False
                self._render_update_bar()
            self._start_app_check(force=True)

    def _start_app_check(self, force):
        """The app's own update check in a thread. Skipped for dev runs (no exe), right
        after an update, while a "can't here" bar is up, while the app's update installs,
        or while a check is running. With an offer up only a forced check (Check again)
        runs: a newer version then replaces the offer (see _poll "app_update")."""
        if not self.app_exe or self._app_checking or self.updating:
            return
        if self.update_mode is not None and not (force and self.update_mode == "offer"):
            return
        self._app_checking = True
        # The failed version is read from disk: remember_failed writes it there.
        threading.Thread(target=_update_check_worker, daemon=True,
                         args=(self.queue, self.github_api, self.allow_local,
                               config.load_settings().get(selfupdate.SETTINGS_FAILED),
                               force)).start()

    def toggle_betas(self):
        """Show beta versions switched: save it and redo the list (cached releases)."""
        if self.busy:   # the box is disabled while busy; put it back if clicked anyway
            self.beta_var.set(self.settings.get("show_betas") is True)
            return
        self.settings = config.load_settings()
        self.settings["show_betas"] = bool(self.beta_var.get())
        config.save_settings(self.settings)
        if self.manifest is None:   # no list yet: a normal check
            self.check_again(force=False)
            return
        # Only re-sort the known addons from the cached releases: no game check, no list
        # download, no "Checking..." and nothing greyed out; one redraw when it's done.
        self.busy = True
        threading.Thread(target=_resort_worker, daemon=True,
                         args=(self.queue, self.manifest, self.allow_local, self.github_api,
                               bool(self.beta_var.get()))).start()

    def _show_known_list(self):
        """At start: the last known list straight away, from settings (known_addons) and
        cache.json (latest versions) plus the installed .toc versions, shown while
        "Checking for updates..." runs (buttons locked: busy) until the fresh list
        replaces it. Reads only; nothing is saved."""
        try:
            addons = github.cached_preview(
                self.settings.get("known_addons") or [], self.github_api, self.allow_local,
                config.cache_path(), bool(self.beta_var.get()))
        except Exception:   # never stop the window from opening
            log.exception("Last known list failed")
            addons = []
        self.rows = [Row(a.folder, a.name, a.version or None, a if a.version else None,
                         a.description) for a in addons]

    def toggle_share(self):
        """Share anonymous counts switched: save it. On again: send today's count now if it
        wasn't sent yet (off: nothing is sent from now on)."""
        self.settings = config.load_settings()
        self.settings[SHARE_SETTING] = bool(self.share_var.get())
        config.save_settings(self.settings)
        if self.share_var.get():
            self._maybe_send_stats()

    # ---------- settings window ----------
    def open_settings(self, show=True):
        """The small Settings window with both switches; changes apply at once.
        show=False (tests): built but never shown, so nothing pops up."""
        if self.settings_win is not None:
            if show:
                self.settings_win.deiconify()
                self.settings_win.lift()
            return self.settings_win
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("Settings")
        win.configure(background=theme.BG)
        win.resizable(False, False)
        win.transient(self.root)
        frame = ttk.Frame(win, padding=theme.px(14))
        frame.pack(fill="both", expand=True)
        theme.frame_window(win, frame)
        theme.heading(frame, "Settings").pack(fill="x")
        self.beta_var.set(self.settings.get("show_betas") is True)
        self.share_var.set(_share_on(config.load_settings()))
        checks = []
        for text, var, command, note in (
                (BETA_TEXT, self.beta_var, self.toggle_betas, BETA_NOTE),
                (SHARE_TEXT, self.share_var, self.toggle_share, SHARE_NOTE)):
            check = ttk.Checkbutton(frame, text=text, variable=var, command=command)
            check.pack(anchor="w", pady=(theme.px(10), 0))
            ttk.Label(frame, text=note, style="Muted.TLabel", wraplength=theme.px(320),
                      justify="left").pack(anchor="w", padx=(theme.px(24), 0))
            checks.append(check)
        self.beta_check, self.share_check = checks
        # The folders every help text points to, as plain links (no right-click needed).
        links = ttk.Frame(frame)
        links.pack(anchor="w", pady=(theme.px(12), 0))
        self.settings_links = {}
        for key, text, command, ok in (
                ("addons", OPEN_ADDONS_MENU, self.open_addons_folder, bool(self.addons_dir)),
                ("log", OPEN_LOG_MENU, self.open_log_folder, True)):
            link = tk.Label(links, text=text, background=theme.BG,
                            foreground=theme.LINK if ok else theme.DISABLED,
                            cursor="hand2" if ok else "", font=theme.font("underline"))
            link.pack(side="left", padx=(0, theme.px(16)))
            if ok:
                link.bind("<Button-1>", lambda _e, c=command: c())
            self.settings_links[key] = link
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(theme.px(14), 0))
        self.settings_close = ttk.Button(buttons, text="Close", command=self.close_settings)
        self.settings_close.pack(side="right")
        theme.even_buttons(buttons)
        self.licences_link = tk.Label(buttons, text=errors.LICENCES_LINK, cursor="hand2",
                                      background=theme.BG, foreground=theme.MUTED,
                                      font=theme.font("underline", bigger=-1))
        self.licences_link.pack(side="left", anchor="s")
        self.licences_link.bind("<Button-1>", lambda _e: self.open_licences())
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        win.bind("<Escape>", lambda _e: self.close_settings())
        self.settings_win = win
        self._set_enabled(self.beta_check, not self.busy)
        if show:
            win.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{x}+{y}")
            win.deiconify()
            theme.dark_title_bar(win)
            self.settings_close.focus_set()
            win.grab_set()
        return win

    def _dialog_busy(self):
        """A backup/restore runs, or Save zip... copies: its window stays until done."""
        d = self.backup_dialog
        return d is not None and (d.running or getattr(d, "saving", False))

    def close_settings(self):
        if self.backup_dialog is not None:
            if self._dialog_busy():
                return   # a backup/restore is running: its window stays until it's done
            self.backup_dialog.close()   # the Licences window
            if self.backup_dialog is not None:
                return   # it stayed open (stopping something first)
        win, self.settings_win = self.settings_win, None
        self.beta_check = self.share_check = None
        if win is not None:
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass

    # ---------- the Backup window (main window's Backup button; see am\backup_ui.py) ----------
    def open_backup_window(self, show=True):
        """The Backup window: Make / Restore / Send / Receive and the last backup.
        show=False (tests): built but never shown."""
        if self.backup_win is not None:
            if show:
                self.backup_win.win.deiconify()
                self.backup_win.win.lift()
            return self.backup_win
        self.backup_win = backup_ui.BackupWindow(self, show=show)
        return self.backup_win

    def close_backup_window(self):
        if self.backup_dialog is not None:
            if self._dialog_busy():
                if getattr(self.backup_dialog, "saving", False):
                    self.backup_dialog.close()   # Save zip...: stop the copy; windows stay
                return   # a backup/restore/save is running: its window stays until it's done
            self.backup_dialog.close()
            if self.backup_dialog is not None:
                return   # it stayed open (stopping something first): so does this window
        hub, self.backup_win = self.backup_win, None
        if hub is not None:
            hub.destroy()

    def _backup_note(self, text):
        if self.backup_win is not None:
            self.backup_win.set_status(text)
        else:
            self.set_message(text)

    def _ask_backup_file_dialog(self):
        parent = backup_ui.owner_window(self)
        start = backup_ui.backup_folder(config.load_settings())
        return filedialog.askopenfilename(
            parent=parent, title=errors.RESTORE_PICK_FILE,
            initialdir=start if os.path.isdir(start) else None,
            filetypes=[("Backup", ("*.zip", "*" + backup.LOCKED_EXT)), ("All files", "*.*")])

    def _ask_save_zip_dialog(self, name):
        """Save zip...: where to save the plain backup zip (the dialog asks before
        replacing a file). Starts in the last folder used, else Documents."""
        parent = (self.backup_dialog.win if self.backup_dialog is not None
                  else backup_ui.owner_window(self))
        settings = config.load_settings()
        start = settings.get("save_zip_folder") or backup.documents_dir() or ""
        if os.name != "nt":
            _show_hidden_folders(self.root)
        path = filedialog.asksaveasfilename(
            parent=parent, title=errors.SAVE_ZIP_TITLE, initialfile=name,
            initialdir=start if start and os.path.isdir(start) else None,
            defaultextension=".zip", confirmoverwrite=True,
            filetypes=[("Zip", "*.zip"), ("All files", "*.*")])
        if path:
            settings = config.load_settings()
            settings["save_zip_folder"] = os.path.dirname(path)
            config.save_settings(settings)
        return path

    def _ask_backup_folder_dialog(self, start):
        parent = (self.backup_dialog.win if self.backup_dialog is not None
                  else backup_ui.owner_window(self))
        if os.name != "nt":
            _show_hidden_folders(self.root)
        return filedialog.askdirectory(parent=parent, mustexist=False,
                                       initialdir=start if os.path.isdir(start) else None,
                                       title=errors.BACKUP_TITLE)

    def open_backup(self, show=True):
        """The Make a backup window (show=False in tests: built, never shown)."""
        if self.busy or not self.addons_dir or self.backup_dialog is not None:
            return self.backup_dialog
        self._backup_note("")
        self.backup_dialog = backup_ui.BackupDialog(self, show=show)
        return self.backup_dialog

    def open_licences(self, show=True):
        """The Licences window (THIRD-PARTY-NOTICES.txt, read-only)."""
        if self.backup_dialog is not None:
            return None
        self.backup_dialog = backup_ui.LicenceDialog(self, show=show)
        return self.backup_dialog

    def open_send(self, show=True):
        """Send to another PC (am/share_ui.py). Returns the window, or None."""
        if (self.busy or not self.addons_dir or self.backup_dialog is not None
                or not self.share_url):
            return None
        self._backup_note("")
        self.backup_dialog = share_ui.SendDialog(self, show=show)
        return self.backup_dialog

    def open_receive(self, show=True):
        """Receive from another PC: type the code, then the Restore window."""
        if (self.busy or not self.addons_dir or self.backup_dialog is not None
                or not self.share_url):
            return None
        self._backup_note("")
        self.backup_dialog = share_ui.ReceiveDialog(self, show=show)
        return self.backup_dialog

    def open_restore(self, show=True, path=None):
        """No path: the Backups found window (backup_ui.BackupListDialog). With a path:
        check it, then the Restore window (nothing changes until the player says yes
        there). Returns the window, or None."""
        if self.busy or not self.addons_dir or self.backup_dialog is not None:
            return None
        self._backup_note("")
        if not path or backup_lock.is_locked(path):
            self.backup_dialog = backup_ui.BackupListDialog(self, show=show)
            if path:   # a locked kept copy: opened (or its code asked) like a picked row
                self.backup_dialog.pick(path)
            return self.backup_dialog
        if self.backup_win is not None and show:
            try:
                self.backup_win.win.grab_set()
            except tk.TclError:
                pass
        try:
            info = backup.read(path)
        except Exception as e:
            log.warning("Not a usable backup: %s (%s)", path, e)
            self._backup_note(errors.backup_message(e))
            return None
        self.backup_dialog = backup_ui.RestoreDialog(self, info, show=show)
        return self.backup_dialog

    def _backup_event(self, msg):
        kind = msg[0]
        if kind == "backup_last":   # the Backup window's "Last backup" line
            if self.backup_win is not None:
                self.backup_win.handle(msg)
            return
        final = kind in ("backup_done", "backup_error", "restore_done", "restore_error")
        if final:
            self.busy = False
        dialog = self.backup_dialog
        if dialog is not None and dialog.alive():
            dialog.handle(msg)
        elif kind in ("backup_error", "restore_error"):
            self.set_message(msg[1])
        if kind in ("restore_opened", "restore_unlocked") and len(msg) > 3:
            # A locked backup opened for a window that is gone: its plain copy goes now.
            now = self.backup_dialog
            if getattr(now, "kept", None) is not msg[3] or not now.alive():
                msg[3].discard()
        if not final:
            return
        if kind == "restore_done":
            self._restored_others(msg[1])
            self.set_message(errors.restored_message(msg[1]))
        self.render()   # the rows read the installed versions from disk again

    def _restored_others(self, result):
        """After a restore: the backup's "Other addons" record joins this PC's, and the
        Other addons rows are read again (restored folders show without Check again)."""
        others = getattr(result, "others", None)
        try:
            if others:
                # Only the newly added ones count ("N other addons added"): guild folders
                # show as guild rows, and folders still tracked here keep their entry.
                before = self.files.tracked()
                merged = fileinstall.merge_restored(before, others, self._guild_folders())
                known = {f.lower() for f in before}
                result.others = {f: i for f, i in merged.items() if f.lower() not in known}
                if result.others:
                    self.files._save_tracked(merged)
            self.files.refresh()
        except Exception:
            log.exception("Could not add the restored Other addons")
            if others:
                result.others = {}   # nothing was added: the message says nothing about it

    def _apply_manifest(self, m, error, problems=None):
        self.manifest = m
        if m is not None:
            problems = problems or {}
            self.rows, notes = [], []
            for a in m.addons:
                if a.folder in problems:
                    row = Row(a.folder, a.name, None, None, a.description)
                    row.problem, full = errors.release_message(problems[a.folder], a.name)
                    row.problem_full = full
                    row.problem_kind = getattr(problems[a.folder], "kind", "")
                    if full not in notes:
                        notes.append(full)
                else:
                    row = Row(a.folder, a.name, a.version, a, a.description)
                self.rows.append(row)
            self.settings = config.load_settings()
            self.settings["known_addons"] = [[a.folder, a.name] for a in m.addons]
            config.save_settings(self.settings)
            # A removed addon that is back (from the bin, or by hand) counts as normal again.
            back = {f for f in self._removed() if self._installed(f)[0]}
            if back:
                self._set_removed(self._removed() - back)
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
        removed = self._removed()
        for row in self.rows:
            if not row.addon:
                continue
            exists, v = self._installed(row.folder)
            if not exists and row.folder in removed:
                continue   # the player removed it: only its own Install button brings it back
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
        self._set_removed(self._removed() - {r.folder for r in rows})
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
            while not self.closed:   # a message may close the app (Save zip stopped on close)
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        if self.closed:
            return
        self._polls += 1
        lock = self.instance_lock
        if lock is not None and self._polls % 5 == 0:
            # A file handed over by a second start (dropped on the icon while open). Not
            # holding the lock yet (an old exe was closing after an update): try again.
            if lock.held or (self._polls % 50 == 0 and lock.acquire()):
                self.files.poll_requests(lambda: instance.take_requests(config.home_dir()))
        self._after = self.root.after(100, self._poll)

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.closed = True
            if getattr(self, "_resize_due", False):
                try:
                    self.root.after_cancel(self._resize_due)
                except tk.TclError:
                    pass
                self._resize_due = False
            if getattr(self, "_draw_due", None):   # the rows' idle redraw (_list_resized)
                try:
                    self.root.after_cancel(self._draw_due)
                except tk.TclError:
                    pass
                self._draw_due = None
            theme.release_images([getattr(self, "_logo", None)]
                                 + list(getattr(self.root, "_am_icons", None) or []))
            self.files.close()   # an open Add from file plan's temp folder
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
            self.files.refresh()   # Other addons: a folder deleted by hand drops out
            self.busy = False
            self.render()
            if msg[1] is not None and self.addons_dir and not self.message_var.get():
                self.set_message(errors.list_summary(*self._counts))
            self._maybe_send_stats()
            self.files.after_load()   # a file dropped/handed over during the check
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
        elif kind == "removed":
            folder, where = msg[1], msg[2]
            self.busy = False
            self._set_removed(self._removed() | {folder})
            row = self._row(folder)
            if row:
                row.state = None
                self.set_message(msg[3] if len(msg) > 3
                                 else errors.removed_message(row.name, where))
            self.render()
        elif kind == "remove_error":
            self.busy = False
            row = self._row(msg[1])
            if row:
                row.state = None
            self.set_message(msg[2])
            self.render()
        elif kind.startswith(("backup_", "restore_")):
            self._backup_event(msg)
        elif kind.startswith("file_"):
            self.files.handle(msg)   # Add from file (am\fileinstall_ui.py)
        elif kind.startswith("share_"):
            dialog = self.backup_dialog
            if dialog is not None and dialog.alive():
                dialog.handle(msg)
        elif kind == "stats_sent":
            self.settings = config.load_settings()
            self.settings[stats.DAY_SETTING], self.settings[stats.WEEK_SETTING] = msg[1], msg[2]
            config.save_settings(self.settings)
        elif kind == "app_update":
            self._app_checking = False
            new = msg[1]
            self._app_checked = True
            self._app_newer = new is not None or self.update_mode == "offer"
            self._maybe_send_stats()
            if new is None:
                pass   # nothing newer (or offline): an offer already up stays
            elif self.update_mode is None:
                self.app_update, self.update_mode = new, "offer"
                self._render_update_bar()
            elif (self.update_mode == "offer" and not self.updating and self.app_update
                  and versions.compare(new.version, self.app_update.version) == 1):
                self.app_update = new   # Check again found an even newer one: offer that
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
                # The ones that did install first, so a failure doesn't hide them.
                done = [errors.installed_list(msg[1])] if msg[1] else []
                self.set_message("  ".join(done + [r.message for r in failed]))
            elif msg[1]:
                if len(msg[1]) == 1:
                    self.set_message(self.message_var.get())
                else:
                    self.set_message("Installed: " + ", ".join(msg[1])
                                     + ". " + errors.RELOAD + " to use them.")
            self.render()
            self.files.after_load()

    def _maybe_send_stats(self):
        """The daily anonymous count (see am\\stats.py): once a day, after the addon list
        and the app's own update check are done, in a thread; silent if it fails.
        Nothing at all while Share anonymous counts is off (checked again just before
        sending, in the thread)."""
        if (self._stats_started or not self.stats_url or not self._app_checked
                or self.manifest is None or not self.addons_dir):
            return
        settings = config.load_settings()
        if not _share_on(settings):
            return   # not "started": switching it back on later this run still sends
        self._stats_started = True
        if not stats.due(settings):
            return
        entries = []
        for row in self.rows:
            if not row.addon:
                continue
            exists, installed_v = self._installed(row.folder)
            if exists and installed_v:
                a = row.addon
                stable = a.stable_version or ("" if a.beta else a.version)
                entries.append((row.folder, installed_v, stable, a.beta_version))
        payload = stats.build_payload(entries, APP_VERSION, self._app_newer,
                                      stats.first_this_week(settings))
        threading.Thread(target=_stats_worker, daemon=True,
                         args=(self.queue, self.stats_url, payload, self.allow_local,
                               stats.day(), stats.week())).start()

    def close(self):
        if self._dialog_busy():
            if getattr(self.backup_dialog, "saving", False):
                # stop the Save zip... copy (its hidden part is removed); the app closes
                # once the copy has stopped (RestoreDialog._save_event)
                self.close_pending = True
                self.backup_dialog.close()
            return   # never stop a backup/restore half-way; it finishes in seconds
        if self.files.running:
            return   # an install from a file finishes first (it can't stop half-way safely)
        if self._exit_worker is not None:
            return   # already stopping a Receive/Send; it closes by itself (_close_when_stopped)
        d = self.backup_dialog
        if d is not None and getattr(d, "transfer_running", None) and d.transfer_running():
            # A Receive/Send runs: stop it like its Cancel (a Send-server download asks
            # first), then close once its thread has ended (the locking step finishes).
            if not d.stop_for_exit():
                return   # the player chose to keep receiving
            self._exit_worker, self._exit_dialog = d.worker, d
            self._exit_since = time.monotonic()
            self.root.after(CLOSE_POLL_MS, self._close_when_stopped)
            return
        self._close_now()

    def _close_when_stopped(self):
        """Close the app once the stopped transfer's thread has ended; after CLOSE_WAIT
        anyway (logged), except while the locked copy is still being written."""
        w, d = self._exit_worker, self._exit_dialog
        waited = time.monotonic() - self._exit_since
        if w is not None and w.is_alive() and (
                waited < CLOSE_WAIT or getattr(d, "locking", False)):
            try:
                self.root.after(CLOSE_POLL_MS, self._close_when_stopped)
            except tk.TclError:
                pass
            return
        if w is not None and w.is_alive():
            log.info("Closing while a transfer is still stopping")
        self._exit_worker = self._exit_dialog = None
        if self.closed:
            return
        try:   # the transfer's last messages (a backup that came anyway keeps its locked copy)
            while not self.closed:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        except Exception:   # noqa: BLE001
            log.info("Last messages not read before closing")
        if self.closed:
            return
        if d is not None and not d.closed:
            d.running = False
            d.active = False
            d.close()
        self._close_now()

    def _close_now(self):
        self._save_geometry()
        if self.backup_dialog is not None and getattr(self.backup_dialog, "kept", None):
            try:   # a Restore window of a locked backup: its hidden plain copy goes now
                self.backup_dialog.close()
            except Exception:   # noqa: BLE001 (closing anyway; the next start cleans it)
                log.info("Restore window not closed cleanly")
        self.closed = True
        self.files.close()
        self.root.destroy()


def _confirm_keys(win, answer, enter):
    """Keys of the yes/no question: Esc = no; Enter = yes only when enter is True
    (questions that don't remove, delete or overwrite anything), else Enter does nothing."""
    win.bind("<Escape>", lambda _e: answer(False))
    for key in ("<Return>", "<KP_Enter>"):
        if enter:
            win.bind(key, lambda _e: answer(True))
        else:   # the OK button has focus: Enter must not press it here
            win.bind(key, lambda _e: "break")


def _parse_geometry(text):
    """'WxH+X+Y' (X/Y may be negative: a screen left of the main one) -> (w, h, x, y)."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})\+(-?\d{1,6})\+(-?\d{1,6})", text.strip())
    if not m:
        return None
    return tuple(int(v) for v in m.groups())


def _on_screen(root, x, y, w, monitor_at=None):
    """True when the window's title bar (top strip, the part you drag) is on a screen.
    Windows: asks for a monitor under the strip (none = that screen is gone). Elsewhere:
    inside the screen Tk reports. monitor_at(left, top, right, bottom) for tests."""
    left, top, right, bottom = x + 40, y, x + max(41, w - 40), y + 30
    if monitor_at is None and os.name == "nt":
        monitor_at = _windows_monitor_at
    if monitor_at is not None:
        try:
            return bool(monitor_at(left, top, right, bottom))
        except Exception:
            log.exception("Monitor check failed")
            return False
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    return right > 0 and left < sw and top >= 0 and bottom <= sh


def _windows_monitor_at(left, top, right, bottom):
    import ctypes
    from ctypes import wintypes
    rect = wintypes.RECT(left, top, right, bottom)
    user32 = ctypes.windll.user32
    user32.MonitorFromRect.restype = ctypes.c_void_p
    user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    return user32.MonitorFromRect(ctypes.byref(rect), 0)   # 0 = MONITOR_DEFAULTTONULL


def _show_hidden_folders(root):
    """Linux Tk dialog: show the "Show hidden" switch and hidden folders. Harmless if
    the Tk version names these differently."""
    try:
        try:
            root.tk.call("tk_getOpenFile", "-foobar")   # loads the dialog's code only
        except tk.TclError:
            pass
        root.tk.call("set", "::tk::dialog::file::showHiddenBtn", "1")
        root.tk.call("set", "::tk::dialog::file::showHiddenVar", "1")
    except tk.TclError:
        pass


def _linux_start_folder():
    """Where the folder dialog starts on Linux: Steam's compatdata (Proton prefixes) if
    it exists, else the home folder."""
    home = os.path.expanduser("~")
    for lib in wowpath.steam_libraries(home, media_roots=()):
        compat = os.path.join(lib, "steamapps", "compatdata")
        if os.path.isdir(compat):
            return compat
    return home if os.path.isdir(home) else None


def _game_running(check_game):
    try:
        return bool(check_game())
    except Exception:
        return False


def _load_worker(q, check_game, location, allow_local, github_api=None, force=True,
                 show_betas=False):
    """Thread: game check + addon list + latest GitHub releases (pre-releases too if
    show_betas). Results go to the window through q as
    ("manifest", Manifest or None, error text, {folder: ReleaseError}).
    The game check (tasklist, ~0.4 s) runs at the same time as the downloads; its answer
    still reaches the window before the list."""
    game = threading.Thread(target=lambda: q.put(("game", _game_running(check_game))),
                            daemon=True)
    game.start()
    try:
        m = github.load_list(location, allow_local=allow_local)
        m.addons, problems = github.resolve_all(
            m.addons, github_api, allow_local, cache_file=config.cache_path(),
            use_fresh_cache=not force, show_betas=show_betas)
        msg = ("manifest", m, None, problems)
    except Exception as e:
        log.warning("Addon list failed: %s", e)
        msg = ("manifest", None, errors.manifest_message(e), None)
    game.join(10)
    q.put(msg)


def _resort_worker(q, manifest, allow_local, github_api, show_betas):
    """Thread: the same addon list again with the beta switch changed, from the release
    cache (GitHub only if the cache is old). Answers like _load_worker ("manifest", ...)."""
    try:
        addons, problems = github.resolve_all(
            list(manifest.addons), github_api, allow_local, cache_file=config.cache_path(),
            use_fresh_cache=True, show_betas=show_betas)
        q.put(("manifest", dataclasses.replace(manifest, addons=addons), None, problems))
    except Exception as e:   # never leave the window busy
        log.warning("Re-sorting the list failed: %s", e)
        q.put(("manifest", manifest, None, None))


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


def _remove_worker(q, folder, name, addons_dir, guild_folders, send, where):
    """Thread: remove one guild addon; ("removed", folder, where) or ("remove_error", ...)."""
    try:
        installer.remove(folder, addons_dir, guild_folders, send=send)
        q.put(("removed", folder, where))
    except Exception as e:
        log.exception("Removing %s failed", folder)
        if getattr(e, "kind", "") == "partly":   # gone from the game; the rest is hidden
            q.put(("removed", folder, None, errors.remove_message(e, name)))
        else:
            q.put(("remove_error", folder, errors.remove_message(e, name)))


def _stats_worker(q, url, payload, allow_local, today, this_week):
    """Thread: the usage ping, only if Share anonymous counts is still on right now."""
    if _share_on(config.load_settings()):
        stats.worker(q, url, payload, allow_local, today, this_week)


def _update_check_worker(q, github_api, allow_local, failed, force=False):
    """Thread: is there a newer Addon Manager? ("app_update", Update or None).
    force (Check again): ask GitHub even if the cache is fresh, like the addon releases."""
    q.put(("app_update", selfupdate.check(APP_VERSION, github_api, allow_local,
                                          cache_file=config.cache_path(),
                                          use_fresh_cache=not force, failed=failed)))


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
        github_api=None, updated_from=None, open_path=None, instance_lock=None):
    root = tk.Tk()
    if hidden:
        root.withdraw()
    root.report_callback_exception = _report_callback_exception
    app = App(root, manifest_location, allow_local, wow_override, github_api=github_api,
              app_exe=selfupdate.exe_path(), updated_from=updated_from, open_path=open_path,
              instance_lock=instance_lock)
    if not hidden:
        app.restore_geometry()
    theme.dark_title_bar(root)
    root.mainloop()
    return app


def _report_callback_exception(exc, value, tb):
    log.error("Unexpected error", exc_info=(exc, value, tb))
