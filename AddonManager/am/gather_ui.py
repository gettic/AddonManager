"""Settings > Share game data > "What is sent...": what All-in-One Guide's game data holds,
what it never holds, and the exact pages as they would be sent (read in a thread, only while
WoW is closed: am\\gather.py). Read-only; no logo or game art."""

import threading
import tkinter as tk
from tkinter import ttk

from am import backup, backup_ui, errors, gather, theme
from am.backup_ui import _Dialog

TITLE = "What is sent"
WHAT = (
    "All-in-One Guide keeps game facts on this PC while you play:\n"
    "- quests: XP, givers, chains, objectives, rewards offered and picked, abandoned and "
    "failed quests;\n"
    "- the world: NPC and mob spots (whole map percent), drops, loot gold, deaths, fights "
    "(time to kill, spells used), professions, reputation gains, dungeon runs, travel and "
    "flights;\n"
    "- the guide: skips, stuck steps, your settings, addon errors and speed;\n"
    "- you: race, realm, class, spec, group size (a number), game language, addon version, "
    "gear item levels, stats, rested XP and the day (no time).")
NEVER = ("Never your name, account, BattleTag, guild, chat, other players, your gold total, "
         "reputation standing or health.")
WHY = "Shared pages help fix and speed up the guides for everyone."
WHEN = ("The app sends them quietly while WoW is closed. Switch off \"Share game data\" in "
        "Settings to stop it.")
IP = ("Our server doesn't store your IP address (the web host sees it while sending, like any "
      "website).")
SENDER = ("A random sender code made on this PC is sent with the pages, so bad data from one "
          "sender can be spotted and left out. It isn't linked to your account.")
RIGHTS = ("By sharing, you give up your own rights in these lines (CC0), so they can be used "
          "freely in the guides; game content stays Blizzard's.")
INFO = "\n\n".join((WHAT, NEVER, WHY, WHEN, IP, SENDER, RIGHTS))
PAGES_HEAD = "The pages on this PC, exactly as they are sent:"
LOADING = "Reading the pages..."
GAME_OPEN = "WoW is open: close it to see the pages (they are only read while it's closed)."
NO_FOLDER = "Choose your WoW folder first to see the pages."
NONE_YET = "No game data on this PC yet."
NO_PAGES = "(no pages yet)"


class GameDataDialog(_Dialog):
    """The explanation and the exact pages in one read-only box (selecting/copying work)."""

    def __init__(self, app, show=True):
        super().__init__(app, TITLE, show)
        box = ttk.Frame(self.frame)
        box.pack(fill="both", expand=True, pady=(theme.px(8), 0))
        # read at run time (the Linux width; tests set it for the Linux look)
        self.text = tk.Text(box, width=backup_ui.NOTICES_CHARS, height=24, wrap="none",
                            background=theme.FIELD, foreground=theme.TEXT,
                            insertbackground=theme.TEXT, relief="flat", borderwidth=0,
                            highlightthickness=1, highlightbackground=theme.BORDER,
                            font=theme.font(bigger=-1), padx=theme.px(6), pady=theme.px(4))
        # the explanation wraps at the box; the pages never do (exact lines, sideways bar)
        self.text.tag_configure("info", wrap="word")
        self.text.tag_configure("list", wrap="word", lmargin2=theme.px(12))
        self.text.tag_configure("head", wrap="word", foreground=theme.HEAD,
                                font=theme.font("bold", bigger=-1))
        self.vbar = ttk.Scrollbar(box, orient="vertical", command=self.text.yview,
                                  style="Gold.Vertical.TScrollbar")
        self.hbar = ttk.Scrollbar(box, orient="horizontal", command=self.text.xview,
                                  style="Gold.Horizontal.TScrollbar")
        self.text.configure(yscrollcommand=self.vbar.set, xscrollcommand=self._xset)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns", padx=(theme.px(4), 0))
        self.hbar.grid(row=1, column=0, sticky="ew", pady=(theme.px(4), 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.pages = None   # [(label, [pages])] once read
        self._fill(LOADING if app.addons_dir else NO_FOLDER)
        self.buttons = ttk.Frame(self.frame)
        self.buttons.pack(fill="x", pady=(theme.px(10), 0))
        self.close_button = ttk.Button(self.buttons, text=errors.CLOSE, command=self.close)
        self.close_button.pack(side="right")
        self._show(self.close_button)
        if app.addons_dir:
            try:
                accounts = backup.places(app.addons_dir).accounts
            except Exception:   # noqa: BLE001
                self._fill(NONE_YET)
                return
            threading.Thread(target=gather.preview_worker, daemon=True,
                             args=(app.queue, accounts, app.check_game)).start()

    def _xset(self, lo, hi):
        """The sideways bar only shows when something is out of view."""
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.hbar.grid_remove()
        else:
            self.hbar.grid()
        self.hbar.set(lo, hi)

    def _fill(self, note=None):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        # the "- ..." lines hang (wrapped lines indent past the dash)
        for i, para in enumerate(INFO.split("\n\n")):
            for line in para.split("\n"):
                t.insert("end", line + "\n", "list" if line.startswith("- ") else "info")
            t.insert("end", "\n", "info")
        t.insert("end", PAGES_HEAD + "\n", "head")
        if note:
            t.insert("end", note + "\n", "info")
        for label, pages in self.pages or ():
            t.insert("end", label + "\n", "head")
            t.insert("end", ("\n".join(pages) if pages else NO_PAGES) + "\n")
        t.configure(state="disabled")

    def page_lines(self):
        """The page lines shown (tests): every line of the box that is a page."""
        return [ln for ln in self.text.get("1.0", "end").splitlines() if ln.startswith("AOGD1 ")]

    def handle(self, msg):
        if msg[0] != "gather_preview" or self.closed:
            return
        result = msg[1]
        if result is None:
            self._fill(GAME_OPEN)
        elif not result:
            self._fill(NONE_YET)
        else:
            self.pages = result
            self._fill()
